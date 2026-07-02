#!/usr/bin/env bash
# Build FillerCoach.app — a Dock-launchable wrapper around coach.py.
# Rebuild after moving the project folder (the launcher embeds this path).
set -euo pipefail
cd "$(dirname "$0")"
PROJECT_DIR="$(pwd)"
APP="FillerCoach.app"

if [ ! -d ".venv" ]; then
  echo "No .venv found. Run ./setup.sh first."
  exit 1
fi

rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$APP/Contents/Resources"

# --- icon: dark rounded square with a struck-through "um" ---
.venv/bin/python - <<'PY'
from Cocoa import (NSImage, NSMakeRect, NSColor, NSBezierPath, NSFont,
                   NSMakeSize, NSBitmapImageRep, NSPNGFileType,
                   NSFontAttributeName, NSForegroundColorAttributeName)
from Foundation import NSString

S = 1024
img = NSImage.alloc().initWithSize_(NSMakeSize(S, S))
img.lockFocus()
# background
NSColor.colorWithCalibratedRed_green_blue_alpha_(0.067, 0.075, 0.102, 1.0).setFill()
NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
    NSMakeRect(64, 64, S-128, S-128), 180, 180).fill()
# "um" text
text = NSString.stringWithString_("um")
attrs = {NSFontAttributeName: NSFont.boldSystemFontOfSize_(430),
         NSForegroundColorAttributeName:
             NSColor.colorWithCalibratedRed_green_blue_alpha_(0.91, 0.925, 0.96, 1.0)}
size = text.sizeWithAttributes_(attrs)
text.drawAtPoint_withAttributes_(((S-size.width)/2, (S-size.height)/2 + 40), attrs)
# red strike-through
NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.36, 0.42, 1.0).setFill()
p = NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
    NSMakeRect(200, 470, S-400, 56), 28, 28)
p.fill()
img.unlockFocus()
tiff = img.TIFFRepresentation()
rep = NSBitmapImageRep.imageRepWithData_(tiff)
png = rep.representationUsingType_properties_(NSPNGFileType, None)
png.writeToFile_atomically_("/tmp/fillercoach_icon.png", True)
print("icon png written")
PY

ICONSET="/tmp/FillerCoach.iconset"
rm -rf "$ICONSET" && mkdir -p "$ICONSET"
for sz in 16 32 128 256 512; do
  sips -z $sz $sz /tmp/fillercoach_icon.png --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null
  dbl=$((sz*2))
  sips -z $dbl $dbl /tmp/fillercoach_icon.png --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$APP/Contents/Resources/AppIcon.icns"

# --- Info.plist ---
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>FillerCoach</string>
  <key>CFBundleDisplayName</key><string>Filler Coach</string>
  <key>CFBundleIdentifier</key><string>local.fillercoach</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>FillerCoach</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>NSMicrophoneUsageDescription</key>
  <string>Filler Coach listens to your microphone locally to count filler words. Audio never leaves this Mac.</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# --- launcher (embeds absolute project path) ---
cat > "$APP/Contents/MacOS/FillerCoach" <<LAUNCH
#!/bin/bash
exec "$PROJECT_DIR/.venv/bin/python" "$PROJECT_DIR/coach.py" --dock
LAUNCH
chmod +x "$APP/Contents/MacOS/FillerCoach"

echo ""
echo "==> Built $PROJECT_DIR/$APP"
echo "    • Double-click it, or drag it to /Applications or the Dock."
echo "    • First launch: right-click → Open (unsigned app), then allow Microphone."
