# app.py  ── Windows-compatible version
from flask import Flask, render_template, request, jsonify, Response, send_file
import subprocess, os, uuid, shutil, json, threading, time, re, signal, sys, atexit
import tempfile, io, base64, logging
from pathlib import Path
import multiprocessing

# ── Logging ────────────────────────────────────────────────────────────────────
logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[logging.StreamHandler(sys.stdout)]
)
log = logging.getLogger(__name__)

app = Flask(__name__)

UPLOAD_DIR = tempfile.mkdtemp(prefix='mcp_up_')
log.info(f'Upload dir: {UPLOAD_DIR}')

jobs          = {}
jobs_lock     = threading.Lock()
result_store  = {}
result_lock   = threading.Lock()

CPU_CORES  = multiprocessing.cpu_count()
FF_THREADS = max(1, CPU_CORES - 1)

# Windows null device
NULL_DEV = 'NUL' if sys.platform == 'win32' else '/dev/null'

ALLOWED_EXTENSIONS = {
    'mp4','mkv','avi','mov','wmv','flv','webm','m4v','mpeg','mpg','3gp',
    'ts','mts','m2ts','vob','rmvb','rm','asf','ogv','mxf','f4v','divx',
    'xvid','hevc','h264','h265','mp2','m2v','m4p','m4b','m4r',
    'mp3','wav','aac','ogg','opus','flac','wma','m4a'
}
AUDIO_EXTENSIONS = {
    'mp3','wav','aac','ogg','opus','flac','wma','m4a','m4b','m4r'
}
MIME_MAP = {
    'mp4' : 'video/mp4',
    'mkv' : 'video/x-matroska',
    'avi' : 'video/x-msvideo',
    'mov' : 'video/quicktime',
    'webm': 'video/webm',
    'ts'  : 'video/mp2t',
    'flv' : 'video/x-flv',
    'wmv' : 'video/x-ms-wmv',
    'mp3' : 'audio/mpeg',
    'aac' : 'audio/aac',
    'wav' : 'audio/wav',
    'ogg' : 'audio/ogg',
    'opus': 'audio/opus',
    'flac': 'audio/flac',
    'm4a' : 'audio/mp4',
}


# ── Cleanup ────────────────────────────────────────────────────────────────────
def cleanup_all():
    try:
        shutil.rmtree(UPLOAD_DIR, ignore_errors=True)
        log.info('Cleaned up temp dir')
    except Exception as e:
        log.warning(f'Cleanup error: {e}')

atexit.register(cleanup_all)

def _sig(s, f):
    cleanup_all()
    sys.exit(0)

signal.signal(signal.SIGINT,  _sig)
signal.signal(signal.SIGTERM, _sig)
if hasattr(signal, 'SIGBREAK'):
    signal.signal(signal.SIGBREAK, _sig)

def _bg_clean():
    while True:
        time.sleep(600)
        now = time.time()
        try:
            for fn in os.listdir(UPLOAD_DIR):
                fp = os.path.join(UPLOAD_DIR, fn)
                if os.path.isfile(fp) and now - os.path.getmtime(fp) > 7200:
                    try:
                        os.remove(fp)
                    except Exception:
                        pass
        except Exception:
            pass
        with result_lock:
            stale = [k for k, v in result_store.items()
                     if now - v.get('ts', 0) > 7200]
            for k in stale:
                del result_store[k]
        with jobs_lock:
            stale = [k for k, v in jobs.items()
                     if now - v.get('ts', 0) > 7200]
            for k in stale:
                del jobs[k]

threading.Thread(target=_bg_clean, daemon=True).start()


# ── FFmpeg helpers ─────────────────────────────────────────────────────────────
def check_ffmpeg() -> bool:
    try:
        r = subprocess.run(
            ['ffmpeg', '-version'],
            capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False

def check_ffprobe() -> bool:
    try:
        r = subprocess.run(
            ['ffprobe', '-version'],
            capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False

def get_hw_accel() -> dict:
    hw = {
        'nvenc': False, 'qsv': False,
        'videotoolbox': False, 'vaapi': False
    }
    try:
        r = subprocess.run(
            ['ffmpeg', '-encoders'],
            capture_output=True, text=True, timeout=10)
        o = r.stdout
        if 'h264_nvenc'        in o: hw['nvenc']        = True
        if 'h264_qsv'          in o: hw['qsv']          = True
        if 'h264_videotoolbox' in o: hw['videotoolbox'] = True
        if 'h264_vaapi'        in o: hw['vaapi']        = True
    except Exception as e:
        log.warning(f'HW accel check: {e}')
    return hw

HW = get_hw_accel()

def best_encoder() -> str:
    if HW['nvenc']:        return 'h264_nvenc'
    if HW['videotoolbox']: return 'h264_videotoolbox'
    if HW['qsv']:          return 'h264_qsv'
    if HW['vaapi']:        return 'h264_vaapi'
    return 'libx264'

def allowed_file(filename: str) -> bool:
    return ('.' in filename and
            filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS)

def is_audio_file(filename: str) -> bool:
    return ('.' in filename and
            filename.rsplit('.', 1)[1].lower() in AUDIO_EXTENSIONS)


# ── Media info ─────────────────────────────────────────────────────────────────
def get_media_info(path: str) -> dict:
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet',
             '-print_format', 'json',
             '-show_streams', '-show_format', path],
            capture_output=True, text=True, timeout=120)

        if r.returncode != 0:
            log.error(f'ffprobe rc={r.returncode}: {r.stderr[:300]}')
            return None

        data = json.loads(r.stdout)
        sz   = os.path.getsize(path)
        info = {
            'duration'     : 0,
            'width'        : 0,
            'height'       : 0,
            'fps'          : 0,
            'size'         : sz,
            'size_mb'      : round(sz / 1048576, 2),
            'video_codec'  : '',
            'audio_codec'  : '',
            'bitrate'      : 0,
            'has_audio'    : False,
            'is_audio_only': False,
            'sample_rate'  : 0,
            'channels'     : 0,
        }

        fmt = data.get('format', {})
        try:    info['duration'] = float(fmt.get('duration') or 0)
        except: pass
        try:    info['bitrate']  = int(float(fmt.get('bit_rate') or 0))
        except: pass

        has_video = False
        for s in data.get('streams', []):
            ct = s.get('codec_type', '')
            if ct == 'video' and not has_video:
                has_video           = True
                info['width']       = int(s.get('width',  0) or 0)
                info['height']      = int(s.get('height', 0) or 0)
                info['video_codec'] = s.get('codec_name', '') or ''
                try:
                    parts = s.get('r_frame_rate', '0/1').split('/')
                    n, d  = int(parts[0]), int(parts[1])
                    info['fps'] = round(n / d, 2) if d else 0
                except Exception:
                    info['fps'] = 0
                if not info['duration']:
                    try: info['duration'] = float(s.get('duration') or 0)
                    except: pass
            elif ct == 'audio':
                info['has_audio']   = True
                info['audio_codec'] = s.get('codec_name', '') or ''
                try:    info['sample_rate'] = int(s.get('sample_rate') or 0)
                except: pass
                try:    info['channels']    = int(s.get('channels') or 0)
                except: pass
                if not info['duration']:
                    try: info['duration'] = float(s.get('duration') or 0)
                    except: pass

        info['is_audio_only'] = (not has_video) and info['has_audio']

        if not info['duration']:
            try:
                r2 = subprocess.run(
                    ['ffprobe', '-v', 'error',
                     '-show_entries', 'format=duration',
                     '-of', 'default=noprint_wrappers=1:nokey=1', path],
                    capture_output=True, text=True, timeout=60)
                info['duration'] = float(r2.stdout.strip() or 0)
            except Exception:
                pass

        log.debug(f'media_info OK: {info["size_mb"]} MB  '
                  f'dur={info["duration"]:.1f}s  '
                  f'{info["width"]}x{info["height"]}  '
                  f'v={info["video_codec"]}  a={info["audio_codec"]}')
        return info

    except json.JSONDecodeError as e:
        log.error(f'ffprobe JSON error: {e}')
        return None
    except subprocess.TimeoutExpired:
        log.error('ffprobe timed out')
        return None
    except Exception as e:
        log.error(f'get_media_info error: {e}', exc_info=True)
        return None


def make_thumb_b64(path: str, ts: float = 5.0) -> str:
    """Generate thumbnail — returns base64 data URI or None."""
    try:
        ts = max(0.0, min(float(ts), 30.0))
        cmd = [
            'ffmpeg', '-y',
            '-ss', f'{ts:.2f}',
            '-i', path,
            '-vframes', '1',
            '-q:v', '5',
            '-vf', 'scale=480:-2',
            '-f', 'image2pipe',
            '-vcodec', 'mjpeg',
            'pipe:1'
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode == 0 and len(r.stdout) > 200:
            b64 = base64.b64encode(r.stdout).decode()
            return f'data:image/jpeg;base64,{b64}'
        log.debug(f'Thumb rc={r.returncode}  '
                  f'stderr={r.stderr[-80:].decode(errors="replace")}')
    except subprocess.TimeoutExpired:
        log.warning('Thumbnail timed out')
    except Exception as e:
        log.warning(f'Thumbnail error: {e}')
    return None


# ── Progress ───────────────────────────────────────────────────────────────────
def set_prog(jid: str, pct: int, msg: str,
             status: str = 'processing', extra: dict = None) -> None:
    with jobs_lock:
        jobs[jid] = {
            'pct'   : pct,
            'msg'   : msg,
            'status': status,
            'ts'    : time.time(),
            **(extra or {}),
        }

def get_prog(jid: str) -> dict:
    with jobs_lock:
        return dict(jobs.get(jid, {}))


# ── Bitrate math ───────────────────────────────────────────────────────────────
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


# ── Windows-safe subprocess helper ────────────────────────────────────────────
def _popen(cmd: list) -> subprocess.Popen:
    """
    Launch FFmpeg. On Windows we need CREATE_NO_WINDOW so the console
    doesn't flash, and we must NOT use shell=True (security + path issues).
    stderr is merged into stdout so we only read one pipe.
    """
    kwargs = dict(
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        text=True,
        bufsize=1,
    )
    if sys.platform == 'win32':
        kwargs['creationflags'] = subprocess.CREATE_NO_WINDOW
    return subprocess.Popen(cmd, **kwargs)


# ── Compression worker ─────────────────────────────────────────────────────────
def do_compress(job_id: str, src: str, target_mb: float,
                out_fmt: str, info: dict) -> None:
    tmp_out  = None
    tmp_pass = None

    try:
        set_prog(job_id, 2, 'Analysing media…')
        log.info(f'[{job_id}] START  '
                 f'src={os.path.basename(src)}  '
                 f'target={target_mb} MB  fmt={out_fmt}')

        dur          = float(info['duration'])
        orig_mb      = float(info['size_mb'])
        is_audio_out = out_fmt in ('mp3','aac','wav','ogg','opus','flac','m4a')
        is_audio_src = bool(info.get('is_audio_only'))

        if dur <= 0:
            raise ValueError('Duration is 0 — cannot calculate bitrate.')

        # ── Temp output file ───────────────────────────────────────────────
        tmp_fd, tmp_out = tempfile.mkstemp(
            suffix='.' + out_fmt, dir=UPLOAD_DIR)
        os.close(tmp_fd)
        # Delete the empty placeholder so FFmpeg can create it fresh
        os.remove(tmp_out)
        log.debug(f'[{job_id}] tmp_out={tmp_out}')

        use_2pass = False

        # ══════════════════════════════════════════════════════════════════
        if is_audio_out or is_audio_src:
            # ── AUDIO path ─────────────────────────────────────────────────
            kbps = calc_audio_bitrate(target_mb, dur)
            log.info(f'[{job_id}] AUDIO  kbps={kbps}  dur={dur:.1f}s')
            set_prog(job_id, 10,
                     f'Audio encode → {kbps} kbps '
                     f'(target {target_mb} MB from {orig_mb} MB)…')

            codec_map = {
                'mp3' : ['-c:a', 'libmp3lame', '-b:a', f'{kbps}k',
                          '-compression_level', '0'],
                'aac' : ['-c:a', 'aac',          '-b:a', f'{kbps}k'],
                'm4a' : ['-c:a', 'aac',           '-b:a', f'{kbps}k'],
                'ogg' : ['-c:a', 'libvorbis',     '-b:a', f'{kbps}k'],
                'opus': ['-c:a', 'libopus',       '-b:a', f'{kbps}k',
                          '-vbr', 'on'],
                'flac': ['-c:a', 'flac',
                          '-compression_level', '8'],
                'wav' : ['-c:a', 'pcm_s16le'],
            }
            codec_args = codec_map.get(
                out_fmt,
                ['-c:a', 'libmp3lame', '-b:a', f'{kbps}k'])

            cmd = (['ffmpeg', '-y',
                    '-threads', str(FF_THREADS),
                    '-i', src, '-vn']
                   + codec_args
                   + [tmp_out])
            base_pct = 15

        # ══════════════════════════════════════════════════════════════════
        else:
            # ── VIDEO path ─────────────────────────────────────────────────
            a_kbps    = 128
            v_kbps    = calc_video_bitrate(target_mb, dur, a_kbps)
            enc       = best_encoder()
            use_2pass = (enc == 'libx264')

            log.info(f'[{job_id}] VIDEO  enc={enc}  '
                     f'v={v_kbps}k  a={a_kbps}k  '
                     f'2pass={use_2pass}  dur={dur:.1f}s')
            set_prog(job_id, 8,
                     f'Video {v_kbps} kbps + audio {a_kbps} kbps '
                     f'(target {target_mb} MB)…')

            if use_2pass:
                # ── Pass 1 ────────────────────────────────────────────────
                # Use a file in our own UPLOAD_DIR — avoids permission issues
                tmp_pass = os.path.join(
                    UPLOAD_DIR, f'pass_{job_id}')

                cmd1 = [
                    'ffmpeg', '-y',
                    '-threads', str(FF_THREADS),
                    '-i', src,
                    '-c:v', 'libx264',
                    '-b:v', f'{v_kbps}k',
                    '-pass', '1',
                    '-passlogfile', tmp_pass,
                    '-preset', 'fast',
                    '-an',
                    '-f', 'null', NULL_DEV,
                ]
                set_prog(job_id, 10, 'Pass 1/2 — analysing…')
                log.debug(f'[{job_id}] pass1: {" ".join(cmd1)}')

                p1 = _popen(cmd1)
                for line in p1.stdout:
                    m = re.search(r'time=(\d+):(\d+):([\d.]+)', line)
                    if m and dur > 0:
                        el = (int(m.group(1)) * 3600
                              + int(m.group(2)) * 60
                              + float(m.group(3)))
                        ratio = min(el / dur, 1.0)
                        set_prog(job_id,
                                 10 + int(ratio * 30),
                                 f'Pass 1/2 — {int(ratio * 100)}%')

                try:
                    p1.wait(timeout=14400)
                except subprocess.TimeoutExpired:
                    p1.kill()
                    raise RuntimeError('Pass 1 timed out')

                if p1.returncode != 0:
                    raise RuntimeError(
                        f'FFmpeg pass-1 failed (rc={p1.returncode})')

                # ── Pass 2 ────────────────────────────────────────────────
                set_prog(job_id, 42, 'Pass 2/2 — encoding…')
                cmd = [
                    'ffmpeg', '-y',
                    '-threads', str(FF_THREADS),
                    '-i', src,
                    '-c:v', 'libx264',
                    '-b:v',     f'{v_kbps}k',
                    '-maxrate', f'{int(v_kbps * 1.4)}k',
                    '-bufsize', f'{v_kbps * 2}k',
                    '-pass', '2',
                    '-passlogfile', tmp_pass,
                    '-preset', 'fast',
                    '-c:a', 'aac', '-b:a', f'{a_kbps}k',
                    '-movflags', '+faststart',
                    tmp_out,
                ]
                base_pct = 42

            else:
                # ── Single-pass HW encoder ────────────────────────────────
                hw_map = {
                    'h264_nvenc': [
                        '-rc', 'vbr',
                        '-b:v', f'{v_kbps}k',
                        '-maxrate', f'{int(v_kbps * 1.5)}k',
                        '-preset', 'p4',
                    ],
                    'h264_videotoolbox': ['-b:v', f'{v_kbps}k'],
                    'h264_qsv' : ['-b:v', f'{v_kbps}k', '-preset', 'fast'],
                    'h264_vaapi': ['-b:v', f'{v_kbps}k'],
                }
                v_args = hw_map.get(enc, ['-b:v', f'{v_kbps}k'])
                cmd = ([
                    'ffmpeg', '-y',
                    '-threads', str(FF_THREADS),
                    '-i', src,
                    '-c:v', enc]
                    + v_args
                    + ['-c:a', 'aac', '-b:a', f'{a_kbps}k',
                       '-movflags', '+faststart',
                       tmp_out])
                base_pct = 15
                set_prog(job_id, 15,
                         f'Encoding ({enc}) @ {v_kbps} kbps…')

        # ── Run main encode ────────────────────────────────────────────────
        log.debug(f'[{job_id}] encode: {" ".join(cmd)}')
        proc = _popen(cmd)

        stderr_lines = []
        for line in proc.stdout:
            stderr_lines.append(line)
            m = re.search(r'time=(\d+):(\d+):([\d.]+)', line)
            if m and dur > 0:
                el    = (int(m.group(1)) * 3600
                         + int(m.group(2)) * 60
                         + float(m.group(3)))
                ratio = min(el / dur, 1.0)
                pct   = base_pct + int(ratio * (94 - base_pct))

                spd_m = re.search(r'speed=([\d.]+)x', line)
                spd   = float(spd_m.group(1)) if spd_m else 0
                eta_s = (f' · ETA {int((dur - el) / spd)}s'
                         if spd > 0.01 and el < dur else '')

                sz_m = re.search(r'size=\s*(\d+)kB', line)
                cur  = (f' · {round(int(sz_m.group(1)) / 1024, 1)} MB written'
                        if sz_m else '')

                set_prog(job_id, min(pct, 93),
                         f'Compressing… {int(ratio * 100)}%{cur}{eta_s}')

        try:
            proc.wait(timeout=14400)
        except subprocess.TimeoutExpired:
            proc.kill()
            raise RuntimeError('Encode timed out (4 h limit)')

        # ── Validate ───────────────────────────────────────────────────────
        if proc.returncode != 0:
            tail = ''.join(stderr_lines[-20:])
            log.error(f'[{job_id}] ffmpeg FAILED rc={proc.returncode}\n{tail}')
            raise RuntimeError(
                f'FFmpeg failed (rc={proc.returncode}). '
                f'Details: {tail[-300:]}')

        if not os.path.exists(tmp_out):
            raise RuntimeError('Output file was not created by FFmpeg.')

        out_size = os.path.getsize(tmp_out)
        if out_size < 512:
            raise RuntimeError(
                f'Output file too small ({out_size} B) — encode failed.')

        # ── Read into RAM ──────────────────────────────────────────────────
        set_prog(job_id, 96, 'Finalising…')
        with open(tmp_out, 'rb') as fh:
            data_bytes = fh.read()

        final_mb  = round(len(data_bytes) / 1048576, 2)
        reduction = round((1 - final_mb / orig_mb) * 100, 1) if orig_mb else 0

        log.info(f'[{job_id}] DONE  '
                 f'{orig_mb} MB → {final_mb} MB  '
                 f'({reduction}% smaller)')

        with result_lock:
            result_store[job_id] = {
                'data': data_bytes,
                'ext' : out_fmt,
                'ts'  : time.time(),
            }

        set_prog(job_id, 100,
                 f'✅ {orig_mb} MB → {final_mb} MB ({reduction}% smaller)',
                 'completed', {
                     'original_mb': orig_mb,
                     'final_mb'   : final_mb,
                     'target_mb'  : target_mb,
                     'reduction'  : reduction,
                     'ext'        : out_fmt,
                 })

    except Exception as ex:
        log.error(f'[{job_id}] COMPRESSION ERROR: {ex}', exc_info=True)
        set_prog(job_id, 0, f'❌ {ex}', 'error')

    finally:
        # ── Clean temp output ──────────────────────────────────────────────
        if tmp_out and os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except Exception as e:
                log.warning(f'Cannot remove tmp_out: {e}')

        # ── Clean 2-pass log files ─────────────────────────────────────────
        if tmp_pass:
            for sfx in ('', '.log', '-0.log',
                        '.log.mbtree', '-0.log.mbtree'):
                p = tmp_pass + sfx
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass


# ── Routes ─────────────────────────────────────────────────────────────────────
@app.route('/')
def index():
    return render_template('index.html', ffmpeg_ok=check_ffmpeg())


@app.route('/upload', methods=['POST'])
def upload():
    try:
        f = request.files.get('file')
        if not f or not f.filename:
            return jsonify({'error': 'No file provided'}), 400

        filename = f.filename.strip()
        if not allowed_file(filename):
            ext = filename.rsplit('.', 1)[-1] if '.' in filename else '?'
            return jsonify(
                {'error': f'File type ".{ext}" is not supported'}), 400

        uid   = uuid.uuid4().hex[:12]
        ext   = Path(filename).suffix.lower() or '.bin'
        fname = f'src_{uid}{ext}'
        fpath = os.path.join(UPLOAD_DIR, fname)

        log.info(f'Receiving upload: {filename}  uid={uid}')

        # ── Stream file to disk ────────────────────────────────────────────
        CHUNK   = 4 * 1024 * 1024   # 4 MB
        written = 0
        try:
            with open(fpath, 'wb') as out:
                stream = f.stream
                while True:
                    chunk = stream.read(CHUNK)
                    if not chunk:
                        break
                    out.write(chunk)
                    written += len(chunk)
        except Exception as e:
            try: os.remove(fpath)
            except Exception: pass
            log.error(f'Stream write failed: {e}', exc_info=True)
            return jsonify({'error': f'File write failed: {e}'}), 500

        log.info(f'Saved {written:,} bytes → {fpath}')

        if written < 512:
            try: os.remove(fpath)
            except Exception: pass
            return jsonify({'error': 'File too small or empty'}), 400

        # ── Probe ──────────────────────────────────────────────────────────
        info = get_media_info(fpath)
        if not info:
            try: os.remove(fpath)
            except Exception: pass
            return jsonify(
                {'error': 'Cannot read media info — '
                          'is the file a valid video/audio?'}), 400

        if info['duration'] <= 0:
            try: os.remove(fpath)
            except Exception: pass
            return jsonify(
                {'error': 'Cannot determine duration — '
                          'file may be corrupt or unsupported.'}), 400

        # ── Thumbnail ──────────────────────────────────────────────────────
        thumb      = None
        audio_only = is_audio_file(filename) or info['is_audio_only']
        if not audio_only:
            ts    = min(5.0, max(0.0, info['duration'] * 0.1))
            thumb = make_thumb_b64(fpath, ts)

        return jsonify({
            'file_id' : uid,
            'filename': filename,
            'info'    : info,
            'thumb'   : thumb,
            'is_audio': audio_only,
        })

    except Exception as ex:
        log.error(f'/upload unhandled: {ex}', exc_info=True)
        return jsonify({'error': f'Upload error: {ex}'}), 500


@app.route('/compress', methods=['POST'])
def compress():
    try:
        body = request.get_json(force=True, silent=True)
        if not body:
            return jsonify({'error': 'Invalid or empty JSON body'}), 400

        file_id  = str(body.get('file_id')  or '').strip()
        out_fmt  = str(body.get('output_format') or 'mp4').lower().strip()

        try:
            target_mb = float(body.get('target_mb', 0))
        except (TypeError, ValueError):
            return jsonify({'error': 'Invalid target_mb value'}), 400

        if not file_id:
            return jsonify({'error': 'No file_id provided'}), 400
        if target_mb < 0.05:
            return jsonify(
                {'error': 'Target too small (minimum 0.05 MB)'}), 400

        # Locate upload
        src = None
        try:
            for fn in os.listdir(UPLOAD_DIR):
                if fn.startswith(f'src_{file_id}'):
                    candidate = os.path.join(UPLOAD_DIR, fn)
                    if os.path.isfile(candidate):
                        src = candidate
                        break
        except Exception as e:
            log.error(f'listdir error: {e}')

        if not src:
            return jsonify(
                {'error': 'Upload not found — please re-upload'}), 404

        info = get_media_info(src)
        if not info:
            return jsonify({'error': 'Cannot read file info'}), 400

        if target_mb >= info['size_mb']:
            return jsonify({'error': (
                f'Target ({target_mb} MB) must be smaller than '
                f'original ({info["size_mb"]} MB)')}), 400

        allowed_fmts = {
            'mp4','mkv','avi','mov','webm','ts','flv',
            'mp3','aac','wav','ogg','opus','flac','m4a'
        }
        if out_fmt not in allowed_fmts:
            out_fmt = 'mp4'

        job_id = uuid.uuid4().hex[:8]
        set_prog(job_id, 0, 'Queued…', 'queued')

        log.info(f'Queuing job {job_id}  '
                 f'{info["size_mb"]} MB → {target_mb} MB  fmt={out_fmt}')

        threading.Thread(
            target=do_compress,
            args=(job_id, src, target_mb, out_fmt, info),
            daemon=True
        ).start()

        return jsonify({'job_id': job_id})

    except Exception as ex:
        log.error(f'/compress unhandled: {ex}', exc_info=True)
        return jsonify({'error': f'Server error: {ex}'}), 500


@app.route('/progress/<job_id>')
def progress(job_id):
    d = get_prog(job_id)
    if not d:
        return jsonify({'error': 'Job not found', 'status': 'unknown'}), 404
    return jsonify(d)


@app.route('/progress_stream/<job_id>')
def progress_stream(job_id):
    def gen():
        last  = None
        ticks = 0
        while ticks < 96000:          # max ~8 h
            d = get_prog(job_id)
            s = json.dumps(d)
            if s != last:
                yield f'data: {s}\n\n'
                last = s
                if d.get('status') in ('completed', 'error'):
                    break
            time.sleep(0.3)
            ticks += 1
        yield 'data: {"_done":true}\n\n'

    return Response(
        gen(),
        mimetype='text/event-stream',
        headers={
            'Cache-Control'    : 'no-cache',
            'X-Accel-Buffering': 'no',
            'Connection'       : 'keep-alive',
        }
    )


@app.route('/download/<job_id>')
def download_file(job_id):
    with result_lock:
        res = result_store.get(job_id)

    if not res:
        return jsonify(
            {'error': 'Result not found or expired (max 2 h)'}), 404

    raw  = request.args.get('filename', f'compressed.{res["ext"]}')
    safe = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', raw).strip('_') or 'compressed'
    ext  = res['ext']

    if not safe.lower().endswith('.' + ext):
        safe = re.sub(r'\.[^.]*$', '', safe) + '.' + ext
    if safe == ('.' + ext):
        safe = f'compressed.{ext}'

    mime = MIME_MAP.get(ext, 'application/octet-stream')
    buf  = io.BytesIO(res['data'])
    buf.seek(0)

    log.info(f'Download job={job_id}  '
             f'file={safe}  '
             f'size={len(res["data"]) / 1048576:.2f} MB')

    return send_file(buf, mimetype=mime,
                     as_attachment=True,
                     download_name=safe)


@app.route('/delete_upload/<file_id>', methods=['POST'])
def delete_upload(file_id):
    if not re.match(r'^[a-f0-9]{12}$', file_id):
        return jsonify({'error': 'Invalid file_id'}), 400
    removed = 0
    try:
        for fn in os.listdir(UPLOAD_DIR):
            if fn.startswith(f'src_{file_id}'):
                try:
                    os.remove(os.path.join(UPLOAD_DIR, fn))
                    removed += 1
                except Exception:
                    pass
    except Exception:
        pass
    with result_lock:
        if file_id in result_store:
            del result_store[file_id]
            removed += 1
    return jsonify({'removed': removed})


# ── Error handlers ─────────────────────────────────────────────────────────────
@app.errorhandler(413)
def too_large(_):
    return jsonify({'error': 'File too large (max 10 GB)'}), 413

@app.errorhandler(500)
def server_error(e):
    log.error(f'Unhandled 500: {e}')
    return jsonify({'error': f'Internal server error: {e}'}), 500

@app.errorhandler(404)
def not_found(_):
    return jsonify({'error': 'Not found'}), 404


# ── Entry point ────────────────────────────────────────────────────────────────
if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    ff   = check_ffmpeg()
    fp   = check_ffprobe()
    enc  = best_encoder()

    print('\n' + '=' * 54)
    print('  🗜️  Media Compressor  (Windows build)')
    print('=' * 54)
    print(f'  URL      : http://localhost:{port}')
    print(f'  FFmpeg   : {"✅ ready" if ff  else "❌ NOT FOUND"}')
    print(f'  FFprobe  : {"✅ ready" if fp  else "❌ NOT FOUND"}')
    print(f'  Encoder  : {enc}')
    print(f'  HW accel : '
          f'{[k for k,v in HW.items() if v] or ["none — using software"]}')
    print(f'  CPU cores: {CPU_CORES}  (FFmpeg threads: {FF_THREADS})')
    print(f'  Temp dir : {UPLOAD_DIR}')
    print('=' * 54 + '\n')

    app.run(
        debug=True,          # show full tracebacks in terminal
        host='0.0.0.0',
        port=port,
        threaded=True,
        use_reloader=False,  # prevent double-startup on Windows
    )