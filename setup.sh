#!/usr/bin/env bash
# Filler Killer — one-time setup. Creates a venv, installs offline STT deps,
# downloads the Vosk English model. No API keys, no cloud accounts.
set -euo pipefail

cd "$(dirname "$0")"
echo "==> Filler Killer setup"

# --- portaudio (needed by sounddevice for mic capture) ---
if ! brew list portaudio >/dev/null 2>&1; then
  echo "==> Installing portaudio via Homebrew..."
  brew install portaudio
else
  echo "==> portaudio already installed"
fi

# --- venv (built from system python3 so it inherits tkinter) ---
if [ ! -d ".venv" ]; then
  echo "==> Creating virtualenv (.venv)"
  /usr/bin/python3 -m venv .venv
fi
# shellcheck disable=SC1091
source .venv/bin/activate

echo "==> Installing Python packages (vosk, sounddevice)"
python -m pip install --upgrade pip >/dev/null
python -m pip install vosk sounddevice pyobjc-framework-Cocoa pyobjc-framework-AVFoundation

# sanity: tkinter must be importable in the venv
if ! python -c "import tkinter" 2>/dev/null; then
  echo "!! tkinter not available in this Python. The overlay needs Tk."
  echo "   Try: brew install python-tk  (then re-run setup)"
fi

# --- Vosk model ---
MODEL_DIR="model"
MODEL_NAME="vosk-model-small-en-us-0.15"
MODEL_URL="https://alphacephei.com/vosk/models/${MODEL_NAME}.zip"
# Pinned to match the Homebrew formula's resource checksum. Update both together.
MODEL_SHA256="30f26242c4eb449f948e42cb302dd7a686cb29a3423a8367f99ff41780942498"
if [ ! -d "$MODEL_DIR" ]; then
  echo "==> Downloading Vosk model (~40MB): $MODEL_NAME"
  curl -fL -o model.zip "$MODEL_URL"
  echo "==> Verifying checksum"
  echo "${MODEL_SHA256}  model.zip" | shasum -a 256 -c - || {
    echo "!! Model checksum mismatch — refusing this download." >&2
    rm -f model.zip
    exit 1
  }
  echo "==> Unzipping model"
  unzip -q model.zip
  mv "$MODEL_NAME" "$MODEL_DIR"
  rm -f model.zip
else
  echo "==> Model already present in ./model"
fi

echo ""
echo "==> Done. Launch with:  ./run.sh"
echo "    First run: macOS will ask for Microphone permission — allow it."
