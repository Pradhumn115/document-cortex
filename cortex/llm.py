"""Minimal LLM clients. Every client is a callable (system, user) -> str.

Providers:
  anthropic  Claude via ANTHROPIC_API_KEY (default model: claude-haiku-5-5)
  openai     any OpenAI-compatible server: LM Studio (default base URL),
             Groq, vLLM, Ollama's /v1 ...  Set --base-url / OPENAI_BASE_URL.
  fake       deterministic stub for tests and dry runs.
"""
from __future__ import annotations

import json
import os
import re
import time
from typing import Callable

LLM = Callable[[str, str], str]

DEFAULT_MODELS = {
    "anthropic": "claude-haiku-5-5",
    "openai": "qwen/qwen3-next-80b",
    "groq": "openai/gpt-oss-120b",  # best card writer in bench/ (2026-11-13)
}


def load_env_file(path, rename: dict[str, str] | None = None) -> None:
    """Read KEY=VALUE lines into os.environ (without overriding what's set).
    rename maps a key in the file to a different env name (e.g. a second
    project's GROQ_API_KEY -> GROQ_PAID_API_KEY)."""
    from pathlib import Path
    p = Path(path).expanduser()
    if not p.exists():
        return
    for line in p.read_text(errors="ignore").splitlines():
        m = re.match(r"\s*([A-Z_][A-Z0-9_]*)\s*=\s*(.*)$", line)
        if not m:
            continue
        name = (rename or {}).get(m.group(1), m.group(1) if rename is None else None)
        if name:
            os.environ.setdefault(name, m.group(2).strip().strip("'\""))


# Groq quotas are per model, so when one model's daily quota is gone the next
# one usually still has requests left. Ordered by benchmark quality.
GROQ_FALLBACK = ["openai/gpt-oss-120b", "qwen/qwen3.8-27b", "openai/gpt-oss-20b"]


def make_llm(provider: str, model: str | None = None, base_url: str | None = None,
             max_tokens: int = 3000, temperature: float = 0.2, fallback: bool = True,
             log=print, api_key: str | None = None) -> LLM:
    # 3000 leaves room for reasoning models (gpt-oss, qwen3) that think before answering.
    if provider == "groq" and not model and fallback:
        # Free key first; when its daily quota runs out, the paid key (GROQ_PAID_API_KEY)
        # on the same best model, so card quality stays consistent. Without a paid key,
        # fall back through the free key's other models instead.
        chain, names = [], []
        free, paid = os.environ.get("GROQ_API_KEY"), os.environ.get("GROQ_PAID_API_KEY")
        if free:
            chain.append(make_llm("groq", GROQ_FALLBACK[0], base_url, max_tokens, temperature, False))
            names.append(f"{GROQ_FALLBACK[0].split('/')[-1]} on Groq (first key)")
        if paid:
            chain.append(make_llm("groq", GROQ_FALLBACK[0], base_url, max_tokens, temperature, False,
                                  api_key=paid))
            names.append(f"{GROQ_FALLBACK[0].split('/')[-1]} on Groq (paid key)")
        elif free:
            for m in GROQ_FALLBACK[1:]:
                chain.append(make_llm("groq", m, base_url, max_tokens, temperature, False))
                names.append(f"{m.split('/')[-1]} on Groq (first key)")
        if not chain:
            raise RuntimeError("no Groq key: set GROQ_API_KEY and/or GROQ_PAID_API_KEY")
        return _fallback_chain(chain, names, log)
    model = model or DEFAULT_MODELS.get(provider)
    if provider == "anthropic":
        import anthropic

        client = anthropic.Anthropic()

        def call(system: str, user: str) -> str:
            msg = client.messages.create(
                model=model, max_tokens=max_tokens, temperature=temperature,
                system=system, messages=[{"role": "user", "content": user}],
            )
            return "".join(b.text for b in msg.content if getattr(b, "type", "") == "text")
        return _with_retries(call, label=f"{model} (Anthropic)")

    if provider in ("openai", "groq"):
        import openai

        if provider == "groq":
            base_url = base_url or "https://api.groq.com/openai/v1"
            key = api_key or os.environ.get("GROQ_API_KEY") or os.environ.get("OPENAI_API_KEY")
            if not key:
                raise RuntimeError("set GROQ_API_KEY (or pass --env-file pointing at a .env that has it)")
        else:
            base_url = base_url or os.environ.get("OPENAI_BASE_URL", "http://127.0.0.1:1234/v1")
            key = os.environ.get("OPENAI_API_KEY", "lm-studio")
        client = openai.OpenAI(base_url=base_url, api_key=key, timeout=90, max_retries=0)

        def call(system: str, user: str) -> str:
            r = client.chat.completions.create(
                model=model, max_tokens=max_tokens, temperature=temperature,
                messages=[{"role": "system", "content": system}, {"role": "user", "content": user}],
            )
            return r.choices[0].message.content or ""
        return _with_retries(call, label=f"{model.split('/')[-1]}" + (" (Groq)" if provider == "groq" else ""))

    if provider == "fake":
        return fake_llm
    raise ValueError(f"unknown provider {provider!r}")


class DailyLimit(RuntimeError):
    """The provider's per-day quota is used up; retrying today is pointless."""


def _fallback_chain(llms: list[LLM], names: list[str], log=print) -> LLM:
    """Use the first model; on DailyLimit move to the next for the rest of the run."""
    import threading
    state = {"i": 0}
    lock = threading.Lock()

    def call(system: str, user: str) -> str:
        while True:
            with lock:
                i = state["i"]
            if i >= len(llms):
                raise DailyLimit("daily quota used up on every model: " + ", ".join(names))
            try:
                return llms[i](system, user)
            except DailyLimit:
                with lock:
                    if state["i"] == i:
                        state["i"] = i + 1
                        nxt = names[i + 1] if i + 1 < len(names) else "none left"
                        call.label = nxt
                        call.switched.append(f"{names[i]} daily quota used up -> {nxt}")
                        log(f"  ! daily quota used up on {names[i]}; switching to {nxt}")
    call.label = names[0] if names else "?"
    call.switched = []
    return call


_DAILY = re.compile(r"per day|\(RPD\)|\(TPD\)|daily", re.I)


def _with_retries(fn: LLM, attempts: int = 7, label: str = "") -> LLM:
    def call(system: str, user: str) -> str:
        for i in range(attempts):
            try:
                return fn(system, user)
            except Exception as e:  # network, rate limit (free tiers reset per minute), overload
                if _DAILY.search(str(e)):
                    raise DailyLimit(str(e)[:300]) from e
                if i == attempts - 1:
                    raise
                time.sleep(min(60, 2 ** i * 3))
        raise AssertionError("unreachable")
    call.label = label
    return call


def fake_llm(system: str, user: str) -> str:
    """Echo a valid card built from the first words of the input."""
    title = re.search(r"TITLE:\s*(.+)", user)
    words = re.findall(r"\S+", user.split("CONTENT:", 1)[-1])[:30]
    return json.dumps({
        "summary": " ".join(words),
        "key_terms": words[:5],
        "entities": [],
        "questions": [f"What does {title.group(1).strip() if title else 'this'} say?"],
    }, ensure_ascii=False)


def parse_json_object(text: str) -> dict:
    """Pull the first JSON object out of a model reply (tolerates ```json fences
    and leading prose)."""
    text = re.sub(r"^```(?:json)?|```$", "", text.strip(), flags=re.M).strip()
    try:
        return json.loads(text, strict=False)  # tolerate raw newlines inside strings
    except json.JSONDecodeError:
        pass
    start = text.find("{")
    depth = 0
    for i in range(start, len(text)) if start >= 0 else ():
        if text[i] == "{":
            depth += 1
        elif text[i] == "}":
            depth -= 1
            if depth == 0:
                return json.loads(text[start:i + 1], strict=False)
    raise ValueError(f"no JSON object in reply: {text[:200]!r}")
