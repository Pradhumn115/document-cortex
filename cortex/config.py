"""Where data lives and where API keys come from.

CORTEX_HOME (default ~/.cortex) holds the library:
    ~/.cortex/.env              API keys (see .env.example)
    ~/.cortex/docs/<slug>/      one folder per document (source, tree, cards, index, OCR cache)
    ~/.cortex/history.jsonl     questions, answers and feedback (episodic memory)

Keys are read, without overriding the environment, from: ./.env, then $CORTEX_HOME/.env.
  GROQ_API_KEY        used first (free tier is fine)
  GROQ_PAID_API_KEY   used when the first key's daily quota runs out (optional)
  ANTHROPIC_API_KEY   for --provider anthropic (optional)
"""
from __future__ import annotations

import os
from pathlib import Path


def home() -> Path:
    h = Path(os.environ.get("CORTEX_HOME", Path.home() / ".cortex")).expanduser()
    (h / "docs").mkdir(parents=True, exist_ok=True)
    return h


def load_keys(extra: list[str | Path] | None = None) -> None:
    from .llm import load_env_file
    for p in [Path.cwd() / ".env", home() / ".env", *(extra or [])]:
        if p:
            load_env_file(p)


def keys_status() -> dict:
    return {k: bool(os.environ.get(k)) for k in ("GROQ_API_KEY", "GROQ_PAID_API_KEY", "ANTHROPIC_API_KEY")}
