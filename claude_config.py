"""Local credentials shared by the HackLab voice bridge and CLI director."""

import os
from pathlib import Path


def load_env(path: Path) -> None:
    """KEY=VALUE lines of a .env file into the environment; what is already set wins."""
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return
    for line in lines:
        key, sep, value = line.strip().removeprefix("export ").partition("=")
        if sep and key.strip() and not key.startswith("#"):
            os.environ.setdefault(key.strip(), value.strip().strip("'\""))


def claude_api_key() -> str | None:
    key = os.environ.get("CLAUDE_API_KEY", "").strip()
    return key if key and key.casefold() != "enter your key" else None
