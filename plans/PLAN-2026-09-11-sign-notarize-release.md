# Plan: Sign & notarize FillerKiller.app for direct distribution

| | |
|---|---|
| **Date** | 2026-09-11 |
| **Status** | In Progress — updated as work progresses |
| **Project** | filler-killer |
| **Backlog Card ID** | TBD |

## Why
Matt now has an approved Apple Developer account. Today the app is ad-hoc signed,
so downloaders (brew tap / GitHub release) hit Gatekeeper "unidentified developer"
friction. Goal: Developer ID signing + notarization + stapling so the app opens
cleanly on any Mac, distributed direct (GitHub release zip + brew cask) — **not**
the Mac App Store (user's explicit choice; sandboxing would complicate the
embedded venv + mic capture anyway).

## Scope
**In scope:**
- New bundle ID `com.mattbakerpm.fillerkiller` (replaces `local.fillerkiller` — Developer ID signing wants a real reverse-DNS ID; requires a fresh mic grant on first signed launch)
- `make_app.sh` gains a `--sign` path: sign every Mach-O in the embedded venv (vosk/sounddevice/pyobjc dylibs & .so files) inside-out, then the launcher/bundle, with hardened runtime + entitlements
- `entitlements.plist`: `com.apple.security.cs.allow-unsigned-executable-memory` NOT needed (no JIT); but `audio-input` entitlement IS needed under hardened runtime; also `allow-dyld-environment-variables`/`disable-library-validation` for the Python child loading pip-installed dylibs
- `notarize.sh`: ditto-zip the app, `xcrun notarytool submit --wait --keychain-profile fillerkiller-notary`, `stapler staple`, produce release zip
- Release: tag, GitHub release with the notarized zip; convert brew tap from formula to **cask** pointing at the zip

**Out of scope / later:**
- Mac App Store submission
- Sparkle/auto-update

## Approach
1. USER (manual, blocked on): in Xcode → Settings → Accounts → Manage Certificates → create **Developer ID Application** certificate (or via developer.apple.com portal). Machine currently has **0 valid codesigning identities**.
2. USER (manual): create app-specific password at appleid.apple.com, then
   `xcrun notarytool store-credentials fillerkiller-notary --apple-id mattbakerpm@gmail.com --team-id <TEAMID>`.
3. Update `make_app.sh`: bundle ID, version from git tag, `--sign "<identity>"` flag that signs venv binaries then bundle with hardened runtime + entitlements (replacing the ad-hoc sign).
4. Add `entitlements.plist` and `notarize.sh`.
5. Build, sign, notarize, staple; verify with `spctl -a -vv` and a fresh-quarantine open test.
6. Tag release, upload zip to GitHub, switch tap to a cask.
7. Update backlog card.

## Decisions & Tradeoffs

| Decision | Rationale |
|---|---|
| Direct distribution, not App Store | User's choice; App Store sandbox would break the embedded-venv architecture |
| Real bundle ID `com.mattbakerpm.fillerkiller` | `local.*` is unfit for a signed public app; costs one re-grant of the mic permission |
| `disable-library-validation` entitlement | Python child loads pip wheels' dylibs signed by third parties (or re-signed by us); library validation under hardened runtime would block them otherwise |
| Brew **cask** (prebuilt signed app) over formula | Formula rebuilds venv per-machine — that copy is unsigned, defeating notarization. Cask ships the exact notarized bundle |

## Changes Made

| File | Change |
|---|---|
| plans/PLAN-2026-09-11-sign-notarize-release.md | created |
| entitlements.plist | new — audio-input, disable-library-validation, allow-dyld-env for hardened runtime |
| notarize.sh | new — zip → notarytool submit --wait → staple → release zip → spctl check |
| make_app.sh | bundle ID → com.mattbakerpm.fillerkiller, version from git tag, `--sign` mode signing venv Mach-Os inside-out then bundle with hardened runtime |

## Follow-on Turns

| Turn / Date | What changed |
|---|---|
| | |

## Result

**Status:** In progress — blocked on user creating Developer ID cert + notary credentials
