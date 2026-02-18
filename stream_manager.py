#!/usr/bin/env python3
"""
Live Stream Switcher
Cycles through YouTube live camera streams with seamless switching.

Uses a single persistent ffmpeg process reading from a named pipe (FIFO).
Camera switches are done by swapping which yt-dlp process writes to the pipe.
This keeps the output stream (HLS preview or YouTube RTMP) continuous.
"""

import os
import sys
import time
import signal
import subprocess
import threading
import tempfile
from pathlib import Path
from http.server import HTTPServer, BaseHTTPRequestHandler

import yaml
import yt_dlp
import colorlog
from colorlog import ColoredFormatter

CONFIG_FILE = "config.yaml"
SCRIPT_DIR = Path(__file__).parent

logger = None
config = None
current_camera_index = 0
running = False
stop_event = threading.Event()

http_server = None
hls_dir = None

# Persistent ffmpeg process and FIFO
ffmpeg_proc = None
fifo_path = None
current_ydl = None
current_feeder_ffmpeg = None
feeder_thread = None
feeder_lock = threading.Lock()


# ─── HLS preview server ────────────────────────────────────────────────

class StreamHandler(BaseHTTPRequestHandler):
    def do_GET(self):
        if self.path == "/" or self.path == "/index.html":
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
            filename = os.path.basename(self.path)
            # Map live.m3u8 to ffmpeg's stream.m3u8
            if filename == "live.m3u8":
                filename = "stream.m3u8"
            filepath = os.path.join(hls_dir, filename)
            if os.path.exists(filepath):
                self.send_response(200)
                if filename.endswith(".m3u8"):
                    self.send_header("Content-Type", "application/vnd.apple.mpegurl")
                elif filename.endswith(".ts"):
                    self.send_header("Content-Type", "video/mp2t")
                self.send_header("Cache-Control", "no-cache")
                self.send_header("Access-Control-Allow-Origin", "*")
                self.end_headers()
                with open(filepath, "rb") as f:
                    self.wfile.write(f.read())
            else:
                self.send_response(404)
                self.end_headers()
        else:
            self.send_response(302)
            self.send_header("Location", "/")
            self.end_headers()

    def log_message(self, format, *args):
        pass


def start_http_server():
    global http_server
    http_server = HTTPServer(("0.0.0.0", 8080), StreamHandler)
    http_server.serve_forever()


# ─── Core setup ─────────────────────────────────────────────────────────

def setup_logging():
    global logger
    formatter = ColoredFormatter(
        "%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%H:%M:%S",
    )
    handler = colorlog.StreamHandler()
    handler.setFormatter(formatter)
    logger = colorlog.getLogger()
    logger.setLevel("INFO")
    logger.addHandler(handler)


def load_config():
    global config
    config_path = SCRIPT_DIR / CONFIG_FILE
    if not config_path.exists():
        logger.error(f"Config file not found: {config_path}")
        sys.exit(1)

    with open(config_path, "r") as f:
        config = yaml.safe_load(f)

    logger.info(f"Loaded config with {len(config['cameras'])} cameras")
    logger.info(f"Switch interval: {config['stream']['switch_interval']} seconds")
    logger.info(f"Preview mode: {config['stream']['preview_mode']}")


# ─── Camera helpers ─────────────────────────────────────────────────────

def get_current_camera():
    cameras = config["cameras"]
    return cameras[current_camera_index % len(cameras)]


def advance_camera():
    global current_camera_index
    cameras = config["cameras"]
    current_camera_index = (current_camera_index + 1) % len(cameras)
    return get_current_camera()


# ─── FIFO + persistent ffmpeg ───────────────────────────────────────────

def create_fifo():
    """Create a named pipe for feeding data to ffmpeg."""
    global fifo_path
    tmp_dir = tempfile.mkdtemp(prefix="livestream_fifo_")
    fifo_path = os.path.join(tmp_dir, "feed.pipe")
    os.mkfifo(fifo_path)
    logger.info(f"Created FIFO: {fifo_path}")


def start_ffmpeg():
    """Start the single persistent ffmpeg process reading from the FIFO."""
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
        "-re",
        "-fflags", "+genpts+igndts+discardcorrupt",
        "-rw_timeout", "10000000",
        "-f", "mpegts",
        "-i", fifo_path,
    ]

    if music_file and os.path.exists(music_file):
        ffmpeg_cmd.extend([
            "-stream_loop", "-1", "-i", music_file,
            "-filter_complex", f"[1:a]volume={music_volume}[music];[0:a][music]amix=inputs=2:duration=first[aout]",
            "-map", "0:v", "-map", "[aout]",
        ])

    ffmpeg_cmd.extend([
        "-c:v", "libx264", "-preset", "veryfast",
        "-b:v", video_bitrate, "-maxrate", video_bitrate,
        "-g", str(framerate * 2),
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-r", str(framerate),
    ])

    if preview_mode:
        ffmpeg_cmd.extend([
            "-f", "hls",
            "-hls_time", "2",
            "-hls_list_size", "10",
            "-hls_flags", "delete_segments",
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
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        cwd=str(SCRIPT_DIR),
        start_new_session=True,
    )
    return True


def start_ydl_feeder(youtube_id):
    """Start a yt-dlp process piped through an intermediate ffmpeg that
    normalizes the stream to raw mpegts before writing to the FIFO.
    This ensures consistent format/timestamps across camera switches."""
    global current_ydl, current_feeder_ffmpeg, feeder_thread

    ffmpeg_opts = config["ffmpeg"]
    framerate = ffmpeg_opts.get("framerate", 30)

    def _feed():
        global current_ydl, current_feeder_ffmpeg
        ydl_cmd = [
            "yt-dlp",
            "-f", "best",
            "--hls-prefer-ffmpeg",
            "-o", "-",
            f"https://www.youtube.com/watch?v={youtube_id}",
        ]

        # Intermediate ffmpeg: remux to mpegts without re-encoding
        # The persistent ffmpeg handles the actual encode
        normalize_cmd = [
            "ffmpeg",
            "-i", "pipe:0",
            "-c:v", "copy",
            "-c:a", "aac", "-ar", "44100", "-ac", "2",
            "-fflags", "+genpts",
            "-reset_timestamps", "1",
            "-f", "mpegts",
            "pipe:1",
        ]

        try:
            fifo_fd = os.open(fifo_path, os.O_WRONLY)
        except OSError as e:
            logger.error(f"Failed to open FIFO for writing: {e}")
            return

        try:
            ydl_proc = subprocess.Popen(
                ydl_cmd,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            norm_proc = subprocess.Popen(
                normalize_cmd,
                stdin=ydl_proc.stdout,
                stdout=fifo_fd,
                stderr=subprocess.PIPE,
                start_new_session=True,
            )
            # Allow yt-dlp to receive SIGPIPE if normalize proc exits
            ydl_proc.stdout.close()

            with feeder_lock:
                current_ydl = ydl_proc
                current_feeder_ffmpeg = norm_proc
            norm_proc.wait()
        except Exception as e:
            logger.error(f"yt-dlp feeder error: {e}")
        finally:
            try:
                os.close(fifo_fd)
            except:
                pass

    feeder_thread = threading.Thread(target=_feed, daemon=True)
    feeder_thread.start()
    # Give yt-dlp + normalizer a moment to start
    time.sleep(2)


def stop_ydl_feeder():
    """Stop the current yt-dlp feeder and its intermediate ffmpeg process."""
    global current_ydl, current_feeder_ffmpeg
    with feeder_lock:
        ydl_proc = current_ydl
        norm_proc = current_feeder_ffmpeg
        current_ydl = None
        current_feeder_ffmpeg = None

    for proc in [ydl_proc, norm_proc]:
        if proc and proc.poll() is None:
            try:
                proc.terminate()
                proc.wait(timeout=3)
            except:
                try:
                    proc.kill()
                except:
                    pass


def stop_ffmpeg():
    """Stop the persistent ffmpeg process."""
    global ffmpeg_proc
    if ffmpeg_proc and ffmpeg_proc.poll() is None:
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
    global running, current_camera_index

    switch_interval = config["stream"]["switch_interval"]
    cameras = config["cameras"]

    logger.info(f"Starting stream loop with {len(cameras)} cameras")
    logger.info(f"Each camera shown for {switch_interval} seconds")

    current_cam = get_current_camera()
    logger.info(f"Starting with camera: {current_cam['name']}")

    # Start yt-dlp feeding into the FIFO -> ffmpeg reads from it
    start_ydl_feeder(current_cam["youtube_id"])

    while running and not stop_event.is_set():
        # Wait for the switch interval
        elapsed = 0
        while running and elapsed < switch_interval and not stop_event.is_set():
            # Check if ffmpeg died
            if ffmpeg_proc and ffmpeg_proc.poll() is not None:
                logger.error("ffmpeg process died unexpectedly")
                running = False
                break

            # Check if yt-dlp feeder or normalizer died (camera may be offline)
            with feeder_lock:
                ydl = current_ydl
                norm = current_feeder_ffmpeg
            if (ydl and ydl.poll() is not None) or (norm and norm.poll() is not None):
                logger.warning("yt-dlp feeder ended (camera offline?), switching early...")
                break

            time.sleep(1)
            elapsed += 1

        if not running or stop_event.is_set():
            break

        # Switch to next camera
        next_cam = advance_camera()
        logger.info(f"Switching to camera: {next_cam['name']}")

        # Stop old feeder, start new one - ffmpeg keeps running
        stop_ydl_feeder()
        start_ydl_feeder(next_cam["youtube_id"])

    # Cleanup
    stop_ydl_feeder()

    logger.info("Stream loop ended")


def signal_handler(signum, frame):
    global running
    logger.info("Received shutdown signal")
    running = False
    stop_event.set()


def main():
    global running, hls_dir

    setup_logging()

    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    logger.info("=" * 50)
    logger.info("Live Stream Switcher")
    logger.info("=" * 50)

    load_config()

    preview_mode = config["stream"].get("preview_mode", True)

    if preview_mode:
        hls_dir = tempfile.mkdtemp(prefix="livestream_hls_")
        logger.info(f"HLS segments directory: {hls_dir}")

        server_thread = threading.Thread(target=start_http_server, daemon=True)
        server_thread.start()
        logger.info("HTTP preview server started at http://localhost:8080")

    music_file = config["audio"].get("music_file", "")
    if music_file:
        if not os.path.isabs(music_file):
            music_file = str(SCRIPT_DIR / music_file)
        if not os.path.exists(music_file):
            logger.warning(f"Music file not found: {music_file}")

    # Create FIFO and start the single persistent ffmpeg
    create_fifo()

    running = True

    # Start ffmpeg in a background thread because it blocks until the FIFO
    # is opened for writing (by the first yt-dlp feeder)
    ffmpeg_thread = threading.Thread(target=start_ffmpeg, daemon=True)
    ffmpeg_thread.start()

    try:
        stream_loop()
        ffmpeg_thread.join(timeout=2)
    finally:
        stop_event.set()
        stop_ffmpeg()
        # Clean up FIFO
        if fifo_path:
            try:
                os.unlink(fifo_path)
                os.rmdir(os.path.dirname(fifo_path))
            except:
                pass
        logger.info("Streamer stopped")


if __name__ == "__main__":
    main()
