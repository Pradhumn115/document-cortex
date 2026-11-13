"""Document Cortex: put in any document, ask anything about it.

Everyday use
  cortex serve                      web UI at http://127.0.0.1:8765
  cortex add FILE [FILE ...]        PDF (text or scanned), DOCX, MD, TXT, images
  cortex list                       documents in the library
  cortex ask "question" [--doc SLUG ...] [--show-check]
  cortex remove SLUG
  cortex history | consolidate      episodic memory: past questions, learn from 👍
  cortex keys                       which API keys were found

Data lives in $CORTEX_HOME (default ~/.cortex); keys in ./.env or ~/.cortex/.env.

Developer / benchmark commands: tree, cards, index, search, stats, bench, bench-retrieval
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import warnings
from pathlib import Path

from .tree import Tree, build_chunks


def _quiet_hf():
    # Work offline once the default embedding model is cached (faster start, no network needed).
    cached = Path.home() / ".cache/huggingface/hub/models--intfloat--multilingual-e5-small"
    if cached.exists():
        os.environ.setdefault("HF_HUB_OFFLINE", "1")
    os.environ.setdefault("TOKENIZERS_PARALLELISM", "false")
    os.environ.setdefault("HF_HUB_DISABLE_TELEMETRY", "1")
    warnings.filterwarnings("ignore")
    try:
        from transformers.utils import logging as hf_logging
        hf_logging.set_verbosity_error()
        hf_logging.disable_progress_bar()
    except Exception:
        pass


def _keys(a):
    from . import config
    from .llm import load_env_file
    config.load_keys([a.env_file] if getattr(a, "env_file", None) else None)
    if getattr(a, "paid_env_file", None):
        load_env_file(a.paid_env_file, rename={"GROQ_API_KEY": "GROQ_PAID_API_KEY"})


def cmd_tree(a):
    if a.profile == "bhavaprakasha":
        from .profiles import bhavaprakasha
        tree = bhavaprakasha.build(Path(a.src), Path(a.fulltext), limit_groups=a.groups)
    else:
        from .profiles import markdown
        tree = markdown.build(Path(a.src))
    build_chunks(tree, a.chunk_chars)
    a.workdir.mkdir(parents=True, exist_ok=True)
    tree.save(a.workdir / "tree.json")
    print(f"tree: {len(tree.nodes)} nodes, {len(tree.chunks)} chunks -> {a.workdir / 'tree.json'}")


def cmd_cards(a):
    from .cards import write_cards
    from .llm import make_llm
    tree = Tree.load(a.workdir / "tree.json")
    keep = None
    if a.under:
        allowed = set()
        stack = [a.under]
        while stack:
            nid = stack.pop()
            allowed.add(nid)
            stack.extend(tree.nodes[nid].children)
        keep = allowed.__contains__
    from .cards import write_cards_by_section
    from .llm import DailyLimit
    llm = make_llm(a.provider, a.model, a.base_url)
    kw = dict(workers=a.workers, reasoning=a.reasoning, short_leaf=a.short_leaf)
    out = a.workdir / "cards.jsonl"
    try:
        if a.by_section and not a.under:
            cards = write_cards_by_section(tree, llm, out, **kw)
        else:
            cards = write_cards(tree, llm, out, node_filter=keep, **kw)
    except DailyLimit as e:
        print(f"DAILY_LIMIT: {e}", flush=True)
        sys.exit(75)  # EX_TEMPFAIL: scripts/cards_daily.sh waits and resumes
    print(f"cards: {len(cards)} in {out}")


def cmd_index(a):
    _quiet_hf()
    from .cards import load_cards
    from .search import build_index, e5_embedder
    tree = Tree.load(a.workdir / "tree.json")
    cards = load_cards(a.workdir / "cards.jsonl")
    if a.only_carded:
        # Restrict chunks to subtrees that have cards (useful for partial runs).
        tree.chunks = [c for c in tree.chunks if c.node_id in cards]
    build_index(tree, cards, a.workdir / "index", e5_embedder())
    print(f"index: {len(cards)} cards + {len(tree.chunks)} chunks -> {a.workdir / 'index'}")


def cmd_search(a):
    _quiet_hf()
    from .search import Searcher, e5_embedder
    s = Searcher(a.workdir / "index", e5_embedder())
    hits = s.search(a.query, k=a.k, kind=a.kind)
    if a.json:
        print(json.dumps(hits, ensure_ascii=False, indent=1))
        return
    for i, h in enumerate(hits, 1):
        body = h["text"].split("\n", 1)[-1].replace("\n", " ")
        print(f"{i}. [{h['kind']}] {h['path']}")
        print(f"   rrf={h['score']} bm25#{h['bm25_rank']} dense#{h['dense_rank']}")
        print(f"   {body[:220]}")


def cmd_stats(a):
    from collections import Counter
    from .cards import load_cards
    tree = Tree.load(a.workdir / "tree.json")
    cards = load_cards(a.workdir / "cards.jsonl")
    kinds = Counter(n.kind for n in tree.nodes.values())
    print(f"nodes: {dict(kinds)}  chunks: {len(tree.chunks)}  cards: {len(cards)}")
    for gid in tree.root.children:
        g = tree.nodes[gid]
        print(f"  {gid} {g.title}: {len(g.children)} sections{'  [card]' if gid in cards else ''}")


def cmd_bench(a):
    _quiet_hf()
    from .bench import ModelSpec, run_baseline, run_model, table
    from .search import e5_embedder
    tree = Tree.load(a.workdir / "tree.json")
    gold = [json.loads(l) for l in Path(a.gold).read_text(encoding="utf-8").splitlines() if l.strip()]
    out = Path(a.out)
    embed = e5_embedder()
    if a.reeval:
        from .bench import reeval
        reeval(tree, gold, out, embed)
    if a.baseline:
        print(json.dumps(run_baseline(tree, gold, out, embed), ensure_ascii=False))
    if a.model:
        spec = ModelSpec(a.name or a.model.replace("/", "_"), a.provider, a.model,
                         a.base_url, a.reasoning, a.workers)
        print(json.dumps(run_model(tree, spec, gold, out, embed), ensure_ascii=False))
    print(table(out))


def cmd_bench_retrieval(a):
    _quiet_hf()
    from .bench import retrieval_bench
    tree = Tree.load(a.workdir / "tree.json")
    gold = [json.loads(l) for l in Path(a.gold).read_text(encoding="utf-8").splitlines() if l.strip()]
    rows = retrieval_bench(tree, gold, Path(a.cards_dir), Path(a.out),
                           a.embedders.split(","), a.rerankers.split(","), log=lambda *_: None)
    cols = ["embedder", "mode", "reranker", "all_hit@1", "all_hit@5", "all_mrr", "all_herb@1",
            "rerank_sec_per_q"]
    print("| " + " | ".join(cols) + " |\n|" + "---|" * len(cols))
    for r in sorted(rows, key=lambda r: -r["all_mrr"]):
        print("| " + " | ".join(str(r.get(c, "")) for c in cols) + " |")


def cmd_add(a):
    _quiet_hf()
    _keys(a)
    from .library import Library
    lib = Library()
    for f in a.files:
        src = Path(f).expanduser()
        if not src.exists():
            print(f"not found: {src}")
            continue
        print(f"== {src.name}")
        slug = lib.add(src, provider=a.provider, model=a.model, workers=a.workers, embedder=a.embedder,
                       ocr=a.ocr, ocr_engine=a.ocr_engine, ocr_langs=a.ocr_langs, progress_print=True)
        m = lib.meta(slug)
        print(f"   {m.get('status')}: {m.get('message')}  (slug: {slug})")
        if m.get("ocr") and m["ocr"].get("low_confidence_pages"):
            print(f"   OCR low confidence on pages {m['ocr']['low_confidence_pages']}")


def cmd_list(a):
    from .library import Library
    docs = Library().list()
    if not docs:
        print("No documents yet. Add one: cortex add FILE")
    for d in docs:
        print(f"  {d['slug']:<34} {d.get('status', '?'):<10} {d.get('cards', '-'):>5} cards  {d.get('name', '')}")


def cmd_ask(a):
    _quiet_hf()
    _keys(a)
    from .ask import pages_str
    from .library import Library
    from .verify import summary_line
    lib = Library()
    res = lib.ask(a.question, a.doc or None, provider=a.provider, model=a.model, verify=not a.no_verify, k=a.k)
    print(res["answer"])
    print("\nSources:" + ("  (overview of the whole document)" if res["route"] == "overview" else ""))
    multi = len({s["doc"] for s in res["sources"]}) > 1
    for i, s in enumerate(res["sources"], 1):
        where = (f"{s['doc_name']} › " if multi else "") + s["path"]
        print(f"  [{i}] {where}  {pages_str(s['pages'])}".rstrip())
    if res["check"] is not None:
        print("\n" + summary_line(res["check"]))
        if a.show_check:
            for d in res["check"]["details"]:
                print(f"  - {d['verdict']:<12} cited {d['cited']} -> {d['supported_by']}  {d['claim'][:70]}"
                      + (f"  ({d['note']})" if d["note"] and d["verdict"] != "supported" else ""))
    print(f"\n(id {res['id']}: rate it with  cortex feedback {res['id']} up|down)")
    if a.json_out:
        Path(a.json_out).write_text(json.dumps(res, ensure_ascii=False, indent=1))


def cmd_remove(a):
    from .library import Library
    Library().delete(a.slug)
    print(f"removed {a.slug}")


def cmd_history(a):
    from .library import Library
    for h in Library().history_items(a.n):
        fb = {1: "👍", -1: "👎"}.get(h.get("feedback", 0), "  ")
        print(f"{fb} {h['ts']}  [{', '.join(h['docs'])[:40]}]  {h['question'][:80]}  (id {h['id']})")


def cmd_feedback(a):
    from .library import Library
    Library().feedback(a.id, 1 if a.value in ("up", "+1", "1", "good") else -1)
    print("saved")


def cmd_consolidate(a):
    _quiet_hf()
    from .library import Library
    changed = Library().consolidate()
    print(f"learned questions added to {len(changed)} documents" if changed else "nothing new to learn (no new 👍)")


def cmd_keys(a):
    _keys(a)
    from . import config
    for k, v in config.keys_status().items():
        print(f"  {k:<20} {'found' if v else '-'}")
    print(f"  data: {config.home()}")


def cmd_serve(a):
    _quiet_hf()
    _keys(a)
    from .web import serve
    serve(a.host, a.port, open_browser=not a.no_browser)


def main(argv=None):
    p = argparse.ArgumentParser(prog="cortex", description=__doc__,
                                formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = p.add_subparsers(dest="cmd", required=True)

    def llm_opts(x):
        x.add_argument("--provider", default="groq", choices=["groq", "openai", "anthropic", "fake"])
        x.add_argument("--model")
        x.add_argument("--env-file", help="extra .env to read keys from")
        x.add_argument("--paid-env-file", help="a .env whose GROQ_API_KEY is used as the paid fallback key")

    ad = sub.add_parser("add", aliases=["ingest"], help="add documents to the library")
    ad.add_argument("files", nargs="+")
    llm_opts(ad)
    ad.add_argument("--workers", type=int, default=4)
    ad.add_argument("--embedder", default="e5-small", choices=["e5-small", "bge-m3", "qwen3-emb-0.6b"])
    ad.add_argument("--ocr", default="auto", choices=["auto", "always", "never"],
                    help="auto: OCR only pages without a text layer (scans)")
    ad.add_argument("--ocr-engine", default="auto", choices=["auto", "tesseract", "surya", "vlm"],
                    help="auto: tesseract, vision model for low-confidence pages (handwriting)")
    ad.add_argument("--ocr-langs", help="Tesseract languages, e.g. hin+san+eng (default: detected)")
    ad.set_defaults(fn=cmd_add)

    sub.add_parser("list", help="list documents").set_defaults(fn=cmd_list)

    ak = sub.add_parser("ask", help="ask the library")
    ak.add_argument("question")
    ak.add_argument("--doc", action="append", help="limit to this document slug (repeatable)")
    ak.add_argument("-k", type=int, default=6)
    llm_opts(ak)
    ak.add_argument("--no-verify", action="store_true", help="skip the citation check")
    ak.add_argument("--show-check", action="store_true", help="print the verdict for every claim")
    ak.add_argument("--json-out", help="save the full result as JSON")
    ak.set_defaults(fn=cmd_ask)

    rm = sub.add_parser("remove", help="delete a document from the library")
    rm.add_argument("slug")
    rm.set_defaults(fn=cmd_remove)

    hi = sub.add_parser("history", help="past questions")
    hi.add_argument("-n", type=int, default=30)
    hi.set_defaults(fn=cmd_history)

    fb = sub.add_parser("feedback", help="rate an answer: up or down")
    fb.add_argument("id")
    fb.add_argument("value", choices=["up", "down"])
    fb.set_defaults(fn=cmd_feedback)

    sub.add_parser("consolidate", help="learn from 👍 answers").set_defaults(fn=cmd_consolidate)

    ky = sub.add_parser("keys", help="show which API keys were found")
    ky.add_argument("--env-file")
    ky.set_defaults(fn=cmd_keys)

    sv = sub.add_parser("serve", help="start the web UI")
    sv.add_argument("--host", default="127.0.0.1")
    sv.add_argument("--port", type=int, default=8765)
    sv.add_argument("--no-browser", action="store_true")
    sv.add_argument("--env-file")
    sv.add_argument("--paid-env-file")
    sv.set_defaults(fn=cmd_serve)

    # ---- developer / benchmark commands
    t = sub.add_parser("tree")
    t.add_argument("workdir", type=Path)
    t.add_argument("--profile", choices=["bhavaprakasha", "markdown"], default="markdown")
    t.add_argument("--src", required=True)
    t.add_argument("--fulltext")
    t.add_argument("--groups", type=int, help="keep only the first N groups (vargas)")
    t.add_argument("--chunk-chars", type=int, default=900)
    t.set_defaults(fn=cmd_tree)

    c = sub.add_parser("cards")
    c.add_argument("workdir", type=Path)
    c.add_argument("--provider", default="anthropic", choices=["anthropic", "openai", "fake"])
    c.add_argument("--model")
    c.add_argument("--base-url")
    c.add_argument("--workers", type=int, default=4)
    c.add_argument("--under", help="only write cards for this node's subtree (e.g. a herb id)")
    c.add_argument("--reasoning", choices=["low", "medium", "high", "nothink"],
                   help="reasoning effort hint (gpt-oss) or nothink (Qwen3)")
    c.add_argument("--short-leaf", type=int, default=150,
                   help="leaves with at most this many chars skip the model (0 = card every leaf)")
    c.add_argument("--by-section", action="store_true",
                   help="finish one section (herb) at a time; best under a daily request cap")
    c.set_defaults(fn=cmd_cards)

    i = sub.add_parser("index")
    i.add_argument("workdir", type=Path)
    i.add_argument("--only-carded", action="store_true")
    i.set_defaults(fn=cmd_index)

    s = sub.add_parser("search")
    s.add_argument("workdir", type=Path)
    s.add_argument("query")
    s.add_argument("-k", type=int, default=8)
    s.add_argument("--kind", default="all", choices=["all", "card", "chunk"])
    s.add_argument("--json", action="store_true")
    s.set_defaults(fn=cmd_search)

    st = sub.add_parser("stats")
    st.add_argument("workdir", type=Path)
    st.set_defaults(fn=cmd_stats)

    b = sub.add_parser("bench")
    b.add_argument("workdir", type=Path)
    b.add_argument("--gold", default="bench/gold.jsonl")
    b.add_argument("--out", default="data/bench")
    b.add_argument("--baseline", action="store_true")
    b.add_argument("--reeval", action="store_true", help="re-score saved indexes, no LLM calls")
    b.add_argument("--name")
    b.add_argument("--provider", default="openai", choices=["anthropic", "openai", "fake"])
    b.add_argument("--model")
    b.add_argument("--base-url")
    b.add_argument("--reasoning", choices=["low", "medium", "high", "nothink"])
    b.add_argument("--workers", type=int, default=1)
    b.set_defaults(fn=cmd_bench)

    br = sub.add_parser("bench-retrieval")
    br.add_argument("workdir", type=Path)
    br.add_argument("--cards-dir", default="data/bench/groq-gpt-oss-120b")
    br.add_argument("--gold", default="bench/gold.jsonl")
    br.add_argument("--out", default="data/bench/retrieval")
    br.add_argument("--embedders", default="e5-small,bge-m3,qwen3-emb-0.6b")
    br.add_argument("--rerankers", default="none,qwen3-rr-0.6b,bge-rr-m3")
    br.set_defaults(fn=cmd_bench_retrieval)

    a = p.parse_args(argv)
    a.fn(a)


if __name__ == "__main__":
    sys.exit(main())
