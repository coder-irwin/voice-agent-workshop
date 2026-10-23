#!/usr/bin/env bash
# Start the voice agent, then open http://localhost:8000
cd "$(dirname "$0")"
if ! curl -s localhost:11434 >/dev/null; then
  echo "Starting Ollama in the background…"
  (nohup ollama serve >/tmp/ollama.log 2>&1 &)
  for _ in $(seq 1 20); do curl -s localhost:11434 >/dev/null && break; sleep 1; done
fi
curl -s localhost:11434 >/dev/null || { echo "❌ Could not start Ollama. Open the Ollama app or run: ollama serve"; exit 1; }
echo "🎙️  Open http://localhost:8000 in Chrome, click 'Start Listening' and talk!"
exec .venv/bin/python -m uvicorn app.main:app --host 0.0.0.0 --port 8000
