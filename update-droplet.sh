#!/bin/bash
# Update script for webcam-streamer

cd /app

# Pull latest code
git pull origin master

# Restart service
systemctl restart webcam-streamer

# Check status
systemctl status webcam-streamer
