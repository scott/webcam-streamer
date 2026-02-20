#!/bin/bash
# Setup script for webcam-streamer on Ubuntu 24.04

set -e

echo "=== webcam-streamer setup ==="

# Update and install dependencies
echo "Installing dependencies..."
apt update
apt install -y \
    ffmpeg \
    python3 \
    python3-pip \
    python3-venv \
    git \
    curl \
    ca-certificates \
    gnupg

# Install Node.js 20.x
echo "Installing Node.js..."
mkdir -p /etc/apt/keyrings
curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg
echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_20.x nodistro main" | tee /etc/apt/sources.list.d/nodesource.list
apt update
apt install -y nodejs

# Create app directory
mkdir -p /app
cd /app

# Clone or copy repo (uncomment if needed)
# git clone https://github.com/scott/webcam-streamer.git .
# Or copy files to /app

# Create virtual environment
echo "Setting up Python environment..."
python3 -m venv venv
source venv/bin/activate

# Install Python dependencies
pip install --upgrade pip
pip install -r requirements.txt

# Set up environment variables
echo "Setting up environment..."
# Create .env file with your secrets
cat > .env << 'EOF'
# YouTube stream key (from YouTube Studio → Content → Live streaming)
YOUTUBE_KEY_SKI_RESORT=your_stream_key_here

# YouTube Data API key (optional, for better reliability)
YOUTUBE_API_KEY=your_api_key_here

# YouTube cookies (optional, for bypassing bot detection)
# Export from browser and paste content below
YOUTUBE_COOKIES=
EOF

echo "=== Setup complete! ==="
echo ""
echo "To run the streamer:"
echo "  cd /app"
echo "  source venv/bin/activate"
echo "  python stream_manager.py --config configs/streams/ski-resort.yaml"
echo ""
echo "Edit .env file with your actual secrets before running!"
