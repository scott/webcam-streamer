#!/usr/bin/env python3
"""
Live Stream Switcher
Cycles through YouTube live camera streams with seamless switching.

Uses a single persistent ffmpeg process reading from stdin via a buffer thread.
Camera switches are done by swapping which yt-dlp process the buffer reads from.
This keeps the output stream (HLS preview or YouTube RTMP) continuous.

Supports multiple streams via config inheritance:
  - Base config: configs/base.yaml (shared defaults)
  - Stream config: configs/streams/<name>.yaml (per-stream settings)
"""

import os
import sys
import time
import signal
import subprocess
import threading
import tempfile
import select
import logging
import argparse
import re
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler
from queue import Queue, Empty
from copy import deepcopy

import yaml
import yt_dlp
import colorlog
from colorlog import ColoredFormatter

SCRIPT_DIR = Path(__file__).parent

logger = None
config = None
current_camera_index = 0
running = False
stop_event = threading.Event()

http_server = None
hls_dir = None

# Persistent ffmpeg process
ffmpeg_proc = None

# Buffer thread and camera switching
buffer_thread = None
buffer_stop_event = threading.Event()
current_camera_proc = None  # The yt-dlp + ffmpeg normalizer pipeline
camera_lock = threading.Lock()


def setup_logging():
    """Setup colored logging."""
    global logger
    formatter = ColoredFormatter(
        "%(log_color)s%(asctime)s [%(levelname)s]%(reset)s %(message)s",
        datefmt="%H:%M:%S",
        log_colors={
            'DEBUG': 'cyan',
            'INFO': 'green',
            'WARNING': 'yellow',
            'ERROR': 'red',
            'CRITICAL': 'red,bg_white',
        }
    )
    handler = colorlog.StreamHandler()
    handler.setFormatter(formatter)
    logger = colorlog.getLogger(__name__)
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)


# ─── Config Loading ───────────────────────────────────────────────────────

def expand_env_vars(value):
    """Recursively expand ${VAR_NAME} patterns in config values."""
    if isinstance(value, str):
        pattern = r'\$\{(\w+)\}'
        def replace(match):
            var_name = match.group(1)
            env_value = os.environ.get(var_name, '')
            if not env_value:
                logger.warning(f"Environment variable {var_name} is not set")
            return env_value
        return re.sub(pattern, replace, value)
    elif isinstance(value, dict):
        return {k: expand_env_vars(v) for k, v in value.items()}
    elif isinstance(value, list):
        return [expand_env_vars(item) for item in value]
    else:
        return value


def deep_merge(base, override):
    """Deep merge override dict into base dict. Override takes precedence."""
    result = deepcopy(base)
    for key, value in override.items():
        if key in result and isinstance(result[key], dict) and isinstance(value, dict):
            result[key] = deep_merge(result[key], value)
        else:
            result[key] = value
    return result


def derive_stream_key_env(stream_name):
    """Derive env var name from stream config filename."""
    return f"YOUTUBE_KEY_{stream_name.upper().replace('-', '_')}"


def load_config(stream_config_path, base_config_path=None):
    """Load configuration with inheritance from base config."""
    global config
    
    stream_path = Path(stream_config_path)
    if not stream_path.exists():
        logger.error(f"Stream config file not found: {stream_path}")
        sys.exit(1)
    
    # Load stream config
    with open(stream_path) as f:
        stream_cfg = yaml.safe_load(f)
    
    # Get display name from config, derive env var name from filename
    display_name = stream_cfg.get('name') or stream_path.stem
    stream_filename = stream_path.stem  # e.g., "ski-resort" from "ski-resort.yaml"
    
    # Load base config if provided
    base_cfg = {}
    if base_config_path:
        base_path = Path(base_config_path)
        if base_path.exists():
            with open(base_path) as f:
                base_cfg = yaml.safe_load(f) or {}
    
    # Deep merge: base first, then stream overrides
    config = deep_merge(base_cfg, stream_cfg)
    
    # Set stream name if not in config
    if 'name' not in config:
        config['name'] = display_name
    
    # Handle stream key: either from explicit config or derived from env var
    stream_opts = config.get('stream', {})
    youtube_opts = stream_opts.get('youtube', {})
    
    if 'stream_key' not in youtube_opts:
        # Try to get from env var - use filename for derivation
        env_var = youtube_opts.get('stream_key_env') or derive_stream_key_env(stream_filename)
        stream_key = os.environ.get(env_var, '')
        if stream_key:
            youtube_opts['stream_key'] = stream_key
            # Remove the _env key after use
            youtube_opts.pop('stream_key_env', None)
        else:
            logger.warning(f"No stream key found for {stream_name}. "
                          f"Expected env var {env_var} to be set.")
    
    # Expand any ${VAR} patterns in config
    config = expand_env_vars(config)
    
    logger.info(f"Loaded config for stream: {config.get('name', 'unnamed')}")
    logger.info(f"Switch interval: {config['stream']['switch_interval']} seconds")
    logger.info(f"Preview mode: {config['stream'].get('preview_mode', True)}")


# ─── HLS preview server ────────────────────────────────────────────────

class StreamHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/health":
            self.send_response(200)
            self.send_header("Content-Type", "text/plain")
            self.end_headers()
            self.wfile.write(b"OK")
        elif self.path == "/" or self.path == "/index.html":
            self.send_response(200)
            self.send_header("Content-Type", "text/html")
            self.end_headers()
            self.wfile.write(b"""<!DOCTYPE html>
<html><head><title>Live Stream Preview</title>
<script src="https://cdn.jsdelivr.net/npm/hls.js@latest"></script>
<style>
  body { background: #111; color: #eee; font-family: sans-serif; text-align: center; margin: 2em; }
  video { max-width: 100%; background: #000; }
  #status { margin-top: 1em; color: #aaa; }
</style>
</head>
<body>
<h1>Live Stream Preview</h1>
<video id="video" controls autoplay muted></video>
<div id="status">Connecting...</div>
<script>
var video = document.getElementById('video');
var status = document.getElementById('status');
if (Hls.isSupported()) {
    var hls = new Hls({
        liveSyncDuration: 3,
        liveMaxLatencyDuration: 10,
        liveDurationInfinity: true,
        manifestLoadingTimeOut: 10000,
        manifestLoadingMaxRetry: 30,
        manifestLoadingRetryDelay: 1000,
    });
    hls.loadSource('/stream/live.m3u8');
    hls.attachMedia(video);
    hls.on(Hls.Events.MANIFEST_PARSED, function() {
        status.textContent = 'Playing';
        video.play();
    });
    hls.on(Hls.Events.ERROR, function(event, data) {
        if (data.fatal) {
            status.textContent = 'Reconnecting...';
            setTimeout(function() { hls.loadSource('/stream/live.m3u8'); }, 2000);
        }
    });
} else if (video.canPlayType('application/vnd.apple.mpegurl')) {
    video.src = '/stream/live.m3u8';
    video.addEventListener('loadedmetadata', function() {
        status.textContent = 'Playing';
        video.play();
    });
}
</script>
</body></html>""")
        elif self.path.startswith("/stream/"):
            filepath = Path(hls_dir) / self.path[8:]
            if filepath.exists():
                self.send_response(200)
                if self.path.endswith(".m3u8"):
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                    self.send_header("Cache-Control", "no-cache")
                else:
                    self.send_header("Content-Type", "video/MP2T")
                    self.send_header("Cache-Control", "no-cache")
                self.end_headers()
                try:
                    with open(filepath, "rb") as f:
                        self.wfile.write(f.read())
                except Exception:
                    pass
            else:
                self.send_error(404)
        else:
            self.send_error(404)

    def log_message(self, format, *args):
        pass


def start_http_server(port):
    """Start HTTP server for HLS preview."""
    global http_server
    server = HTTPServer(("", port), StreamHandler)
    http_server = server
    logger.info(f"HLS preview server on http://localhost:{port}")
    server.serve_forever()


# ─── Camera Management ─────────────────────────────────────────────────

def get_current_camera():
    """Get the current camera from config."""
    cameras = config["cameras"]
    return cameras[current_camera_index % len(cameras)]


def advance_camera():
    """Advance to the next camera."""
    global current_camera_index
    cameras = config["cameras"]
    current_camera_index = (current_camera_index + 1) % len(cameras)
    return get_current_camera()


def start_camera_feed(camera):
    """Start a camera feed and return the output pipe.
    
    Accepts either:
    - camera with 'stream_url': use ffmpeg directly on the HLS URL
    - camera with 'youtube_id': use yt-dlp to get stream (fallback)
    """
    ffmpeg_opts = config["ffmpeg"]
    video_bitrate = ffmpeg_opts.get("video_bitrate", "6800k")
    audio_bitrate = ffmpeg_opts.get("audio_bitrate", "128k")
    resolution = ffmpeg_opts.get("resolution", "1920x1080")
    framerate = ffmpeg_opts.get("framerate", 30)

    stream_url = camera.get("stream_url")
    youtube_id = camera.get("youtube_id")
    youtube_api_key = os.environ.get("YOUTUBE_API_KEY")

    if stream_url:
        source_cmd = [
            "ffmpeg",
            "-hide_banner", "-loglevel", "warning",
            "-i", stream_url,
            "-reconnect", "1",
            "-reconnect_streamed", "1",
            "-reconnect_delay_max", "5",
        ]
    elif youtube_id:
        ydl_cmd = [
            "yt-dlp",
            "-f", "best",
            "--hls-prefer-ffmpeg",
            "-o", "-",
        ]
        if youtube_api_key:
            ydl_cmd.extend(["--extractor-args", f"youtube:api_key={youtube_api_key}"])
        ydl_cmd.append(f"https://www.youtube.com/watch?v={youtube_id}")
        source_cmd = ydl_cmd
    else:
        logger.error("Camera config must have either stream_url or youtube_id")
        return None

    normalize_cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-i", "pipe:0",
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", video_bitrate, "-maxrate", video_bitrate,
        "-bufsize", str(int(video_bitrate.replace("k", "")) * 2) + "k",
        "-s", resolution,
        "-r", str(framerate),
        "-g", str(framerate * 2),
        "-c:a", "aac", "-b:a", audio_bitrate, "-ar", "44100", "-ac", "2",
        "-fflags", "+genpts",
        "-reset_timestamps", "1",
        "-f", "mpegts",
        "pipe:1",
    ]

    def log_stderr(proc, name):
        def reader():
            for line in proc.stderr:
                logger.debug(f"{name}: {line.decode().strip()}")
        return reader

    try:
        if stream_url:
            source_proc = subprocess.Popen(
                source_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            stderr_thread = threading.Thread(target=log_stderr(source_proc, "ffmpeg-source"), daemon=True)
            stderr_thread.start()
            norm_proc = subprocess.Popen(
                normalize_cmd,
                stdin=source_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            source_proc.stdout.close()
            return (source_proc, norm_proc)
        else:
            ydl_proc = subprocess.Popen(
                source_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            stderr_thread = threading.Thread(target=log_stderr(ydl_proc, "yt-dlp"), daemon=True)
            stderr_thread.start()
            norm_proc = subprocess.Popen(
                normalize_cmd,
                stdin=ydl_proc.stdout,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            ydl_proc.stdout.close()
            return (ydl_proc, norm_proc)
    except Exception as e:
        logger.error(f"Failed to start camera feed: {e}")
        return None


def stop_camera_feed(camera_proc):
    """Stop a camera feed."""
    if not camera_proc:
        return
    ydl_proc, norm_proc = camera_proc
    for proc in [ydl_proc, norm_proc]:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=2)
            except:
                try:
                    proc.kill()
                except:
                    pass


# ─── Buffer Thread ─────────────────────────────────────────────────────

def buffer_writer():
    """
    Continuously reads from the active camera and writes to ffmpeg's stdin.
    This runs in a separate thread and never stops - we just swap which
    camera we read from when switching.
    """
    global current_camera_proc

    while not buffer_stop_event.is_set():
        # Get current camera process - re-check frequently to detect switches
        with camera_lock:
            cam_proc = current_camera_proc

        if not cam_proc:
            time.sleep(0.01)  # Short sleep when no camera
            continue

        _, norm_proc = cam_proc
        stdout = norm_proc.stdout

        # Read data from camera and write to ffmpeg
        # Use very short timeout so we re-check current_camera_proc frequently
        data_written = False
        try:
            for _ in range(10):  # Try multiple reads per camera check
                ready, _, _ = select.select([stdout], [], [], 0.01)
                if ready:
                    data = stdout.read(262144)  # Read up to 256KB chunks
                    if data and ffmpeg_proc and ffmpeg_proc.stdin:
                        try:
                            ffmpeg_proc.stdin.write(data)
                            ffmpeg_proc.stdin.flush()
                            data_written = True
                        except (BrokenPipeError, OSError):
                            # ffmpeg died
                            return
                    elif not data:
                        # Camera ended (EOF) - break to check for new camera
                        break
                else:
                    # No data available, check if camera changed
                    break
        except (ValueError, OSError):
            # Pipe closed or other error - camera probably stopped
            time.sleep(0.005)
        except Exception as e:
            logger.debug(f"Buffer writer error: {e}")
            time.sleep(0.005)

        # If we didn't write any data, do a tiny sleep to prevent busy-wait
        if not data_written:
            time.sleep(0.005)


# ─── FFmpeg ────────────────────────────────────────────────────────────

def start_ffmpeg():
    """Start the single persistent ffmpeg process reading from stdin via pipe."""
    global ffmpeg_proc

    ffmpeg_opts = config["ffmpeg"]
    stream_opts = config["stream"]
    audio_opts = config["audio"]
    preview_mode = stream_opts.get("preview_mode", True)

    video_bitrate = ffmpeg_opts.get("video_bitrate", "4500k")
    audio_bitrate = ffmpeg_opts.get("audio_bitrate", "128k")
    framerate = ffmpeg_opts.get("framerate", 30)
    music_volume = audio_opts.get("music_volume", 0.3)
    music_file = audio_opts.get("music_file", "")

    if music_file and not os.path.isabs(music_file):
        music_file = str(SCRIPT_DIR / music_file)

    ffmpeg_cmd = [
        "ffmpeg",
        "-hide_banner", "-loglevel", "error",
        "-nostdin",
        "-fflags", "+genpts+igndts+discardcorrupt+nobuffer",
        "-flags", "+low_delay",
        "-thread_queue_size", "4096",
        "-f", "mpegts",
        "-err_detect", "ignore_err",
        "-i", "pipe:0",  # Read from stdin
    ]

    if music_file and os.path.exists(music_file):
        ffmpeg_cmd.extend([
            "-stream_loop", "-1", "-i", music_file,
            "-filter_complex", f"[1:a]volume={music_volume}[music];[0:a][music]amix=inputs=2:duration=first[aout]",
            "-map", "0:v", "-map", "[aout]",
        ])

    ffmpeg_cmd.extend([
        "-c:v", "copy",
        "-c:a", "aac", "-b:a", audio_bitrate,
    ])

    if preview_mode:
        ffmpeg_cmd.extend([
            "-f", "hls",
            "-hls_time", "1",
            "-hls_list_size", "5",
            "-hls_flags", "delete_segments+omit_endlist",
            "-hls_segment_type", "mpegts",
            "-hls_segment_filename", os.path.join(hls_dir, "seg%05d.ts"),
            os.path.join(hls_dir, "stream.m3u8"),
        ])
    else:
        rtmp_url = stream_opts.get("youtube", {}).get("rtmp_url", "")
        stream_key = stream_opts.get("youtube", {}).get("stream_key", "")
        if not stream_key:
            logger.error("No stream key configured!")
            return False
        ffmpeg_cmd.extend(["-f", "flv", f"{rtmp_url}/{stream_key}"])

    logger.info(f"Starting persistent ffmpeg ({'HLS preview' if preview_mode else 'YouTube RTMP'})")

    ffmpeg_proc = subprocess.Popen(
        ffmpeg_cmd,
        stdin=subprocess.PIPE,  # We write to this
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(SCRIPT_DIR),
        start_new_session=True,
    )
    return True


def stop_ffmpeg():
    """Stop the persistent ffmpeg process."""
    global ffmpeg_proc
    if ffmpeg_proc and ffmpeg_proc.poll() is None:
        try:
            ffmpeg_proc.stdin.close()
        except:
            pass
        try:
            ffmpeg_proc.terminate()
            ffmpeg_proc.wait(timeout=5)
        except:
            try:
                ffmpeg_proc.kill()
            except:
                pass
    ffmpeg_proc = None


# ─── Main loop ──────────────────────────────────────────────────────────

def stream_loop():
    """Main loop that cycles through cameras."""
    global running, current_camera_index, current_camera_proc, buffer_thread

    switch_interval = config["stream"]["switch_interval"]
    cameras = config["cameras"]

    logger.info(f"Starting stream loop with {len(cameras)} cameras")
    logger.info(f"Each camera shown for {switch_interval} seconds")

    current_cam = get_current_camera()
    logger.info(f"Starting with camera: {current_cam['name']}")

    # Start first camera
    cam_proc = start_camera_feed(current_cam)
    if not cam_proc:
        logger.error("Failed to start first camera")
        running = False
        return

    with camera_lock:
        current_camera_proc = cam_proc

    # Start buffer thread
    buffer_stop_event.clear()
    buffer_thread = threading.Thread(target=buffer_writer, daemon=True)
    buffer_thread.start()

    while running and not stop_event.is_set():
        # Wait for the switch interval
        elapsed = 0
        while running and elapsed < switch_interval and not stop_event.is_set():
            # Check if ffmpeg died
            if ffmpeg_proc and ffmpeg_proc.poll() is not None:
                logger.error("ffmpeg process died unexpectedly")
                running = False
                break

            # Check if current camera died (offline)
            with camera_lock:
                cam = current_camera_proc
            if cam:
                ydl_proc, norm_proc = cam
                ydl_exit = ydl_proc.poll()
                norm_exit = norm_proc.poll()
                if ydl_exit is not None or norm_exit is not None:
                    logger.warning(f"Camera feed ended (offline?), switching early... ydl_exit={ydl_exit}, norm_exit={norm_exit}")
                    break

            time.sleep(1)
            elapsed += 1

        if not running or stop_event.is_set():
            break

        # Switch to next camera
        next_cam = advance_camera()
        logger.info(f"Starting transition to camera: {next_cam['name']} (stream will update in ~5 seconds)")

        # Start new camera BEFORE stopping old one (seamless transition)
        new_cam_proc = start_camera_feed(next_cam)
        if not new_cam_proc:
            logger.error(f"Failed to start camera: {next_cam['name']}, keeping current")
            continue

        # Wait for new camera to actually produce data before swapping
        # This prevents gaps in the stream
        _, new_norm_proc = new_cam_proc
        new_stdout = new_norm_proc.stdout
        data_ready = False
        wait_start = time.time()
        max_wait = 10  # Max 10 seconds to start producing data

        while time.time() - wait_start < max_wait and not stop_event.is_set():
            ready, _, _ = select.select([new_stdout], [], [], 0.1)
            if ready:
                data_ready = True
                break
            time.sleep(0.1)

        if not data_ready:
            logger.warning(f"Camera {next_cam['name']} slow to start, switching anyway")

        logger.info(f"Switching to camera: {next_cam['name']}")

        # Atomically swap to new camera
        old_cam_proc = None
        with camera_lock:
            old_cam_proc = current_camera_proc
            current_camera_proc = new_cam_proc

        # Stop old camera after swap
        if old_cam_proc:
            stop_camera_feed(old_cam_proc)

        # Allow time for ffmpeg to process buffered data and start new camera
        # HLS has inherent delay (segment_time * list_size + encoding buffer)
        time.sleep(2)

        logger.info(f"Switched to camera: {next_cam['name']} (new camera now visible)")

    # Cleanup
    buffer_stop_event.set()
    if buffer_thread:
        buffer_thread.join(timeout=2)

    with camera_lock:
        if current_camera_proc:
            stop_camera_feed(current_camera_proc)
            current_camera_proc = None

    logger.info("Stream loop ended")


def signal_handler(signum, frame):
    global running
    logger.info("Received shutdown signal")
    running = False
    stop_event.set()
    buffer_stop_event.set()


def main():
    global running, hls_dir

    # Parse CLI arguments
    parser = argparse.ArgumentParser(description='Webcam Streamer - Live stream switcher')
    parser.add_argument('--config', required=True, help='Path to stream config file')
    parser.add_argument('--port', type=int, default=8080, help='HTTP server port (default: 8080)')
    parser.add_argument('--base-config', default='configs/base.yaml', help='Base config to inherit from')
    args = parser.parse_args()

    setup_logging()
    load_config(args.config, args.base_config)

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    # Create temp dir for HLS segments
    temp_dir = tempfile.mkdtemp(prefix="livestream_")
    hls_dir = temp_dir
    logger.info(f"Using temp dir: {temp_dir}")

    # Start HLS preview server if enabled
    preview_mode = config["stream"].get("preview_mode", True)
    if preview_mode:
        http_port = args.port
        http_thread = threading.Thread(target=start_http_server, args=(http_port,), daemon=True)
        http_thread.start()

    running = True

    # Start ffmpeg
    if not start_ffmpeg():
        logger.error("Failed to start ffmpeg")
        sys.exit(1)

    # Give ffmpeg a moment to initialize
    time.sleep(1)

    try:
        stream_loop()
    except Exception as e:
        logger.error(f"Stream loop error: {e}")
    finally:
        running = False
        stop_event.set()
        buffer_stop_event.set()
        stop_ffmpeg()
        if buffer_thread:
            buffer_thread.join(timeout=2)
        with camera_lock:
            if current_camera_proc:
                stop_camera_feed(current_camera_proc)
        import shutil
        shutil.rmtree(temp_dir, ignore_errors=True)
        logger.info("Streamer stopped")


if __name__ == "__main__":
    main()
