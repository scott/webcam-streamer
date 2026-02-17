#!/usr/bin/env python3
"""
Live Stream Switcher
Cycles through YouTube live camera streams with seamless switching.
"""

import os
import sys
import time
import signal
import subprocess
import threading
from pathlib import Path

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

current_ffmpeg = None
buffer_processes = {}


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


def get_current_camera():
    global current_camera_index
    cameras = config["cameras"]
    return cameras[current_camera_index % len(cameras)]


def next_camera():
    global current_camera_index
    cameras = config["cameras"]
    current_camera_index = (current_camera_index + 1) % len(cameras)
    return get_current_camera()


def get_next_camera():
    cameras = config["cameras"]
    return cameras[(current_camera_index + 1) % len(cameras)]


def get_stream_url(youtube_id):
    """Extract direct HLS stream URL from YouTube video."""
    ydl_opts = {
        "format": "best",
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
    }
    
    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(
                f"https://www.youtube.com/watch?v={youtube_id}",
                download=False
            )
            if info:
                return info.get("url")
    except Exception as e:
        logger.error(f"Error fetching stream URL: {e}")
    return None


def start_stream_proc(youtube_id, preview=True):
    """Start yt-dlp piped to ffmpeg."""
    ffmpeg_opts = config["ffmpeg"]
    stream_opts = config["stream"]
    audio_opts = config["audio"]
    
    res = ffmpeg_opts.get("resolution", "1920x1080").split("x")
    width, height = int(res[0]), int(res[1])
    video_bitrate = ffmpeg_opts.get("video_bitrate", "4500k")
    audio_bitrate = ffmpeg_opts.get("audio_bitrate", "128k")
    framerate = ffmpeg_opts.get("framerate", 30)
    
    music_volume = audio_opts.get("music_volume", 0.3)
    music_file = audio_opts.get("music_file", "")
    
    if music_file and not os.path.isabs(music_file):
        music_file = str(SCRIPT_DIR / music_file)
    
    ydl_cmd = [
        "yt-dlp",
        "-f", "best",
        "--hls-prefer-ffmpeg",
        "-o", "-",
        f"https://www.youtube.com/watch?v={youtube_id}"
    ]
    
    ffmpeg_cmd = [
        "ffmpeg",
        "-re",
        "-i", "pipe:0",
    ]
    
    if music_file and os.path.exists(music_file):
        ffmpeg_cmd.extend([
            "-stream_loop", "0", "-i", music_file,
            "-filter_complex", f"[1:a]volume={music_volume}[music];[0:a][music]amix=inputs=2:duration=first[aout]",
            "-map", "0:v", "-map", "[aout]",
        ])
    else:
        ffmpeg_cmd.extend(["-c:v", "copy"])
    
    ffmpeg_cmd.extend([
        "-c:v", "libx264", "-preset", "veryfast", "-b:v", video_bitrate, "-maxrate", video_bitrate, "-g", str(framerate * 2),
        "-c:a", "aac", "-b:a", audio_bitrate,
        "-r", str(framerate),
    ])
    
    if preview:
        width_half = width // 2
        height_half = height // 2
        ffmpeg_cmd.extend([
            "-f", "null", "-"
        ])
    else:
        rtmp_url = stream_opts.get("youtube", {}).get("rtmp_url", "")
        stream_key = stream_opts.get("youtube", {}).get("stream_key", "")
        if stream_key:
            ffmpeg_cmd.extend(["-f", "flv", f"{rtmp_url}/{stream_key}"])
        else:
            logger.error("No stream key configured!")
            return None
    
    try:
        ydl_proc = subprocess.Popen(
            ydl_cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        
        ffmpeg_proc = subprocess.Popen(
            ffmpeg_cmd,
            stdin=ydl_proc.stdout,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            cwd=str(SCRIPT_DIR)
        )
        
        ydl_proc.stdout.close()
        
        return {"ydl": ydl_proc, "ffmpeg": ffmpeg_proc}
        
    except Exception as e:
        logger.error(f"Failed to start stream: {e}")
        return None


def stop_stream_proc(proc_dict):
    if not proc_dict:
        return
    
    try:
        if proc_dict.get("preview"):
            proc_dict["ffmpeg"].terminate()
            proc_dict["ffmpeg"].wait(timeout=3)
        else:
            proc_dict["ffmpeg"].terminate()
            proc_dict["ydl"].terminate()
            proc_dict["ffmpeg"].wait(timeout=3)
            proc_dict["ydl"].wait(timeout=3)
    except:
        proc_dict["ffmpeg"].kill()
        if proc_dict.get("ydl"):
            proc_dict["ydl"].kill()


def stream_loop():
    """Main loop that cycles through cameras."""
    global running, current_camera_index, current_ffmpeg
    
    switch_interval = config["stream"]["switch_interval"]
    cameras = config["cameras"]
    preview_mode = config["stream"].get("preview_mode", True)
    
    logger.info(f"Starting stream loop with {len(cameras)} cameras")
    logger.info(f"Each camera shown for {switch_interval} seconds")
    
    current_cam = get_current_camera()
    logger.info(f"Starting with camera: {current_cam['name']}")
    
    current_ffmpeg = start_stream_proc(current_cam["youtube_id"], preview_mode)
    if not current_ffmpeg:
        logger.error("Failed to start initial stream")
        return
    
    prebuffered_stream = {}
    prebuffer_camera_id = None
    
    while running and not stop_event.is_set():
        elapsed = 0
        while running and elapsed < switch_interval and not stop_event.is_set():
            if current_ffmpeg and current_ffmpeg["ffmpeg"].poll() is not None:
                logger.warning("Stream process ended, restarting...")
                current_cam = get_current_camera()
                current_ffmpeg = start_stream_proc(current_cam["youtube_id"], preview_mode)
                if not current_ffmpeg:
                    time.sleep(5)
                    break
            
            time.sleep(1)
            elapsed += 1
            
            if elapsed >= switch_interval - 3 and not prebuffered_stream:
                next_cam = get_next_camera()
                logger.info(f"Pre-buffering next camera: {next_cam['name']}")
                prebuffered_stream = start_stream_proc(next_cam["youtube_id"], preview_mode)
                prebuffer_camera_id = next_cam["youtube_id"]
                time.sleep(1)
        
        if not running or stop_event.is_set():
            break
        
        next_cam = next_camera()
        logger.info(f"Switching to camera: {next_cam['name']}")
        
        if current_ffmpeg:
            stop_stream_proc(current_ffmpeg)
        
        if prebuffered_stream and prebuffer_camera_id == next_cam["youtube_id"]:
            current_ffmpeg = prebuffered_stream
            prebuffered_stream = {}
            prebuffer_camera_id = None
        else:
            if prebuffered_stream:
                stop_stream_proc(prebuffered_stream)
            current_ffmpeg = start_stream_proc(next_cam["youtube_id"], preview_mode)
        
        if not current_ffmpeg:
            logger.error("Failed to start stream after switch, retrying in 5s")
            time.sleep(5)
    
    if current_ffmpeg:
        stop_stream_proc(current_ffmpeg)
    if prebuffered_stream:
        stop_stream_proc(prebuffered_stream)
    
    logger.info("Stream loop ended")


def signal_handler(signum, frame):
    global running
    logger.info("Received shutdown signal")
    running = False
    stop_event.set()


def main():
    global running
    
    setup_logging()
    
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    
    logger.info("=" * 50)
    logger.info("Live Stream Switcher")
    logger.info("=" * 50)
    
    load_config()
    
    music_file = config["audio"].get("music_file", "")
    if music_file:
        if not os.path.isabs(music_file):
            music_file = str(SCRIPT_DIR / music_file)
        if not os.path.exists(music_file):
            logger.warning(f"Music file not found: {music_file}")
    
    try:
        running = True
        stream_loop()
    finally:
        stop_event.set()
        logger.info("Streamer stopped")


if __name__ == "__main__":
    main()
