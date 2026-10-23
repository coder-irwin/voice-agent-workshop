#!/usr/bin/env bash
# One-time setup for the workshop. Safe to run again (skips what's already done).
# Usage:  ./setup.sh
set -e
cd "$(dirname "$0")"

say() { printf "\n\033[1;36m▶ %s\033[0m\n" "$1"; }

say "1/6  Checking tools (python 3.11/3.12, ollama, ffmpeg, espeak-ng, compiler)"
PY=$(command -v python3.12 || command -v python3.11 || true)
[ -z "$PY" ] && { echo "❌ Need Python 3.11 or 3.12 (3.13+ breaks webrtcvad). Install: brew install python@3.12"; exit 1; }
command -v ollama >/dev/null || { echo "❌ Install Ollama first: https://ollama.com/download"; exit 1; }
command -v gcc >/dev/null || command -v clang >/dev/null || { echo "❌ C compiler (gcc/clang) is required to build webrtcvad & pyaudio. On Ubuntu/Debian/WSL run: sudo apt install -y build-essential python3-dev portaudio19-dev"; exit 1; }
command -v ffmpeg >/dev/null || echo "⚠️  ffmpeg missing (only needed for voice_test.py): brew install ffmpeg"
command -v espeak-ng >/dev/null || echo "⚠️  espeak-ng missing (needed by Kokoro voice): brew install espeak-ng"

say "2/6  Creating Python virtual environment (.venv)"
[ -d .venv ] || "$PY" -m venv .venv
.venv/bin/pip install --quiet --upgrade pip

say "3/6  Installing Python packages (takes a few minutes the first time)"
.venv/bin/pip install --quiet -r requirements.txt

say "4/6  Installing the Playwright browser"
[ -f .env ] || cp .env.example .env
WANT=$(grep -E '^BROWSER=' .env | cut -d= -f2 | tr -d ' ')
[ "${WANT:-chromium}" = "chromium" ] || echo "   (your .env asks for BROWSER=$WANT — installing it too)"
.venv/bin/python -m playwright install chromium
case "$WANT" in firefox|webkit|chrome|msedge) .venv/bin/python -m playwright install "$WANT" ;; esac

say "5/6  Downloading the voice (TTS) models"
mkdir -p models/kokoro models/piper
dl() { [ -s "$2" ] || curl -L --fail -o "$2" "$1"; }
dl https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/libritts_r/medium/en_US-libritts_r-medium.onnx      models/piper/en_US-libritts_r-medium.onnx
dl https://huggingface.co/rhasspy/piper-voices/resolve/main/en/en_US/libritts_r/medium/en_US-libritts_r-medium.onnx.json models/piper/en_US-libritts_r-medium.onnx.json
dl https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/voices.bin          models/kokoro/voices.bin
dl https://github.com/thewh1teagle/kokoro-onnx/releases/download/model-files/kokoro-v0_19.onnx   models/kokoro/kokoro-v0_19.onnx

say "6/6  Downloading the brain (Ollama model, ~2 GB)"
if ! curl -s localhost:11434 >/dev/null; then
  echo "Starting Ollama in the background…"
  (nohup ollama serve >/tmp/ollama.log 2>&1 &)
  for _ in $(seq 1 20); do curl -s localhost:11434 >/dev/null && break; sleep 1; done
fi
curl -s localhost:11434 >/dev/null || { echo "❌ Could not start Ollama. Open the Ollama app or run: ollama serve"; exit 1; }
ollama pull "$(grep ^OLLAMA_MODEL .env | cut -d= -f2)"

printf "\n\033[1;32m✅ All set!  Now run:  ./run.sh\033[0m\n"
