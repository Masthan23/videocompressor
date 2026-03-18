# app.py  ── Streamlit Cloud version
import streamlit as st
import subprocess, os, uuid, json, tempfile, base64, time, re, sys, io
from pathlib import Path

# ── Page config ────────────────────────────────────────────────────────────────
st.set_page_config(
    page_title="🗜️ Media Compressor",
    page_icon="🗜️",
    layout="centered",
)

# ── Custom CSS (same dark theme) ───────────────────────────────────────────────
st.markdown("""
<style>
  /* Dark background */
  .stApp { background: #07090f; color: #dde1f5; }
  .block-container { max-width: 740px; padding-top: 1.5rem; }

  /* Hide Streamlit branding */
  #MainMenu, footer, header { visibility: hidden; }

  /* Buttons */
  .stButton > button {
    background: linear-gradient(135deg, #6c5fff, #a09aff);
    color: white; border: none; border-radius: 10px;
    font-weight: 700; width: 100%;
  }
  .stButton > button:hover {
    background: linear-gradient(135deg, #5a4eee, #8f89ff);
    border: none;
  }

  /* Progress bar */
  .stProgress > div > div > div {
    background: linear-gradient(90deg, #6c5fff, #a09aff);
  }

  /* Download button */
  .stDownloadButton > button {
    background: linear-gradient(135deg, #2ecc71, #27ae60) !important;
    color: #050f0a !important; border: none !important;
    border-radius: 10px !important; font-weight: 800 !important;
    width: 100% !important; font-size: 1rem !important;
  }

  /* Metric cards */
  [data-testid="metric-container"] {
    background: #0d0f1c;
    border: 1px solid #1e2238;
    border-radius: 10px;
    padding: 10px;
  }
</style>
""", unsafe_allow_html=True)


# ── FFmpeg helpers ─────────────────────────────────────────────────────────────
@st.cache_resource
def check_ffmpeg() -> bool:
    try:
        r = subprocess.run(['ffmpeg', '-version'],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False

@st.cache_resource
def check_ffprobe() -> bool:
    try:
        r = subprocess.run(['ffprobe', '-version'],
                           capture_output=True, timeout=10)
        return r.returncode == 0
    except Exception:
        return False


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
            'size': sz, 'size_mb': round(sz / 1048576, 2),
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
                    n, d = s.get('r_frame_rate','0/1').split('/')
                    info['fps'] = round(int(n)/int(d), 2) if int(d) else 0
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


def make_thumb_b64(path: str, ts: float = 5.0) -> str | None:
    try:
        ts = max(0.0, min(float(ts), 30.0))
        cmd = ['ffmpeg', '-y', '-ss', f'{ts:.2f}', '-i', path,
               '-vframes', '1', '-q:v', '5', '-vf', 'scale=480:-2',
               '-f', 'image2pipe', '-vcodec', 'mjpeg', 'pipe:1']
        r = subprocess.run(cmd, capture_output=True, timeout=30)
        if r.returncode == 0 and len(r.stdout) > 200:
            return base64.b64encode(r.stdout).decode()
    except Exception:
        pass
    return None


def fmt_duration(s: float) -> str:
    s = max(0, int(s))
    h, rem = divmod(s, 3600)
    m, sc  = divmod(rem, 60)
    if h:   return f"{h}h {m}m {sc}s"
    if m:   return f"{m}m {sc}s"
    return f"{sc}s"


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


# ── Compression (runs synchronously — Streamlit has no background threads) ─────
def compress_file(src: str, target_mb: float, out_fmt: str,
                  info: dict, progress_bar, status_text) -> bytes | None:
    """
    Compress src → bytes.
    Updates the Streamlit progress_bar and status_text during encoding.
    Returns raw bytes of the compressed file, or None on failure.
    """
    tmp_out  = None
    tmp_pass = None
    null_dev = 'NUL' if sys.platform == 'win32' else '/dev/null'

    try:
        dur          = float(info['duration'])
        orig_mb      = float(info['size_mb'])
        is_audio_out = out_fmt in ('mp3','aac','wav','ogg','opus','flac','m4a')
        is_audio_src = bool(info.get('is_audio_only'))

        if dur <= 0:
            st.error("Cannot determine duration — file may be corrupt.")
            return None

        tmp_fd, tmp_out = tempfile.mkstemp(suffix='.' + out_fmt)
        os.close(tmp_fd)
        os.remove(tmp_out)   # let FFmpeg create it fresh

        # ── Build command ──────────────────────────────────────────────────
        use_2pass = False

        if is_audio_out or is_audio_src:
            kbps = calc_audio_bitrate(target_mb, dur)
            status_text.text(
                f"🎵 Audio encode → {kbps} kbps  "
                f"(target {target_mb} MB from {orig_mb} MB)")
            codec_map = {
                'mp3' : ['-c:a','libmp3lame','-b:a',f'{kbps}k',
                          '-compression_level','0'],
                'aac' : ['-c:a','aac',        '-b:a',f'{kbps}k'],
                'm4a' : ['-c:a','aac',         '-b:a',f'{kbps}k'],
                'ogg' : ['-c:a','libvorbis',   '-b:a',f'{kbps}k'],
                'opus': ['-c:a','libopus',     '-b:a',f'{kbps}k','-vbr','on'],
                'flac': ['-c:a','flac',         '-compression_level','8'],
                'wav' : ['-c:a','pcm_s16le'],
            }
            codec_args = codec_map.get(
                out_fmt, ['-c:a','libmp3lame','-b:a',f'{kbps}k'])
            cmd = (['ffmpeg','-y','-i',src,'-vn']
                   + codec_args + [tmp_out])
            base_pct = 0.15

        else:
            a_kbps    = 128
            v_kbps    = calc_video_bitrate(target_mb, dur, a_kbps)
            use_2pass = True   # always use 2-pass for best accuracy

            # ── Pass 1 ────────────────────────────────────────────────────
            tmp_pass = tempfile.mktemp(prefix='ffpass_')
            cmd1 = [
                'ffmpeg', '-y', '-i', src,
                '-c:v', 'libx264',
                '-b:v', f'{v_kbps}k',
                '-pass', '1', '-passlogfile', tmp_pass,
                '-preset', 'fast', '-an',
                '-f', 'null', null_dev,
            ]
            status_text.text("📊 Pass 1/2 — analysing…")
            progress_bar.progress(0.05)

            p1 = subprocess.Popen(
                cmd1,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True, bufsize=1)

            for line in p1.stdout:
                m = re.search(r'time=(\d+):(\d+):([\d.]+)', line)
                if m and dur > 0:
                    el    = (int(m.group(1))*3600
                             + int(m.group(2))*60
                             + float(m.group(3)))
                    ratio = min(el / dur, 1.0)
                    progress_bar.progress(0.05 + ratio * 0.35)
                    status_text.text(
                        f"📊 Pass 1/2 — {int(ratio*100)}%")

            p1.wait(timeout=14400)
            if p1.returncode != 0:
                st.error(f"Pass 1 failed (rc={p1.returncode})")
                return None

            # ── Pass 2 ────────────────────────────────────────────────────
            cmd = [
                'ffmpeg', '-y', '-i', src,
                '-c:v', 'libx264',
                '-b:v',     f'{v_kbps}k',
                '-maxrate', f'{int(v_kbps*1.4)}k',
                '-bufsize', f'{v_kbps*2}k',
                '-pass', '2', '-passlogfile', tmp_pass,
                '-preset', 'fast',
                '-c:a', 'aac', '-b:a', f'{a_kbps}k',
                '-movflags', '+faststart',
                tmp_out,
            ]
            base_pct = 0.42
            status_text.text(
                f"🎬 Pass 2/2 — encoding  "
                f"{v_kbps} kbps video + {a_kbps} kbps audio…")
            progress_bar.progress(base_pct)

        # ── Run main (or only) encode ──────────────────────────────────────
        proc = subprocess.Popen(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True, bufsize=1)

        for line in proc.stdout:
            m = re.search(r'time=(\d+):(\d+):([\d.]+)', line)
            if m and dur > 0:
                el    = (int(m.group(1))*3600
                         + int(m.group(2))*60
                         + float(m.group(3)))
                ratio = min(el / dur, 1.0)
                pct   = base_pct + ratio * (0.95 - base_pct)
                progress_bar.progress(min(pct, 0.95))

                spd_m = re.search(r'speed=([\d.]+)x', line)
                spd   = float(spd_m.group(1)) if spd_m else 0
                eta   = (f"  ETA ~{int((dur-el)/spd)}s"
                         if spd > 0.01 and el < dur else "")
                sz_m  = re.search(r'size=\s*(\d+)kB', line)
                cur   = (f"  {round(int(sz_m.group(1))/1024,1)} MB written"
                         if sz_m else "")
                status_text.text(
                    f"🗜️ Compressing… {int(ratio*100)}%{cur}{eta}")

        proc.wait(timeout=14400)

        if proc.returncode != 0:
            st.error(f"FFmpeg failed (rc={proc.returncode})")
            return None

        if not os.path.exists(tmp_out) or os.path.getsize(tmp_out) < 512:
            st.error("Output file missing or empty after encoding.")
            return None

        progress_bar.progress(1.0)
        status_text.text("✅ Done!")

        with open(tmp_out, 'rb') as fh:
            return fh.read()

    except Exception as ex:
        st.error(f"Compression error: {ex}")
        return None

    finally:
        if tmp_out and os.path.exists(tmp_out):
            try: os.remove(tmp_out)
            except Exception: pass
        if tmp_pass:
            for sfx in ('','.log','-0.log','.log.mbtree','-0.log.mbtree'):
                p = tmp_pass + sfx
                if os.path.exists(p):
                    try: os.remove(p)
                    except Exception: pass


# ══════════════════════════════════════════════════════════════════════════════
# UI
# ══════════════════════════════════════════════════════════════════════════════
def main():
    ff_ok = check_ffmpeg()

    # ── Header ────────────────────────────────────────────────────────────────
    col_t, col_b = st.columns([4, 1])
    with col_t:
        st.markdown("# 🗜️ Media Compressor")
        st.caption("Compress MP4 · MP3 · any video or audio to your exact target size")
    with col_b:
        if ff_ok:
            st.success("✅ FFmpeg")
        else:
            st.error("❌ FFmpeg missing")
            st.info("On Streamlit Cloud add `ffmpeg` to **packages.txt**")
            st.stop()

    st.divider()

    # ── Step 1 — Upload ────────────────────────────────────────────────────────
    st.markdown("### 📂 Step 1 — Upload Your File")
    uploaded = st.file_uploader(
        "Drop a video or audio file (up to 2 GB on Streamlit Cloud)",
        type=[
            'mp4','mkv','avi','mov','wmv','flv','webm','m4v','mpeg','mpg',
            '3gp','ts','mts','m2ts','vob','asf','ogv',
            'mp3','wav','aac','ogg','opus','flac','wma','m4a',
        ],
        label_visibility='collapsed',
    )

    if not uploaded:
        st.info("👆 Upload a file to get started")
        st.stop()

    # ── Save upload to temp file ───────────────────────────────────────────────
    # Only re-save if the file changed
    cache_key = f"saved_{uploaded.name}_{uploaded.size}"
    if st.session_state.get('cache_key') != cache_key:
        with st.spinner("Saving upload…"):
            tmp_fd, tmp_src = tempfile.mkstemp(
                suffix=Path(uploaded.name).suffix.lower() or '.bin')
            os.close(tmp_fd)
            CHUNK = 4 * 1024 * 1024
            with open(tmp_src, 'wb') as f:
                uploaded.seek(0)
                while True:
                    chunk = uploaded.read(CHUNK)
                    if not chunk: break
                    f.write(chunk)
            st.session_state['tmp_src']   = tmp_src
            st.session_state['cache_key'] = cache_key
            st.session_state.pop('result', None)   # clear old result
    else:
        tmp_src = st.session_state['tmp_src']

    # ── Probe file ─────────────────────────────────────────────────────────────
    with st.spinner("Reading file info…"):
        info = get_media_info(tmp_src)

    if not info:
        st.error("Cannot read media info — is the file valid?")
        st.stop()
    if info['duration'] <= 0:
        st.error("Cannot determine duration — file may be corrupt.")
        st.stop()

    audio_only = (Path(uploaded.name).suffix.lower().lstrip('.')
                  in {'mp3','wav','aac','ogg','opus','flac','wma','m4a'}
                  or info['is_audio_only'])

    # ── File info display ──────────────────────────────────────────────────────
    st.markdown("### 📊 File Information")

    # Thumbnail
    if not audio_only:
        ts    = min(5.0, max(0.0, info['duration'] * 0.1))
        thumb = make_thumb_b64(tmp_src, ts)
        if thumb:
            th_col, info_col = st.columns([1, 2])
            with th_col:
                st.image(
                    f"data:image/jpeg;base64,{thumb}",
                    use_column_width=True)
            with info_col:
                _show_chips(uploaded.name, info, audio_only)
        else:
            _show_chips(uploaded.name, info, audio_only)
    else:
        st.markdown("🎵 **Audio file**")
        _show_chips(uploaded.name, info, audio_only)

    # Local browser preview
    if not audio_only:
        st.video(uploaded)
    else:
        st.audio(uploaded)

    st.divider()

    # ── Step 2 — Settings ──────────────────────────────────────────────────────
    st.markdown("### 🗜️ Step 2 — Compression Settings")

    orig_mb = info['size_mb']
    max_mb  = max(0.5, round(orig_mb * 0.98, 1))
    def_mb  = max(0.1, round(orig_mb * 0.5, 1))

    target_mb = st.slider(
        f"Target size (original: **{orig_mb} MB**)",
        min_value=0.1,
        max_value=float(max_mb),
        value=float(min(def_mb, max_mb)),
        step=0.1,
        format="%.1f MB",
    )

    # Quick presets
    st.markdown("**Quick presets:**")
    preset_cols = st.columns(8)
    presets = [1, 5, 10, 25, 50, 100, 250, 500]
    for i, p in enumerate(presets):
        if preset_cols[i].button(f"{p} MB", key=f"pre_{p}",
                                  disabled=(p >= orig_mb)):
            target_mb = float(p)

    # Size comparison
    reduction = round((1 - target_mb / orig_mb) * 100, 1) if orig_mb else 0
    saved_mb  = round(orig_mb - target_mb, 1)

    m1, m2, m3 = st.columns(3)
    m1.metric("Original",  f"{orig_mb} MB")
    m2.metric("Target",    f"{target_mb} MB", delta=f"-{saved_mb} MB")
    m3.metric("Reduction", f"{reduction}%")

    if target_mb >= orig_mb:
        st.error(f"Target ({target_mb} MB) must be smaller than original ({orig_mb} MB)")
        st.stop()

    # Format picker
    st.markdown("**Output Format:**")
    fmt_col1, fmt_col2 = st.columns(2)
    with fmt_col1:
        st.markdown("📹 **Video**")
        video_fmt = st.radio(
            "Video format",
            ['mp4', 'mkv', 'avi', 'mov', 'webm'],
            horizontal=True,
            label_visibility='collapsed',
        )
    with fmt_col2:
        st.markdown("🎵 **Audio**")
        audio_fmt = st.radio(
            "Audio format",
            ['mp3', 'aac', 'wav', 'ogg', 'opus', 'flac', 'm4a'],
            horizontal=True,
            label_visibility='collapsed',
        )

    if audio_only:
        out_fmt = audio_fmt
        st.info(f"Output: **{audio_fmt.upper()}** (audio)")
    else:
        fmt_type = st.radio(
            "Use format type:",
            ["Video", "Audio (extract audio only)"],
            horizontal=True,
        )
        out_fmt = video_fmt if fmt_type == "Video" else audio_fmt
        st.info(f"Output: **{out_fmt.upper()}**")

    st.divider()

    # ── Step 3 — Compress ──────────────────────────────────────────────────────
    st.markdown("### 🚀 Step 3 — Compress")

    # Show result from previous run if available
    if 'result' in st.session_state and st.session_state['result']:
        res = st.session_state['result']
        final_mb  = round(len(res['data']) / 1048576, 2)
        reduction = round((1 - final_mb / orig_mb) * 100, 1)
        saved     = round(orig_mb - final_mb, 1)

        st.success(
            f"✅ Compressed!  "
            f"{orig_mb} MB → {final_mb} MB  "
            f"({reduction}% smaller, saved {saved} MB)")

        base_name = Path(uploaded.name).stem + '_compressed'
        st.download_button(
            label=f"⬇️ Download  {base_name}.{res['ext']}  ({final_mb} MB)",
            data=res['data'],
            file_name=f"{base_name}.{res['ext']}",
            mime=_mime(res['ext']),
            use_container_width=True,
        )

        if st.button("🔄 Compress Again with Different Settings",
                     use_container_width=True):
            st.session_state.pop('result', None)
            st.rerun()

    else:
        if st.button(
            f"🗜️ Compress  →  {target_mb} MB  as {out_fmt.upper()}",
            type="primary",
            use_container_width=True,
        ):
            st.markdown("---")
            progress_bar = st.progress(0.0)
            status_text  = st.empty()
            status_text.text("Starting…")

            result_bytes = compress_file(
                src=tmp_src,
                target_mb=target_mb,
                out_fmt=out_fmt,
                info=info,
                progress_bar=progress_bar,
                status_text=status_text,
            )

            if result_bytes:
                final_mb  = round(len(result_bytes) / 1048576, 2)
                reduction = round((1 - final_mb / orig_mb) * 100, 1)
                saved     = round(orig_mb - final_mb, 1)

                st.session_state['result'] = {
                    'data': result_bytes,
                    'ext' : out_fmt,
                }
                st.rerun()
            else:
                st.error("Compression failed. Check the error above.")


def _show_chips(filename, info, audio_only):
    """Display file info as columns."""
    dur_str = fmt_duration(info['duration'])
    if audio_only:
        c1, c2, c3, c4 = st.columns(4)
        c1.metric("Duration",    dur_str)
        c2.metric("Size",        f"{info['size_mb']} MB")
        c3.metric("Codec",       info['audio_codec'].upper() or "—")
        c4.metric("Bitrate",
                  f"{info['bitrate']//1000} kbps" if info['bitrate'] else "—")
    else:
        c1, c2, c3, c4, c5 = st.columns(5)
        res = (f"{info['width']}×{info['height']}"
               if info['width'] and info['height'] else "—")
        c1.metric("Duration",   dur_str)
        c2.metric("Size",       f"{info['size_mb']} MB")
        c3.metric("Resolution", res)
        c4.metric("FPS",        str(info['fps']) if info['fps'] else "—")
        c5.metric("Codec",      info['video_codec'].upper() or "—")


def _mime(ext: str) -> str:
    return {
        'mp4':'video/mp4', 'mkv':'video/x-matroska',
        'avi':'video/x-msvideo', 'mov':'video/quicktime',
        'webm':'video/webm', 'mp3':'audio/mpeg',
        'aac':'audio/aac', 'wav':'audio/wav',
        'ogg':'audio/ogg', 'opus':'audio/opus',
        'flac':'audio/flac', 'm4a':'audio/mp4',
    }.get(ext, 'application/octet-stream')


if __name__ == '__main__':
    main()
