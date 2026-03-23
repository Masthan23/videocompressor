# app.py  ←  pure Streamlit, no Flask, no disk storage
from __future__ import annotations

import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
from pathlib import Path
from typing import Any, Dict, Optional
import base64

import streamlit as st

st.set_page_config(
    page_title="🗜️ Media Compressor",
    page_icon="🗜️",
    layout="centered",
)

st.markdown("""
<style>
  .stApp { background: #07090f; color: #dde1f5; }
  .block-container { max-width: 780px; padding-top: 1.5rem; }
  #MainMenu, footer, header { visibility: hidden; }

  .stButton > button {
    background: linear-gradient(135deg, #6c5fff, #a09aff) !important;
    color: white !important; border: none !important;
    border-radius: 10px !important; font-weight: 700 !important;
    width: 100% !important;
  }
  .stButton > button:hover {
    background: linear-gradient(135deg, #5a4eee, #8f89ff) !important;
  }
  .stButton > button:disabled {
    opacity: 0.4 !important; cursor: not-allowed !important;
  }
  .stDownloadButton > button {
    background: linear-gradient(135deg, #2ecc71, #27ae60) !important;
    color: #050f0a !important; border: none !important;
    border-radius: 10px !important; font-weight: 800 !important;
    width: 100% !important; font-size: 1rem !important;
    padding: 0.75rem !important;
  }
  .stDownloadButton > button:hover {
    transform: translateY(-2px) !important;
    box-shadow: 0 6px 20px rgba(46,204,113,.35) !important;
  }
  .stProgress > div > div > div > div {
    background: linear-gradient(90deg, #6c5fff, #a09aff) !important;
  }
  [data-testid="metric-container"] {
    background: #0d0f1c !important;
    border: 1px solid #1e2238 !important;
    border-radius: 10px !important;
    padding: 12px !important;
  }
  [data-testid="metric-container"] label {
    color: #565a7a !important;
    font-size: 0.72rem !important;
    text-transform: uppercase !important;
    letter-spacing: 0.5px !important;
  }
  [data-testid="metric-container"] [data-testid="stMetricValue"] {
    color: #dde1f5 !important;
    font-size: 1.1rem !important;
    font-weight: 800 !important;
  }
  [data-testid="stFileUploader"] {
    background: #0d0f1c !important;
    border: 2px dashed #1e2238 !important;
    border-radius: 14px !important;
    padding: 1rem !important;
  }
  .stRadio > div { gap: 8px !important; }
  .stRadio > div > label {
    background: #0d0f1c !important;
    border: 1px solid #1e2238 !important;
    border-radius: 8px !important;
    padding: 4px 10px !important;
    cursor: pointer !important;
  }
  .stAlert { border-radius: 10px !important; }
  hr { border-color: #1e2238 !important; }

  .result-box {
    background: linear-gradient(135deg,
      rgba(46,204,113,.08), rgba(108,95,255,.06));
    border: 1px solid rgba(46,204,113,.25);
    border-radius: 14px; padding: 20px 22px; margin: 12px 0;
  }
  .result-title {
    font-size: 1.3rem; font-weight: 900;
    color: #2ecc71; margin-bottom: 4px;
  }
  .result-sub { color: #565a7a; font-size: .85rem; }
  .size-badge {
    display: inline-block;
    background: rgba(46,204,113,.12);
    border: 1px solid rgba(46,204,113,.28);
    border-radius: 20px; padding: 4px 14px;
    font-size: .88rem; font-weight: 800;
    color: #2ecc71; margin-top: 8px;
  }
  .warn-banner {
    background: rgba(231,76,60,.07);
    border: 1px solid rgba(231,76,60,.25);
    border-radius: 10px; padding: 10px 14px;
    color: #e74c3c; font-size: .85rem; margin: 8px 0;
  }
  .info-card {
    background: #0d0f1c;
    border: 1px solid #1e2238;
    border-radius: 12px;
    padding: 16px 18px;
    margin: 8px 0;
  }
  .step-header {
    font-size: 1.05rem; font-weight: 800;
    color: #dde1f5; margin-bottom: 4px;
  }
  .muted { color: #565a7a; font-size: .82rem; }
</style>
""", unsafe_allow_html=True)


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
        r = subprocess.run(
            [_ffmpeg(), '-version'],
            capture_output=True, timeout=10,
        )
        return r.returncode == 0
    except Exception:
        return False


# ══════════════════════════════════════════════════════════════════════════════
# Media probe
# ══════════════════════════════════════════════════════════════════════════════
def get_media_info(path: str) -> Optional[Dict[str, Any]]:
    try:
        r = subprocess.run(
            [_ffprobe(), '-v', 'quiet',
             '-print_format', 'json',
             '-show_streams', '-show_format', path],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            return None

        data = json.loads(r.stdout)
        sz = os.path.getsize(path)
        info: Dict[str, Any] = {
            'duration': 0, 'width': 0, 'height': 0, 'fps': 0,
            'size': sz,
            'size_mb': round(sz / 1_048_576, 2),
            'video_codec': '', 'audio_codec': '',
            'bitrate': 0, 'has_audio': False,
            'is_audio_only': False,
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
                info['width']       = int(s.get('width',  0) or 0)
                info['height']      = int(s.get('height', 0) or 0)
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
                info['has_audio']   = True
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
# Helpers
# ══════════════════════════════════════════════════════════════════════════════
def fmt_duration(s: float) -> str:
    s = max(0, int(s))
    h, rem = divmod(s, 3600)
    m, sc  = divmod(rem, 60)
    if h:
        return f"{h}h {m}m {sc}s"
    if m:
        return f"{m}m {sc}s"
    return f"{sc}s"


def fmt_size(mb: float) -> str:
    if mb >= 1024:
        return f"{mb / 1024:.2f} GB"
    return f"{mb:.1f} MB"


def calc_video_bitrate(target_mb: float, dur: float,
                       audio_kbps: int = 128) -> int:
    if dur <= 0:
        return 500
    total_bits = target_mb * 8 * 1_048_576
    audio_bits = audio_kbps * 1024 * dur
    video_bits = max(0, total_bits - audio_bits)
    return max(80, int(video_bits / dur / 1024 * 0.97))


def calc_audio_bitrate(target_mb: float, dur: float) -> int:
    if dur <= 0:
        return 96
    total_bits = target_mb * 8 * 1_048_576
    kbps = int(total_bits / dur / 1024 * 0.97)
    return max(32, min(320, kbps))


def mime_for(ext: str) -> str:
    return {
        'mp4': 'video/mp4',        'mkv': 'video/x-matroska',
        'avi': 'video/x-msvideo',  'mov': 'video/quicktime',
        'webm': 'video/webm',      'mp3': 'audio/mpeg',
        'aac': 'audio/aac',        'wav': 'audio/wav',
        'ogg': 'audio/ogg',        'opus': 'audio/opus',
        'flac': 'audio/flac',      'm4a': 'audio/mp4',
    }.get(ext, 'application/octet-stream')


# ══════════════════════════════════════════════════════════════════════════════
# Save uploaded file to temp (FFmpeg needs a real file path)
# ══════════════════════════════════════════════════════════════════════════════
def save_upload(uploaded_file) -> Optional[str]:
    try:
        suffix = Path(uploaded_file.name).suffix.lower() or '.bin'
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        os.close(tmp_fd)

        uploaded_file.seek(0)
        CHUNK = 16 * 1024 * 1024   # 16 MB chunks
        with open(tmp_path, 'wb') as f:
            while True:
                chunk = uploaded_file.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)

        if os.path.getsize(tmp_path) < 512:
            os.remove(tmp_path)
            return None
        return tmp_path
    except Exception as e:
        st.error(f"Failed to save upload: {e}")
        return None


# ══════════════════════════════════════════════════════════════════════════════
# Compression — result goes straight into io.BytesIO, temp file deleted after
# ══════════════════════════════════════════════════════════════════════════════
def compress_file(
    src: str,
    target_mb: float,
    out_fmt: str,
    info: dict,
    progress_bar,
    status_text,
) -> Optional[bytes]:
    """
    Compress src → tmp_out, read result into BytesIO, delete tmp_out.
    Nothing is stored permanently on disk.
    """
    tmp_out:  Optional[str] = None
    tmp_pass: Optional[str] = None
    null_dev = 'NUL' if sys.platform == 'win32' else '/dev/null'

    try:
        dur     = float(info['duration'])
        orig_mb = float(info['size_mb'])

        is_audio_out = out_fmt in {
            'mp3', 'aac', 'wav', 'ogg', 'opus', 'flac', 'm4a'
        }
        is_audio_src = bool(info.get('is_audio_only'))

        if dur <= 0:
            st.error("Cannot determine duration.")
            return None

        # temp output — FFmpeg writes here, we read it, then delete it
        tmp_fd, tmp_out = tempfile.mkstemp(suffix='.' + out_fmt)
        os.close(tmp_fd)
        os.remove(tmp_out)   # let ffmpeg create it fresh

        # ── AUDIO ──────────────────────────────────────────────────────────
        if is_audio_out or is_audio_src:
            kbps = calc_audio_bitrate(target_mb, dur)
            status_text.markdown(
                f"🎵 **Encoding audio** → **{kbps} kbps** "
                f"as {out_fmt.upper()}")
            progress_bar.progress(0.05)

            codec_map: Dict[str, list] = {
                'mp3':  ['-c:a', 'libmp3lame', '-b:a', f'{kbps}k',
                         '-compression_level', '0'],
                'aac':  ['-c:a', 'aac',         '-b:a', f'{kbps}k'],
                'm4a':  ['-c:a', 'aac',         '-b:a', f'{kbps}k'],
                'ogg':  ['-c:a', 'libvorbis',   '-b:a', f'{kbps}k'],
                'opus': ['-c:a', 'libopus',     '-b:a', f'{kbps}k',
                         '-vbr', 'on'],
                'flac': ['-c:a', 'flac', '-compression_level', '8'],
                'wav':  ['-c:a', 'pcm_s16le'],
            }
            codec_args = codec_map.get(
                out_fmt,
                ['-c:a', 'libmp3lame', '-b:a', f'{kbps}k'],
            )
            cmd = (
                [_ffmpeg(), '-y', '-i', src, '-vn']
                + codec_args + [tmp_out]
            )
            base_pct = 0.08

            proc = subprocess.Popen(
                cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            last_ui = time.time()
            for line in proc.stdout:  # type: ignore
                m = re.search(r'time=(\d+):(\d+):([\d.]+)', line)
                if m and dur > 0:
                    el    = (int(m.group(1)) * 3600
                             + int(m.group(2)) * 60
                             + float(m.group(3)))
                    ratio = min(el / dur, 1.0)
                    pct   = base_pct + ratio * (0.93 - base_pct)
                    now   = time.time()
                    if now - last_ui >= 0.4:
                        last_ui = now
                        progress_bar.progress(min(pct, 0.93))
                        spd_m = re.search(r'speed=([\d.]+)x', line)
                        spd   = float(spd_m.group(1)) if spd_m else 0.0
                        eta   = (f" · ETA ~{int((dur-el)/spd)}s"
                                 if spd > 0.01 and el < dur else "")
                        status_text.markdown(
                            f"🎵 **Encoding…** {int(ratio*100)}%{eta}")
            proc.wait(timeout=86400)
            if proc.returncode != 0:
                st.error(f"FFmpeg failed (rc={proc.returncode})")
                return None

        # ── VIDEO 2-PASS ───────────────────────────────────────────────────
        else:
            a_kbps  = 128
            v_kbps  = calc_video_bitrate(target_mb, dur, a_kbps)
            tmp_pass = tempfile.mktemp(prefix='ffpass_')

            # ── Pass 1 ────────────────────────────────────────────────────
            status_text.markdown("📊 **Pass 1 / 2** — analysing…")
            progress_bar.progress(0.03)
            cmd1 = [
                _ffmpeg(), '-y', '-i', src,
                '-c:v', 'libx264', '-b:v', f'{v_kbps}k',
                '-pass', '1', '-passlogfile', tmp_pass,
                '-preset', 'fast', '-an',
                '-f', 'null', null_dev,
            ]
            p1 = subprocess.Popen(
                cmd1,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            last_ui = time.time()
            for line in p1.stdout:  # type: ignore
                m = re.search(r'time=(\d+):(\d+):([\d.]+)', line)
                if m and dur > 0:
                    el    = (int(m.group(1)) * 3600
                             + int(m.group(2)) * 60
                             + float(m.group(3)))
                    ratio = min(el / dur, 1.0)
                    now   = time.time()
                    if now - last_ui >= 0.4:
                        last_ui = now
                        progress_bar.progress(0.03 + ratio * 0.35)
                        status_text.markdown(
                            f"📊 **Pass 1 / 2** — {int(ratio*100)}%")
            p1.wait(timeout=86400)
            if p1.returncode != 0:
                st.error(f"Pass 1 failed (rc={p1.returncode})")
                return None

            # ── Pass 2 ────────────────────────────────────────────────────
            status_text.markdown(
                f"🎬 **Pass 2 / 2** — "
                f"{v_kbps} kbps video + {a_kbps} kbps audio…")
            progress_bar.progress(0.40)
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
                cmd2,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, bufsize=1,
            )
            last_ui = time.time()
            for line in proc.stdout:  # type: ignore
                m = re.search(r'time=(\d+):(\d+):([\d.]+)', line)
                if m and dur > 0:
                    el    = (int(m.group(1)) * 3600
                             + int(m.group(2)) * 60
                             + float(m.group(3)))
                    ratio = min(el / dur, 1.0)
                    pct   = 0.40 + ratio * 0.54
                    now   = time.time()
                    if now - last_ui >= 0.4:
                        last_ui = now
                        progress_bar.progress(min(pct, 0.94))
                        spd_m = re.search(r'speed=([\d.]+)x', line)
                        spd   = float(spd_m.group(1)) if spd_m else 0.0
                        sz_m  = re.search(r'size=\s*(\d+)kB', line)
                        cur   = (f" · {round(int(sz_m.group(1))/1024,1)} MB"
                                 if sz_m else "")
                        eta   = (f" · ETA ~{int((dur-el)/spd)}s"
                                 if spd > 0.01 and el < dur else "")
                        status_text.markdown(
                            f"🎬 **Pass 2 / 2** — "
                            f"{int(ratio*100)}%{cur}{eta}")
            proc.wait(timeout=86400)
            if proc.returncode != 0:
                st.error(f"Pass 2 failed (rc={proc.returncode})")
                return None

        # ── Read result into memory, then delete the temp file ─────────────
        if not os.path.exists(tmp_out):
            st.error("Output file was not created.")
            return None
        out_size = os.path.getsize(tmp_out)
        if out_size < 512:
            st.error(f"Output too small ({out_size} bytes).")
            return None

        progress_bar.progress(0.97)
        status_text.markdown("📦 **Reading result…**")

        buf = io.BytesIO()
        with open(tmp_out, 'rb') as fh:
            while True:
                piece = fh.read(16 * 1024 * 1024)
                if not piece:
                    break
                buf.write(piece)

        progress_bar.progress(1.0)
        status_text.markdown("✅ **Done!**")
        return buf.getvalue()

    except subprocess.TimeoutExpired:
        st.error("Compression timed out.")
        return None
    except Exception as ex:
        st.error(f"Compression error: {ex}")
        return None

    finally:
        # ── Always wipe temp files immediately ─────────────────────────────
        if tmp_out and os.path.exists(tmp_out):
            try:
                os.remove(tmp_out)
            except Exception:
                pass
        if tmp_pass:
            for sfx in ('', '.log', '-0.log',
                        '.log.mbtree', '-0.log.mbtree'):
                p = tmp_pass + sfx
                if os.path.exists(p):
                    try:
                        os.remove(p)
                    except Exception:
                        pass


# ══════════════════════════════════════════════════════════════════════════════
# File info chips
# ══════════════════════════════════════════════════════════════════════════════
def show_file_info(info: dict, audio_only: bool) -> None:
    dur_str = fmt_duration(info['duration'])
    if audio_only:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("⏱ Duration", dur_str)
        c2.metric("💾 Size",    fmt_size(info['size_mb']))
        c3.metric("🎙 Codec",   info['audio_codec'].upper() or "—")
        c4.metric("📡 Bitrate",
                  f"{info['bitrate']//1000} kbps" if info['bitrate'] else "—")
    else:
        c1, c2, c3, c4, c5 = st.columns(5)
        res = (f"{info['width']}×{info['height']}"
               if info['width'] and info['height'] else "—")
        c1.metric("⏱ Duration",   dur_str)
        c2.metric("💾 Size",       fmt_size(info['size_mb']))
        c3.metric("📐 Resolution", res)
        c4.metric("🎞 FPS",
                  str(info['fps']) if info['fps'] else "—")
        c5.metric("🎬 Codec",
                  info['video_codec'].upper() or "—")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN
# ══════════════════════════════════════════════════════════════════════════════
def main() -> None:
    # ── FFmpeg check ──────────────────────────────────────────────────────────
    ff_ok = check_ffmpeg()

    col_title, col_badge = st.columns([5, 1])
    with col_title:
        st.markdown("# 🗜️ Media Compressor")
        st.caption(
            "Compress MP4 · MP3 · any video or audio "
            "to your exact target size  ·  up to **10 GB**")
    with col_badge:
        st.markdown("<div style='height:18px'></div>",
                    unsafe_allow_html=True)
        if ff_ok:
            st.success("✅ FFmpeg")
        else:
            st.error("❌ FFmpeg missing")
            st.info(
                "Add `ffmpeg` to **packages.txt** in your repo root, "
                "then reboot the app.")
            st.stop()

    st.divider()

    # ── Step 1 · Upload ───────────────────────────────────────────────────────
    st.markdown("### 📂 Step 1 — Upload Your File")
    st.caption(
        "Supported: MP4, MKV, AVI, MOV, WebM, MP3, WAV, AAC, "
        "OGG, OPUS, FLAC, M4A  ·  Max **10 GB**")

    uploaded = st.file_uploader(
        "upload",
        type=[
            'mp4','mkv','avi','mov','wmv','flv','webm',
            'm4v','mpeg','mpg','3gp','ts','mts','ogv',
            'mp3','wav','aac','ogg','opus','flac','wma','m4a',
        ],
        label_visibility='collapsed',
    )

    if not uploaded:
        st.markdown("""
        <div style="background:#0d0f1c;border:2px dashed #1e2238;
          border-radius:14px;padding:40px;text-align:center;
          color:#565a7a;margin-top:8px">
          <div style="font-size:2.5rem;margin-bottom:8px">🎬</div>
          <div style="font-size:1rem;font-weight:700;
                      color:#dde1f5;margin-bottom:4px">
            Drop your video or audio file here
          </div>
          <div style="font-size:.82rem">
            or click <b>Browse files</b> above · up to 10 GB
          </div>
        </div>
        """, unsafe_allow_html=True)
        st.stop()

    # ── Validate size ─────────────────────────────────────────────────────────
    MAX_BYTES = 10 * 1024 * 1024 * 1024
    if uploaded.size > MAX_BYTES:
        st.error(
            f"File too large: **{fmt_size(uploaded.size/1_048_576)}**  \n"
            f"Maximum is **10 GB**.")
        st.stop()

    # ── Save to temp (only for FFmpeg; deleted after) ─────────────────────────
    file_key = f"{uploaded.name}_{uploaded.size}"

    if st.session_state.get('file_key') != file_key:
        # New file — clear old session data and remove old temp
        old_tmp = st.session_state.get('tmp_src')
        st.session_state.clear()
        if old_tmp and os.path.exists(str(old_tmp)):
            try:
                os.remove(old_tmp)
            except Exception:
                pass

        with st.spinner(
            f"Saving **{uploaded.name}** "
            f"({fmt_size(uploaded.size/1_048_576)})…"
        ):
            tmp_src = save_upload(uploaded)

        if not tmp_src:
            st.error("Failed to save uploaded file.")
            st.stop()

        st.session_state['tmp_src']  = tmp_src
        st.session_state['file_key'] = file_key

    tmp_src = st.session_state.get('tmp_src')
    if not tmp_src or not os.path.exists(str(tmp_src)):
        st.error("Temp file missing — please re-upload.")
        st.session_state.clear()
        st.stop()

    # ── Probe media info (cached in session) ──────────────────────────────────
    if 'info' not in st.session_state:
        with st.spinner("Reading file info…"):
            info = get_media_info(str(tmp_src))
        if not info:
            st.error("Cannot read media info. Is this a valid media file?")
            st.stop()
        if info['duration'] <= 0:
            st.error("Cannot determine file duration.")
            st.stop()
        st.session_state['info'] = info
    else:
        info = st.session_state['info']

    audio_only = (
        Path(uploaded.name).suffix.lower().lstrip('.')
        in {'mp3','wav','aac','ogg','opus','flac','wma','m4a'}
        or info['is_audio_only']
    )

    # ── File information display ──────────────────────────────────────────────
    st.markdown("### 📊 File Information")

    if not audio_only:
        with st.expander("▶️ Preview video", expanded=False):
            st.video(uploaded)

        c_thumb, c_info = st.columns([1, 2])
        with c_thumb:
            st.markdown(
                f"<div class='info-card' style='text-align:center;"
                f"font-size:3rem'>🎬</div>",
                unsafe_allow_html=True)
        with c_info:
            st.markdown(
                f"<div class='info-card'>"
                f"<div style='font-weight:800;font-size:.95rem;"
                f"word-break:break-all'>{uploaded.name}</div>"
                f"<div class='muted'>"
                f"{info['video_codec'].upper() or 'Video'}  ·  "
                f"{info['width']}×{info['height']}  ·  "
                f"{info['fps']} fps  ·  "
                f"{fmt_duration(info['duration'])}"
                f"</div></div>",
                unsafe_allow_html=True)
    else:
        with st.expander("🔊 Preview audio", expanded=False):
            st.audio(uploaded)
        st.markdown(
            f"<div class='info-card'>"
            f"<div style='font-weight:800'>🎵 {uploaded.name}</div>"
            f"<div class='muted'>"
            f"{info['audio_codec'].upper() or 'Audio'}  ·  "
            f"{fmt_duration(info['duration'])}"
            f"</div></div>",
            unsafe_allow_html=True)

    show_file_info(info, audio_only)

    # ── Step 2 · Settings ─────────────────────────────────────────────────────
    st.divider()
    st.markdown("### 🗜️ Step 2 — Compression Settings")

    orig_mb  = info['size_mb']
    max_mb   = max(0.5, round(orig_mb * 0.98, 1))
    def_mb   = max(0.1, round(orig_mb * 0.5,  1))
    slider_v = min(def_mb, max_mb)

    target_mb = st.slider(
        f"🎯 Target size  *(original: **{fmt_size(orig_mb)}**)*",
        min_value=0.1,
        max_value=float(max_mb),
        value=float(slider_v),
        step=0.1,
        format="%.1f MB",
    )

    # Quick presets
    st.markdown(
        "<div class='muted' style='text-transform:uppercase;"
        "letter-spacing:.5px;margin-bottom:4px'>"
        "Quick presets</div>",
        unsafe_allow_html=True)

    presets      = [1, 5, 10, 25, 50, 100, 250, 500, 1000, 2000, 5000]
    preset_cols  = st.columns(len(presets))
    for i, p in enumerate(presets):
        disabled = float(p) >= orig_mb
        label    = f"{p} MB" if p < 1024 else f"{p//1024} GB"
        if preset_cols[i].button(
            label,
            key=f"pre_{p}",
            disabled=disabled,
            use_container_width=True,
        ):
            target_mb = float(p)
            st.rerun()

    # Comparison metrics
    reduction = (round((1 - target_mb / orig_mb) * 100, 1)
                 if orig_mb else 0)
    saved_mb  = round(orig_mb - target_mb, 1)

    if target_mb >= orig_mb:
        st.markdown(
            f'<div class="warn-banner">⚠️ Target '
            f'({fmt_size(target_mb)}) must be smaller than '
            f'original ({fmt_size(orig_mb)})</div>',
            unsafe_allow_html=True)
    else:
        m1, m2, m3 = st.columns(3)
        m1.metric("📦 Original", fmt_size(orig_mb))
        m2.metric("🎯 Target",   fmt_size(target_mb),
                  delta=f"-{fmt_size(saved_mb)}")
        m3.metric("📉 Reduction", f"{reduction}%")

    st.markdown("<div style='height:8px'></div>",
                unsafe_allow_html=True)

    # Output format
    st.markdown("**🎯 Output Format**")
    out_fmt: str
    if audio_only:
        out_fmt = st.radio(
            "Audio format",
            ['mp3', 'aac', 'ogg', 'opus', 'flac', 'wav', 'm4a'],
            horizontal=True,
            label_visibility='collapsed',
        )
        st.caption(f"Output: **{out_fmt.upper()}** (audio only)")
    else:
        fmt_type = st.radio(
            "Format type",
            ["📹 Video", "🎵 Audio (extract audio)"],
            horizontal=True,
            label_visibility='collapsed',
        )
        if fmt_type == "📹 Video":
            out_fmt = st.radio(
                "Video format",
                ['mp4', 'mkv', 'avi', 'mov', 'webm'],
                horizontal=True,
                label_visibility='collapsed',
            )
        else:
            out_fmt = st.radio(
                "Audio format",
                ['mp3', 'aac', 'ogg', 'opus', 'flac', 'wav', 'm4a'],
                horizontal=True,
                label_visibility='collapsed',
            )
        st.caption(f"Output: **{out_fmt.upper()}**")

    # ── Step 3 · Compress & Download ─────────────────────────────────────────
    st.divider()
    st.markdown("### 🚀 Step 3 — Compress & Download")

    # ── Show download button if result already in session ─────────────────────
    if st.session_state.get('result'):
        res      = st.session_state['result']
        # result_bytes stored directly — no disk file
        raw      = res['data']
        final_mb = round(len(raw) / 1_048_576, 2)
        red      = round((1 - final_mb / orig_mb) * 100, 1)
        saved    = round(orig_mb - final_mb, 1)
        base_name = Path(uploaded.name).stem + '_compressed'

        st.markdown(
            f"""
            <div class="result-box">
              <div class="result-title">✅ Compression Complete!</div>
              <div class="result-sub">
                {fmt_size(orig_mb)} → {fmt_size(final_mb)}
              </div>
              <div class="size-badge">
                🎉 {red}% smaller &nbsp;·&nbsp; saved {fmt_size(saved)}
              </div>
            </div>
            """,
            unsafe_allow_html=True)

        # ── Download button — streams directly, no temp file ─────────────
        st.download_button(
            label=(
                f"⬇️  Download  {base_name}.{res['ext']}"
                f"  ({fmt_size(final_mb)})"
            ),
            data=raw,
            file_name=f"{base_name}.{res['ext']}",
            mime=mime_for(res['ext']),
            use_container_width=True,
            type="primary",
        )

        col_a, col_b = st.columns(2)
        with col_a:
            if st.button("🔄 Compress Again",
                         use_container_width=True):
                st.session_state.pop('result', None)
                st.rerun()
        with col_b:
            if st.button("📂 Upload New File",
                         use_container_width=True):
                old = st.session_state.get('tmp_src')
                if old and os.path.exists(str(old)):
                    try:
                        os.remove(old)
                    except Exception:
                        pass
                st.session_state.clear()
                st.rerun()

    else:
        # ── Compress button ───────────────────────────────────────────────
        btn_disabled = target_mb >= orig_mb
        btn_label = (
            f"🗜️  Compress  →  {fmt_size(target_mb)}"
            f"  as {out_fmt.upper()}"
            if not btn_disabled
            else "⚠️ Target must be smaller than original"
        )

        if st.button(
            btn_label,
            type="primary",
            disabled=btn_disabled,
            use_container_width=True,
        ):
            st.markdown("---")
            progress_bar = st.progress(0.0)
            status_text  = st.empty()
            status_text.markdown("🔄 **Starting compression…**")
            t_start = time.time()

            result_bytes = compress_file(
                src=str(tmp_src),
                target_mb=target_mb,
                out_fmt=str(out_fmt),
                info=info,
                progress_bar=progress_bar,
                status_text=status_text,
            )

            elapsed = time.time() - t_start

            if result_bytes:
                final_mb = round(len(result_bytes) / 1_048_576, 2)
                status_text.markdown(
                    f"✅ **Done in {elapsed:.0f}s** — "
                    f"{fmt_size(orig_mb)} → {fmt_size(final_mb)}")

                # Store result bytes in session — NO disk file
                st.session_state['result'] = {
                    'data': result_bytes,   # bytes in RAM
                    'ext':  str(out_fmt),
                }
                st.rerun()
            else:
                status_text.markdown(
                    "❌ **Compression failed** — see error above")


if __name__ == '__main__':
    main()
