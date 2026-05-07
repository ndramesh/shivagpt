#!/usr/bin/env bash
set -e

# Setup directory
DATA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../data" && pwd)"
if [ -z "$DATA_DIR" ]; then
    # Fallback if data doesn't exist yet
    mkdir -p "$(dirname "${BASH_SOURCE[0]}")/../data"
    DATA_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")/../data" && pwd)"
fi

UPSCAYL_DIR="$DATA_DIR/upscayl"
mkdir -p "$UPSCAYL_DIR"
cd "$UPSCAYL_DIR"

# Download the zip provided by the user
ZIP_URL="https://github.com/upscayl/upscayl/releases/download/v2.15.0/upscayl-2.15.0-linux.zip"
ZIP_FILE="upscayl-2.15.0-linux.zip"

echo "Downloading Upscayl (Linux) from $ZIP_URL..."
wget -q -O "$ZIP_FILE" "$ZIP_URL"

echo "Extracting..."
unzip -q -o "$ZIP_FILE"

# Clean up
rm "$ZIP_FILE"

# Make binary executable
chmod +x resources/bin/upscayl-bin

echo "Done! Upscayl is installed in $UPSCAYL_DIR"
