#!/usr/bin/env bash
set -euo pipefail
pip install -r requirements.txt
mkdir -p bin
if [ ! -x bin/ffmpeg ]; then
  curl -fsSL -o bin/ffmpeg "https://github.com/eugeneware/ffmpeg-static/releases/download/b4.4/ffmpeg-linux-x64"
  chmod +x bin/ffmpeg
fi
