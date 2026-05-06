#!/usr/bin/env bash
# Build a real ShivaGPT.app from one Swift file. No Xcode project needed.
#
#   cd mac && ./build.sh
#   open ShivaGPT.app                  # or move it to /Applications
#
# Requires: Xcode Command Line Tools (`xcode-select --install`).

set -euo pipefail
cd "$(dirname "$0")"

APP_NAME="ShivaGPT"
APP="$APP_NAME.app"
SRC="ShivaGPT.swift"
SVG="../frontend/icon.svg"
BUILD="build"

if ! command -v swiftc >/dev/null 2>&1; then
  echo "swiftc not found. Install with: xcode-select --install"
  exit 1
fi

echo "==> Compile Swift"
mkdir -p "$BUILD"
swiftc -O -o "$BUILD/$APP_NAME" "$SRC" \
  -framework Cocoa -framework WebKit \
  -target "$(uname -m)-apple-macos11.0"

echo "==> Bundle .app"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"
cp "$BUILD/$APP_NAME" "$APP/Contents/MacOS/$APP_NAME"

cat > "$APP/Contents/Info.plist" <<EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>ShivaGPT</string>
  <key>CFBundleDisplayName</key><string>ShivaGPT</string>
  <key>CFBundleIdentifier</key><string>org.shiva.shivagpt</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundleShortVersionString</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>$APP_NAME</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>LSMinimumSystemVersion</key><string>11.0</string>
  <key>NSHighResolutionCapable</key><true/>
  <key>NSSupportsAutomaticGraphicsSwitching</key><true/>
  <!-- Allow plain HTTP to the LAN host (we don't run TLS). -->
  <key>NSAppTransportSecurity</key>
  <dict>
    <key>NSAllowsArbitraryLoads</key><true/>
  </dict>
</dict>
</plist>
EOF

echo "==> Build app icon from $SVG"
ICONSET="$BUILD/AppIcon.iconset"
rm -rf "$ICONSET" && mkdir -p "$ICONSET"

# Render the SVG to a master 1024x1024 PNG. macOS QuickLook handles SVG.
MASTER="$BUILD/icon-1024.png"
rendered=0
if command -v rsvg-convert >/dev/null 2>&1; then
  rsvg-convert -w 1024 -h 1024 "$SVG" -o "$MASTER" && rendered=1
fi
if [ "$rendered" = 0 ]; then
  # qlmanage is on every Mac
  qlmanage -t -s 1024 -o "$BUILD" "$SVG" >/dev/null 2>&1 || true
  if [ -f "$BUILD/icon.svg.png" ]; then
    mv "$BUILD/icon.svg.png" "$MASTER"; rendered=1
  fi
fi
if [ "$rendered" = 0 ]; then
  # Last resort: ask the running server for a flat PNG
  echo "  (no SVG renderer available — falling back to server PNG)"
  curl -fsS "http://kailash:8000/icon-512.png" -o "$MASTER" || true
fi

if [ ! -f "$MASTER" ]; then
  echo "  WARNING: couldn't generate any icon — app will use a generic one"
else
  for s in 16 32 64 128 256 512 1024; do
    sips -z $s $s "$MASTER" --out "$ICONSET/tmp_$s.png" >/dev/null
  done
  cp "$ICONSET/tmp_16.png"   "$ICONSET/icon_16x16.png"
  cp "$ICONSET/tmp_32.png"   "$ICONSET/icon_16x16@2x.png"
  cp "$ICONSET/tmp_32.png"   "$ICONSET/icon_32x32.png"
  cp "$ICONSET/tmp_64.png"   "$ICONSET/icon_32x32@2x.png"
  cp "$ICONSET/tmp_128.png"  "$ICONSET/icon_128x128.png"
  cp "$ICONSET/tmp_256.png"  "$ICONSET/icon_128x128@2x.png"
  cp "$ICONSET/tmp_256.png"  "$ICONSET/icon_256x256.png"
  cp "$ICONSET/tmp_512.png"  "$ICONSET/icon_256x256@2x.png"
  cp "$ICONSET/tmp_512.png"  "$ICONSET/icon_512x512.png"
  cp "$ICONSET/tmp_1024.png" "$ICONSET/icon_512x512@2x.png"
  rm -f "$ICONSET"/tmp_*.png
  iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"
fi

echo "==> Ad-hoc code-sign"
codesign --force --deep --sign - "$APP" >/dev/null 2>&1 || \
  echo "  (codesign warning — app will still run; Gatekeeper may prompt once)"

echo
echo "==> Built $(pwd)/$APP"
echo
echo "Run it:        open $APP"
echo "Install:       mv $APP /Applications/"
echo "Different URL: SHIVAGPT_URL=http://otherbox:8000 open ./$APP"
echo "Change URL later: in the app, ShivaGPT menu → Server URL… (⌘,)"
