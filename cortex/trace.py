"""Process trace: the real steps a question (or a document) goes through.

Each step is {"kind", "title", "detail", "items", "t", "ms"}:
  kind    short machine name (route, search, entities, rank, expand, answer, check, ...)
  title   one line a person can read
  detail  optional sentence explaining why
  items   optional list of short lines (hits, sources, verdicts)
  t       seconds since the trace started
  ms      duration of the step, if it was timed
Steps are pushed to `on_step` as they happen (the web UI streams them live).
"""
from __future__ import annotations

import time
from contextlib import contextmanager
from typing import Callable


class Trace:
    def __init__(self, on_step: Callable[[dict], None] | None = None):
        self.steps: list[dict] = []
        self.on_step = on_step
        self.t0 = time.time()

    def step(self, kind: str, title: str, detail: str | None = None, items: list[str] | None = None,
             ms: float | None = None) -> dict:
        rec = {"kind": kind, "title": title, "detail": detail, "items": items or [],
               "t": round(time.time() - self.t0, 2), "ms": round(ms) if ms is not None else None}
        self.steps.append(rec)
        if self.on_step:
            try:
                self.on_step(rec)
            except Exception:
                pass
        return rec

    @contextmanager
    def timed(self, kind: str, title: str):
        """with trace.timed("answer", "Writing the answer") as s: ...; s["detail"] = "..." """
        info: dict = {"detail": None, "items": [], "title": title}
        start = time.time()
        if self.on_step:  # tell the UI the step has started
            try:
                self.on_step({"kind": kind, "title": title, "running": True, "t": round(start - self.t0, 2)})
            except Exception:
                pass
        yield info
        self.step(kind, info["title"], info["detail"], info["items"], (time.time() - start) * 1000)


class NullTrace(Trace):
    def step(self, *a, **k) -> dict:
        return {}

    @contextmanager
    def timed(self, kind: str, title: str):
        yield {"detail": None, "items": [], "title": title}
