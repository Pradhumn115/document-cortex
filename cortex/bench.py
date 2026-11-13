"""Benchmark card-writing models on a fixed slice of the book.

For each model: write cards for the benchmark subtrees, build a hybrid index
over those cards + the same chunks, and ask the gold questions.

Metrics
  card_hit@1/@5, card_mrr   search over cards only: isolates card quality
  all_hit@1/@5              cards + chunks together (what the system uses)
  herb@1                    top result is inside the right herb at all
  grounded                  share of Devanagari key terms that literally occur
                            in the node's source text (hallucination proxy)
  json_fail                 replies that were not parseable JSON
  sec_per_call              mean wall time per model call
A "chunks-only" baseline (no cards at all) shows what cards add.
"""
from __future__ import annotations

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from .cards import write_cards
from .llm import make_llm
from .search import Searcher, build_index
from .tree import Tree

BENCH_HERBS = ["h006", "h114", "h331", "h442"]
DEVANAGARI = re.compile(r"[ऀ-ॿ]+")


@dataclass
class ModelSpec:
    name: str
    provider: str
    model: str
    base_url: str | None = None
    reasoning: str | None = None
    workers: int = 1


def subtree(tree: Tree, root: str) -> list[str]:
    out, stack = [], [root]
    while stack:
        n = stack.pop()
        out.append(n)
        stack.extend(tree.nodes[n].children)
    return out


def under(tree: Tree, node_id: str, gold: list[str]) -> bool:
    cur = node_id
    while cur:
        if cur in gold:
            return True
        cur = tree.nodes[cur].parent
    return False


def herb_of(tree: Tree, node_id: str) -> str | None:
    cur = node_id
    while cur and tree.nodes[cur].kind != "section":
        cur = tree.nodes[cur].parent
    return cur


def subset_tree(tree: Tree, keep: set[str]) -> Tree:
    t = Tree.__new__(Tree)
    t.nodes, t.root = tree.nodes, tree.root
    t.chunks = [c for c in tree.chunks if c.node_id in keep]
    return t


def evaluate(tree: Tree, searcher: Searcher, gold: list[dict], kinds=("card", "all"),
             mode: str = "hybrid") -> dict:
    res = {}
    for kind in kinds:
        h1 = h5 = rr = herb1 = 0
        for g in gold:
            hits = searcher.search(g["q"], k=10, kind=kind, mode=mode)
            ranks = [i for i, h in enumerate(hits) if under(tree, h["node_id"], g["gold"])]
            if ranks:
                h1 += ranks[0] == 0
                h5 += ranks[0] < 5
                rr += 1 / (ranks[0] + 1)
            if hits and herb_of(tree, hits[0]["node_id"]) == herb_of(tree, g["gold"][0]):
                herb1 += 1
        n = len(gold)
        res[f"{kind}_hit@1"] = round(h1 / n, 3)
        res[f"{kind}_hit@5"] = round(h5 / n, 3)
        res[f"{kind}_mrr"] = round(rr / n, 3)
        res[f"{kind}_herb@1"] = round(herb1 / n, 3)
    return res


def groundedness(tree: Tree, cards: dict, keep: set[str]) -> float:
    found = total = 0
    for nid in keep:
        c = cards.get(nid)
        if not c or c.get("auto"):
            continue
        src = " ".join(tree.nodes[x].text for x in subtree(tree, nid))
        for term in c.get("key_terms", []):
            words = DEVANAGARI.findall(term)
            if not words:
                continue
            total += 1
            found += all(w in src for w in words)
    return round(found / total, 3) if total else 0.0


def run_model(tree: Tree, spec: ModelSpec, gold: list[dict], out_root: Path, embed) -> dict:
    keep = {n for h in BENCH_HERBS for n in subtree(tree, h)}
    out = out_root / spec.name
    out.mkdir(parents=True, exist_ok=True)
    llm = make_llm(spec.provider, spec.model, spec.base_url)
    stats = {"calls": 0, "seconds": 0.0, "json_fail": 0}

    def timed(system, user):
        t0 = time.time()
        try:
            return llm(system, user)
        finally:
            stats["calls"] += 1
            stats["seconds"] += time.time() - t0

    errors: list[str] = []

    def log(msg):
        if msg.strip().startswith("!"):
            errors.append(msg)
            if "JSON" in msg or "Expecting" in msg or "no JSON" in msg:
                stats["json_fail"] += 1

    t0 = time.time()
    cards = write_cards(tree, timed, out / "cards.jsonl", workers=spec.workers,
                        node_filter=keep.__contains__, log=log, reasoning=spec.reasoning)
    wall = time.time() - t0
    cards = {k: v for k, v in cards.items() if k in keep}
    build_index(subset_tree(tree, keep), cards, out / "index", embed)
    metrics = evaluate(tree, Searcher(out / "index", embed), gold)
    metrics.update({
        "model": spec.name,
        "cards": len(cards),
        "missing_cards": len(keep) - len(cards),
        "llm_calls": stats["calls"],
        "sec_per_call": round(stats["seconds"] / stats["calls"], 1) if stats["calls"] else None,
        "wall_sec": round(wall),
        "json_fail": stats["json_fail"],
        "errors": len(errors),
        "grounded": groundedness(tree, cards, keep),
    })
    (out / "metrics.json").write_text(json.dumps(metrics, ensure_ascii=False, indent=1))
    (out / "errors.log").write_text("\n".join(errors))
    return metrics


def run_baseline(tree: Tree, gold: list[dict], out_root: Path, embed) -> dict:
    keep = {n for h in BENCH_HERBS for n in subtree(tree, h)}
    out = out_root / "chunks-only"
    build_index(subset_tree(tree, keep), {}, out / "index", embed)
    m = evaluate(tree, Searcher(out / "index", embed), gold)
    m["model"] = "chunks-only (no cards)"
    (out / "metrics.json").write_text(json.dumps(m, ensure_ascii=False, indent=1))
    return m


def reeval(tree: Tree, gold: list[dict], out_root: Path, embed) -> None:
    """Re-score every saved index (e.g. after a search change) without any LLM calls."""
    for mfile in sorted(out_root.glob("*/metrics.json")):
        m = json.loads(mfile.read_text())
        m.update(evaluate(tree, Searcher(mfile.parent / "index", embed), gold))
        mfile.write_text(json.dumps(m, ensure_ascii=False, indent=1))


def retrieval_bench(tree: Tree, gold: list[dict], cards_dir: Path, out_root: Path,
                    embedders: list[str], rerankers: list[str], log=print) -> list[dict]:
    """Fix one card set; compare embedders x {bm25, dense, hybrid} x rerankers."""
    from .cards import load_cards
    from .models import make_embedder, make_reranker

    keep = {n for h in BENCH_HERBS for n in subtree(tree, h)}
    cards = {k: v for k, v in load_cards(cards_dir / "cards.jsonl").items() if k in keep}
    sub = subset_tree(tree, keep)
    rows = []
    rr_models = {r: make_reranker(r) for r in rerankers if r != "none"}
    for emb_name in embedders:
        embed = make_embedder(emb_name)
        idx = out_root / f"{cards_dir.name}__{emb_name}"
        t0 = time.time()
        build_index(sub, cards, idx, embed)
        log(f"  indexed with {emb_name} in {time.time() - t0:.0f}s")
        modes = ["bm25", "dense", "hybrid"] if emb_name == embedders[0] else ["dense", "hybrid"]
        for mode in modes:
            m = evaluate(tree, Searcher(idx, embed), gold, kinds=("all",), mode=mode)
            m.update({"embedder": emb_name if mode != "bm25" else "-", "mode": mode, "reranker": "none"})
            rows.append(m)
            log(f"  {m}")
        for rr_name, rr in rr_models.items():
            t0 = time.time()
            m = evaluate(tree, Searcher(idx, embed, reranker=rr), gold, kinds=("all",))
            m.update({"embedder": emb_name, "mode": "hybrid", "reranker": rr_name,
                      "rerank_sec_per_q": round((time.time() - t0) / len(gold), 2)})
            rows.append(m)
            log(f"  {m}")
    (out_root / f"retrieval__{cards_dir.name}.json").write_text(json.dumps(rows, indent=1))
    return rows


COLUMNS = ["model", "card_hit@1", "card_hit@5", "card_mrr", "all_hit@1", "all_hit@5",
           "all_mrr", "all_herb@1", "grounded", "json_fail", "missing_cards", "sec_per_call", "wall_sec"]


def table(out_root: Path) -> str:
    rows = [json.loads(p.read_text()) for p in sorted(out_root.glob("*/metrics.json"))]
    rows.sort(key=lambda r: (-(r.get("all_mrr") or 0)))
    lines = ["| " + " | ".join(COLUMNS) + " |", "|" + "---|" * len(COLUMNS)]
    for r in rows:
        lines.append("| " + " | ".join(str(r.get(c, "")) for c in COLUMNS) + " |")
    return "\n".join(lines)
