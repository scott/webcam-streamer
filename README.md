# Live Stream Switcher

A Python application that cycles through live camera streams with background music. Can run in preview mode locally or stream to YouTube Live.

## Features

- Cycles through a set of live cameras
- Configurable switch interval (default: 10 seconds)
- Background ambient music support
- Mixes camera audio with background music
- Preview mode for local testing
- YouTube Live streaming support

## Requirements

- Python 3.9+
- FFmpeg
- yt-dlp (installed via pip)

## Installation

1. Install Python dependencies:
```bash
pip install -r requirements.txt
```

2. Install FFmpeg:
```bash
# macOS
brew install ffmpeg

# Ubuntu/Debian
sudo apt install ffmpeg

# Windows
# Download from https://ffmpeg.org/download.html
```

3. (Optional) Add background music:
```bash
# Place an ambient music file in music/ambient.mp3
# Supported formats: mp3, wav, ogg, flac
```

## Usage

### Preview Mode (Test Locally)

Run in preview mode to test on your computer:
```bash
python stream_manager.py
```

This will open a preview window showing the camera feeds. The stream cycles through each camera for 10 seconds.

### Streaming to YouTube Live

1. Create a YouTube channel (if you don't have one)
2. Enable live streaming (may take 24 hours to activate)
3. Get your stream key from YouTube Studio > Live streaming > Stream setup
4. Edit `config.yaml` and add your stream key:
```yaml
stream:
  preview_mode: false
  youtube:
    rtmp_url: "rtmps://a.rtmp.youtube.com/live2"
    stream_key: "your-stream-key-here"
```

5. Start streaming:
```bash
python stream_manager.py
```

## Docker Deployment

### Prerequisites

- Docker
- Docker Compose

### Quick Start

1. Edit `config.yaml` with your cameras and stream settings

2. Build and run:
```bash
docker-compose up -d
```

3. View logs:
```bash
docker-compose logs -f
```

4. Stop:
```bash
docker-compose down
```

### Building the Image

```bash
docker build -t live-streamer .
```

## Configuration

Edit `config.yaml` to customize:

| Setting | Description | Default |
|---------|-------------|---------|
| `switch_interval` | Seconds per camera | 10 |
| `music_volume` | Background music volume (0-1) | 0.3 |
| `camera_audio_volume` | Camera audio volume (0-1) | 0.7 |
| `include_camera_audio` | Include camera audio | true |
| `resolution` | Output resolution | 1920x1080 |
| `video_bitrate` | Video bitrate | 4500k |

### Adding/Removing Cameras

Edit the `cameras` section in `config.yaml`:
```yaml
cameras:
  - name: "Camera Name"
    youtube_id: "VIDEO_ID"
```

The YouTube ID is the part after `v=` in the URL:
- `https://www.youtube.com/watch?v=uE_ent5rC3Y` → ID: `uE_ent5rC3Y`

## Camera Sources

The default cameras are configured in `config.yaml`. Edit the file to add or remove cameras.

## Troubleshooting

### FFmpeg not found
Make sure FFmpeg is installed and in your PATH. Verify with:
```bash
ffmpeg -version
```

### No stream URL found
Some cameras may go offline. The streamer will automatically skip to the next camera.

### Audio issues
- Ensure FFmpeg is compiled with AAC support
- Check that music file exists at `music/ambient.mp3`

### YouTube streaming issues
- Verify stream key is correct
- Ensure YouTube live streaming is enabled
- Check network firewall allows RTMP (port 1935)

## License

MIT
