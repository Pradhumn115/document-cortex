"""The document library: ingest, list, ask, remember.

Every document lives in $CORTEX_HOME/docs/<slug>/:
    source.<ext>     the original file
    meta.json        name, status, progress, counts, errors
    tree.json        structure (sections, page ranges, chunks)
    cards.jsonl      index cards
    entities.json    entity -> sections (lightweight concept graph)
    index/           search index (items + vectors)
    ocr/             per-page OCR cache

Episodic memory: $CORTEX_HOME/history.jsonl records every question, its answer,
sources, citation check, and 👍/👎 feedback. `consolidate()` adds questions that
were answered well (👍) to the cards of the sections that answered them, then
re-indexes, so the same kind of question finds the right section faster next time.
"""
from __future__ import annotations

import json
import re
import shutil
import threading
import time
import uuid
from pathlib import Path
from typing import Callable

from . import config

Progress = Callable[[str, float], None]  # (message, fraction 0..1)


def slugify(name: str) -> str:
    s = re.sub(r"[^\w.-]+", "-", Path(name).stem, flags=re.U).strip("-")[:60] or "doc"
    return s


# ------------------------------------------------------------------ ingest
def ingest_document(src: Path, work: Path, progress: Progress | None = None, *, provider: str = "groq",
                    model: str | None = None, workers: int = 4, embedder: str = "e5-small",
                    chunk_chars: int = 900, ocr: str = "auto", ocr_engine: str = "auto",
                    ocr_langs: str | None = None, title: str | None = None, log=print) -> dict:
    """Full pipeline for one file. Returns counts; raises on hard failure."""
    from .ask import build_entity_index
    from .cards import load_cards, write_cards
    from .ingest import build_tree
    from .llm import DailyLimit, make_llm
    from .models import make_embedder
    from .search import build_index
    from .tree import build_chunks

    say = progress or (lambda m, f: log(m))
    work.mkdir(parents=True, exist_ok=True)

    say(f"Reading {src.name} ({src.stat().st_size / 1024:,.0f} KB)", 0.02)
    tree = build_tree(src, ocr=ocr, ocr_engine=ocr_engine, ocr_langs=ocr_langs, cache_dir=work / "ocr",
                      log=lambda m: say(m.strip(), 0.1))
    if title or tree.root.title in ("source", src.stem):
        tree.root.title = Path(title or src.name).stem  # stored as source.<ext>; show the real name
    build_chunks(tree, chunk_chars)
    tree.save(work / "tree.json")
    meta = tree.root.meta
    info = {"nodes": len(tree.nodes), "chunks": len(tree.chunks),
            "sections": [tree.nodes[c].title for c in tree.root.children][:30], "ocr": meta.get("ocr"),
            "structure": meta.get("structure")}
    if meta.get("ocr"):
        o = meta["ocr"]
        say(f"OCR done: {o['pages']} pages, engine {o['engine']}"
            + (f"; low confidence on pages {o['low_confidence_pages']}" if o["low_confidence_pages"] else ""), 0.2)
    say(f"Structure: {meta.get('structure') or 'document headings'}", 0.22)
    tops = [tree.nodes[c].title for c in tree.root.children]
    say(f"Outline: {len(tree.nodes) - 1} sections, {len(tops)} at the top level"
        + (f" — {', '.join(t[:40] for t in tops[:5])}{'…' if len(tops) > 5 else ''}" if tops else ""), 0.23)
    say(f"Chunks: {len(tree.chunks)} passages of up to {chunk_chars} characters, "
        f"split at paragraph and sentence boundaries", 0.25)

    total = len(tree.nodes)
    done = {"n": 0}
    lock = threading.Lock()

    def card_log(m: str) -> None:
        if m.strip().startswith("!"):
            say(m.strip(" !"), 0.25 + 0.6 * done["n"] / max(1, total))

    llm = make_llm(provider, model, log=card_log)

    def counted(system, user):
        r = llm(system, user)
        with lock:
            done["n"] += 1
            say(f"Writing index cards: {done['n']} of ~{total}", 0.25 + 0.6 * min(1, done["n"] / max(1, total)))
        return r

    note = None
    say(f"Index cards: writing one per section with {getattr(llm, 'label', 'the card model')} "
        f"(summary, key terms, entities, questions it answers), deepest sections first", 0.26)
    try:
        cards = write_cards(tree, counted, work / "cards.jsonl", workers=workers, log=card_log)
    except DailyLimit as e:
        cards = {k: v for k, v in load_cards(work / "cards.jsonl").items() if k in tree.nodes}
        note = f"Stopped early: {e}. Cards so far are saved; re-run ingest later to finish."
    tree.save(work / "tree.json")  # synthetic part titles were filled in by the card writer
    info["cards"] = len(cards)
    auto = sum(1 for c in cards.values() if c.get("auto"))
    say(f"Index cards ready: {len(cards)} ({done['n']} written by the model, "
        f"{max(0, len(cards) - done['n'] - auto)} reused from cache, {auto} short sections used as-is)", 0.88)

    say(f"Search index: embedding {len(cards) + len(tree.chunks):,} cards and chunks with {embedder}, "
        f"plus a keyword (BM25) index", 0.9)
    build_index(tree, cards, work / "index", make_embedder(embedder))
    (work / "index" / "meta.json").write_text(json.dumps({"embedder": embedder}))
    ents = build_entity_index(cards)
    (work / "entities.json").write_text(json.dumps(ents, ensure_ascii=False))
    say(f"Entity index: {len(ents):,} named things linked to the sections that mention them", 0.97)
    if note:
        info["note"] = note
    say("Ready" if not note else note, 1.0)
    return info


# ----------------------------------------------------------------- library
class Library:
    def __init__(self, root: Path | None = None):
        self.root = Path(root) if root else config.home()
        self.docs_dir = self.root / "docs"
        self.docs_dir.mkdir(parents=True, exist_ok=True)
        self.history = self.root / "history.jsonl"
        self._cache: dict[str, object] = {}
        self._embedders: dict[str, object] = {}
        self._lock = threading.Lock()

    # ---- documents
    def meta(self, slug: str) -> dict:
        p = self.docs_dir / slug / "meta.json"
        return json.loads(p.read_text(encoding="utf-8")) if p.exists() else {}

    def _write_meta(self, slug: str, **upd) -> dict:
        m = self.meta(slug)
        m.update(upd)
        (self.docs_dir / slug / "meta.json").write_text(json.dumps(m, ensure_ascii=False, indent=1),
                                                        encoding="utf-8")
        return m

    def list(self) -> list[dict]:
        out = []
        for d in sorted(self.docs_dir.iterdir(), key=lambda p: p.stat().st_mtime, reverse=True):
            if (d / "meta.json").exists():
                out.append({"slug": d.name, **self.meta(d.name)})
        return out

    def add(self, src: Path, name: str | None = None, background: bool = False,
            progress_print: bool = False, **opts) -> str:
        name = name or src.name
        slug = slugify(name)
        base, i = slug, 2
        while (self.docs_dir / slug / "meta.json").exists() and self.meta(slug).get("name") != name:
            slug, i = f"{base}-{i}", i + 1
        work = self.docs_dir / slug
        work.mkdir(parents=True, exist_ok=True)
        dest = work / f"source{src.suffix.lower()}"
        if src.resolve() != dest.resolve():
            shutil.copyfile(src, dest)
        self._write_meta(slug, name=name, file=src.name, added=time.strftime("%Y-%m-%d %H:%M"),
                         status="queued", progress=0.0, message="Queued", error=None)
        self._cache.pop(slug, None)

        def run():
            last = {"msg": None, "cards": 0}

            steps: list[dict] = []
            t0 = time.time()

            def prog(msg, frac):
                # Card progress updates one line in place; every other message is a new step.
                rec = {"t": round(time.time() - t0, 1), "msg": msg}
                if steps and msg.startswith("Writing index cards") and steps[-1]["msg"].startswith("Writing index cards"):
                    steps[-1] = rec
                elif not steps or steps[-1]["msg"] != msg:
                    steps.append(rec)
                self._write_meta(slug, status="processing", progress=round(frac, 3), message=msg, steps=steps[-200:])
                if progress_print and msg != last["msg"]:
                    is_card = msg.startswith("Writing index cards")
                    last["cards"] += is_card
                    if not is_card or last["cards"] % 10 == 1:
                        print(f"   [{int(frac * 100):3d}%] {msg}", flush=True)
                    last["msg"] = msg
            try:
                info = ingest_document(dest, work, prog, title=name, **opts)
                self._write_meta(slug, status="ready" if not info.get("note") else "partial",
                                 progress=1.0, message=info.get("note") or "Ready",
                                 seconds=round(time.time() - t0), **{
                                     k: info[k] for k in ("nodes", "chunks", "cards", "sections", "ocr", "structure")})
            except Exception as e:
                self._write_meta(slug, status="error", message=str(e)[:500], error=type(e).__name__)
            self._cache.pop(slug, None)

        if background:
            threading.Thread(target=run, daemon=True).start()
        else:
            run()
        return slug

    def delete(self, slug: str) -> None:
        target = (self.docs_dir / slug).resolve()
        if target.parent != self.docs_dir.resolve() or not target.exists():
            raise FileNotFoundError(slug)
        shutil.rmtree(target)
        self._cache.pop(slug, None)

    def embedder(self, name: str):
        with self._lock:
            if name not in self._embedders:
                from .models import make_embedder
                self._embedders[name] = make_embedder(name)
            return self._embedders[name]

    def doc(self, slug: str):
        from .ask import Doc
        if slug not in self._cache:
            work = self.docs_dir / slug
            emb = json.loads((work / "index" / "meta.json").read_text())["embedder"] \
                if (work / "index" / "meta.json").exists() else "e5-small"
            self._cache[slug] = Doc.load(work, self.embedder(emb), name=self.meta(slug).get("name"))
        return self._cache[slug]

    def ready(self) -> list[str]:
        return [d["slug"] for d in self.list() if d.get("status") in ("ready", "partial")]

    # ---- asking
    def ask(self, question: str, slugs: list[str] | None = None, *, provider: str = "groq",
            model: str | None = None, verify: bool = True, k: int = 6, on_step=None) -> dict:
        """Answer from the library. on_step(step) receives each process step live."""
        from .ask import answer_docs
        from .llm import make_llm
        from .trace import Trace
        slugs = slugs or self.ready()
        if not slugs:
            raise ValueError("no documents are ready yet; add one first")
        trace = Trace(on_step)
        with trace.timed("load", f"Opening {len(slugs)} document index{'es' if len(slugs) > 1 else ''}") as st:
            docs = [self.doc(s) for s in slugs]
            st["items"] = [f"{d.name}: {len(d.tree.nodes)} sections, {len(d.searcher.items):,} cards and chunks"
                           for d in docs][:12]
        quiet = lambda *_: None
        llm = make_llm(provider, model, log=quiet)
        res = answer_docs(docs, llm, question, k=k, trace=trace)
        for note in getattr(llm, "switched", []):
            trace.step("quota", "Switched API key", note)
        res["raw_answer"] = res["answer"]
        res["check"] = None
        if verify:
            from .verify import summary_line, verify as check
            checker = make_checker(provider, quiet)
            with trace.timed("check", f"Checking every cited sentence against its source "
                                      f"({getattr(checker, 'label', 'small model')})") as st:
                res["answer"], res["check"] = check(checker, res["answer"], res["sources"])
                st["detail"] = summary_line(res["check"])
                mark = {"supported": "✓", "partial": "◐", "re-cited": "↻", "unsupported": "✗"}
                st["items"] = [f"{mark.get(d['verdict'], '?')} {d['verdict']}: “{d['claim'][:90]}”"
                               + (f" — {d['note']}" if d.get("note") and d["verdict"] != "supported" else "")
                               for d in res["check"].get("details", [])]
        else:
            trace.step("check", "Citation check skipped", "Turned off for this question.")
        res["trace"] = trace.steps
        res["id"] = uuid.uuid4().hex[:12]
        res["question"] = question
        res["docs"] = slugs
        self._log({"id": res["id"], "ts": time.strftime("%Y-%m-%d %H:%M:%S"), "question": question,
                   "docs": slugs, "route": res["route"], "answer": res["answer"],
                   "sources": [{"doc": s["doc"], "node_id": s["node_id"], "path": s["path"]}
                               for s in res["sources"]],
                   "check": {k: v for k, v in (res["check"] or {}).items() if k != "details"},
                   "trace": [{k: v for k, v in st.items() if k != "items"} for st in trace.steps],
                   "feedback": 0})
        return res

    # ---- memory
    def _log(self, rec: dict) -> None:
        with self._lock, self.history.open("a", encoding="utf-8") as f:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def history_items(self, limit: int = 100) -> list[dict]:
        if not self.history.exists():
            return []
        items: dict[str, dict] = {}
        for line in self.history.read_text(encoding="utf-8").splitlines():
            if line.strip():
                r = json.loads(line)
                if r.get("type") == "feedback":
                    if r["id"] in items:
                        items[r["id"]]["feedback"] = r["value"]
                else:
                    items[r["id"]] = r
        return list(items.values())[-limit:][::-1]

    def feedback(self, qid: str, value: int) -> None:
        self._log({"type": "feedback", "id": qid, "value": max(-1, min(1, int(value))),
                   "ts": time.strftime("%Y-%m-%d %H:%M:%S")})

    def consolidate(self, log=print) -> dict:
        """👍 answers teach the cards: the question is added to the cards of the
        sections it cited, and those documents are re-indexed."""
        from .ask import build_entity_index
        from .cards import load_cards
        from .search import build_index
        from .tree import Tree
        learned: dict[str, dict[str, list[str]]] = {}
        for h in self.history_items(limit=100000):
            if h.get("feedback", 0) > 0:
                cited = {int(n) for n in re.findall(r"\[(\d+)\]", h.get("answer", ""))}
                for i, s in enumerate(h.get("sources", []), 1):
                    if i in cited:
                        learned.setdefault(s["doc"], {}).setdefault(s["node_id"], []).append(h["question"])
        changed = {}
        for slug, per_node in learned.items():
            work = self.docs_dir / slug
            if not work.exists():
                continue
            cards = load_cards(work / "cards.jsonl")
            n = 0
            for nid, qs in per_node.items():
                if nid in cards:
                    have = set(cards[nid].get("learned_questions", []))
                    new = [q for q in qs if q not in have]
                    if new:
                        cards[nid]["learned_questions"] = sorted(have | set(new))
                        n += len(new)
            if not n:
                continue
            with (work / "cards.jsonl").open("w", encoding="utf-8") as f:
                for c in cards.values():
                    f.write(json.dumps(c, ensure_ascii=False) + "\n")
            tree = Tree.load(work / "tree.json")
            emb = json.loads((work / "index" / "meta.json").read_text())["embedder"]
            build_index(tree, {k: v for k, v in cards.items() if k in tree.nodes}, work / "index",
                        self.embedder(emb))
            (work / "entities.json").write_text(json.dumps(build_entity_index(cards), ensure_ascii=False))
            self._cache.pop(slug, None)
            changed[slug] = n
            log(f"  {slug}: {n} questions learned")
        return changed


def make_checker(provider: str, log):
    """Small model for citation checks: gpt-oss-20b, first key then paid key."""
    import os
    from .llm import _fallback_chain, make_llm
    if provider != "groq":
        return make_llm(provider, None, log=log)
    chain, names = [], []
    for env, label in (("GROQ_API_KEY", "first"), ("GROQ_PAID_API_KEY", "paid")):
        if os.environ.get(env):
            chain.append(make_llm("groq", "openai/gpt-oss-20b", fallback=False, api_key=os.environ[env]))
            names.append(f"gpt-oss-20b ({label} key)")
    return _fallback_chain(chain, names, log)
