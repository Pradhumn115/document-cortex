"""Index cards: one per tree node, written bottom-up.

A leaf's card is written from its own text. A parent's card is written from its
own text (truncated) plus its children's cards, so cost grows with the number
of nodes, not with the size of the book at each level.

Cards are cached in cards.jsonl keyed by node id plus a hash of the input, so a
run can be stopped and resumed, and only changed nodes are rewritten.
"""
from __future__ import annotations

import hashlib
import json
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path

from .llm import LLM, DailyLimit, parse_json_object
from .tree import Tree

SYSTEM = """You write library index cards for sections of a document so that a \
search system and an AI agent can find the right section later.

Return ONLY a JSON object with these keys:
  "summary":    2-4 plain sentences in English saying what this section covers \
and its main claims. Be specific (names, quantities, conditions), no filler.
  "key_terms":  up to 12 exact terms as they appear in the source script \
(e.g. Devanagari), each optionally followed by an English gloss in parentheses.
  "entities":   up to 12 named things (substances, people, places, conditions, \
concepts) in "Name (type)" form.
  "questions":  4-6 questions a reader could answer from this section. Write \
about half in the source language and half in English (all English if the \
source is English).
  "title":      a short descriptive title for the section (3-8 words).

Use only information present in the input. If the input is mostly noise, say \
so in the summary and keep lists short."""

LEAF_LIMIT = 6000
SHORT_LEAF = 150  # leaves at or under this many characters get a card without a model call
OWN_TEXT_LIMIT = 2000
CHILD_CARD_LIMIT = 600


def card_input(tree: Tree, node_id: str, cards: dict[str, dict]) -> str:
    node = tree.nodes[node_id]
    parts = [f"PATH: {tree.path_str(node_id)}", f"TITLE: {node.title}", "CONTENT:"]
    if not node.children:
        parts.append(node.text[:LEAF_LIMIT] or "(empty)")
        return "\n".join(parts)
    if node.text:
        parts.append(node.text[:OWN_TEXT_LIMIT])
    parts.append("\nSUBSECTIONS (summarised):")
    budget = max(CHILD_CARD_LIMIT, 24000 // max(1, len(node.children)))
    for cid in node.children:
        c = cards.get(cid, {})
        line = f"- {tree.nodes[cid].title}: {c.get('summary', '')}"
        parts.append(line[:budget])
    return "\n".join(parts)


def _hash(s: str) -> str:
    return hashlib.sha1(s.encode("utf-8")).hexdigest()[:16]


def load_cards(path: Path) -> dict[str, dict]:
    cards: dict[str, dict] = {}
    if path.exists():
        for line in path.read_text(encoding="utf-8").splitlines():
            if line.strip():
                rec = json.loads(line)
                cards[rec["node_id"]] = rec  # later lines win
    return cards


def write_cards(tree: Tree, llm: LLM, out: Path, workers: int = 4,
                node_filter=None, log=print, reasoning: str | None = None,
                short_leaf: int = SHORT_LEAF) -> dict[str, dict]:
    # gpt-oss reads "Reasoning: low|medium|high" from the system prompt.
    # Qwen3-family models skip their thinking phase when the prompt says /no_think.
    if reasoning == "nothink":
        system = f"/no_think\n\n{SYSTEM}"
    else:
        system = f"Reasoning: {reasoning}\n\n{SYSTEM}" if reasoning else SYSTEM
    cards = load_cards(out)
    lock = threading.Lock()
    stats = {"written": 0, "cached": 0, "failed": 0}

    stop = threading.Event()

    def one(nid: str) -> None:
        if stop.is_set():
            return
        text = card_input(tree, nid, cards)
        h = _hash(text)
        if nid in cards and cards[nid].get("input_hash") == h:
            stats["cached"] += 1
            return
        node = tree.nodes[nid]
        try:
            if not node.children and len(node.text.strip()) <= short_leaf:
                # Too short to be worth a model call: the text is its own summary.
                data = {"summary": node.text.strip() or node.title, "auto": True}
            else:
                data = parse_json_object(llm(system, text))
        except DailyLimit:
            stop.set()
            raise
        except Exception as e:  # keep going; the node keeps no card
            stats["failed"] += 1
            log(f"  ! {nid} {tree.nodes[nid].title}: {e}")
            return
        if node.meta.get("synthetic_title") and str(data.get("title", "")).strip():
            # "Part 3 (p. 5–7)" becomes "Socket server setup (p. 5–7)".
            node.title = f"{str(data['title']).strip()} — {node.title}"
            node.meta["synthetic_title"] = False
        rec = {
            "node_id": nid,
            "title": tree.nodes[nid].title,
            "path": tree.path_str(nid),
            "summary": str(data.get("summary", "")).strip(),
            "key_terms": [str(x) for x in data.get("key_terms", [])][:12],
            "entities": [str(x) for x in data.get("entities", [])][:12],
            "questions": [str(x) for x in data.get("questions", [])][:6],
            "input_hash": h,
        }
        if data.get("auto"):
            rec["auto"] = True
        with lock:
            cards[nid] = rec
            with out.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            stats["written"] += 1

    for level in tree.bottom_up():
        ids = [n for n in level if node_filter is None or node_filter(n)]
        if not ids:
            continue
        with ThreadPoolExecutor(max_workers=workers) as pool:
            futures = [pool.submit(one, nid) for nid in ids]
            for i, f in enumerate(as_completed(futures), start=1):
                try:
                    f.result()
                except DailyLimit:
                    pass  # stop is set; remaining tasks return immediately
                if i % 25 == 0 or i == len(ids):
                    log(f"  depth {tree.depth(ids[0])}: {i}/{len(ids)}  {stats}")
        if stop.is_set():
            raise DailyLimit(f"daily quota reached after {stats['written']} new cards")
    return {k: v for k, v in cards.items() if k in tree.nodes}  # drop cards of old outlines


def write_cards_by_section(tree: Tree, llm: LLM, out: Path, log=print, **kw) -> dict[str, dict]:
    """Finish one section (herb) subtree at a time, in book order, then the
    groups and the root. With a daily request cap this means every finished
    section is complete and searchable, instead of the whole book being
    half-done at the leaf level."""
    sections = [n for g in tree.root.children for n in tree.nodes[g].children]
    cards: dict[str, dict] = {}
    for i, sid in enumerate(sections, 1):
        members = set()
        stack = [sid]
        while stack:
            n = stack.pop()
            members.add(n)
            stack.extend(tree.nodes[n].children)
        cards = write_cards(tree, llm, out, node_filter=members.__contains__, log=lambda *_: None, **kw)
        done = sum(1 for m in members if m in cards)
        log(f"[{i}/{len(sections)}] {sid} {tree.nodes[sid].title}: {done}/{len(members)} cards")
    tops = set(tree.root.children) | {"root"}
    return write_cards(tree, llm, out, node_filter=tops.__contains__, log=log, **kw)
