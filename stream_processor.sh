#!/bin/bash
#
# FFmpeg stream processor for Live Stream Switcher
# Uses yt-dlp to get fresh stream URLs
#

set -e

VIDEO_ID=""
MUSIC_FILE=""
MUSIC_VOL="0.3"
CAM_AUDIO_VOL="0.7"
SIZE="1920x1080"
VB="4500k"
AB="128k"
RATE="30"
OUTPUT=""
PREVIEW=false

while [[ $# -gt 0 ]]; do
    case $1 in
        -i)
            VIDEO_ID="$2"
            shift 2
            ;;
        -m)
            MUSIC_FILE="$2"
            shift 2
            ;;
        -mv)
            MUSIC_VOL="$2"
            shift 2
            ;;
        -av)
            CAM_AUDIO_VOL="$2"
            shift 2
            ;;
        -s)
            SIZE="$2"
            shift 2
            ;;
        -b:v)
            VB="$2"
            shift 2
            ;;
        -b:a)
            AB="$2"
            shift 2
            ;;
        -r)
            RATE="$2"
            shift 2
            ;;
        -o)
            OUTPUT="$2"
            shift 2
            ;;
        -p)
            PREVIEW=true
            shift
            ;;
        *)
            echo "Unknown option: $1"
            exit 1
            ;;
    esac
done

echo "Starting stream for video ID: $VIDEO_ID"

export PATH="$HOME/Library/Python/3.12/bin:$PATH"

if [ "$PREVIEW" = true ]; then
    # Preview mode: use yt-dlp to get direct stream and pipe to ffplay
    W=$(( ${SIZE%x*} / 2 ))
    H=$(( ${SIZE#*x} / 2 ))
    
    exec yt-dlp -f "bestvideo[width<=?1280][height<=?720]+bestaudio/best" \
        --hls-prefer-ffmpeg \
        -o - \
        "https://www.youtube.com/watch?v=$VIDEO_ID" | \
        ffplay -fflags nobuffer -flags low_delay -x "$W" -y "$H" -i -
else
    # Live streaming mode
    exec yt-dlp -f "bestvideo+bestaudio/best" \
        --hls-prefer-ffmpeg \
        -o - \
        "https://www.youtube.com/watch?v=$VIDEO_ID" | \
        ffmpeg -i - \
        -c:v libx264 -preset veryfast -b:v $VB -maxrate $VB -g $(($RATE * 2)) \
        -c:a aac -b:a $AB -r $RATE \
        -f flv "$OUTPUT"
fi
