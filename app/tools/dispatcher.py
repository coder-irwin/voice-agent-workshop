"""
dispatcher.py — Turns "what the user said" into "tools that ran".

Flow:
    transcript ──► GATE: "is this a command for the computer, or just chat?"
                      │ chat ──► [] (normal reply, no tool is touched)
                      ▼ command
                   Ollama (with the tool list attached): "which tool?"
                      │ no tool_calls ──► []
                      ▼
               run each tool (registry.run_tool) ──► results

Both are small, separate LLM calls. The normal streaming reply
(and the voice) is generated afterwards and told what just happened.

Why a gate? Small models over-call tools on casual chat ("hello" -> get_current_time).
We measured it (qwen2.5:3b, 10 chat phrases + 10 commands): the router alone wrongly
fired on 3 of 10 chat phrases; with the yes/no gate in front, 0 of 10. 10 of 10 real
commands routed correctly (after sharpening the open_website docstring).
"""

import json
import logging
import os
import time

from .registry import get_schemas, run_tool

logger = logging.getLogger(__name__)

TOOLS_ENABLED = os.environ.get("TOOLS_ENABLED", "1") == "1"
TOOL_GATE = os.environ.get("TOOL_GATE", "1") == "1"   # 0 = skip the chat/command check
OLLAMA_URL = os.environ.get("OLLAMA_BASE_URL", "http://localhost:11434")

ROUTER_PROMPT = (
    "You are the action router of a voice assistant. "
    "If the user asks for something a tool can do (open a website, play a video, search, "
    "scroll, time, save/read/clear notes or reminders...), call the matching tool. "
    "Use as FEW tools as possible: one tool is usually enough (to play something on YouTube, "
    "call only play_youtube). "
    "If the user is just chatting or asking a question, do NOT call any tool."
)


GATE_PROMPT = (
    "Decide if the user is giving a COMMAND for the computer to carry out right now "
    "(open a website, play a video or song, search the web, scroll, take a screenshot, "
    "tell the time, roll a dice, save a note, read my notes, clear my notes, remind me, ...) "
    "or is just talking (greeting, thanks, a question, a joke, chit-chat). "
    'Reply with JSON only: {"command": true} or {"command": false}.'
)


async def _is_command(llm, model: str, user_text: str) -> bool:
    """Cheap yes/no check. If anything goes wrong we say True, so the router still gets a chance."""
    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": GATE_PROMPT},
            {"role": "user", "content": user_text},
        ],
        "stream": False,
        "format": "json",
        "keep_alive": "30m",
        "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 20},
    }
    try:
        async with llm._session.post(f"{OLLAMA_URL}/api/chat", json=payload) as resp:
            if resp.status != 200:
                return True
            data = await resp.json()
        return bool(json.loads(data["message"]["content"]).get("command"))
    except Exception as e:
        logger.warning(f"[tools] gate failed ({e}); falling back to the router")
        return True


async def detect_and_run(llm, model: str, user_text: str) -> list[dict]:
    """
    Ask the LLM which tools to call, run them, and return one dict per call:
        {"name", "arguments", "ok", "result", "ms"}
    Returns [] when no action is needed.
    """
    if not TOOLS_ENABLED or not llm.is_ollama_available:
        return []

    model = os.environ.get("TOOL_MODEL", model)
    if TOOL_GATE:
        t_gate = time.perf_counter()
        is_command = await _is_command(llm, model, user_text)
        logger.info(f"[tools] gate: command={is_command} ({(time.perf_counter() - t_gate) * 1000:.0f}ms)")
        if not is_command:
            return []

    payload = {
        "model": model,
        "messages": [
            {"role": "system", "content": ROUTER_PROMPT},
            {"role": "user", "content": user_text},
        ],
        "tools": get_schemas(),
        "stream": False,
        "keep_alive": "30m",
        "options": {"temperature": 0, "num_ctx": 2048, "num_predict": 120},
    }

    t0 = time.perf_counter()
    try:
        async with llm._session.post(f"{OLLAMA_URL}/api/chat", json=payload) as resp:
            if resp.status != 200:
                logger.warning(f"[tools] router returned {resp.status}: {await resp.text()}")
                return []
            data = await resp.json()
    except Exception as e:
        logger.warning(f"[tools] router failed: {e}")
        return []

    tool_calls = data.get("message", {}).get("tool_calls") or []
    logger.info(f"[tools] router decided in {(time.perf_counter() - t0) * 1000:.0f}ms -> {len(tool_calls)} call(s)")

    results = []
    for call in tool_calls[:3]:  # safety cap: never more than 3 actions per sentence
        fn = call.get("function", {})
        name, args = fn.get("name", ""), fn.get("arguments") or {}
        t_tool = time.perf_counter()
        ok, result = await run_tool(name, args)
        ms = round((time.perf_counter() - t_tool) * 1000)
        logger.info(f"[tools] {name}({args}) -> ok={ok} '{result}' ({ms}ms)")
        results.append({"name": name, "arguments": args, "ok": ok, "result": result, "ms": ms})
    return results
