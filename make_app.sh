#!/usr/bin/env bash
# Build FillerKiller.app — a SELF-CONTAINED Dock app.
#
# The bundle carries its own Python runtime, the Vosk model, coach.py, and its
# own config.json. Nothing is read from this project folder at runtime, so
# macOS's Documents-folder privacy protection can't kill it when launched
# from the Dock, and the app keeps working even if you move this project.
#
# Rebuild after changing coach.py to update the app.
#
#   ./make_app.sh                          build ./FillerKiller.app (ad-hoc signed)
#   ./make_app.sh --install                build + install to /Applications + launch
#   ./make_app.sh --sign "Developer ID Application: NAME (TEAMID)"
#                                          build + Developer ID sign with hardened
#                                          runtime (then run ./notarize.sh)
set -euo pipefail
cd "$(dirname "$0")"
APP="FillerKiller.app"
RES="$APP/Contents/Resources"
BUNDLE_ID="com.mattbakerpm.fillerkiller"
VERSION=$(git describe --tags --abbrev=0 2>/dev/null | sed 's/^v//' || echo "0.0.0")

SIGN_ID=""
if [ "${1:-}" = "--sign" ]; then
  SIGN_ID="${2:?usage: ./make_app.sh --sign \"Developer ID Application: ...\"}"
fi

if [ ! -d "model" ]; then
  echo "No ./model found. Run ./setup.sh first."
  exit 1
fi

echo "==> Building $APP (self-contained)"
rm -rf "$APP"
mkdir -p "$APP/Contents/MacOS" "$RES/app"

# --- embedded Python: a full copy of the framework's Versions/3.x subtree.
#     A venv can't ship: its pyvenv.cfg `home` and interpreter point at the
#     Xcode/CLT install, which end users don't have. The framework layout is
#     self-relocating — bin/python3 finds its dylib (@executable_path/../Python3)
#     and stdlib relative to itself — so a copy inside the bundle is truly
#     self-contained and signable. ---
PYPREFIX=$(/usr/bin/python3 -c "import sys; print(sys.prefix)")
echo "==> Embedding Python from $PYPREFIX"
mkdir -p "$RES/python"
ditto "$PYPREFIX" "$RES/python"
rm -rf "$RES/python/lib/python3."*/test "$RES/python/lib/python3."*/idlelib 2>/dev/null || true
# drop Apple's framework seal — our edits invalidate it, and a stale seal makes
# codesign/notarization read python/ as a broken bundle
rm -rf "$RES/python/_CodeSignature"
find "$RES/python" -name "__pycache__" -type d -prune -exec rm -rf {} + 2>/dev/null || true
PYBIN="$RES/python/bin/python3"
echo "==> Installing packages into the app (vosk, sounddevice, pyobjc Cocoa+AVFoundation)"
"$PYBIN" -m pip install --quiet --upgrade pip
"$PYBIN" -m pip install --quiet vosk sounddevice pyobjc-framework-Cocoa pyobjc-framework-AVFoundation

# --- app code, config, model, brand assets (About window logo) ---
cp coach.py "$RES/app/"
cp config.json "$RES/app/"
cp -R assets "$RES/app/assets"
echo "==> Copying Vosk model (~40MB)"
cp -R model "$RES/app/model"

# --- icon: brand mark (assets/filler-killer-mark.svg) on a white tile ---
"$PYBIN" - <<'PY'
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
  <key>CFBundleIdentifier</key><string>${BUNDLE_ID}</string>
  <key>CFBundleVersion</key><string>${VERSION}</string>
  <key>CFBundleShortVersionString</key><string>${VERSION}</string>
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
# A shell script that exec's python breaks microphone TCC: at access time the
# process identity is Apple's python3 (no usage description) and macOS silently
# auto-denies without prompting. A compiled binary that runs python as a CHILD
# keeps FillerKiller.app as the responsible process, so the mic prompt appears
# and is attributed to this app.
cat > /tmp/fillerkiller_launcher.swift <<'SWIFT'
import Foundation

let exeURL = URL(fileURLWithPath: CommandLine.arguments[0]).resolvingSymlinksInPath()
let res = exeURL.deletingLastPathComponent()          // MacOS/
    .deletingLastPathComponent()                       // Contents/
    .appendingPathComponent("Resources")

let proc = Process()
proc.executableURL = res.appendingPathComponent("python/bin/python3")
proc.arguments = [res.appendingPathComponent("app/coach.py").path, "--dock"]

signal(SIGTERM, SIG_IGN)
let src = DispatchSource.makeSignalSource(signal: SIGTERM)
src.setEventHandler { proc.terminate() }
src.resume()

do {
    try proc.run()
} catch {
    FileHandle.standardError.write("launch failed: \(error)\n".data(using: .utf8)!)
    exit(1)
}
proc.waitUntilExit()
exit(proc.terminationStatus)
SWIFT
swiftc -O /tmp/fillerkiller_launcher.swift -o "$APP/Contents/MacOS/FillerKiller"

if [ -n "$SIGN_ID" ]; then
  # Developer ID signing, inside-out: notarization requires every Mach-O in
  # the bundle (pip wheels' .so/.dylib, the embedded python) to carry a
  # hardened-runtime Developer ID signature before the outer bundle is signed.
  # notarization forbids symlinks that resolve outside the bundle
  BAD=$(find "$RES" -type l -print0 | while IFS= read -r -d '' l; do
    tgt=$(readlink "$l")
    if [ ! -e "$l" ] || [ "${tgt#/}" != "$tgt" ]; then echo "$l -> $tgt"; fi
  done)
  [ -z "$BAD" ] || { echo "external/broken symlinks in bundle:"; echo "$BAD"; exit 1; }
  echo "==> Signing embedded binaries with: $SIGN_ID"
  # every Mach-O in the runtime, whatever its extension (vosk ships libvosk.dyld)
  find "$RES/python" -type f ! -path "*/python/Python3" -print0 |
    while IFS= read -r -d '' f; do
      file -b "$f" | grep -q Mach-O || continue
      codesign --force --options runtime --timestamp \
        --entitlements entitlements.plist --sign "$SIGN_ID" "$f"
    done
  # codesign reads python/ as a framework whose Python3 seal covers Resources/,
  # so the nested Python.app bundle must be sealed first and Python3 after it
  codesign --force --options runtime --timestamp \
    --entitlements entitlements.plist --sign "$SIGN_ID" \
    "$RES/python/Resources/Python.app"
  codesign --force --options runtime --timestamp \
    --entitlements entitlements.plist --sign "$SIGN_ID" "$RES/python/Python3"
  echo "==> Signing bundle"
  codesign --force --options runtime --timestamp \
    --entitlements entitlements.plist --sign "$SIGN_ID" "$APP"
  codesign --verify --deep --strict --verbose=2 "$APP"
  echo "==> Signed. Next: ./notarize.sh"
else
  # ad-hoc sign so TCC has a stable code identity for the permission grant
  codesign --force --deep --sign - "$APP" 2>/dev/null || true
fi

du -sh "$APP" | awk '{print "==> Built " $2 " (" $1 ")"}'

if [ "${1:-}" = "--install" ]; then
  echo "==> Installing to /Applications"
  # stop any running copy first — replacing the bundle under a live app
  # orphans it, and the next open would start a SECOND instance whose
  # voice-processing engine can't start while the orphan holds the mic
  pkill -f "FillerKiller.app/Contents" 2>/dev/null && sleep 1 || true
  rm -rf /Applications/FillerKiller.app /Applications/FillerCoach.app  # drop pre-rebrand app too
  ditto "$APP" /Applications/FillerKiller.app
  # clear any stale auto-denied mic decision so the prompt can appear
  tccutil reset Microphone "$BUNDLE_ID" >/dev/null 2>&1 || true
  tccutil reset Microphone local.fillerkiller >/dev/null 2>&1 || true  # pre-signing bundle ID
  echo "==> Launching"
  open -a /Applications/FillerKiller.app
  echo "    Allow the Microphone prompt on first run, then right-click the"
  echo "    Dock icon → Options → Keep in Dock."
fi
