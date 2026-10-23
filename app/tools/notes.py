"""
notes.py — Voice-controlled notes tool.

Lets the user save, read and clear short text notes entirely by voice.

Usage examples:
    "save a note: buy oat milk"           -> save_note()
    "read my notes" / "what are my notes" -> read_notes()
    "clear my notes"                      -> clear_notes()
"""

import os
from datetime import datetime
from pathlib import Path

from .registry import tool

# Notes are stored as a plain text file next to the project root
_NOTES_FILE = Path(os.environ.get("NOTES_FILE", "notes.txt"))

# Maximum number of notes read back aloud at once (keeps the reply short)
_MAX_READ_BACK = 5


def _load_notes() -> list[str]:
    """Return a list of non-empty lines from the notes file."""
    if not _NOTES_FILE.exists():
        return []
    return [line.strip() for line in _NOTES_FILE.read_text(encoding="utf-8").splitlines() if line.strip()]


@tool
def save_note(text: str) -> str:
    """Save a note, reminder or message so you can read it back later.

    Use when the user says: save a note, remind me, note that, remember that, write this down.

    Args:
        text: the content of the note to save, for example buy oat milk
    """
    if not text.strip():
        return "The note was empty — nothing was saved."

    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    entry = f"[{timestamp}] {text.strip()}"

    with _NOTES_FILE.open("a", encoding="utf-8") as f:
        f.write(entry + "\n")

    return f"Note saved: {text.strip()}"


@tool
def read_notes() -> str:
    """Read back saved notes or reminders. Returns the most recent notes (up to 5).

    Use when the user says: read my notes, what are my notes, show my reminders, what did I save.
    """
    notes = _load_notes()
    if not notes:
        return "You have no notes saved yet."

    recent = notes[-_MAX_READ_BACK:]
    count = len(notes)

    if count > _MAX_READ_BACK:
        intro = f"You have {count} notes. Here are the last {_MAX_READ_BACK}: "
    else:
        intro = f"You have {count} note{'s' if count != 1 else ''}: "

    # Join with a pause-friendly separator for TTS
    return intro + " ... ".join(recent)


@tool
def clear_notes() -> str:
    """Delete all saved notes and reminders.

    Use when the user says: clear my notes, delete all notes, erase my reminders.
    """
    if not _NOTES_FILE.exists() or not _load_notes():
        return "There are no notes to clear."

    count = len(_load_notes())
    _NOTES_FILE.write_text("", encoding="utf-8")
    return f"Cleared {count} note{'s' if count != 1 else ''}."
