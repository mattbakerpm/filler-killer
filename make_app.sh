#!/usr/bin/env bash
# Build FillerKiller.app — a SELF-CONTAINED Dock app.
#
# The bundle carries its own Python venv, the Vosk model, coach.py, and its
# own config.json. Nothing is read from this project folder at runtime, so
# macOS's Documents-folder privacy protection can't kill it when launched
# from the Dock, and the app keeps working even if you move this project.
#
# Rebuild after changing coach.py to update the app.
#
#   ./make_app.sh             build ./FillerKiller.app
#   ./make_app.sh --install   build + install to /Applications + launch
set -euo pipefail
cd "$(dirname "$0")"
APP="FillerKiller.app"
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

# --- icon: brand mark (assets/filler-killer-mark.svg) on a white tile ---
"$RES/venv/bin/python" - <<'PY'
from Cocoa import (NSImage, NSMakeRect, NSColor, NSBezierPath,
                   NSMakeSize, NSBitmapImageRep, NSPNGFileType,
                   NSCompositingOperationSourceOver)

mark = NSImage.alloc().initWithContentsOfFile_("assets/filler-killer-mark.svg")
assert mark is not None, "could not load assets/filler-killer-mark.svg"

S = 1024
img = NSImage.alloc().initWithSize_(NSMakeSize(S, S))
img.lockFocus()
NSColor.colorWithCalibratedRed_green_blue_alpha_(0.98, 0.98, 0.97, 1.0).setFill()
NSBezierPath.bezierPathWithRoundedRect_xRadius_yRadius_(
    NSMakeRect(64, 64, S - 128, S - 128), 180, 180).fill()
inset = 140  # breathing room inside the tile
mark.drawInRect_fromRect_operation_fraction_(
    NSMakeRect(inset, inset, S - 2 * inset, S - 2 * inset),
    NSMakeRect(0, 0, mark.size().width, mark.size().height),
    NSCompositingOperationSourceOver, 1.0)
img.unlockFocus()
rep = NSBitmapImageRep.imageRepWithData_(img.TIFFRepresentation())
png = rep.representationUsingType_properties_(NSPNGFileType, None)
png.writeToFile_atomically_("/tmp/fillerkiller_icon.png", True)
print("icon rendered from brand mark")
PY

ICONSET="/tmp/FillerKiller.iconset"
rm -rf "$ICONSET" && mkdir -p "$ICONSET"
for sz in 16 32 128 256 512; do
  sips -z $sz $sz /tmp/fillerkiller_icon.png --out "$ICONSET/icon_${sz}x${sz}.png" >/dev/null
  dbl=$((sz*2))
  sips -z $dbl $dbl /tmp/fillerkiller_icon.png --out "$ICONSET/icon_${sz}x${sz}@2x.png" >/dev/null
done
iconutil -c icns "$ICONSET" -o "$RES/AppIcon.icns"

# --- Info.plist ---
cat > "$APP/Contents/Info.plist" <<PLIST
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>CFBundleName</key><string>FillerKiller</string>
  <key>CFBundleDisplayName</key><string>Filler Killer</string>
  <key>CFBundleIdentifier</key><string>local.fillerkiller</string>
  <key>CFBundleVersion</key><string>1.0</string>
  <key>CFBundlePackageType</key><string>APPL</string>
  <key>CFBundleExecutable</key><string>FillerKiller</string>
  <key>CFBundleIconFile</key><string>AppIcon</string>
  <key>NSMicrophoneUsageDescription</key>
  <string>Filler Killer listens to your microphone locally to count filler words. Audio never leaves this Mac.</string>
  <key>NSHighResolutionCapable</key><true/>
</dict>
</plist>
PLIST

# --- relocatable launcher (paths relative to the bundle itself) ---
cat > "$APP/Contents/MacOS/FillerKiller" <<'LAUNCH'
#!/bin/bash
RES="$(cd "$(dirname "${BASH_SOURCE[0]}")/../Resources" && pwd)"
exec "$RES/venv/bin/python" "$RES/app/coach.py" --dock
LAUNCH
chmod +x "$APP/Contents/MacOS/FillerKiller"

du -sh "$APP" | awk '{print "==> Built " $2 " (" $1 ")"}'

if [ "${1:-}" = "--install" ]; then
  echo "==> Installing to /Applications"
  rm -rf /Applications/FillerKiller.app /Applications/FillerCoach.app  # drop pre-rebrand app too
  ditto "$APP" /Applications/FillerKiller.app
  echo "==> Launching"
  open -a /Applications/FillerKiller.app
  echo "    Allow the Microphone prompt on first run, then right-click the"
  echo "    Dock icon → Options → Keep in Dock."
fi
