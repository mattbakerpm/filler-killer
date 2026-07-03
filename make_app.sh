#!/usr/bin/env bash
# Build FillerCoach.app — a SELF-CONTAINED Dock app.
#
# The bundle carries its own Python venv, the Vosk model, coach.py, and its
# own config.json. Nothing is read from this project folder at runtime, so
# macOS's Documents-folder privacy protection can't kill it when launched
# from the Dock, and the app keeps working even if you move this project.
#
# Rebuild after changing coach.py to update the app.
#
#   ./make_app.sh             build ./FillerCoach.app
#   ./make_app.sh --install   build + install to /Applications + launch
set -euo pipefail
cd "$(dirname "$0")"
APP="FillerCoach.app"
RES="$APP/Contents/Resources"

if [ ! -d "model" ]; then
  echo "No ./model found. Run ./setup.sh first."
  exit 1
fi

echo "==> Building $APP (self-contained)"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$RES/app"

# --- embedded venv (symlinked interpreter: Apple's python3 can't self-copy;
#     the symlink targets the system framework, NOT this project folder, so
#     the app still never reads ~/Documents at launch) ---
echo "==> Creating embedded venv"
/usr/bin/python3 -m venv "$RES/venv"
"$RES/venv/bin/python" -m pip install --quiet --upgrade pip
echo "==> Installing packages into the app (vosk, sounddevice, pyobjc Cocoa)"
"$RES/venv/bin/python" -m pip install --quiet vosk sounddevice pyobjc-framework-Cocoa

# --- app code, config, model ---
cp coach.py "$RES/app/"
cp config.json "$RES/app/"
echo "==> Copying Vosk model (~40MB)"
cp -R model "$RES/app/model"

# --- icon: dark rounded square with a struck-through "um" ---
"$RES/venv/bin/python" - <<'PY'
from Cocoa import (NSImage, NSMakeRect, NSColor, NSBezierPath, NSFont,
                   NSMakeSize, NSBitmapImageRep, NSPNGFileType,
                   NSFontAttributeName, NSForegroundColorAttributeName)
from Foundation import NSString

S = 1024
img = NSImage.alloc().initWithSize_(NSMakeSize(S, S))
img.lockFocus()
NSColor.colorWithCalibratedRed_green_blue_alpha_(0.067, 0.075, 0.102, 1.0).setFill()
NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
    NSMakeRect(64, 64, S-128, S-128), 180, 180).fill()
text = NSString.stringWithString_("um")
attrs = {NSFontAttributeName: NSFont.boldSystemFontOfSize_(430),
         NSForegroundColorAttributeName:
             NSColor.colorWithCalibratedRed_green_blue_alpha_(0.91, 0.925, 0.96, 1.0)}
size = text.sizeWithAttributes_(attrs)
text.drawAtPoint_withAttributes_(((S-size.width)/2, (S-size.height)/2 + 40), attrs)
NSColor.colorWithCalibratedRed_green_blue_alpha_(1.0, 0.36, 0.42, 1.0).setFill()
NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
    NSMakeRect(200, 470, S-400, 56), 28, 28).fill()
img.unlockFocus()
rep = NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
png = rep.representationUsingType_properties_(NSPNGFileType, None)
png.writeToFile_atomically_("/tmp/fillercoach_icon.png", True)
PY

ICONSET="/tmp/FillerCoach.iconset"
rm -rf "$ICONSET" && mkdir -p "$ICONSET"
for sz in 16 32 128 256 512; do
  sips -z $sz $sz /tmp/fillercoach_icon.png --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null
  dbl=$((sz*2))
  sips -z $dbl $dbl /tmp/fillercoach_icon.png --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$RES/AppIcon.icns"

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

# --- relocatable launcher (paths relative to the bundle itself) ---
cat > "$APP/Contents/MacOS/FillerCoach" <<'LAUNCH'
#!/bin/bash
RES="$(cd "$(dirname "${BASH_SOURCE[0]}")/../Resources" && pwd)"
exec "$RES/venv/bin/python" "$RES/app/coach.py" --dock
LAUNCH
chmod +x "$APP/Contents/MacOS/FillerCoach"

du -sh "$APP" | awk '{print "==> Built " $2 " (" $1 ")"}'

if [ "${1:-}" = "--install" ]; then
  echo "==> Installing to /Applications"
  rm -rf /Applications/FillerCoach.app
  ditto "$APP" /Applications/FillerCoach.app
  echo "==> Launching"
  open -a /Applications/FillerCoach.app
  echo "    Allow the Microphone prompt on first run, then right-click the"
  echo "    Dock icon → Options → Keep in Dock."
fi
