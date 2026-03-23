# app.py
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import threading
import time
import uuid
import base64
from pathlib import Path
from typing import Any, Dict, Optional

from flask import (
    Flask, Response, jsonify, render_template_string,
    request, stream_with_context,
)
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config['MAX_CONTENT_LENGTH'] = 10 * 1024 * 1024 * 1024  # 10 GB

# ── In-memory stores (no disk files for results) ───────────────────────────────
# uploads  : file_id → { path, filename, info, is_audio, thumb }
# jobs     : job_id  → { status, pct, msg, result_bytes, ext,
#                        original_mb, final_mb, reduction }
_uploads: Dict[str, Dict] = {}
_jobs: Dict[str, Dict] = {}
_lock = threading.Lock()


# ══════════════════════════════════════════════════════════════════════════════
# FFmpeg helpers
# ══════════════════════════════════════════════════════════════════════════════
def _ffmpeg() -> str:
    for p in ('/usr/bin/ffmpeg', '/usr/local/bin/ffmpeg', '/bin/ffmpeg'):
        if os.path.isfile(p):
            return p
    return 'ffmpeg'


def _ffprobe() -> str:
    for p in ('/usr/bin/ffprobe', '/usr/local/bin/ffprobe', '/bin/ffprobe'):
        if os.path.isfile(p):
            return p
    return 'ffprobe'


def check_ffmpeg() -> bool:
    try:
        r = subprocess.run([_ffmpeg(), '-version'],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Media probe
# ══════════════════════════════════════════════════════════════════════════════
def get_media_info(path: str) -> Optional[Dict[str, Any]]:
    try:
        r = subprocess.run(
            [_ffprobe(), '-v', 'quiet', '-print_format', 'json',
             '-show_streams', '-show_format', path],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            return None

        data = json.loads(r.stdout)
        sz = os.path.getsize(path)
        info: Dict[str, Any] = {
            'duration': 0, 'width': 0, 'height': 0, 'fps': 0,
            'size': sz, 'size_mb': round(sz / 1048576, 2),
            'video_codec': '', 'audio_codec': '',
            'bitrate': 0, 'has_audio': False, 'is_audio_only': False,
            'sample_rate': 0, 'channels': 0,
        }

        fmt = data.get('format', {})
        try:
            info['duration'] = float(fmt.get('duration') or 0)
        except Exception:
            pass
        try:
            info['bitrate'] = int(float(fmt.get('bit_rate') or 0))
        except Exception:
            pass

        has_video = False
        for s in data.get('streams', []):
            ct = s.get('codec_type', '')
            if ct == 'video' and not has_video:
                has_video = True
                info['width'] = int(s.get('width', 0) or 0)
                info['height'] = int(s.get('height', 0) or 0)
                info['video_codec'] = s.get('codec_name', '') or ''
                try:
                    n, d = s.get('r_frame_rate', '0/1').split('/')
                    info['fps'] = round(int(n) / int(d), 2) if int(d) else 0
                except Exception:
                    pass
                if not info['duration']:
                    try:
                        info['duration'] = float(s.get('duration') or 0)
                    except Exception:
                        pass
            elif ct == 'audio':
                info['has_audio'] = True
                info['audio_codec'] = s.get('codec_name', '') or ''
                try:
                    info['sample_rate'] = int(s.get('sample_rate') or 0)
                except Exception:
                    pass
                try:
                    info['channels'] = int(s.get('channels') or 0)
                except Exception:
                    pass
                if not info['duration']:
                    try:
                        info['duration'] = float(s.get('duration') or 0)
                    except Exception:
                        pass

        info['is_audio_only'] = (not has_video) and info['has_audio']
        return info
    except Exception:
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Thumbnail — returns base64 JPEG string (no file saved)
# ══════════════════════════════════════════════════════════════════════════════
def make_thumb_b64(path: str, ts: float = 5.0) -> Optional[str]:
    try:
        ts = max(0.0, min(float(ts), 30.0))
        cmd = [
            _ffmpeg(), '-y', '-ss', f'{ts:.2f}', '-i', path,
            '-vframes', '1', '-q:v', '5', '-vf', 'scale=480:-2',
            '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1',
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode == 0 and len(r.stdout) > 200:
            return 'data:image/jpeg;base64,' + base64.b64encode(r.stdout).decode()
    except Exception:
        pass
    return None


# ══════════════════════════════════════════════════════════════════════════════
# Bitrate calculators
# ══════════════════════════════════════════════════════════════════════════════
def calc_video_bitrate(target_mb: float, dur: float,
                       audio_kbps: int = 128) -> int:
    if dur <= 0:
        return 500
    total_bits = target_mb * 8 * 1048576
    audio_bits = audio_kbps * 1024 * dur
    video_bits = max(0, total_bits - audio_bits)
    return max(80, int(video_bits / dur / 1024 * 0.97))


def calc_audio_bitrate(target_mb: float, dur: float) -> int:
    if dur <= 0:
        return 96
    total_bits = target_mb * 8 * 1048576
    kbps = int(total_bits / dur / 1024 * 0.97)
    return max(32, min(320, kbps))


# ══════════════════════════════════════════════════════════════════════════════
# COMPRESSION — runs in background thread, stores result IN MEMORY only
# ══════════════════════════════════════════════════════════════════════════════
def _compress_thread(job_id: str, src: str, target_mb: float,
                     out_fmt: str, info: dict) -> None:
    job = _jobs[job_id]
    null_dev = 'NUL' if sys.platform == 'win32' else '/dev/null'
    tmp_out: Optional[str] = None
    tmp_pass: Optional[str] = None

    def upd(pct: int, msg: str) -> None:
        job['pct'] = pct
        job['msg'] = msg

    try:
        dur = float(info['duration'])
        orig_mb = float(info['size_mb'])
        is_audio_out = out_fmt in {
            'mp3', 'aac', 'wav', 'ogg', 'opus', 'flac', 'm4a'
        }
        is_audio_src = bool(info.get('is_audio_only'))

        if dur <= 0:
            raise ValueError("Cannot determine duration")

        # ── create temp output file ────────────────────────────────────────
        tmp_fd, tmp_out = tempfile.mkstemp(suffix='.' + out_fmt)
        os.close(tmp_fd)
        os.remove(tmp_out)   # ffmpeg will create it

        # ── AUDIO ENCODE ───────────────────────────────────────────────────
        if is_audio_out or is_audio_src:
            kbps = calc_audio_bitrate(target_mb, dur)
            upd(5, f'Calculating audio bitrate → {kbps} kbps…')

            codec_map = {
                'mp3':  ['-c:a', 'libmp3lame', '-b:a', f'{kbps}k',
                          '-compression_level', '0'],
                'aac':  ['-c:a', 'aac', '-b:a', f'{kbps}k'],
                'm4a':  ['-c:a', 'aac', '-b:a', f'{kbps}k'],
                'ogg':  ['-c:a', 'libvorbis', '-b:a', f'{kbps}k'],
                'opus': ['-c:a', 'libopus', '-b:a', f'{kbps}k', '-vbr', 'on'],
                'flac': ['-c:a', 'flac', '-compression_level', '8'],
                'wav':  ['-c:a', 'pcm_s16le'],
            }
            codec_args = codec_map.get(
                out_fmt, ['-c:a', 'libmp3lame', '-b:a', f'{kbps}k'])
            cmd = [_ffmpeg(), '-y', '-i', src, '-vn'] + codec_args + [tmp_out]
            base_pct = 8

            upd(base_pct, f'Encoding audio as {out_fmt.upper()}…')
            proc = subprocess.Popen(
                cmd, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1)

            for line in proc.stdout:  # type: ignore
                m = re.search(r'time=(\d+):(\d+):([\d.]+)', line)
                if m and dur > 0:
                    el = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                          + float(m.group(3)))
                    ratio = min(el / dur, 1.0)
                    pct = int(base_pct + ratio * (92 - base_pct))
                    spd_m = re.search(r'speed=([\d.]+)x', line)
                    spd = float(spd_m.group(1)) if spd_m else 0.0
                    eta = (f' · ETA ~{int((dur - el) / spd)}s'
                           if spd > 0.01 and el < dur else '')
                    upd(pct, f'Encoding… {int(ratio * 100)}%{eta}')

            proc.wait(timeout=86400)
            if proc.returncode != 0:
                raise RuntimeError(f'FFmpeg failed (rc={proc.returncode})')

        # ── VIDEO 2-PASS ───────────────────────────────────────────────────
        else:
            a_kbps = 128
            v_kbps = calc_video_bitrate(target_mb, dur, a_kbps)
            tmp_pass = tempfile.mktemp(prefix='ffpass_')

            upd(3, 'Pass 1/2 — analysing…')
            cmd1 = [
                _ffmpeg(), '-y', '-i', src,
                '-c:v', 'libx264', '-b:v', f'{v_kbps}k',
                '-pass', '1', '-passlogfile', tmp_pass,
                '-preset', 'fast', '-an', '-f', 'null', null_dev,
            ]
            p1 = subprocess.Popen(
                cmd1, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1)
            for line in p1.stdout:  # type: ignore
                m = re.search(r'time=(\d+):(\d+):([\d.]+)', line)
                if m and dur > 0:
                    el = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                          + float(m.group(3)))
                    ratio = min(el / dur, 1.0)
                    upd(int(3 + ratio * 37),
                        f'Pass 1/2 — {int(ratio * 100)}%')
            p1.wait(timeout=86400)
            if p1.returncode != 0:
                raise RuntimeError(f'Pass 1 failed (rc={p1.returncode})')

            upd(42, f'Pass 2/2 — {v_kbps} kbps video + {a_kbps} kbps audio…')
            cmd2 = [
                _ffmpeg(), '-y', '-i', src,
                '-c:v', 'libx264',
                '-b:v', f'{v_kbps}k',
                '-maxrate', f'{int(v_kbps * 1.4)}k',
                '-bufsize', f'{v_kbps * 2}k',
                '-pass', '2', '-passlogfile', tmp_pass,
                '-preset', 'fast',
                '-c:a', 'aac', '-b:a', f'{a_kbps}k',
                '-movflags', '+faststart',
                tmp_out,
            ]
            proc = subprocess.Popen(
                cmd2, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1)

            for line in proc.stdout:  # type: ignore
                m = re.search(r'time=(\d+):(\d+):([\d.]+)', line)
                if m and dur > 0:
                    el = (int(m.group(1)) * 3600 + int(m.group(2)) * 60
                          + float(m.group(3)))
                    ratio = min(el / dur, 1.0)
                    pct = int(42 + ratio * 52)
                    spd_m = re.search(r'speed=([\d.]+)x', line)
                    spd = float(spd_m.group(1)) if spd_m else 0.0
                    sz_m = re.search(r'size=\s*(\d+)kB', line)
                    cur = (f' · {round(int(sz_m.group(1)) / 1024, 1)} MB'
                           if sz_m else '')
                    eta = (f' · ETA ~{int((dur - el) / spd)}s'
                           if spd > 0.01 and el < dur else '')
                    upd(pct, f'Pass 2/2 — {int(ratio * 100)}%{cur}{eta}')

            proc.wait(timeout=86400)
            if proc.returncode != 0:
                raise RuntimeError(f'Pass 2 failed (rc={proc.returncode})')

        # ── Read result INTO MEMORY (no permanent disk storage) ───────────
        if not os.path.exists(tmp_out):
            raise RuntimeError('Output file was not created')
        out_size = os.path.getsize(tmp_out)
        if out_size < 512:
            raise RuntimeError(f'Output too small ({out_size} bytes)')

        upd(97, 'Reading result into memory…')
        buf = io.BytesIO()
        with open(tmp_out, 'rb') as fh:
            while True:
                piece = fh.read(4 * 1024 * 1024)
                if not piece:
                    break
                buf.write(piece)

        result_bytes = buf.getvalue()
        final_mb = round(len(result_bytes) / 1048576, 2)
        reduction = round((1 - final_mb / orig_mb) * 100, 1)

        # Store result in memory, mark done
        job['result_bytes'] = result_bytes
        job['ext'] = out_fmt
        job['original_mb'] = orig_mb
        job['final_mb'] = final_mb
        job['reduction'] = reduction
        job['status'] = 'completed'
        upd(100, f'Done! {orig_mb} MB → {final_mb} MB')

    except Exception as ex:
        job['status'] = 'error'
        job['msg'] = str(ex)

    finally:
        # ── Always delete temp files immediately ───────────────────────────
        if tmp_out and os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except Exception:
                pass
        if tmp_pass:
            for sfx in ('', '.log', '-0.log', '.log.mbtree', '-0.log.mbtree'):
                p = tmp_pass + sfx
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass


# ══════════════════════════════════════════════════════════════════════════════
# Cleanup helpers
# ══════════════════════════════════════════════════════════════════════════════
def _cleanup_upload(file_id: str) -> None:
    """Delete the upload temp file and remove from registry."""
    with _lock:
        entry = _uploads.pop(file_id, None)
    if entry:
        path = entry.get('path', '')
        if path and os.path.exists(path):
            try:
                os.remove(path)
            except Exception:
                pass


def _cleanup_job(job_id: str) -> None:
    """Remove job (result bytes freed from memory)."""
    with _lock:
        _jobs.pop(job_id, None)


def _schedule_cleanup(job_id: str, delay: int = 300) -> None:
    """Free result bytes from memory after `delay` seconds."""
    def _run():
        time.sleep(delay)
        with _lock:
            job = _jobs.get(job_id)
            if job:
                job.pop('result_bytes', None)  # free memory
    t = threading.Thread(target=_run, daemon=True)
    t.start()


# ══════════════════════════════════════════════════════════════════════════════
# ROUTES
# ══════════════════════════════════════════════════════════════════════════════

@app.route('/')
def index():
    ffmpeg_ok = check_ffmpeg()
    return render_template_string(HTML_TEMPLATE, ffmpeg_ok=ffmpeg_ok)


# ── Upload ─────────────────────────────────────────────────────────────────────
@app.route('/upload', methods=['POST'])
def upload():
    f = request.files.get('file')
    if not f or not f.filename:
        return jsonify({'error': 'No file'}), 400

    filename = secure_filename(f.filename)
    suffix = Path(filename).suffix.lower() or '.bin'

    # Save to temp file (only upload lives on disk temporarily)
    try:
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        os.close(tmp_fd)
        f.save(tmp_path)
    except Exception as ex:
        return jsonify({'error': f'Save failed: {ex}'}), 500

    if os.path.getsize(tmp_path) < 512:
        os.remove(tmp_path)
        return jsonify({'error': 'File too small'}), 400

    info = get_media_info(tmp_path)
    if not info:
        os.remove(tmp_path)
        return jsonify({'error': 'Cannot read media info'}), 400
    if info['duration'] <= 0:
        os.remove(tmp_path)
        return jsonify({'error': 'Cannot determine duration'}), 400

    is_audio = (
        Path(filename).suffix.lower().lstrip('.')
        in {'mp3', 'wav', 'aac', 'ogg', 'opus', 'flac', 'wma', 'm4a'}
        or info['is_audio_only']
    )

    # Thumbnail — base64, no file stored
    thumb = None
    if not is_audio:
        ts = min(5.0, max(0.0, info['duration'] * 0.1))
        thumb = make_thumb_b64(tmp_path, ts)

    file_id = str(uuid.uuid4())
    with _lock:
        _uploads[file_id] = {
            'path': tmp_path,
            'filename': filename,
            'info': info,
            'is_audio': is_audio,
        }

    return jsonify({
        'file_id': file_id,
        'filename': filename,
        'is_audio': is_audio,
        'thumb': thumb,
        'info': info,
    })


# ── Start compression ──────────────────────────────────────────────────────────
@app.route('/compress', methods=['POST'])
def compress():
    body = request.get_json(force=True) or {}
    file_id = body.get('file_id', '')
    target_mb = float(body.get('target_mb', 0))
    out_fmt = str(body.get('output_format', 'mp4')).lower().strip('.')

    with _lock:
        upload_entry = _uploads.get(file_id)
    if not upload_entry:
        return jsonify({'error': 'Unknown file_id'}), 400

    src = upload_entry['path']
    if not os.path.exists(src):
        return jsonify({'error': 'Upload file missing — please re-upload'}), 400

    info = upload_entry['info']
    orig_mb = info['size_mb']

    if target_mb <= 0 or target_mb >= orig_mb:
        return jsonify({'error': 'Invalid target size'}), 400

    ALLOWED_FMTS = {'mp4','mkv','avi','mov','webm',
                    'mp3','aac','wav','ogg','opus','flac','m4a'}
    if out_fmt not in ALLOWED_FMTS:
        return jsonify({'error': f'Unsupported format: {out_fmt}'}), 400

    job_id = str(uuid.uuid4())
    with _lock:
        _jobs[job_id] = {
            'status': 'running',
            'pct': 0,
            'msg': 'Starting…',
            'result_bytes': None,
            'ext': out_fmt,
            'original_mb': orig_mb,
            'final_mb': 0,
            'reduction': 0,
        }

    t = threading.Thread(
        target=_compress_thread,
        args=(job_id, src, target_mb, out_fmt, info),
        daemon=True,
    )
    t.start()

    return jsonify({'job_id': job_id})


# ── SSE progress stream ────────────────────────────────────────────────────────
@app.route('/progress_stream/<job_id>')
def progress_stream(job_id: str):
    def _generate():
        last_pct = -1
        for _ in range(7200):   # max 2 h
            with _lock:
                job = _jobs.get(job_id)
            if not job:
                yield 'data: {"_done":true}\n\n'
                return

            status = job['status']
            pct = job['pct']
            msg = job['msg']

            if pct != last_pct or status in ('completed', 'error'):
                last_pct = pct
                payload = {
                    'status': status, 'pct': pct, 'msg': msg,
                }
                if status == 'completed':
                    payload.update({
                        'ext': job['ext'],
                        'original_mb': job['original_mb'],
                        'final_mb': job['final_mb'],
                        'reduction': job['reduction'],
                    })
                    yield f'data: {json.dumps(payload)}\n\n'
                    yield 'data: {"_done":true}\n\n'
                    # Schedule memory cleanup after 5 min
                    _schedule_cleanup(job_id, delay=300)
                    return
                elif status == 'error':
                    yield f'data: {json.dumps(payload)}\n\n'
                    yield 'data: {"_done":true}\n\n'
                    return
                else:
                    yield f'data: {json.dumps(payload)}\n\n'

            time.sleep(0.5)

        yield 'data: {"_done":true}\n\n'

    return Response(
        stream_with_context(_generate()),
        mimetype='text/event-stream',
        headers={
            'Cache-Control': 'no-cache',
            'X-Accel-Buffering': 'no',
        },
    )


# ── Poll fallback ──────────────────────────────────────────────────────────────
@app.route('/progress/<job_id>')
def progress_poll(job_id: str):
    with _lock:
        job = _jobs.get(job_id)
    if not job:
        return jsonify({'status': 'error', 'msg': 'Not found', 'pct': 0})
    out = {
        'status': job['status'], 'pct': job['pct'], 'msg': job['msg'],
    }
    if job['status'] == 'completed':
        out.update({
            'ext': job['ext'],
            'original_mb': job['original_mb'],
            'final_mb': job['final_mb'],
            'reduction': job['reduction'],
        })
    return jsonify(out)


# ── Download — streams result bytes directly, then frees memory ────────────────
@app.route('/download/<job_id>')
def download(job_id: str):
    with _lock:
        job = _jobs.get(job_id)

    if not job:
        return jsonify({'error': 'Job not found'}), 404
    if job['status'] != 'completed':
        return jsonify({'error': 'Not ready yet'}), 425
    if not job.get('result_bytes'):
        return jsonify({'error': 'Result already downloaded or expired'}), 410

    result_bytes = job['result_bytes']
    ext = job.get('ext', 'mp4')

    # Filename from query param or default
    raw_name = request.args.get('filename', '')
    if raw_name:
        safe = secure_filename(raw_name)
        if not safe:
            safe = f'compressed.{ext}'
    else:
        safe = f'compressed.{ext}'

    mime_map = {
        'mp4': 'video/mp4', 'mkv': 'video/x-matroska',
        'avi': 'video/x-msvideo', 'mov': 'video/quicktime',
        'webm': 'video/webm', 'mp3': 'audio/mpeg',
        'aac': 'audio/aac', 'wav': 'audio/wav',
        'ogg': 'audio/ogg', 'opus': 'audio/opus',
        'flac': 'audio/flac', 'm4a': 'audio/mp4',
    }
    mime = mime_map.get(ext, 'application/octet-stream')

    # Stream the bytes to browser
    def _stream():
        buf = io.BytesIO(result_bytes)
        while True:
            chunk = buf.read(1 * 1024 * 1024)   # 1 MB chunks
            if not chunk:
                break
            yield chunk
        # Free memory immediately after streaming
        with _lock:
            if job_id in _jobs:
                _jobs[job_id].pop('result_bytes', None)

    return Response(
        stream_with_context(_stream()),
        mimetype=mime,
        headers={
            'Content-Disposition': f'attachment; filename="{safe}"',
            'Content-Length': str(len(result_bytes)),
        },
    )


# ── Delete upload (called on reset) ───────────────────────────────────────────
@app.route('/delete_upload/<file_id>', methods=['POST'])
def delete_upload(file_id: str):
    _cleanup_upload(file_id)
    return jsonify({'ok': True})


# ══════════════════════════════════════════════════════════════════════════════
# HTML  (identical to your original — zero changes needed)
# ══════════════════════════════════════════════════════════════════════════════
HTML_TEMPLATE = '''<!DOCTYPE html>
<html lang="en">

<head>
    <meta charset="UTF-8" />
    <meta name="viewport" content="width=device-width,initial-scale=1" />
    <title>🗜️ Media Compressor</title>
    <style>
        *,
        *::before,
        *::after {
            box-sizing: border-box;
            margin: 0;
            padding: 0
        }

        :root {
            --bg: #07090f;
            --s1: #0d0f1c;
            --s2: #121525;
            --s3: #171b2e;
            --border: #1e2238;
            --accent: #6c5fff;
            --ag: rgba(108, 95, 255, .28);
            --green: #2ecc71;
            --gg: rgba(46, 204, 113, .26);
            --red: #e74c3c;
            --yellow: #f39c12;
            --text: #dde1f5;
            --muted: #565a7a;
            --r: 14px;
            --rs: 10px;
            --sh: 0 8px 40px rgba(0, 0, 0, .65);
        }

        html { scroll-behavior: smooth }

        body {
            font-family: 'Segoe UI', system-ui, sans-serif;
            background: var(--bg);
            color: var(--text);
            min-height: 100vh;
            line-height: 1.6
        }

        ::-webkit-scrollbar { width: 5px }
        ::-webkit-scrollbar-track { background: var(--s1) }
        ::-webkit-scrollbar-thumb { background: var(--border); border-radius: 3px }
        ::-webkit-scrollbar-thumb:hover { background: var(--accent) }

        .wrap { max-width: 740px; margin: 0 auto; padding: 22px 14px 100px }

        .hdr {
            background: linear-gradient(135deg, #0e1228, #171d38);
            border: 1px solid var(--border);
            border-radius: var(--r);
            padding: 22px 26px;
            margin-bottom: 18px;
            display: flex;
            align-items: center;
            gap: 14px;
            box-shadow: var(--sh);
            position: relative;
            overflow: hidden
        }
        .hdr::after {
            content: '';
            position: absolute;
            inset: 0;
            background: radial-gradient(ellipse at 78% 50%, rgba(108,95,255,.13), transparent 65%);
            pointer-events: none
        }
        .hdr-ico { font-size: 2.2rem; filter: drop-shadow(0 0 12px var(--accent)) }
        .hdr-title {
            font-size: 1.65rem; font-weight: 800;
            background: linear-gradient(90deg, #fff 20%, #afa8ff);
            -webkit-background-clip: text; -webkit-text-fill-color: transparent
        }
        .hdr-sub { color: var(--muted); font-size: .81rem; margin-top: 1px }
        .ffpill { margin-left: auto; padding: 5px 12px; border-radius: 20px; font-size: .72rem; font-weight: 700; white-space: nowrap }
        .ffok  { background: rgba(46,204,113,.1); color: var(--green); border: 1px solid rgba(46,204,113,.22) }
        .ffbad { background: rgba(231,76,60,.1);  color: var(--red);   border: 1px solid rgba(231,76,60,.22) }

        .card { background: var(--s1); border: 1px solid var(--border); border-radius: var(--r); padding: 22px; margin-bottom: 16px; box-shadow: var(--sh) }
        .card-hd { display: flex; align-items: center; gap: 8px; font-size: .93rem; font-weight: 700; color: #b5b9d8; margin-bottom: 16px; padding-bottom: 12px; border-bottom: 1px solid var(--border) }

        .drop { border: 2px dashed var(--border); border-radius: var(--r); padding: 44px 18px; text-align: center; cursor: pointer; transition: all .3s; background: var(--s2); position: relative }
        .drop:hover, .drop.over { border-color: var(--accent); background: rgba(108,95,255,.06); box-shadow: 0 0 24px var(--ag) }
        .drop-ico { font-size: 3rem; display: block; margin-bottom: 9px; transition: transform .3s }
        .drop:hover .drop-ico, .drop.over .drop-ico { transform: translateY(-5px) scale(1.07) }
        .drop-title { font-size: 1.05rem; font-weight: 700; margin-bottom: 3px }
        .drop-sub { color: var(--muted); font-size: .81rem }
        .ft-tags { display: flex; flex-wrap: wrap; gap: 4px; justify-content: center; margin-top: 11px }
        .ft { background: var(--s3); border: 1px solid var(--border); border-radius: 3px; padding: 1px 6px; font-size: .65rem; color: var(--muted); font-family: monospace }
        #file-input { display: none }
        .browse-btn { display: inline-flex; align-items: center; gap: 6px; margin-top: 13px; padding: 9px 20px; background: linear-gradient(135deg, var(--accent), #9a90ff); border: none; border-radius: var(--rs); color: #fff; font-size: .84rem; font-weight: 600; cursor: pointer; transition: all .25s }
        .browse-btn:hover { transform: translateY(-2px); box-shadow: 0 5px 16px var(--ag) }

        .upwrap { display: none; margin-top: 14px }
        .up-hdr { display: flex; justify-content: space-between; align-items: center; margin-bottom: 5px }
        .up-name { font-size: .8rem; color: var(--muted); overflow: hidden; text-overflow: ellipsis; white-space: nowrap; max-width: 60% }
        .up-pct { font-size: .88rem; font-weight: 800; color: var(--accent) }
        .pb-track { background: var(--s3); border-radius: 20px; height: 8px; overflow: hidden; margin-bottom: 5px; position: relative }
        .pb-fill { height: 100%; border-radius: 20px; background: linear-gradient(90deg, var(--accent), #9a90ff); transition: width .12s linear; width: 0% }
        .pb-fill.done { background: linear-gradient(90deg, var(--green), #27ae60); transition: width .3s ease }
        .up-row2 { display: flex; justify-content: space-between; font-size: .72rem; color: var(--muted) }

        #info-section { display: none }
        .file-hero { background: linear-gradient(135deg,rgba(108,95,255,.09),rgba(46,204,113,.06)); border: 1px solid rgba(108,95,255,.18); border-radius: var(--rs); padding: 15px 17px; margin-bottom: 13px; display: flex; align-items: center; gap: 13px; flex-wrap: wrap }
        .fh-icon { font-size: 2rem; flex-shrink: 0 }
        .fh-name { font-size: .95rem; font-weight: 700; word-break: break-all; margin-bottom: 1px }
        .fh-meta { font-size: .77rem; color: var(--muted) }
        .fh-sz { margin-left: auto; text-align: right; flex-shrink: 0 }
        .fh-sz-big { font-size: 1.85rem; font-weight: 900; font-family: monospace; line-height: 1; background: linear-gradient(90deg, var(--accent), var(--green)); -webkit-background-clip: text; -webkit-text-fill-color: transparent }
        .fh-sz-lbl { font-size: .61rem; color: var(--muted); text-transform: uppercase; letter-spacing: .5px }
        .media-row { display: flex; gap: 13px; margin-bottom: 13px; align-items: flex-start }
        .thumb-box { width: 150px; min-width: 150px; border-radius: var(--rs); overflow: hidden; border: 1px solid var(--border); flex-shrink: 0 }
        .thumb-box img { width: 100%; display: block }
        .thumb-ph { width: 150px; height: 84px; background: var(--s3); display: flex; align-items: center; justify-content: center; font-size: 2rem; border-radius: var(--rs); flex-shrink: 0 }
        .chips { display: grid; grid-template-columns: repeat(auto-fill,minmax(100px,1fr)); gap: 6px; flex: 1 }
        .chip { background: var(--s2); border: 1px solid var(--border); border-radius: var(--rs); padding: 7px 9px }
        .chip-l { font-size: .61rem; color: var(--muted); text-transform: uppercase; letter-spacing: .5px }
        .chip-v { font-size: .86rem; font-weight: 700; margin-top: 1px; word-break: break-all }
        .prev-wrap { background: #000; border-radius: var(--rs); overflow: hidden; margin-bottom: 12px; border: 1px solid var(--border); display: none }
        video { width: 100%; display: block; max-height: 230px; object-fit: contain }
        .vctrl { display: flex; align-items: center; gap: 7px; padding: 7px 10px; background: var(--s2); border-top: 1px solid var(--border); flex-wrap: wrap }
        .tdisp { font-family: monospace; font-size: .77rem; color: var(--muted); min-width: 96px }
        .sbar { flex: 1; min-width: 60px; -webkit-appearance: none; height: 3px; border-radius: 2px; background: var(--s3); cursor: pointer; outline: none }
        .sbar::-webkit-slider-thumb { -webkit-appearance: none; width: 11px; height: 11px; border-radius: 50%; background: var(--accent); cursor: pointer }
        .vico { cursor: pointer; font-size: .83rem; color: var(--muted) }
        .vico:hover { color: var(--text) }

        #compress-section { display: none }
        .target-card { background: linear-gradient(155deg,#0e122a,#161c36); border: 2px solid rgba(108,95,255,.36); border-radius: var(--r); padding: 24px 20px; position: relative; overflow: hidden; margin-bottom: 16px }
        .target-card::before { content: ''; position: absolute; inset: 0; background: radial-gradient(ellipse at 50% 0%,rgba(108,95,255,.13),transparent 58%); pointer-events: none }
        .tc-title { font-size: 1.2rem; font-weight: 900; text-align: center; margin-bottom: 2px; background: linear-gradient(90deg,#fff,#c4bfff); -webkit-background-clip: text; -webkit-text-fill-color: transparent }
        .tc-sub { color: var(--muted); font-size: .81rem; text-align: center; margin-bottom: 20px }
        .spin-row { display: flex; align-items: stretch; justify-content: center; margin-bottom: 6px }
        .sp-btn { width: 48px; height: 60px; border: 2px solid var(--border); background: var(--s3); color: var(--text); font-size: 1.6rem; font-weight: 700; cursor: pointer; transition: all .18s; display: flex; align-items: center; justify-content: center }
        .sp-btn.minus { border-radius: var(--rs) 0 0 var(--rs); border-right: none }
        .sp-btn.plus  { border-radius: 0 var(--rs) var(--rs) 0; border-left: none }
        .sp-btn.minus:hover { background: rgba(231,76,60,.2); border-color: var(--red); color: var(--red) }
        .sp-btn.plus:hover  { background: rgba(46,204,113,.14); border-color: var(--green); color: var(--green) }
        .sp-inner { display: flex; flex-direction: column; align-items: center; border: 2px solid var(--accent); border-left: none; border-right: none; background: var(--s2) }
        .sp-num { width: 116px; height: 44px; text-align: center; font-size: 1.85rem; font-weight: 900; font-family: monospace; color: var(--text); background: transparent; border: none; outline: none }
        .sp-unit { font-size: .62rem; font-weight: 700; color: var(--accent); text-transform: uppercase; letter-spacing: .6px; padding-bottom: 3px }
        .sl-wrap { padding: 0 6px; margin-bottom: 17px }
        .sz-sl { width: 100%; -webkit-appearance: none; height: 6px; border-radius: 3px; background: var(--s3); cursor: pointer; outline: none }
        .sz-sl::-webkit-slider-thumb { -webkit-appearance: none; width: 19px; height: 19px; border-radius: 50%; background: var(--accent); cursor: pointer; border: 2px solid #fff; box-shadow: 0 2px 8px var(--ag) }
        .sz-sl::-moz-range-thumb { width: 17px; height: 17px; border-radius: 50%; background: var(--accent); cursor: pointer; border: 2px solid #fff }
        .sl-lbls { display: flex; justify-content: space-between; font-size: .67rem; color: var(--muted); margin-top: 4px }
        .mb-pre { display: flex; flex-wrap: wrap; gap: 6px; justify-content: center; margin-bottom: 18px }
        .mbp { padding: 5px 12px; border-radius: 20px; border: 1.5px solid var(--border); background: var(--s2); color: var(--muted); font-size: .77rem; font-weight: 700; cursor: pointer; transition: all .15s; user-select: none }
        .mbp:hover { border-color: var(--accent); color: var(--text) }
        .mbp.on { background: var(--accent); border-color: var(--accent); color: #fff; box-shadow: 0 3px 10px var(--ag) }
        .vis-cmp { background: var(--s2); border: 1px solid var(--border); border-radius: var(--rs); padding: 13px 15px; margin-bottom: 18px; display: none }
        .vc-row { display: flex; align-items: center; gap: 8px; margin-bottom: 7px; font-size: .83rem }
        .vc-lbl { color: var(--muted); width: 72px; flex-shrink: 0; font-size: .76rem }
        .vc-track { flex: 1; background: var(--s3); border-radius: 8px; height: 8px; overflow: hidden }
        .vc-bar { height: 100%; border-radius: 8px; transition: width .4s }
        .vc-bar.orig { background: linear-gradient(90deg,#e74c3c,#c0392b) }
        .vc-bar.tgt  { background: linear-gradient(90deg,var(--green),#27ae60) }
        .vc-val { font-weight: 700; font-size: .8rem; min-width: 58px; text-align: right }
        .vc-foot { display: flex; justify-content: space-between; padding-top: 7px; border-top: 1px solid var(--border); font-size: .8rem }
        .vc-red { color: var(--red); font-weight: 700 }
        .vc-sav { color: var(--green); font-weight: 700 }
        .fmt-card { background: var(--s2); border: 1px solid var(--border); border-radius: var(--rs); padding: 14px 15px; margin-bottom: 18px }
        .fmt-hd { font-size: .84rem; font-weight: 700; color: #b5b9d8; margin-bottom: 9px; display: flex; align-items: center; gap: 5px }
        .fmt-sel { display: flex; align-items: center; gap: 9px; margin-bottom: 9px; background: var(--s3); border: 1px solid var(--border); border-radius: var(--rs); padding: 8px 11px }
        .fmt-sel-ico { font-size: 1.15rem }
        .fmt-sel-lbl { font-size: .72rem; color: var(--muted) }
        .fmt-sel-name { font-size: .9rem; font-weight: 800 }
        .fmt-grp { margin-bottom: 7px }
        .fmt-grp-lbl { font-size: .63rem; font-weight: 800; text-transform: uppercase; letter-spacing: .6px; color: var(--muted); margin-bottom: 5px }
        .fps { display: flex; flex-wrap: wrap; gap: 5px }
        .fp { padding: 6px 11px; border-radius: 6px; border: 2px solid var(--border); background: var(--s3); color: var(--muted); font-size: .77rem; font-weight: 700; cursor: pointer; transition: all .14s; user-select: none; text-align: center; min-width: 44px }
        .fp:hover { border-color: var(--accent); color: var(--text) }
        .fp.sv { background: var(--accent); border-color: var(--accent); color: #fff; box-shadow: 0 2px 8px var(--ag) }
        .fp.sa { background: linear-gradient(135deg,#f39c12,#f1c40f); border-color: #f39c12; color: #0a0c15; box-shadow: 0 2px 8px rgba(243,156,18,.38); font-weight: 900 }
        .fp[data-fmt="mp3"] { border-color: rgba(243,156,18,.32); color: #f39c12 }
        .fp[data-fmt="mp3"].sa { color: #0a0c15 }
        .compress-btn { display: flex; align-items: center; justify-content: center; gap: 11px; width: 100%; padding: 18px; font-size: 1.1rem; font-weight: 900; background: linear-gradient(135deg,#6c5fff,#a09aff); border: none; border-radius: var(--rs); color: #fff; cursor: pointer; transition: all .3s; box-shadow: 0 6px 24px var(--ag); position: relative; overflow: hidden }
        .compress-btn::before { content: ''; position: absolute; inset: 0; background: linear-gradient(135deg,rgba(255,255,255,.13),transparent); pointer-events: none }
        .compress-btn:hover { transform: translateY(-3px); box-shadow: 0 12px 34px var(--ag) }
        .compress-btn:active { transform: translateY(0) }
        .compress-btn:disabled { opacity: .35; cursor: not-allowed; transform: none; box-shadow: none }
        .cb-ico { font-size: 1.35rem }
        .cb-lbl { display: flex; flex-direction: column; align-items: flex-start; line-height: 1.25 }
        .cb-main { font-size: 1.05rem; font-weight: 900 }
        .cb-sub  { font-size: .71rem; opacity: .75; font-weight: 600 }

        #proc-section { display: none }
        .proc-card { background: linear-gradient(135deg,var(--s1),#0d1122); border: 1px solid var(--border); border-radius: var(--r); padding: 36px 20px; text-align: center }
        .proc-ico { font-size: 2.5rem; margin-bottom: 9px; display: inline-block; animation: spin 2s linear infinite }
        @keyframes spin { to { transform: rotate(360deg) } }
        .proc-title { font-size: 1.1rem; font-weight: 700; margin-bottom: 4px }
        .proc-msg { color: var(--muted); font-size: .83rem; min-height: 17px }
        .bpb-bg { background: var(--s3); border-radius: 20px; height: 11px; overflow: hidden; margin: 12px 0 6px }
        .bpb { height: 100%; background: linear-gradient(90deg,var(--accent),#a09aff,var(--green)); background-size: 200%; border-radius: 20px; transition: width .4s; width: 0%; animation: sh 2s linear infinite }
        @keyframes sh { 0%{background-position:200% 0} 100%{background-position:-200% 0} }
        .bpct { font-size: 2rem; font-weight: 900; background: linear-gradient(90deg,var(--accent),var(--green)); -webkit-background-clip: text; -webkit-text-fill-color: transparent }
        .proc-det { color: var(--muted); font-size: .74rem; margin-top: 3px }

        #res-section { display: none }
        .res-card { background: linear-gradient(135deg,var(--s1),#0c1120); border: 1px solid var(--border); border-radius: var(--r); padding: 26px; text-align: center }
        .res-tick { font-size: 3rem; margin-bottom: 8px; display: block; animation: pop .4s ease }
        @keyframes pop { 0%{transform:scale(0)} 70%{transform:scale(1.14)} 100%{transform:scale(1)} }
        .res-title { font-size: 1.15rem; font-weight: 800; margin-bottom: 13px }
        .sc-wrap { display: flex; align-items: center; justify-content: center; gap: 12px; flex-wrap: wrap; margin-bottom: 14px }
        .sc-box { background: var(--s2); border: 1px solid var(--border); border-radius: var(--rs); padding: 11px 16px; text-align: center; min-width: 105px }
        .sc-box.orig { border-color: rgba(231,76,60,.26) }
        .sc-box.comp { border-color: rgba(46,204,113,.26); background: rgba(46,204,113,.05) }
        .sc-lbl { font-size: .61rem; color: var(--muted); text-transform: uppercase; letter-spacing: .5px; margin-bottom: 2px }
        .sc-val { font-size: 1.45rem; font-weight: 900; font-family: monospace }
        .sc-val.r { color: var(--red) }
        .sc-val.g { color: var(--green) }
        .sc-arr { font-size: 1.5rem; color: var(--muted) }
        .rbadge { display: inline-flex; align-items: center; gap: 7px; background: rgba(46,204,113,.09); border: 1px solid rgba(46,204,113,.24); border-radius: 20px; padding: 6px 16px; font-size: .92rem; font-weight: 800; color: var(--green); margin-bottom: 16px }

        .modal-ov { position: fixed; inset: 0; background: rgba(0,0,0,.78); display: flex; align-items: center; justify-content: center; z-index: 1000; backdrop-filter: blur(4px); animation: fdi .2s ease }
        @keyframes fdi { from{opacity:0} to{opacity:1} }
        .modal { background: var(--s1); border: 1px solid var(--border); border-radius: var(--r); padding: 26px 24px; width: min(400px,92vw); box-shadow: 0 20px 60px rgba(0,0,0,.75); animation: sli2 .25s ease }
        @keyframes sli2 { from{transform:translateY(28px);opacity:0} to{transform:none;opacity:1} }
        .modal-title { font-size: 1.05rem; font-weight: 800; margin-bottom: 4px }
        .modal-sub { color: var(--muted); font-size: .8rem; margin-bottom: 16px }
        .modal-irow { display: flex; align-items: stretch; margin-bottom: 14px }
        .modal-inp { flex: 1; background: var(--s2); border: 2px solid var(--accent); border-radius: var(--rs) 0 0 var(--rs); color: var(--text); padding: 10px 13px; font-size: .93rem; outline: none; border-right: none }
        .modal-inp:focus { border-color: #a09aff }
        .modal-ext { background: var(--s3); border: 2px solid var(--accent); border-left: none; border-radius: 0 var(--rs) var(--rs) 0; padding: 10px 12px; color: var(--muted); font-size: .84rem; font-weight: 700; white-space: nowrap; display: flex; align-items: center }
        .modal-btns { display: flex; gap: 8px }
        .modal-ok { flex: 1; padding: 11px; background: linear-gradient(135deg,var(--green),#27ae60); border: none; border-radius: var(--rs); color: #050f0a; font-size: .92rem; font-weight: 800; cursor: pointer; transition: all .2s }
        .modal-ok:hover { transform: translateY(-1px); box-shadow: 0 4px 14px var(--gg) }
        .modal-cx { padding: 11px 16px; background: var(--s2); border: 1px solid var(--border); border-radius: var(--rs); color: var(--muted); font-size: .84rem; cursor: pointer; transition: all .2s }
        .modal-cx:hover { border-color: var(--accent); color: var(--text) }

        .btn { display: inline-flex; align-items: center; gap: 5px; padding: 8px 15px; border-radius: var(--rs); border: none; font-size: .81rem; font-weight: 600; cursor: pointer; transition: all .2s }
        .btn:disabled { opacity: .35; cursor: not-allowed }
        .btn-ghost { background: var(--s2); border: 1px solid var(--border); color: var(--text) }
        .btn-ghost:hover { border-color: var(--accent); color: var(--accent) }
        .btn-sm { padding: 4px 10px; font-size: .74rem }
        .dl-btn { display: inline-flex; align-items: center; gap: 9px; padding: 13px 32px; font-size: 1rem; font-weight: 800; background: linear-gradient(135deg,var(--green),#27ae60); border: none; border-radius: var(--rs); color: #050f0a; cursor: pointer; transition: all .3s; box-shadow: 0 5px 20px var(--gg) }
        .dl-btn:hover { transform: translateY(-3px); box-shadow: 0 9px 28px var(--gg) }
        .again-btn { display: inline-flex; align-items: center; gap: 6px; margin-top: 8px; padding: 9px 20px; background: var(--s2); border: 1px solid var(--border); border-radius: var(--rs); color: var(--text); font-size: .84rem; font-weight: 600; cursor: pointer; transition: all .2s }
        .again-btn:hover { border-color: var(--accent); color: var(--accent) }
        .warn-box { background: rgba(231,76,60,.07); border: 1px solid rgba(231,76,60,.22); border-radius: var(--rs); padding: 8px 12px; font-size: .79rem; color: var(--red); margin-bottom: 12px; display: none }

        .tw { position: fixed; bottom: 16px; right: 16px; display: flex; flex-direction: column; gap: 5px; z-index: 9999; pointer-events: none }
        .toast { background: var(--s2); border: 1px solid var(--border); border-radius: var(--rs); padding: 9px 12px; font-size: .8rem; max-width: 280px; box-shadow: var(--sh); animation: tsi .3s ease; pointer-events: all }
        .toast.success { border-left: 3px solid var(--green); color: var(--green) }
        .toast.error   { border-left: 3px solid var(--red);   color: var(--red) }
        .toast.info    { border-left: 3px solid var(--accent); color: var(--text) }
        @keyframes tsi { from{transform:translateX(105%);opacity:0} to{transform:none;opacity:1} }
        @keyframes tso { to{transform:translateX(105%);opacity:0} }

        @media(max-width:540px) {
            .file-hero { flex-direction: column; text-align: center }
            .fh-sz { margin-left: 0 }
            .media-row { flex-direction: column }
            .thumb-box, .thumb-ph { width: 100%; min-width: unset }
            .sc-wrap { flex-direction: column }
            .sp-num { width: 100px; font-size: 1.6rem }
            .compress-btn { font-size: .98rem; padding: 15px }
        }
    </style>
</head>

<body>
<div class="wrap">

    <div class="hdr">
        <span class="hdr-ico">🗜️</span>
        <div>
            <div class="hdr-title">Media Compressor</div>
            <div class="hdr-sub">Compress MP4 · MP3 · any video or audio to your exact target size</div>
        </div>
        {% if ffmpeg_ok %}
        <span class="ffpill ffok">✅ FFmpeg Ready</span>
        {% else %}
        <span class="ffpill ffbad">❌ FFmpeg Missing</span>
        {% endif %}
    </div>

    <!-- STEP 1 -->
    <div class="card" id="upload-card">
        <div class="card-hd">📂 Step 1 — Upload Your File</div>
        <div class="drop" id="drop-zone" onclick="$('file-input').click()">
            <span class="drop-ico">🎬</span>
            <div class="drop-title">Drop your video or audio file here</div>
            <div class="drop-sub">Click to browse · up to 10 GB · MP4, MP3, MKV, AVI, WAV, FLAC and more</div>
            <div class="ft-tags">
                <span class="ft">MP4</span><span class="ft">MP3</span><span class="ft">MKV</span>
                <span class="ft">AVI</span><span class="ft">MOV</span><span class="ft">WAV</span>
                <span class="ft">AAC</span><span class="ft">FLAC</span><span class="ft">OGG</span>
                <span class="ft">WebM</span><span class="ft">WMV</span><span class="ft">+ more</span>
            </div>
            <button class="browse-btn" type="button">📁 Browse Files</button>
        </div>
        <input type="file" id="file-input"
            accept="video/*,audio/*,.mkv,.avi,.wmv,.flv,.ts,.mts,.vob,.rm,.rmvb,.flac,.opus,.ogg">

        <div class="upwrap" id="upwrap">
            <div class="up-hdr">
                <span class="up-name" id="up-name">Uploading…</span>
                <span class="up-pct" id="up-pct">0%</span>
            </div>
            <div class="pb-track"><div class="pb-fill" id="pb-fill"></div></div>
            <div class="up-row2">
                <span id="up-transferred">—</span>
                <span id="up-speed">—</span>
                <span id="up-eta">—</span>
            </div>
        </div>
    </div>

    <!-- FILE INFO -->
    <div id="info-section">
        <div class="card">
            <div class="card-hd">
                <span id="info-ico">📊</span>
                <span id="info-ttl">File Information</span>
                <button class="btn btn-ghost btn-sm" style="margin-left:auto" onclick="resetAll()">🔄 New File</button>
            </div>
            <div class="file-hero">
                <span class="fh-icon" id="fh-icon">🎬</span>
                <div style="flex:1;min-width:0">
                    <div class="fh-name" id="fh-name">—</div>
                    <div class="fh-meta" id="fh-meta">—</div>
                </div>
                <div class="fh-sz">
                    <div class="fh-sz-big" id="fh-sz-big">0 MB</div>
                    <div class="fh-sz-lbl">Original Size</div>
                </div>
            </div>
            <div class="media-row">
                <div id="thumb-con" class="thumb-ph">🎞️</div>
                <div class="chips" id="chips"></div>
            </div>
            <div class="prev-wrap" id="prev-wrap">
                <video id="prev-vid" preload="metadata"></video>
                <div class="vctrl">
                    <button class="btn btn-ghost btn-sm" id="play-btn" onclick="togglePlay()">▶️</button>
                    <span class="tdisp" id="tdisp">00:00:00 / 00:00:00</span>
                    <input type="range" class="sbar" id="sbar" min="0" max="1000" value="0">
                    <span class="vico" onclick="toggleMute()">🔊</span>
                </div>
            </div>
        </div>
    </div>

    <!-- STEP 2 -->
    <div id="compress-section">
        <div class="card">
            <div class="card-hd">🗜️ Step 2 — Compression Settings</div>
            <div class="warn-box" id="warn-box"><span id="warn-txt"></span></div>

            <div class="target-card">
                <div class="tc-title">What size do you want?</div>
                <div class="tc-sub" id="tc-sub">Enter your target file size in MB</div>
                <div class="spin-row">
                    <button class="sp-btn minus" onclick="chgMB(-1)">−</button>
                    <div class="sp-inner">
                        <input class="sp-num" type="number" id="mb-input" value="10" min="0.1" max="9999" step="0.1"
                            oninput="onMBInput()" onkeydown="if(event.key==='Enter')doCompress()">
                        <div class="sp-unit">MB</div>
                    </div>
                    <button class="sp-btn plus" onclick="chgMB(1)">+</button>
                </div>
                <div class="sl-wrap">
                    <input type="range" class="sz-sl" id="mb-slider" min="0.1" max="100" step="0.1" value="10"
                        oninput="onSlider()">
                    <div class="sl-lbls">
                        <span>0.1 MB</span>
                        <span id="sl-mid">—</span>
                        <span id="sl-max">—</span>
                    </div>
                </div>
                <div class="mb-pre">
                    <span class="mbp" onclick="setMB(1,this)">1 MB</span>
                    <span class="mbp" onclick="setMB(5,this)">5 MB</span>
                    <span class="mbp on" onclick="setMB(10,this)">10 MB</span>
                    <span class="mbp" onclick="setMB(25,this)">25 MB</span>
                    <span class="mbp" onclick="setMB(50,this)">50 MB</span>
                    <span class="mbp" onclick="setMB(100,this)">100 MB</span>
                    <span class="mbp" onclick="setMB(250,this)">250 MB</span>
                    <span class="mbp" onclick="setMB(500,this)">500 MB</span>
                </div>
                <div class="vis-cmp" id="vis-cmp">
                    <div class="vc-row">
                        <span class="vc-lbl">Original</span>
                        <div class="vc-track"><div class="vc-bar orig" style="width:100%"></div></div>
                        <span class="vc-val" id="vc-orig">—</span>
                    </div>
                    <div class="vc-row" style="margin-bottom:0">
                        <span class="vc-lbl">Target</span>
                        <div class="vc-track"><div class="vc-bar tgt" id="vc-tgt-bar" style="width:50%"></div></div>
                        <span class="vc-val" id="vc-tgt">—</span>
                    </div>
                    <div class="vc-foot">
                        <span>Reduction: <span class="vc-red" id="vc-red">—</span></span>
                        <span>Save: <span class="vc-sav" id="vc-sav">—</span></span>
                    </div>
                </div>
            </div>

            <div class="fmt-card">
                <div class="fmt-hd">🎯 Output Format</div>
                <div class="fmt-sel" id="fmt-sel">
                    <span class="fmt-sel-ico" id="fsi">🎬</span>
                    <div>
                        <div class="fmt-sel-lbl">Selected</div>
                        <div class="fmt-sel-name" id="fsn">MP4 (Video)</div>
                    </div>
                </div>
                <div class="fmt-grp">
                    <div class="fmt-grp-lbl">📹 Video</div>
                    <div class="fps">
                        <span class="fp sv" data-fmt="mp4" data-type="v" data-lbl="MP4 (Video)" data-ico="🎬" onclick="selFmt(this)">MP4</span>
                        <span class="fp" data-fmt="mkv" data-type="v" data-lbl="MKV (Video)" data-ico="🎬" onclick="selFmt(this)">MKV</span>
                        <span class="fp" data-fmt="avi" data-type="v" data-lbl="AVI (Video)" data-ico="🎬" onclick="selFmt(this)">AVI</span>
                        <span class="fp" data-fmt="mov" data-type="v" data-lbl="MOV (Video)" data-ico="🎬" onclick="selFmt(this)">MOV</span>
                        <span class="fp" data-fmt="webm" data-type="v" data-lbl="WebM (Video)" data-ico="🎬" onclick="selFmt(this)">WebM</span>
                    </div>
                </div>
                <div class="fmt-grp" style="margin-top:8px">
                    <div class="fmt-grp-lbl">🎵 Audio</div>
                    <div class="fps">
                        <span class="fp" data-fmt="mp3" data-type="a" data-lbl="MP3 (Audio)" data-ico="🎵" onclick="selFmt(this)">🎵 MP3</span>
                        <span class="fp" data-fmt="aac" data-type="a" data-lbl="AAC (Audio)" data-ico="🎵" onclick="selFmt(this)">AAC</span>
                        <span class="fp" data-fmt="wav" data-type="a" data-lbl="WAV (Audio)" data-ico="🎵" onclick="selFmt(this)">WAV</span>
                        <span class="fp" data-fmt="ogg" data-type="a" data-lbl="OGG (Audio)" data-ico="🎵" onclick="selFmt(this)">OGG</span>
                        <span class="fp" data-fmt="opus" data-type="a" data-lbl="OPUS (Audio)" data-ico="🎵" onclick="selFmt(this)">OPUS</span>
                        <span class="fp" data-fmt="flac" data-type="a" data-lbl="FLAC (Audio)" data-ico="🎵" onclick="selFmt(this)">FLAC</span>
                        <span class="fp" data-fmt="m4a" data-type="a" data-lbl="M4A (Audio)" data-ico="🎵" onclick="selFmt(this)">M4A</span>
                    </div>
                </div>
                <input type="hidden" id="out-fmt" value="mp4">
            </div>

            <button class="compress-btn" id="compress-btn" onclick="doCompress()">
                <span class="cb-ico">🗜️</span>
                <span class="cb-lbl">
                    <span class="cb-main" id="cb-main">Compress to 10 MB</span>
                    <span class="cb-sub" id="cb-sub">as MP4 · click to start</span>
                </span>
            </button>
        </div>
    </div>

    <!-- PROCESSING -->
    <div id="proc-section">
        <div class="proc-card">
            <div class="proc-ico" id="proc-ico">🗜️</div>
            <div class="proc-title" id="proc-title">Compressing…</div>
            <div class="proc-msg" id="proc-msg">Preparing…</div>
            <div class="bpb-bg"><div class="bpb" id="bpb"></div></div>
            <div class="bpct" id="bpct">0%</div>
            <div class="proc-det" id="proc-det"></div>
        </div>
    </div>

    <!-- RESULT -->
    <div id="res-section">
        <div class="res-card">
            <span class="res-tick">✅</span>
            <div class="res-title" id="res-title">Compression Complete!</div>
            <div class="sc-wrap">
                <div class="sc-box orig">
                    <div class="sc-lbl">Original</div>
                    <div class="sc-val r" id="r-orig">—</div>
                </div>
                <div class="sc-arr">→</div>
                <div class="sc-box comp">
                    <div class="sc-lbl">Compressed</div>
                    <div class="sc-val g" id="r-comp">—</div>
                </div>
            </div>
            <div class="rbadge">
                🎉 <span id="r-pct">—</span> smaller
                &nbsp;·&nbsp; saved <span id="r-save">—</span>
            </div>
            <div style="display:flex;flex-direction:column;align-items:center;gap:8px">
                <button class="dl-btn" id="dl-btn" onclick="askFilename()">⬇️ Download Compressed File</button>
                <button class="again-btn" onclick="compressAgain()">🔄 Compress Again</button>
                <button class="again-btn" onclick="resetAll()">📂 Upload New File</button>
            </div>
        </div>
    </div>

</div>

<!-- FILENAME MODAL -->
<div class="modal-ov" id="modal" style="display:none" onclick="if(event.target===this)closeModal()">
    <div class="modal">
        <div class="modal-title">💾 Save As…</div>
        <div class="modal-sub">Choose a name for the compressed file</div>
        <div class="modal-irow">
            <input class="modal-inp" type="text" id="modal-name" placeholder="compressed_file"
                onkeydown="if(event.key==='Enter')confirmDL()">
            <span class="modal-ext" id="modal-ext">.mp4</span>
        </div>
        <div class="modal-btns">
            <button class="modal-ok" onclick="confirmDL()">⬇️ Download</button>
            <button class="modal-cx" onclick="closeModal()">Cancel</button>
        </div>
    </div>
</div>

<div class="tw" id="tw"></div>

<script>
const S = {
    fileId: null, filename: null, originalMB: 0,
    duration: 0, isAudio: false, fmt: 'mp4',
    jobId: null, evtSrc: null, resultExt: 'mp4',
};

const $ = id => document.getElementById(id);

function toast(msg, type = 'info', dur = 4000) {
    const w = $('tw'), el = document.createElement('div');
    el.className = `toast ${type}`; el.textContent = msg; w.appendChild(el);
    setTimeout(() => {
        el.style.animation = 'tso .3s ease forwards';
        setTimeout(() => el.remove(), 300);
    }, dur);
}

function fmtT(s) {
    s = +s || 0;
    const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sc = (s%60).toFixed(1);
    return `${String(h).padStart(2,'0')}:${String(m).padStart(2,'0')}:${sc.padStart(4,'0')}`;
}
function fmtD(s) {
    s = +s || 0;
    const h = Math.floor(s/3600), m = Math.floor((s%3600)/60), sc = Math.floor(s%60);
    if (h) return `${h}h ${m}m ${sc}s`;
    if (m) return `${m}m ${sc}s`;
    return `${sc}s`;
}
function fmtSz(b) {
    if (!b) return '—';
    if (b >= 1073741824) return (b/1073741824).toFixed(2)+' GB';
    if (b >= 1048576)    return (b/1048576).toFixed(1)+' MB';
    return (b/1024).toFixed(0)+' KB';
}
function fmtSpd(bps) {
    if (bps >= 1048576) return (bps/1048576).toFixed(1)+' MB/s';
    return (bps/1024).toFixed(0)+' KB/s';
}
function getMB() { return Math.max(0.1, parseFloat($('mb-input').value) || 0.1); }

/* ── Drag & drop ── */
const dz = $('drop-zone');
['dragenter','dragover'].forEach(e =>
    dz.addEventListener(e, ev => { ev.preventDefault(); dz.classList.add('over'); }));
['dragleave','drop'].forEach(e =>
    dz.addEventListener(e, ev => {
        ev.preventDefault(); dz.classList.remove('over');
        if (e === 'drop' && ev.dataTransfer.files[0])
            uploadFile(ev.dataTransfer.files[0]);
    }));
$('file-input').onchange = ev => { if (ev.target.files[0]) uploadFile(ev.target.files[0]); };

/* ── Upload ── */
function uploadFile(file) {
    if (file.size < 512)            { toast('File too small','error'); return; }
    if (file.size > 10*1073741824)  { toast('Max 10 GB','error'); return; }

    $('prev-vid').src = URL.createObjectURL(file);
    $('upwrap').style.display = 'block';
    $('up-name').textContent = file.name;
    $('up-pct').textContent = '0%';
    $('pb-fill').style.width = '0%';
    $('pb-fill').classList.remove('done');
    $('up-transferred').textContent = '';
    $('up-speed').textContent = '';
    $('up-eta').textContent = '';

    const t0 = Date.now();
    const fd = new FormData();
    fd.append('file', file);

    const xhr = new XMLHttpRequest();
    xhr.open('POST', '/upload');

    xhr.upload.onprogress = ev => {
        if (!ev.lengthComputable) return;
        const pct = Math.round(ev.loaded / ev.total * 100);
        const elapsed = (Date.now() - t0) / 1000;
        const speed = elapsed > 0.5 ? ev.loaded / elapsed : 0;
        const remain = speed > 0 ? Math.round((ev.total - ev.loaded) / speed) : 0;
        $('pb-fill').style.width = pct + '%';
        $('up-pct').textContent = pct + '%';
        $('up-transferred').textContent = `${fmtSz(ev.loaded)} / ${fmtSz(ev.total)}`;
        $('up-speed').textContent = speed > 0 ? fmtSpd(speed) : '';
        $('up-eta').textContent = remain > 1 ? `~${remain}s left` : '';
    };

    xhr.upload.onload = () => {
        $('pb-fill').style.width = '100%';
        $('up-name').textContent = 'Analysing file…';
        $('up-pct').textContent = '100%';
        $('up-speed').textContent = '';
        $('up-eta').textContent = '';
    };

    xhr.onload = () => {
        if (xhr.status === 200) {
            let d;
            try { d = JSON.parse(xhr.responseText); }
            catch(e) { toast('Invalid server response','error'); $('upwrap').style.display='none'; return; }
            if (d.error) { toast(d.error,'error'); $('upwrap').style.display='none'; return; }
            $('pb-fill').classList.add('done');
            $('up-name').textContent = '✅ Ready!';
            $('up-transferred').textContent = '';
            setTimeout(() => $('upwrap').style.display='none', 900);
            onUploaded(d);
        } else {
            let msg = `Upload failed (${xhr.status})`;
            try { msg = JSON.parse(xhr.responseText).error || msg; } catch{}
            toast(msg,'error');
            $('upwrap').style.display = 'none';
        }
    };
    xhr.onerror = () => { toast('Network error','error'); $('upwrap').style.display='none'; };
    xhr.send(fd);
}

/* ── After upload ── */
function onUploaded(data) {
    S.fileId     = data.file_id;
    S.filename   = data.filename;
    S.originalMB = data.info.size_mb;
    S.duration   = data.info.duration;
    S.isAudio    = data.is_audio || false;
    const info   = data.info;

    $('fh-icon').textContent = S.isAudio ? '🎵' : '🎬';
    $('fh-name').textContent = data.filename;
    $('fh-meta').textContent = S.isAudio
        ? [(info.audio_codec||'').toUpperCase()||'Audio', fmtD(info.duration),
           info.sample_rate ? info.sample_rate+' Hz' : null,
           info.channels    ? info.channels+'ch'     : null].filter(Boolean).join(' · ')
        : [(info.video_codec||'').toUpperCase()||'Video',
           info.width&&info.height ? `${info.width}×${info.height}` : null,
           info.fps ? info.fps+' fps' : null, fmtD(info.duration)].filter(Boolean).join(' · ');
    $('fh-sz-big').textContent = S.originalMB + ' MB';

    const tc = $('thumb-con');
    if (data.thumb) {
        tc.className = 'thumb-box';
        tc.innerHTML = `<img src="${data.thumb}" alt="preview">`;
    } else {
        tc.className = 'thumb-ph';
        tc.textContent = S.isAudio ? '🎵' : '🎞️';
    }

    let chips = '';
    const row = (l,v) => `<div class="chip"><div class="chip-l">${l}</div><div class="chip-v">${v||'—'}</div></div>`;
    chips += row('Duration', fmtD(info.duration));
    chips += row('File Size', info.size_mb+' MB');
    if (S.isAudio) {
        chips += row('Codec',       info.audio_codec||'—');
        chips += row('Bitrate',     info.bitrate ? Math.round(info.bitrate/1000)+' kbps' : '—');
        chips += row('Sample Rate', info.sample_rate ? info.sample_rate+' Hz' : '—');
        chips += row('Channels',    info.channels||'—');
    } else {
        chips += row('Resolution',  info.width&&info.height ? `${info.width}×${info.height}` : '—');
        chips += row('FPS',         info.fps||'—');
        chips += row('Video Codec', info.video_codec||'—');
        chips += row('Audio Codec', info.audio_codec||'—');
        chips += row('Bitrate',     info.bitrate ? Math.round(info.bitrate/1000)+' kbps' : '—');
    }
    $('chips').innerHTML = chips;

    $('info-section').style.display = 'block';
    $('prev-wrap').style.display    = 'block';
    setupPlayer();

    if (S.isAudio) {
        const mp3 = document.querySelector('.fp[data-fmt="mp3"]');
        if (mp3) selFmt(mp3);
    }

    const maxMB = Math.max(5, Math.floor(S.originalMB * 0.98));
    $('mb-slider').max = maxMB;
    $('sl-mid').textContent = Math.round(maxMB/2)+' MB';
    $('sl-max').textContent = maxMB+' MB';

    const def = Math.max(0.1, Math.round(S.originalMB * 0.5 * 10) / 10);
    $('mb-input').value     = def;
    $('mb-slider').value    = Math.min(def, maxMB);
    syncPresets(def);
    $('tc-sub').textContent = `Original: ${S.originalMB} MB — enter your target size below`;
    $('compress-section').style.display = 'block';
    updateUI();
    setTimeout(() => $('compress-section').scrollIntoView({behavior:'smooth',block:'start'}), 350);
    toast(`${S.isAudio?'Audio':'Video'} loaded · ${S.originalMB} MB`, 'success');
}

/* ── Player ── */
function setupPlayer() {
    const vid = $('prev-vid'), sb = $('sbar'), td = $('tdisp');
    vid.ontimeupdate = () => {
        const t = vid.currentTime, dur = vid.duration || S.duration;
        sb.value = dur ? Math.round(t/dur*1000) : 0;
        td.textContent = `${fmtT(t)} / ${fmtT(dur)}`;
    };
    vid.onplay  = () => $('play-btn').textContent = '⏸️';
    vid.onpause = () => $('play-btn').textContent = '▶️';
    sb.oninput  = () => { vid.currentTime = (sb.value/1000) * (vid.duration||S.duration); };
}
function togglePlay()  { const v=$('prev-vid'); v.paused?v.play():v.pause(); }
function toggleMute()  {
    const v=$('prev-vid'); v.muted=!v.muted;
    document.querySelector('.vico').textContent = v.muted?'🔇':'🔊';
}

/* ── MB controls ── */
function chgMB(d) {
    const cur = getMB();
    const step = cur>=100?10:cur>=10?1:cur>=1?0.5:0.1;
    const nv = Math.max(0.1, Math.round((cur+d*step)*10)/10);
    $('mb-input').value  = nv;
    $('mb-slider').value = Math.min(nv, parseFloat($('mb-slider').max));
    updateUI(); syncPresets(nv);
}
function onMBInput() { $('mb-slider').value=Math.min(getMB(),parseFloat($('mb-slider').max)); updateUI(); syncPresets(getMB()); }
function onSlider()  { $('mb-input').value=$('mb-slider').value; updateUI(); syncPresets(getMB()); }
function setMB(n,el) {
    $('mb-input').value=$n; $('mb-slider').value=Math.min(n,parseFloat($('mb-slider').max));
    document.querySelectorAll('.mbp').forEach(c=>c.classList.remove('on'));
    if(el) el.classList.add('on'); updateUI();
}
function syncPresets(v) {
    document.querySelectorAll('.mbp').forEach(c=>{
        c.classList.toggle('on', parseFloat(c.textContent)===v);
    });
}

function updateUI() {
    if (!S.originalMB) return;
    const tgt  = getMB();
    const orig = S.originalMB;
    const wb   = $('warn-box');
    if (tgt >= orig) {
        wb.style.display='block';
        $('warn-txt').textContent=`Target (${tgt} MB) must be smaller than original (${orig} MB)`;
        $('compress-btn').disabled=true;
    } else if (tgt < 0.05) {
        wb.style.display='block';
        $('warn-txt').textContent='Target too small (min 0.05 MB)';
        $('compress-btn').disabled=true;
    } else {
        wb.style.display='none';
        $('compress-btn').disabled=false;
    }
    const pct  = Math.min(tgt/orig*100,100).toFixed(1);
    const red  = Math.max(0,(1-tgt/orig)*100).toFixed(1);
    const saved = (orig-tgt).toFixed(1);
    $('vc-tgt-bar').style.width = pct+'%';
    $('vc-orig').textContent = orig+' MB';
    $('vc-tgt').textContent  = tgt+' MB';
    $('vc-red').textContent  = red+'%';
    $('vc-sav').textContent  = saved+' MB';
    $('vis-cmp').style.display = 'block';
    $('cb-main').textContent = `Compress to ${tgt} MB`;
    $('cb-sub').textContent  = `as ${S.fmt.toUpperCase()} · save ~${saved} MB`;
}

/* ── Format ── */
function selFmt(el) {
    document.querySelectorAll('.fp').forEach(p=>p.classList.remove('sv','sa'));
    el.classList.add(el.dataset.type==='a'?'sa':'sv');
    S.fmt = el.dataset.fmt;
    $('out-fmt').value  = el.dataset.fmt;
    $('fsi').textContent = el.dataset.ico;
    $('fsn').textContent = el.dataset.lbl;
    updateUI();
}

/* ── Compress ── */
async function doCompress() {
    if (!S.fileId) { toast('No file uploaded','error'); return; }
    const tgt = getMB();
    if (tgt <= 0 || tgt >= S.originalMB) { toast('Invalid target size','error'); return; }
    const fmt = $('out-fmt').value || 'mp4';
    S.resultExt = fmt;

    ['upload-card','info-section','compress-section'].forEach(id=>$(id).style.display='none');
    $('proc-section').style.display = 'block';
    $('res-section').style.display  = 'none';
    $('proc-ico').style.animation   = 'spin 2s linear infinite';
    $('proc-title').textContent     = `Compressing to ${tgt} MB…`;
    $('proc-msg').textContent       = 'Calculating optimal bitrate…';
    $('bpb').style.width            = '0%';
    $('bpct').textContent           = '0%';
    $('proc-det').textContent       = `${S.originalMB} MB → ${tgt} MB as ${fmt.toUpperCase()}`;

    try {
        const r = await fetch('/compress', {
            method: 'POST',
            headers: {'Content-Type':'application/json'},
            body: JSON.stringify({file_id:S.fileId, target_mb:tgt, output_format:fmt}),
        });
        const d = await r.json();
        if (d.error) { toast(d.error,'error'); backToCompress(); return; }
        S.jobId = d.job_id;
        startSSE(d.job_id);
    } catch(ex) {
        toast('Request failed: '+ex,'error');
        backToCompress();
    }
}

function backToCompress() {
    $('proc-section').style.display = 'none';
    $('upload-card').style.display  = 'block';
    $('info-section').style.display = 'block';
    $('compress-section').style.display = 'block';
}

/* ── SSE ── */
function startSSE(jid) {
    if (S.evtSrc) S.evtSrc.close();
    const es = new EventSource(`/progress_stream/${jid}`);
    S.evtSrc = es;
    es.onmessage = ev => {
        try {
            const d = JSON.parse(ev.data);
            if (d._done) { es.close(); return; }
            $('bpb').style.width   = (d.pct||0)+'%';
            $('bpct').textContent  = (d.pct||0)+'%';
            $('proc-msg').textContent = d.msg || '';
            if (d.status === 'completed') { es.close(); onDone(d); }
            else if (d.status === 'error') {
                es.close();
                toast('Error: '+(d.msg||'unknown'),'error',8000);
                backToCompress();
                $('proc-section').style.display = 'none';
            }
        } catch(e) {}
    };
    es.onerror = () => { es.close(); pollFallback(jid); };
}

function pollFallback(jid) {
    const iv = setInterval(async () => {
        try {
            const r = await fetch('/progress/'+jid);
            const d = await r.json();
            $('bpb').style.width   = (d.pct||0)+'%';
            $('bpct').textContent  = (d.pct||0)+'%';
            $('proc-msg').textContent = d.msg || '';
            if (d.status==='completed'||d.status==='error') {
                clearInterval(iv);
                if (d.status==='completed') onDone(d);
                else {
                    toast('Error: '+(d.msg||'unknown'),'error',8000);
                    backToCompress();
                    $('proc-section').style.display='none';
                }
            }
        } catch(e) {}
    }, 900);
}

/* ── Result ── */
function onDone(data) {
    $('proc-section').style.display = 'none';
    $('proc-ico').style.animation   = 'none';
    $('res-section').style.display  = 'block';
    S.resultExt = data.ext || S.resultExt;

    $('r-orig').textContent  = data.original_mb + ' MB';
    $('r-comp').textContent  = data.final_mb    + ' MB';
    $('r-pct').textContent   = data.reduction   + '%';
    $('r-save').textContent  = (data.original_mb - data.final_mb).toFixed(1) + ' MB';
    $('res-title').textContent = `${data.original_mb} MB → ${data.final_mb} MB`;

    $('res-section').scrollIntoView({behavior:'smooth',block:'start'});
    toast(`✅ ${data.original_mb} MB → ${data.final_mb} MB (${data.reduction}% smaller)`,'success',6000);
}

/* ── Filename modal ── */
function askFilename() {
    if (!S.jobId) { toast('No result ready','error'); return; }
    const base = (S.filename||'file').replace(/\.[^.]+$/,'').replace(/[<>:"/\\|?*]/g,'_');
    $('modal-name').value       = base + '_compressed';
    $('modal-ext').textContent  = '.'+S.resultExt;
    $('modal').style.display    = 'flex';
    setTimeout(()=>{ $('modal-name').focus(); $('modal-name').select(); }, 80);
}
function closeModal() { $('modal').style.display='none'; }
function confirmDL() {
    let name = ($('modal-name').value||'compressed').trim();
    name = name.replace(/[<>:"/\\|?*\x00-\x1f]/g,'_') || 'compressed';
    name = name.replace(/\.[^.]+$/,'');
    $('modal').style.display = 'none';
    // Direct browser download — no base64, streams from server
    window.location.href = `/download/${S.jobId}?filename=${encodeURIComponent(name+'.'+S.resultExt)}`;
}

/* ── Compress again / reset ── */
function compressAgain() {
    $('res-section').style.display  = 'none';
    $('upload-card').style.display  = 'block';
    $('info-section').style.display = 'block';
    $('compress-section').style.display = 'block';
    $('compress-section').scrollIntoView({behavior:'smooth',block:'start'});
}

function resetAll() {
    if (S.fileId)
        fetch('/delete_upload/'+S.fileId,{method:'POST'}).catch(()=>{});
    if (S.evtSrc) S.evtSrc.close();
    Object.assign(S,{fileId:null,filename:null,originalMB:0,duration:0,
        isAudio:false,fmt:'mp4',jobId:null,evtSrc:null,resultExt:'mp4'});

    ['info-section','compress-section','proc-section','res-section']
        .forEach(id=>$(id).style.display='none');
    $('upload-card').style.display = 'block';
    $('file-input').value          = '';
    $('pb-fill').style.width       = '0%';
    $('pb-fill').classList.remove('done');
    $('prev-vid').src              = '';
    $('prev-wrap').style.display   = 'none';
    $('upwrap').style.display      = 'none';
    $('chips').innerHTML           = '';
    $('thumb-con').className       = 'thumb-ph';
    $('thumb-con').textContent     = '🎞️';
    $('mb-input').value            = '10';
    $('mb-slider').value           = '10';
    $('vis-cmp').style.display     = 'none';
    $('warn-box').style.display    = 'none';
    document.querySelectorAll('.fp').forEach(p=>{
        p.classList.remove('sv','sa');
        p.classList.toggle('sv', p.dataset.fmt==='mp4');
    });
    $('out-fmt').value          = 'mp4';
    $('fsi').textContent        = '🎬';
    $('fsn').textContent        = 'MP4 (Video)';
    $('cb-main').textContent    = 'Compress to 10 MB';
    $('cb-sub').textContent     = 'as MP4 · click to start';
    document.querySelectorAll('.mbp').forEach(c=>c.classList.toggle('on',c.textContent==='10 MB'));
    window.scrollTo({top:0,behavior:'smooth'});
    toast('Ready for a new file','info',2000);
}

document.addEventListener('keydown', ev => {
    if (['INPUT','TEXTAREA'].includes(ev.target.tagName)) return;
    const v = $('prev-vid');
    if (ev.code==='Space')      { ev.preventDefault(); togglePlay(); }
    if (ev.code==='ArrowLeft')  v.currentTime = Math.max(0, v.currentTime-5);
    if (ev.code==='ArrowRight') v.currentTime = Math.min(S.duration, v.currentTime+5);
    if (ev.code==='Escape')     closeModal();
});
</script>
</body>
</html>'''


if __name__ == '__main__':
    app.run(debug=False, host='0.0.0.0', port=5000, threaded=True)
