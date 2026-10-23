"""
main.py — End-to-end realtime Voice AI pipeline.

Architecture:
  User Voice → Browser Mic (PCM 16kHz)
    → Persistent WebSocket
      → VAD (energy-based, server-side)
        → Partial STT (every 0.5s, fire-and-forget)
        → Silence > 2s detected
          → Final STT (beam_size=3)
            → Ollama LLM (streaming tokens)
              → llm_partial_response events (each token)
              → llm_final_response event
              → assistant_finished
            → Auto-resume listening

Events emitted to client:
  - session_started
  - user_started_speaking
  - partial_transcript     {text, latency_ms}
  - user_stopped_speaking
  - final_transcript       {text, latency_ms}
  - llm_processing_started
  - llm_partial_response   {token, first_token_ms?}
  - llm_final_response     {text, total_ms}
  - assistant_finished
  - error                  {message}
"""

import os
from pathlib import Path
import time
import uuid
import json
import asyncio
import logging
import base64
from contextlib import asynccontextmanager

from fastapi import FastAPI, Request, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from dotenv import load_dotenv

load_dotenv()

from app.stt import STTEngine
from app.llm import AIAgent
from app.vad import EnergyVAD
from app.session import Session, session_manager, PERSONAS
from app.memory_manager import MemoryManager
from app.tts_manager import TTSManager
from app.persistence import session_storage
from app.tools import list_tools          # 🛠️ action layer (see app/tools/)
from app.tools.browser import browser
from app.tools.dispatcher import detect_and_run

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s.%(msecs)03d | %(levelname)s | %(message)s",
    datefmt="%H:%M:%S"
)
logger = logging.getLogger(__name__)

# ── Configuration ────────────────────────────────────────────────────
SAMPLE_RATE = 16000
BYTES_PER_SAMPLE = 2
BYTES_PER_SEC = SAMPLE_RATE * BYTES_PER_SAMPLE  # 32000

# Partial transcription interval (bytes ≈ 0.5s of audio)
PARTIAL_CHUNK_BYTES = int(BYTES_PER_SEC * 0.5)

# Silence threshold to finalize (seconds)
SILENCE_TIMEOUT = 0.5

# Minimum speech bytes before we bother with transcription
MIN_SPEECH_BYTES = int(BYTES_PER_SEC * 0.3)


class SmartSentenceChunker:
    """
    Splits streaming LLM tokens into TTS-friendly chunks.
    
    EAGER FIRST CHUNK: The first chunk is sent after just ~2-3 words (8 chars)
    so TTS starts speaking ASAP. Subsequent chunks wait for natural sentence
    boundaries for proper prosody.
    """
    def __init__(self, first_chunk_chars: int = 5, min_chars: int = 20, max_chars: int = 120, comma_min: int = 35):
        self.first_chunk_chars = first_chunk_chars
        self.min_chars = min_chars
        self.max_chars = max_chars
        self.comma_min = comma_min
        self.buffer = ""
        self._first_emitted = False

    def _clean_text(self, text: str) -> str:
        """Strips out markdown syntax (asterisks, hashes, list headers, etc.) to ensure conversational TTS stability."""
        import re
        # Remove asterisks (**bold**, *italic*), underscores, and header hashes
        text = text.replace("**", "").replace("*", "").replace("_", "").replace("#", "")
        # Remove bullet point hyphens at the start of sentences
        text = re.sub(r'^\s*-\s+', '', text)
        # Remove numeric list bullets like "1. ", "2. "
        text = re.sub(r'^\s*\d+\.\s+', '', text)
        # Replace multi-spaces or newlines with a single space
        text = re.sub(r'\s+', ' ', text)
        return text.strip()

    def feed(self, token: str) -> list[str]:
        self.buffer += token
        
        chunks = []
        
        # EAGER FIRST CHUNK: emit as soon as we have ~2-3 words
        # This lets TTS start speaking while LLM is still generating
        if not self._first_emitted:
            stripped = self.buffer.strip()
            if len(stripped) >= self.first_chunk_chars:
                # Try to split at a word boundary
                # Look for the best split point: punctuation > space
                split_idx = -1
                for idx in range(len(self.buffer) - 1, -1, -1):
                    if self.buffer[idx] in ['.', '!', '?', ',', ';', ':']:
                        split_idx = idx
                        break
                
                # No punctuation? Split at last space
                if split_idx == -1:
                    space_idx = self.buffer.rfind(' ')
                    if space_idx > 3:
                        split_idx = space_idx - 1  # keep the space on the right side
                
                if split_idx != -1:
                    chunk = self._clean_text(self.buffer[:split_idx+1])
                    self.buffer = self.buffer[split_idx+1:]
                    if chunk:
                        chunks.append(chunk)
                        self._first_emitted = True
                        return chunks
            return chunks
        
        # NORMAL MODE: wait for natural sentence boundaries
        while True:
            stripped = self.buffer.strip()
            if not stripped:
                break
                
            split_idx = -1
            
            # Look for terminal punctuations backwards
            for idx in range(len(self.buffer) - 1, -1, -1):
                char = self.buffer[idx]
                if char in ['.', '!', '?', '\n']:
                    prefix = self.buffer[:idx+1].strip()
                    if len(prefix) >= self.min_chars or len(self.buffer) >= self.max_chars:
                        split_idx = idx
                        break
                        
            # If no terminal, look for commas/semicolons/colons
            if split_idx == -1:
                for idx in range(len(self.buffer) - 1, -1, -1):
                    char = self.buffer[idx]
                    if char in [',', ';', ':']:
                        prefix = self.buffer[:idx+1].strip()
                        if len(prefix) >= self.comma_min or len(self.buffer) >= self.max_chars:
                            split_idx = idx
                            break
                            
            # If buffer is too large, split on space
            if split_idx == -1 and len(self.buffer) >= self.max_chars:
                space_idx = self.buffer.rfind(' ')
                if space_idx != -1 and space_idx > self.min_chars:
                    split_idx = space_idx
                    
            if split_idx != -1:
                chunk = self._clean_text(self.buffer[:split_idx+1])
                self.buffer = self.buffer[split_idx+1:]
                if chunk:
                    chunks.append(chunk)
            else:
                break
                
        return chunks

    def flush(self) -> list[str]:
        chunk = self._clean_text(self.buffer)
        self.buffer = ""
        self._first_emitted = False
        return [chunk] if chunk else []


# ── Lifespan ─────────────────────────────────────────────────────────
@asynccontextmanager
async def lifespan(app: FastAPI):
    """Startup / shutdown — load singleton engines."""
    logger.info("Starting Voice AI Backend (End-to-End Pipeline)…")

    from app.hardware_manager import hardware_manager
    from app.deployment_config import deployment_config

    # STT engine (singleton, model loaded once, dynamic profile-driven model size)
    app.state.stt = STTEngine()

    # LLM agent (singleton, persistent Ollama session)
    app.state.llm = AIAgent()
    await app.state.llm.startup()

    # Memory Manager (orchestrates multi-layer context)
    app.state.memory_manager = MemoryManager(app.state.llm)

    # TTS Manager (modular routing for Piper, Kokoro, etc.)
    app.state.tts = TTSManager()

    # Print beautiful hardware configuration telemetry card
    telemetry_card = (
        f"\n======================================================\n"
        f"⚙️  HARDWARE & DEPLOYMENT TELEMETRY\n"
        f"======================================================\n"
        f"Deployment Profile: {deployment_config.profile_name}\n"
        f"Active Platform:    {hardware_manager.platform}\n"
        f"CPU Cores:          {hardware_manager.cpu_cores}\n"
        f"System RAM:         {hardware_manager.available_ram_gb} GB\n"
        f"CUDA GPU Support:   {hardware_manager.supports_cuda}\n"
        f"VRAM Available:     {hardware_manager.available_vram_gb if hardware_manager.supports_cuda else 'N/A'} GB\n"
        f"STT Model Size:     {deployment_config.get('stt').get('model_size')}\n"
        f"STT Active Device:  {'cuda' if hardware_manager.supports_cuda else 'cpu'}\n"
        f"LLM Dynamic Model:  {app.state.llm.default_model}\n"
        f"TTS Backend Profile: {hardware_manager.tts_backend}\n"
        f"======================================================"
    )
    logger.info(telemetry_card)

    logger.info("Backend ready.")
    yield

    # Cleanup
    await app.state.llm.shutdown()
    await browser.close()
    logger.info("Backend shut down.")


app = FastAPI(title="Voice AI Backend", lifespan=lifespan)


# ── Frontend ─────────────────────────────────────────────────────────
_INDEX_HTML = Path(__file__).resolve().parent.parent / "index.html"


@app.get("/")
async def get_frontend():
    if _INDEX_HTML.exists():
        return HTMLResponse(_INDEX_HTML.read_text(encoding="utf-8"))
    with open("index.html", "r", encoding="utf-8") as f:
        return HTMLResponse(f.read())


# ── Health Diagnostic Endpoint ───────────────────────────────────────
@app.get("/health")
async def health_check():
    """Detailed diagnostic healthcheck verifying all AI pipelines and storage volumes."""
    from fastapi.responses import JSONResponse
    from datetime import datetime
    
    health_status = {
        "status": "healthy",
        "timestamp": datetime.utcnow().isoformat() + "Z",
        "components": {}
    }
    
    # 1. LLM / Ollama Connectivity
    ollama_ok = False
    try:
        llm = app.state.llm
        if llm.is_ollama_available:
            ollama_ok = True
        health_status["components"]["llm"] = {
            "status": "ok" if ollama_ok else "degraded",
            "backend": "ollama" if ollama_ok else "gemini_fallback",
            "default_model": llm.default_model
        }
    except Exception as e:
        health_status["components"]["llm"] = {"status": "error", "error": str(e)}
        health_status["status"] = "degraded"
        
    # 2. STT Initialization Status
    try:
        stt = app.state.stt
        stt_ok = stt._initialized and stt.model is not None
        health_status["components"]["stt"] = {"status": "ok" if stt_ok else "error"}
        if not stt_ok:
            health_status["status"] = "unhealthy"
    except Exception as e:
        health_status["components"]["stt"] = {"status": "error", "error": str(e)}
        health_status["status"] = "unhealthy"

    # 3. TTS Provider Registrations
    try:
        tts = app.state.tts
        kokoro_ok = tts.providers.get("kokoro").kokoro is not None
        piper_ok = len(tts.providers.get("piper").voices) > 0
        health_status["components"]["tts"] = {
            "status": "ok" if (kokoro_ok or piper_ok) else "error",
            "kokoro_loaded": kokoro_ok,
            "piper_voices_found": len(tts.providers.get("piper").voices)
        }
        if not (kokoro_ok or piper_ok):
            health_status["status"] = "unhealthy"
    except Exception as e:
        health_status["components"]["tts"] = {"status": "error", "error": str(e)}
        health_status["status"] = "unhealthy"

    # 4. Storage Persistence Mount Writability
    try:
        from app.deployment_config import deployment_config
        base_dir = deployment_config.get("persistence")["base_dir"]
        os.makedirs(base_dir, exist_ok=True)
        test_file = os.path.join(base_dir, ".health_write_test")
        with open(test_file, "w") as f:
            f.write("health_ok")
        os.remove(test_file)
        health_status["components"]["persistence"] = {"status": "ok", "writable": True}
    except Exception as e:
        health_status["components"]["persistence"] = {"status": "error", "error": str(e)}
        health_status["status"] = "unhealthy"

    if health_status["status"] == "unhealthy":
        return JSONResponse(status_code=503, content=health_status)
    return health_status


# ── Helpers ──────────────────────────────────────────────────────────
async def _send(ws: WebSocket, event_type: str, **kwargs):
    """Send a JSON event to the client. Silently ignores closed sockets."""
    try:
        await ws.send_json({"type": event_type, **kwargs})
    except Exception:
        pass


async def _do_partial_transcription(
    stt: STTEngine, session: Session, ws: WebSocket
):
    """Transcribe current audio buffer and emit partial_transcript if new."""
    buf = bytes(session.live_audio_buffer)
    if len(buf) < MIN_SPEECH_BYTES:
        return

    t0 = time.perf_counter()
    text = await stt.transcribe_chunk(buf)
    latency = (time.perf_counter() - t0) * 1000

    if text and text != session.last_partial:
        session.speech_buffer_text = text
        session.last_partial = text
        await _send(ws, "partial_transcript", text=text, latency_ms=round(latency, 1))
        logger.info(f"[partial] {text}  ({latency:.0f}ms)")


async def _run_actions(llm: AIAgent, session: Session, text: str, ws: WebSocket):
    """Run tools for this utterance, telling the browser UI about each step."""
    await _send(ws, "tool_routing_started")
    results = await detect_and_run(llm, session.llm_model, text)
    for r in results:
        await _send(ws, "tool_call", name=r["name"], arguments=r["arguments"])
        await _send(ws, "tool_result", name=r["name"], ok=r["ok"], result=r["result"], ms=r["ms"])
        yield r
    await _send(ws, "tool_routing_finished", count=len(results))


async def _do_finalize_and_stream(
    stt: STTEngine, llm: AIAgent, memory_manager: MemoryManager, tts: TTSManager, session: Session, ws: WebSocket,
    text_override: str | None = None,
):
    """
    Called when silence > SILENCE_TIMEOUT.
    1. Final STT on full utterance buffer
    2. Stream LLM tokens to client
    3. Triggers memory processing in the background
    4. Reset for next utterance
    """
    buf = bytes(session.live_audio_buffer)
    if text_override is None and len(buf) < MIN_SPEECH_BYTES:
        session.reset_speech_state()
        return

    # ── 1. Final STT (skipped when the user typed instead of spoke) ──
    t_stt = time.perf_counter()
    session.turn_metrics['stt_start_time'] = time.time()
    text = text_override if text_override is not None else await stt.transcribe_final(buf)
    session.turn_metrics['stt_end_time'] = time.time()
    stt_ms = (time.perf_counter() - t_stt) * 1000

    if not text.strip():
        session.reset_speech_state()
        return

    await _send(ws, "final_transcript", text=text, latency_ms=round(stt_ms, 1))
    logger.info(f"[FINAL] {text}  ({stt_ms:.0f}ms)")
    
    # ── 1.5 Background: Persist User Audio and Turn ──
    user_audio_bytes = bytes(buf)
    asyncio.create_task(
        session_storage.append_to_conversation(session.session_id, user_audio_bytes, SAMPLE_RATE)
    )
    # The turn counter will be incremented by append_turn
    asyncio.create_task(
        session_storage.append_turn(session.session_id, "user", text, "conversation.wav")
    )
    # Update local count safely
    session.turn_count += 1

    # ── 2. Stream LLM response ──
    generation_id = f"gen_{uuid.uuid4().hex[:8]}"
    session.active_generation_id = generation_id

    await _send(ws, "llm_processing_started", generation_id=generation_id)
    t_llm = time.perf_counter()

    # Synchronously persist the user turn for perfect conversational continuity
    session.recent_turns.append({"role": "user", "text": text})
    if len(session.recent_turns) > 6:
        session.recent_turns = session.recent_turns[-6:]

    # ── 1.8 🛠️ Action layer: does the user want us to DO something? ──
    tool_results = []
    async for result in _run_actions(llm, session, text, ws):
        tool_results.append(result)

    # Build optimized messages from multi-layer memory
    messages = memory_manager.build_messages(session)
    if tool_results:
        # Tell the LLM what just happened so the spoken reply matches reality
        summary = "; ".join(
            f"{r['name']}: {r['result']}" if r["ok"] else f"{r['name']} FAILED: {r['result']}"
            for r in tool_results
        )
        messages.append({
            "role": "system",
            "content": (
                f"[ACTION ALREADY DONE] {summary}. "
                "Confirm this to the user in ONE short, friendly spoken sentence (max 15 words). "
                "Never read out full titles, URLs or technical details. If it failed, say so briefly."
            ),
        })

    full_response = []
    first_token_ms = None
    
    # --- Pipelined TTS Setup ---
    sentence_queue = asyncio.Queue()
    full_tts_audio = bytearray()
    tts_sample_rate = [24000] # Use list so worker can mutate it

    async def tts_worker():
        first_tts_chunk = True
        await _send(ws, "assistant_speaking_started", generation_id=generation_id)
        
        while True:
            sentence = await sentence_queue.get()
            if sentence is None:
                break
            if session.active_generation_id != generation_id:
                break # barge-in cancels remaining TTS
                
            # Synthesize this sentence
            try:
                async for chunk_bytes, sample_rate in tts.synthesize_stream(sentence, session.tts_engine, session.tts_voice):
                    tts_sample_rate[0] = sample_rate
                    if first_tts_chunk:
                        session.turn_metrics['tts_first_chunk_time'] = time.time()
                        first_tts_chunk = False
                        
                    if session.active_generation_id != generation_id:
                        logger.info(f"[{session.session_id}] TTS streaming interrupted by barge-in")
                        break
                        
                    full_tts_audio.extend(chunk_bytes)
                    chunk_b64 = base64.b64encode(chunk_bytes).decode("utf-8")
                    await _send(
                        ws, 
                        "assistant_audio_chunk", 
                        audio_b64=chunk_b64, 
                        sample_rate=sample_rate, 
                        generation_id=generation_id
                    )
            except Exception as e:
                logger.error(f"[{session.session_id}] TTS streaming error: {e}")

    # Start the background TTS worker
    tts_task = asyncio.create_task(tts_worker())
    session.active_tts_task = tts_task
    
    chunker = SmartSentenceChunker()

    try:
        async for chunk in llm.stream_response(messages, model_name=session.llm_model, generation_id=generation_id):
            if session.active_generation_id != generation_id:
                logger.info(f"[{session.session_id}] Stale stream invalidated")
                break

            token = chunk.get("token", "")
            done = chunk.get("done", False)
            ftm = chunk.get("first_token_ms")

            if ftm is not None:
                first_token_ms = round(ftm, 1)

            if token:
                if 'llm_first_token_time' not in session.turn_metrics:
                    session.turn_metrics['llm_first_token_time'] = time.time()
                full_response.append(token)
                
                await _send(
                    ws,
                    "llm_partial_response",
                    token=token,
                    first_token_ms=first_token_ms if ftm is not None else None,
                    generation_id=generation_id,
                )
                
                # Smart chunking feed
                for sentence_text in chunker.feed(token):
                    sentence_queue.put_nowait(sentence_text)

            if done:
                break

    except asyncio.CancelledError:
        logger.info(f"[{session.session_id}] LLM streaming cancelled")
        session.reset_speech_state()
        sentence_queue.put_nowait(None) # kill worker
        return

    # Flush remaining text from chunker
    for sentence_text in chunker.flush():
        sentence_queue.put_nowait(sentence_text)
    
    # Signal TTS worker to stop and wait for it
    sentence_queue.put_nowait(None)
    await tts_task

    session.turn_metrics['llm_end_time'] = time.time()
    total_llm_ms = round((time.perf_counter() - t_llm) * 1000, 1)
    response_text = "".join(full_response).strip()

    if response_text and session.active_generation_id == generation_id:
        # Synchronously persist the assistant turn
        session.recent_turns.append({"role": "assistant", "text": response_text})
        if len(session.recent_turns) > 6:
            session.recent_turns = session.recent_turns[-6:]

        await _send(
            ws,
            "llm_final_response",
            text=response_text,
            first_token_ms=first_token_ms,
            total_ms=total_llm_ms,
            generation_id=generation_id,
        )
        logger.info(
            f"[LLM] {response_text[:80]}… "
            f"(first_token={first_token_ms}ms, total={total_llm_ms}ms)"
        )
        
        # ── 3. Background Memory Processing ──
        # Offload layer 2 and 3 updates to the background (facts & summary)
        asyncio.create_task(
            memory_manager.process_turn_background(session, text, response_text)
        )
        
        if session.active_generation_id == generation_id:
            await _send(ws, "assistant_speaking_finished", generation_id=generation_id)
            
        # ── 4.5 Background: Persist Assistant Audio and Turn ──
        if full_tts_audio:
            asyncio.create_task(
                session_storage.append_to_conversation(session.session_id, bytes(full_tts_audio), tts_sample_rate[0])
            )
            
        asyncio.create_task(
            session_storage.append_turn(session.session_id, "assistant", response_text, "conversation.wav")
        )

    # ── Print Turn Timeline Log ──
    t0 = session.turn_metrics.get('speech_start_time', 0)
    def ms_diff(key, raw=False):
        t = session.turn_metrics.get(key)
        if raw: return int((t - t0) * 1000) if t else None
        return f"+{(t - t0)*1000:.0f}ms" if t else "N/A"
        
    timeline = (
        f"\n\n=== 🕒 TURN TIMELINE [{session.session_id}] ===\n"
        f"1. Speech Started:     +0ms\n"
        f"2. Speech Ended:       {ms_diff('speech_end_time')}\n"
        f"3. STT Completed:      {ms_diff('stt_end_time')}\n"
        f"4. LLM 1st Token:      {ms_diff('llm_first_token_time')}\n"
        f"5. LLM Completed:      {ms_diff('llm_end_time')}\n"
        f"6. TTS 1st Audio Sent: {ms_diff('tts_first_chunk_time')}\n"
        f"=========================================\n"
    )
    logger.info(timeline)

    await _send(ws, "turn_metrics", metrics={
        "speech_start": 0,
        "speech_end": ms_diff('speech_end_time', raw=True),
        "stt_end": ms_diff('stt_end_time', raw=True),
        "llm_first_token": ms_diff('llm_first_token_time', raw=True),
        "llm_end": ms_diff('llm_end_time', raw=True),
        "tts_first_chunk": ms_diff('tts_first_chunk_time', raw=True)
    })

    await _send(ws, "assistant_finished", generation_id=generation_id)

    # ── 5. Reset for next utterance ──
    session.reset_speech_state()


ACTIVE_WS_CONNECTIONS = set()

# ── WebSocket endpoint ───────────────────────────────────────────────
@app.websocket("/ws/stream")
async def websocket_endpoint(websocket: WebSocket):
    from app.deployment_config import deployment_config
    limits = deployment_config.get("limits")
    max_ws = limits.get("max_websocket_sessions", 5)

    if len(ACTIVE_WS_CONNECTIONS) >= max_ws:
        logger.warning(f"Connection rejected: Max websocket sessions ({max_ws}) reached.")
        await websocket.close(code=1013, reason="Max concurrent connections reached")
        return

    await websocket.accept()
    ACTIVE_WS_CONNECTIONS.add(websocket)

    session_id = str(uuid.uuid4())[:8]
    
    stt: STTEngine = app.state.stt
    llm: AIAgent = app.state.llm
    memory_manager: MemoryManager = app.state.memory_manager
    tts: TTSManager = app.state.tts

    session = session_manager.create_or_restore(session_id)
    # Automatically default to LLM model routed dynamically at startup
    session.llm_model = llm.default_model
    
    vad = EnergyVAD(sample_rate=SAMPLE_RATE)

    # ── Background System Monitoring ──
    async def _system_monitor_loop():
        import psutil
        psutil.cpu_percent(interval=None) # Init
        try:
            while True:
                await asyncio.sleep(1)
                cpu = psutil.cpu_percent(interval=None)
                mem = psutil.virtual_memory()
                await _send(
                    websocket, 
                    "system_metrics", 
                    cpu_percent=round(cpu, 1), 
                    mem_percent=round(mem.percent, 1), 
                    mem_used_gb=round(mem.used / (1024**3), 2)
                )
        except asyncio.CancelledError:
            pass

    monitor_task = asyncio.create_task(_system_monitor_loop())

    logger.info(f"Client connected — session {session_id}")
    
    # We do NOT send session_started immediately here anymore. 
    # We wait for set_config so we can assign/restore the right session ID.
    
    # Send available models to client
    available_llms = await llm.get_available_models()
    await _send(
        websocket, 
        "available_models", 
        llms=available_llms,
        tts_engines=tts.get_available_engines(),
        tts_voices=tts.get_available_voices(),
        personas=list(PERSONAS.keys())
    )

    await _send(websocket, "tools_list", tools=list_tools())

    bytes_since_last_partial = 0

    # Pre-roll: remember the last 0.5s of audio while silent, so the first word
    # isn't clipped when the VAD notices speech a moment late.
    PREROLL_BYTES = int(BYTES_PER_SEC * 0.5)
    preroll = bytearray()

    try:
        while True:
            message = await websocket.receive()

            # Handle WebSocket disconnect gracefully
            if message.get("type") == "websocket.disconnect":
                logger.info(f"Client disconnected cleanly — session {session_id}")
                break

            if "text" in message:
                try:
                    config = json.loads(message["text"])
                    if config.get("type") == "set_config":
                        # If a session_id is explicitly requested by the frontend, use it.
                        requested_session = config.get("session_id")
                        if requested_session and requested_session != session_id:
                            # Cleanup temporary default session and load requested
                            session_manager.remove(session_id)
                            session_id = requested_session
                            session = session_manager.create_or_restore(session_id)
                            
                        if "llm_model" in config:
                            session.llm_model = config["llm_model"]
                        if "tts_engine" in config:
                            session.tts_engine = config["tts_engine"]
                        if "tts_voice" in config:
                            session.tts_voice = config["tts_voice"]
                        if "persona" in config:
                            session.persona = config["persona"]
                            
                        logger.info(f"[{session_id}] Config updated: LLM={session.llm_model}, TTS={session.tts_engine}/{session.tts_voice}, Persona={session.persona}")
                        
                        # Initialize persistence for this session
                        asyncio.create_task(
                            session_storage.initialize_session(session_id, {
                                "llm_model": session.llm_model,
                                "tts_engine": session.tts_engine,
                                "tts_voice": session.tts_voice,
                                "persona": session.persona
                            })
                        )
                        
                        await _send(websocket, "config_updated")
                        await _send(websocket, "session_started", session_id=session_id)

                    elif config.get("type") == "text_input" and config.get("text", "").strip():
                        # Typed command (fallback when the room is too noisy for the mic)
                        session.cancel_active_llm()
                        session.turn_metrics = {'speech_start_time': time.time(), 'speech_end_time': time.time()}
                        typed_task = asyncio.create_task(
                            _do_finalize_and_stream(
                                stt, llm, memory_manager, tts, session, websocket,
                                text_override=config["text"].strip(),
                            )
                        )
                        session._active_llm_task = typed_task
                except Exception as e:
                    logger.error(f"Failed to parse config: {e}")
                continue

            if "bytes" not in message:
                continue

            data = message["bytes"]

            # ── VAD ──
            is_speech = vad.process(data)
            now = time.monotonic()

            if is_speech:
                # ── Speech detected ──
                if not session.is_speaking:
                    session.is_speaking = True
                    session.speech_start = now
                    session.silence_start = None
                    session._llm_triggered = False
                    session.turn_metrics = {'speech_start_time': time.time()}
                    session.live_audio_buffer.extend(preroll)
                    preroll.clear()

                    # Cancel any in-progress LLM generation (barge-in)
                    session.cancel_active_llm()

                    await _send(websocket, "user_started_speaking")
                    logger.info(f"[{session_id}] speech start")

                session.live_audio_buffer.extend(data)
                bytes_since_last_partial += len(data)

                # ── Periodic partial STT ──
                if bytes_since_last_partial >= PARTIAL_CHUNK_BYTES:
                    bytes_since_last_partial = 0
                    asyncio.create_task(
                        _do_partial_transcription(stt, session, websocket)
                    )

            else:
                # ── Silence ──
                if not session.is_speaking:
                    preroll.extend(data)
                    del preroll[:-PREROLL_BYTES]
                if session.is_speaking:
                    session.live_audio_buffer.extend(data)

                    if session.silence_start is None:
                        session.silence_start = now

                    silence_duration = now - session.silence_start

                    if silence_duration >= SILENCE_TIMEOUT and not session._llm_triggered:
                        session._llm_triggered = True
                        session.is_speaking = False
                        session.turn_metrics['speech_end_time'] = time.time()
                        await _send(websocket, "user_stopped_speaking")
                        logger.info(
                            f"[{session_id}] silence {silence_duration:.1f}s → finalizing"
                        )

                        session.cancel_active_llm()

                        finalize_task = asyncio.create_task(
                            _do_finalize_and_stream(stt, llm, memory_manager, tts, session, websocket)
                        )
                        session._active_llm_task = finalize_task

    except WebSocketDisconnect:
        logger.info(f"Client disconnected — session {session_id}")
    except Exception as e:
        logger.error(f"WebSocket error [{session_id}]: {e}", exc_info=True)
        await _send(websocket, "error", message=str(e))
    finally:
        ACTIVE_WS_CONNECTIONS.discard(websocket)
        monitor_task.cancel()
        session.cancel_active_llm()
        session_manager.remove(session_id)


# ── Entrypoint ───────────────────────────────────────────────────────
if __name__ == "__main__":
    import uvicorn
    uvicorn.run("app.main:app", host="0.0.0.0", port=8000, reload=True)
