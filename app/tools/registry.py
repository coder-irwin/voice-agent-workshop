"""
registry.py — The tool "phone book".

A tool is just a normal Python function with the @tool decorator on top.
The decorator reads the function's name, docstring and type hints and turns
them into the JSON schema that the LLM needs in order to call it.

    @tool
    def roll_dice(sides: int = 6) -> str:
        '''Roll a dice and tell the result.

        Args:
            sides: how many sides the dice has
        '''
        return f"Rolled {random.randint(1, sides)}"

That's it — no other file needs to change. See app/tools/my_tools.py.
"""

import asyncio
import inspect
import json
import logging
from typing import Any, Callable

logger = logging.getLogger(__name__)

# name -> {"fn": function, "schema": ollama-style tool schema}
TOOLS: dict[str, dict] = {}

_JSON_TYPES = {str: "string", int: "integer", float: "number", bool: "boolean"}


def _parse_arg_docs(doc: str) -> dict[str, str]:
    """Read the 'Args:' section of a docstring -> {param_name: description}."""
    arg_docs: dict[str, str] = {}
    in_args = False
    for line in doc.splitlines():
        stripped = line.strip()
        if stripped.lower() == "args:":
            in_args = True
            continue
        if in_args and ":" in stripped:
            pname, _, pdesc = stripped.partition(":")
            arg_docs[pname.strip()] = pdesc.strip()
    return arg_docs


def tool(fn: Callable) -> Callable:
    """Decorator: register `fn` as a voice-callable tool."""
    doc = inspect.getdoc(fn) or fn.__name__
    description = doc.split("\n\n")[0].replace("\n", " ").strip()
    arg_docs = _parse_arg_docs(doc)

    properties: dict[str, Any] = {}
    required: list[str] = []
    for pname, param in inspect.signature(fn).parameters.items():
        json_type = _JSON_TYPES.get(param.annotation, "string")
        properties[pname] = {"type": json_type, "description": arg_docs.get(pname, pname)}
        if param.default is inspect.Parameter.empty:
            required.append(pname)

    TOOLS[fn.__name__] = {
        "fn": fn,
        "schema": {
            "type": "function",
            "function": {
                "name": fn.__name__,
                "description": description,
                "parameters": {"type": "object", "properties": properties, "required": required},
            },
        },
    }
    return fn


def get_schemas() -> list[dict]:
    """All tool schemas, ready to send to Ollama's `tools` field."""
    return [t["schema"] for t in TOOLS.values()]


def list_tools() -> list[dict]:
    """Short {name, description} list for showing in the UI."""
    return [
        {"name": n, "description": t["schema"]["function"]["description"]}
        for n, t in TOOLS.items()
    ]


async def run_tool(name: str, arguments: dict) -> tuple[bool, str]:
    """
    Run a tool by name. Returns (ok, result_text).
    Only registered tools can run — anything else the LLM invents is refused.
    """
    entry = TOOLS.get(name)
    if entry is None:
        return False, f"Unknown tool '{name}'"

    fn = entry["fn"]
    allowed = entry["schema"]["function"]["parameters"]["properties"]
    if isinstance(arguments, str):
        try:
            arguments = json.loads(arguments)
        except Exception:
            arguments = {}
    # Drop any argument the LLM made up that the function doesn't accept
    clean_args = {k: v for k, v in (arguments or {}).items() if k in allowed}
    try:
        if asyncio.iscoroutinefunction(fn):
            result = await fn(**clean_args)
        else:
            result = await asyncio.to_thread(fn, **clean_args)
        return True, str(result)
    except Exception as e:  # a broken tool must never crash the voice pipeline
        logger.error(f"[tools] {name} failed: {e}", exc_info=True)
        return False, f"{name} failed: {e}"
