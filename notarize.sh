#!/usr/bin/env bash
# Notarize + staple a signed FillerKiller.app and produce the release zip.
#
# Prereqs (one-time):
#   - Developer ID Application certificate in the login keychain
#   - xcrun notarytool store-credentials fillerkiller-notary \
#       --apple-id mattbakerpm@gmail.com --team-id <TEAMID>
#
# Usage: ./notarize.sh [path/to/FillerKiller.app]
set -euo pipefail
cd "$(dirname "$0")"
APP="${1:-FillerKiller.app}"
PROFILE="fillerkiller-notary"

[ -d "$APP" ] || { echo "No $APP — run ./make_app.sh --sign first."; exit 1; }

codesign --verify --deep --strict "$APP" || {
  echo "App is not validly signed. Run ./make_app.sh --sign \"Developer ID Application: ...\""; exit 1; }

VER=$(defaults read "$PWD/$APP/Contents/Info" CFBundleShortVersionString 2>/dev/null || echo dev)
ZIP="FillerKiller-${VER}.zip"

echo "==> Zipping for submission"
ditto -c -k --keepParent "$APP" "$ZIP"

echo "==> Submitting to Apple notary service (waits for result)"
xcrun notarytool submit "$ZIP" --keychain-profile "$PROFILE" --wait

echo "==> Stapling ticket to the app"
xcrun stapler staple "$APP"

echo "==> Re-zipping stapled app as the release artifact"
rm -f "$ZIP"
ditto -c -k --keepParent "$APP" "$ZIP"

echo "==> Gatekeeper check"
spctl -a -vv --type execute "$APP"

echo "Done: $ZIP  (attach to the GitHub release; point the brew cask at it)"
