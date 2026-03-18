# app.py  ── Streamlit Cloud version  (1 GB upload support)
import streamlit as st
import subprocess, os, uuid, json, tempfile, base64, time, re, sys, io
from pathlib import Path

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="🗜️ Media Compressor",
    page_icon="🗜️",
    layout="centered",
)

# ── Custom CSS ─────────────────────────────────────────────────────────────────
st.markdown("""
<style>
  .stApp { background: #07090f; color: #dde1f5; }
  .block-container { max-width: 780px; padding-top: 1.5rem; }
  #MainMenu, footer, header { visibility: hidden; }

  /* Primary buttons */
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

  /* Download button */
  .stDownloadButton > button {
    background: linear-gradient(135deg, #2ecc71, #27ae60) !important;
    color: #050f0a !important; border: none !important;
    border-radius: 10px !important; font-weight: 800 !important;
    width: 100% !important; font-size: 1rem !important;
    padding: 0.75rem !important;
  }
  .stDownloadButton > button:hover {
    background: linear-gradient(135deg, #27ae60, #1e8449) !important;
    transform: translateY(-2px);
  }

  /* Progress bar */
  .stProgress > div > div > div > div {
    background: linear-gradient(90deg, #6c5fff, #a09aff) !important;
  }

  /* Metric cards */
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

  /* File uploader */
  [data-testid="stFileUploader"] {
    background: #0d0f1c !important;
    border: 2px dashed #1e2238 !important;
    border-radius: 14px !important;
    padding: 1rem !important;
  }
  [data-testid="stFileUploader"]:hover {
    border-color: #6c5fff !important;
  }

  /* Radio buttons */
  .stRadio > div { gap: 8px !important; }
  .stRadio > div > label {
    background: #0d0f1c !important;
    border: 1px solid #1e2238 !important;
    border-radius: 8px !important;
    padding: 4px 10px !important;
    cursor: pointer !important;
  }

  /* Slider */
  .stSlider > div > div > div > div {
    background: #6c5fff !important;
  }

  /* Info / success / error boxes */
  .stAlert { border-radius: 10px !important; }

  /* Divider */
  hr { border-color: #1e2238 !important; }

  /* Success result box */
  .result-box {
    background: linear-gradient(135deg,
      rgba(46,204,113,.08), rgba(108,95,255,.06));
    border: 1px solid rgba(46,204,113,.25);
    border-radius: 14px;
    padding: 20px 22px;
    margin: 12px 0;
  }
  .result-title {
    font-size: 1.3rem; font-weight: 900;
    color: #2ecc71; margin-bottom: 4px;
  }
  .result-sub { color: #565a7a; font-size: .85rem; }

  /* Size badge */
  .size-badge {
    display: inline-block;
    background: rgba(46,204,113,.12);
    border: 1px solid rgba(46,204,113,.28);
    border-radius: 20px;
    padding: 4px 14px;
    font-size: .88rem;
    font-weight: 800;
    color: #2ecc71;
    margin-top: 8px;
  }

  /* Warning */
  .warn-banner {
    background: rgba(231,76,60,.07);
    border: 1px solid rgba(231,76,60,.25);
    border-radius: 10px;
    padding: 10px 14px;
    color: #e74c3c;
    font-size: .85rem;
    margin: 8px 0;
  }
</style>
""", unsafe_allow_html=True)


# ── FFmpeg helpers ─────────────────────────────────────────────────────────────
@st.cache_resource
def check_ffmpeg() -> bool:
    try:
        r = subprocess.run(
            ['ffmpeg', '-version'],
            capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False

@st.cache_resource
def check_ffprobe() -> bool:
    try:
        r = subprocess.run(
            ['ffprobe', '-version'],
            capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


# ── Media probe ────────────────────────────────────────────────────────────────
def get_media_info(path: str) -> dict | None:
    try:
        r = subprocess.run(
            ['ffprobe', '-v', 'quiet',
             '-print_format', 'json',
             '-show_streams', '-show_format', path],
            capture_output=True, text=True, timeout=120)
        if r.returncode != 0:
            return None
        data = json.loads(r.stdout)
        sz   = os.path.getsize(path)
        info = {
            'duration': 0, 'width': 0, 'height': 0, 'fps': 0,
            'size': sz,
            'size_mb': round(sz / 1048576, 2),
            'video_codec': '', 'audio_codec': '',
            'bitrate': 0, 'has_audio': False,
            'is_audio_only': False,
            'sample_rate': 0, 'channels': 0,
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
                    n, d = s.get('r_frame_rate', '0/1').split('/')
                    info['fps'] = round(int(n) / int(d), 2) if int(d) else 0
                except Exception:
                    pass
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
        return info

    except Exception:
        return None


# ── Thumbnail ──────────────────────────────────────────────────────────────────
def make_thumb_b64(path: str, ts: float = 5.0) -> str | None:
    try:
        ts = max(0.0, min(float(ts), 30.0))
        cmd = [
            'ffmpeg', '-y', '-ss', f'{ts:.2f}', '-i', path,
            '-vframes', '1', '-q:v', '5', '-vf', 'scale=480:-2',
            '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1',
        ]
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode == 0 and len(r.stdout) > 200:
            return base64.b64encode(r.stdout).decode()
    except Exception:
        pass
    return None


# ── Helpers ────────────────────────────────────────────────────────────────────
def fmt_duration(s: float) -> str:
    s = max(0, int(s))
    h, rem = divmod(s, 3600)
    m, sc  = divmod(rem, 60)
    if h:  return f"{h}h {m}m {sc}s"
    if m:  return f"{m}m {sc}s"
    return f"{sc}s"

def fmt_size(mb: float) -> str:
    if mb >= 1024: return f"{mb/1024:.2f} GB"
    return f"{mb:.1f} MB"

def calc_video_bitrate(target_mb: float, dur: float,
                       audio_kbps: int = 128) -> int:
    if dur <= 0: return 500
    total_bits = target_mb * 8 * 1048576
    audio_bits = audio_kbps * 1024 * dur
    video_bits = max(0, total_bits - audio_bits)
    return max(80, int(video_bits / dur / 1024 * 0.97))

def calc_audio_bitrate(target_mb: float, dur: float) -> int:
    if dur <= 0: return 96
    total_bits = target_mb * 8 * 1048576
    kbps = int(total_bits / dur / 1024 * 0.97)
    return max(32, min(320, kbps))

def mime_for(ext: str) -> str:
    return {
        'mp4' : 'video/mp4',
        'mkv' : 'video/x-matroska',
        'avi' : 'video/x-msvideo',
        'mov' : 'video/quicktime',
        'webm': 'video/webm',
        'mp3' : 'audio/mpeg',
        'aac' : 'audio/aac',
        'wav' : 'audio/wav',
        'ogg' : 'audio/ogg',
        'opus': 'audio/opus',
        'flac': 'audio/flac',
        'm4a' : 'audio/mp4',
    }.get(ext, 'application/octet-stream')


# ── Save uploaded file to disk (chunked, handles up to 1 GB) ──────────────────
def save_upload(uploaded_file) -> str | None:
    """
    Write the Streamlit UploadedFile to a real temp file on disk.
    Returns the path, or None on failure.
    """
    try:
        suffix = Path(uploaded_file.name).suffix.lower() or '.bin'
        tmp_fd, tmp_path = tempfile.mkstemp(suffix=suffix)
        os.close(tmp_fd)

        CHUNK   = 8 * 1024 * 1024   # 8 MB chunks
        written = 0
        uploaded_file.seek(0)

        with open(tmp_path, 'wb') as f:
            while True:
                chunk = uploaded_file.read(CHUNK)
                if not chunk:
                    break
                f.write(chunk)
                written += len(chunk)

        if written < 512:
            os.remove(tmp_path)
            return None

        return tmp_path

    except Exception as e:
        st.error(f"Failed to save upload: {e}")
        return None


# ── Compression ────────────────────────────────────────────────────────────────
def compress_file(
    src: str,
    target_mb: float,
    out_fmt: str,
    info: dict,
    progress_bar,
    status_text,
) -> bytes | None:
    """
    Compress src → bytes.
    Shows live progress via Streamlit widgets.
    """
    tmp_out  = None
    tmp_pass = None
    null_dev = 'NUL' if sys.platform == 'win32' else '/dev/null'

    try:
        dur          = float(info['duration'])
        orig_mb      = float(info['size_mb'])
        is_audio_out = out_fmt in (
            'mp3', 'aac', 'wav', 'ogg', 'opus', 'flac', 'm4a')
        is_audio_src = bool(info.get('is_audio_only'))

        if dur <= 0:
            st.error("Cannot determine duration — file may be corrupt.")
            return None

        tmp_fd, tmp_out = tempfile.mkstemp(suffix='.' + out_fmt)
        os.close(tmp_fd)
        os.remove(tmp_out)   # let FFmpeg create it fresh

        use_2pass = False

        # ── AUDIO path ─────────────────────────────────────────────────────
        if is_audio_out or is_audio_src:
            kbps = calc_audio_bitrate(target_mb, dur)
            status_text.markdown(
                f"🎵 **Audio encode** → **{kbps} kbps**  "
                f"*(target {target_mb} MB from {orig_mb} MB)*")
            progress_bar.progress(0.08)

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

            cmd      = (['ffmpeg', '-y', '-i', src, '-vn']
                        + codec_args + [tmp_out])
            base_pct = 0.10

        # ── VIDEO path ─────────────────────────────────────────────────────
        else:
            a_kbps    = 128
            v_kbps    = calc_video_bitrate(target_mb, dur, a_kbps)
            use_2pass = True

            # Pass 1
            tmp_pass = tempfile.mktemp(prefix='ffpass_')
            cmd1 = [
                'ffmpeg', '-y', '-i', src,
                '-c:v', 'libx264',
                '-b:v', f'{v_kbps}k',
                '-pass', '1', '-passlogfile', tmp_pass,
                '-preset', 'fast', '-an',
                '-f', 'null', null_dev,
            ]
            status_text.markdown("📊 **Pass 1/2** — analysing…")
            progress_bar.progress(0.05)

            p1 = subprocess.Popen(
                cmd1,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, bufsize=1)

            for line in p1.stdout:
                m = re.search(r'time=(\d+):(\d+):([\d.]+)', line)
                if m and dur > 0:
                    el    = (int(m.group(1)) * 3600
                             + int(m.group(2)) * 60
                             + float(m.group(3)))
                    ratio = min(el / dur, 1.0)
                    progress_bar.progress(0.05 + ratio * 0.35)
                    status_text.markdown(
                        f"📊 **Pass 1/2** — {int(ratio * 100)}%")

            p1.wait(timeout=14400)
            if p1.returncode != 0:
                st.error(f"Pass 1 failed (rc={p1.returncode})")
                return None

            # Pass 2
            cmd = [
                'ffmpeg', '-y', '-i', src,
                '-c:v', 'libx264',
                '-b:v',     f'{v_kbps}k',
                '-maxrate', f'{int(v_kbps * 1.4)}k',
                '-bufsize', f'{v_kbps * 2}k',
                '-pass', '2', '-passlogfile', tmp_pass,
                '-preset', 'fast',
                '-c:a', 'aac', '-b:a', f'{a_kbps}k',
                '-movflags', '+faststart',
                tmp_out,
            ]
            base_pct = 0.42
            status_text.markdown(
                f"🎬 **Pass 2/2** — encoding  "
                f"*{v_kbps} kbps video + {a_kbps} kbps audio*")
            progress_bar.progress(base_pct)

        # ── Run main encode ────────────────────────────────────────────────
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, bufsize=1)

        last_update = time.time()
        for line in proc.stdout:
            m = re.search(r'time=(\d+):(\d+):([\d.]+)', line)
            if m and dur > 0:
                el    = (int(m.group(1)) * 3600
                         + int(m.group(2)) * 60
                         + float(m.group(3)))
                ratio = min(el / dur, 1.0)
                pct   = base_pct + ratio * (0.95 - base_pct)
                progress_bar.progress(min(pct, 0.95))

                # Throttle UI updates to every 0.5 s
                now = time.time()
                if now - last_update >= 0.5:
                    last_update = now
                    spd_m = re.search(r'speed=([\d.]+)x', line)
                    spd   = float(spd_m.group(1)) if spd_m else 0
                    eta   = (f"  ·  ETA ~{int((dur - el) / spd)}s"
                             if spd > 0.01 and el < dur else "")
                    sz_m  = re.search(r'size=\s*(\d+)kB', line)
                    cur   = (f"  ·  {round(int(sz_m.group(1)) / 1024, 1)}"
                             f" MB written"
                             if sz_m else "")
                    status_text.markdown(
                        f"🗜️ **Compressing…** {int(ratio * 100)}%"
                        f"{cur}{eta}")

        proc.wait(timeout=14400)

        # ── Validate ───────────────────────────────────────────────────────
        if proc.returncode != 0:
            st.error(
                f"FFmpeg failed (rc={proc.returncode}). "
                f"Try a different format or lower target size.")
            return None

        if not os.path.exists(tmp_out):
            st.error("Output file was not created.")
            return None

        out_size = os.path.getsize(tmp_out)
        if out_size < 512:
            st.error(
                f"Output file too small ({out_size} bytes). "
                f"Encoding may have failed.")
            return None

        progress_bar.progress(1.0)
        status_text.markdown("✅ **Done!** Reading result…")

        with open(tmp_out, 'rb') as fh:
            return fh.read()

    except subprocess.TimeoutExpired:
        st.error("Compression timed out (4 h limit exceeded).")
        return None
    except Exception as ex:
        st.error(f"Compression error: {ex}")
        return None

    finally:
        if tmp_out and os.path.exists(tmp_out):
            try: os.remove(tmp_out)
            except Exception: pass
        if tmp_pass:
            for sfx in ('', '.log', '-0.log',
                        '.log.mbtree', '-0.log.mbtree'):
                p = tmp_pass + sfx
                if os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass


# ── File info chips ────────────────────────────────────────────────────────────
def show_file_info(filename: str, info: dict, audio_only: bool) -> None:
    dur_str = fmt_duration(info['duration'])
    if audio_only:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("⏱ Duration",  dur_str)
        c2.metric("💾 Size",     fmt_size(info['size_mb']))
        c3.metric("🎙 Codec",    info['audio_codec'].upper() or "—")
        c4.metric("📡 Bitrate",
                  f"{info['bitrate']//1000} kbps"
                  if info['bitrate'] else "—")
    else:
        c1, c2, c3, c4, c5 = st.columns(5)
        res = (f"{info['width']}×{info['height']}"
               if info['width'] and info['height'] else "—")
        c1.metric("⏱ Duration",    dur_str)
        c2.metric("💾 Size",       fmt_size(info['size_mb']))
        c3.metric("📐 Resolution", res)
        c4.metric("🎞 FPS",        str(info['fps']) if info['fps'] else "—")
        c5.metric("🎬 Codec",      info['video_codec'].upper() or "—")


# ══════════════════════════════════════════════════════════════════════════════
# MAIN APP
# ══════════════════════════════════════════════════════════════════════════════
def main():
    ff_ok = check_ffmpeg()

    # ── Header ────────────────────────────────────────────────────────────────
    col_title, col_badge = st.columns([5, 1])
    with col_title:
        st.markdown("# 🗜️ Media Compressor")
        st.caption(
            "Compress MP4 · MP3 · any video or audio "
            "to your exact target size  ·  up to **1 GB**")
    with col_badge:
        st.markdown("<div style='height:18px'></div>", unsafe_allow_html=True)
        if ff_ok:
            st.success("✅ FFmpeg")
        else:
            st.error("❌ FFmpeg missing")
            st.info(
                "Add `ffmpeg` to **packages.txt** in your repo root, "
                "then reboot the app.")
            st.stop()

    st.divider()

    # ── Step 1 — Upload ────────────────────────────────────────────────────────
    st.markdown("### 📂 Step 1 — Upload Your File")
    st.caption("Supported: MP4, MKV, AVI, MOV, WebM, MP3, WAV, AAC, "
               "OGG, OPUS, FLAC, M4A and more  ·  Max **1 GB**")

    uploaded = st.file_uploader(
        "upload",
        type=[
            'mp4', 'mkv', 'avi', 'mov', 'wmv', 'flv', 'webm',
            'm4v', 'mpeg', 'mpg', '3gp', 'ts', 'mts', 'ogv',
            'mp3', 'wav', 'aac', 'ogg', 'opus', 'flac', 'wma', 'm4a',
        ],
        label_visibility='collapsed',
    )

    if not uploaded:
        st.markdown("""
        <div style="
          background:#0d0f1c;border:2px dashed #1e2238;
          border-radius:14px;padding:40px;text-align:center;
          color:#565a7a;margin-top:8px">
          <div style="font-size:2.5rem;margin-bottom:8px">🎬</div>
          <div style="font-size:1rem;font-weight:700;
                      color:#dde1f5;margin-bottom:4px">
            Drop your video or audio file here
          </div>
          <div style="font-size:.82rem">
            or click <b>Browse files</b> above · up to 1 GB
          </div>
        </div>
        """, unsafe_allow_html=True)
        st.stop()

    # ── Save to disk (only when file changes) ─────────────────────────────────
    file_key = f"{uploaded.name}_{uploaded.size}"

    if st.session_state.get('file_key') != file_key:
        # New file uploaded — clear everything
        st.session_state.clear()
        st.session_state['file_key'] = file_key

        # Delete old temp file if it exists
        old = st.session_state.get('tmp_src')
        if old and os.path.exists(old):
            try: os.remove(old)
            except Exception: pass

        with st.spinner(
            f"Saving **{uploaded.name}** "
            f"({fmt_size(uploaded.size / 1048576)}) to disk…"):
            tmp_src = save_upload(uploaded)

        if not tmp_src:
            st.error("Failed to save uploaded file.")
            st.stop()

        st.session_state['tmp_src']  = tmp_src
        st.session_state['file_key'] = file_key

    tmp_src = st.session_state.get('tmp_src')
    if not tmp_src or not os.path.exists(tmp_src):
        st.error("Temp file missing — please re-upload.")
        st.session_state.clear()
        st.stop()

    # ── Probe ──────────────────────────────────────────────────────────────────
    if 'info' not in st.session_state:
        with st.spinner("Reading file info…"):
            info = get_media_info(tmp_src)
        if not info:
            st.error(
                "Cannot read media info — "
                "is the file a valid video/audio?")
            st.stop()
        if info['duration'] <= 0:
            st.error(
                "Cannot determine duration — "
                "file may be corrupt or unsupported.")
            st.stop()
        st.session_state['info'] = info
    else:
        info = st.session_state['info']

    audio_only = (
        Path(uploaded.name).suffix.lower().lstrip('.')
        in {'mp3', 'wav', 'aac', 'ogg', 'opus', 'flac', 'wma', 'm4a'}
        or info['is_audio_only']
    )

    # ── File info ──────────────────────────────────────────────────────────────
    st.markdown("### 📊 File Information")

    if not audio_only:
        ts    = min(5.0, max(0.0, info['duration'] * 0.1))
        thumb = make_thumb_b64(tmp_src, ts)
        if thumb:
            th_col, info_col = st.columns([1, 2])
            with th_col:
                st.image(
                    f"data:image/jpeg;base64,{thumb}",
                    use_column_width=True,
                    caption="Preview")
            with info_col:
                st.markdown(
                    f"**{uploaded.name}**  \n"
                    f"<span style='color:#565a7a;font-size:.8rem'>"
                    f"{info['video_codec'].upper()}  ·  "
                    f"{info['width']}×{info['height']}  ·  "
                    f"{info['fps']} fps  ·  "
                    f"{fmt_duration(info['duration'])}</span>",
                    unsafe_allow_html=True)
                show_file_info(uploaded.name, info, audio_only)
        else:
            st.markdown(f"**{uploaded.name}**")
            show_file_info(uploaded.name, info, audio_only)

        # Browser preview (local blob — no server bandwidth used)
        with st.expander("▶️ Preview", expanded=False):
            st.video(uploaded)
    else:
        st.markdown(
            f"🎵 **{uploaded.name}**  \n"
            f"<span style='color:#565a7a;font-size:.8rem'>"
            f"{info['audio_codec'].upper()}  ·  "
            f"{fmt_duration(info['duration'])}</span>",
            unsafe_allow_html=True)
        show_file_info(uploaded.name, info, audio_only)
        with st.expander("🔊 Preview", expanded=False):
            st.audio(uploaded)

    st.divider()

    # ── Step 2 — Settings ──────────────────────────────────────────────────────
    st.markdown("### 🗜️ Step 2 — Compression Settings")

    orig_mb  = info['size_mb']
    max_mb   = max(0.5, round(orig_mb * 0.98, 1))
    def_mb   = max(0.1, round(orig_mb * 0.5, 1))
    slider_v = min(def_mb, max_mb)

    target_mb = st.slider(
        f"Target size  *(original: **{fmt_size(orig_mb)}**)*",
        min_value=0.1,
        max_value=float(max_mb),
        value=float(slider_v),
        step=0.1,
        format="%.1f MB",
    )

    # Quick preset buttons
    st.markdown(
        "<div style='font-size:.78rem;color:#565a7a;"
        "text-transform:uppercase;letter-spacing:.5px;"
        "margin-bottom:4px'>Quick presets</div>",
        unsafe_allow_html=True)

    presets     = [1, 5, 10, 25, 50, 100, 250, 500]
    preset_cols = st.columns(len(presets))
    for i, p in enumerate(presets):
        disabled = float(p) >= orig_mb
        if preset_cols[i].button(
            f"{p} MB",
            key=f"pre_{p}",
            disabled=disabled,
            use_container_width=True,
        ):
            target_mb = float(p)
            st.rerun()

    # Size comparison
    reduction = round((1 - target_mb / orig_mb) * 100, 1) if orig_mb else 0
    saved_mb  = round(orig_mb - target_mb, 1)

    if target_mb >= orig_mb:
        st.markdown(
            f'<div class="warn-banner">⚠️ Target ({fmt_size(target_mb)}) '
            f'must be smaller than original ({fmt_size(orig_mb)})</div>',
            unsafe_allow_html=True)
    else:
        m1, m2, m3 = st.columns(3)
        m1.metric("📦 Original",   fmt_size(orig_mb))
        m2.metric("🎯 Target",     fmt_size(target_mb),
                  delta=f"-{fmt_size(saved_mb)}")
        m3.metric("📉 Reduction",  f"{reduction}%")

    st.markdown("<div style='height:8px'></div>", unsafe_allow_html=True)

    # ── Format picker ──────────────────────────────────────────────────────────
    st.markdown("**🎯 Output Format**")

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

    st.divider()

    # ── Step 3 — Compress ──────────────────────────────────────────────────────
    st.markdown("### 🚀 Step 3 — Compress")

    # ── Show existing result ───────────────────────────────────────────────────
    if st.session_state.get('result'):
        res       = st.session_state['result']
        final_mb  = round(len(res['data']) / 1048576, 2)
        red       = round((1 - final_mb / orig_mb) * 100, 1)
        saved     = round(orig_mb - final_mb, 1)
        base_name = Path(uploaded.name).stem + '_compressed'

        st.markdown(f"""
        <div class="result-box">
          <div class="result-title">✅ Compression Complete!</div>
          <div class="result-sub">
            {fmt_size(orig_mb)} → {fmt_size(final_mb)}
          </div>
          <div class="size-badge">
            🎉 {red}% smaller  ·  saved {fmt_size(saved)}
          </div>
        </div>
        """, unsafe_allow_html=True)

        st.download_button(
            label=(f"⬇️  Download  "
                   f"{base_name}.{res['ext']}  "
                   f"({fmt_size(final_mb)})"),
            data=res['data'],
            file_name=f"{base_name}.{res['ext']}",
            mime=mime_for(res['ext']),
            use_container_width=True,
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
                # Clean up temp file
                if tmp_src and os.path.exists(tmp_src):
                    try: os.remove(tmp_src)
                    except Exception: pass
                st.session_state.clear()
                st.rerun()

    # ── Compress button ────────────────────────────────────────────────────────
    else:
        btn_disabled = (target_mb >= orig_mb)
        btn_label    = (
            f"🗜️  Compress  →  {fmt_size(target_mb)}  "
            f"as {out_fmt.upper()}"
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
            status_text.markdown("🔄 Starting compression…")
            time_start   = time.time()

            result_bytes = compress_file(
                src=tmp_src,
                target_mb=target_mb,
                out_fmt=out_fmt,
                info=info,
                progress_bar=progress_bar,
                status_text=status_text,
            )

            elapsed = time.time() - time_start

            if result_bytes:
                final_mb = round(len(result_bytes) / 1048576, 2)
                status_text.markdown(
                    f"✅ **Done in {elapsed:.0f}s** — "
                    f"{fmt_size(orig_mb)} → {fmt_size(final_mb)}")

                st.session_state['result'] = {
                    'data': result_bytes,
                    'ext' : out_fmt,
                }
                st.rerun()
            else:
                status_text.markdown(
                    "❌ **Compression failed** — see error above")


if __name__ == '__main__':
    main()
