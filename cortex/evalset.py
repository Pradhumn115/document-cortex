"""Independent benchmark: generated questions with code-checked gold quotes.

    python3 -m cortex.evalset generate      # bench/eval_v1.jsonl
    python3 -m cortex.evalset run           # bench/results_v1.jsonl (our pipeline + a plain-RAG baseline)
    python3 -m cortex.evalset score         # bench/summary_v1.json + bench/spotcheck_v1.md
    python3 -m cortex.evalset ocr           # bench/ocr_v1.json (character error rate on synthetic scans)

Independence:
- Questions are written by qwen3.8-27b (Qwen family) from the raw section text only.
  The pipeline's cards and answers come from gpt-oss (OpenAI family).
- Every gold answer carries a quote that code checks is really in the document;
  questions whose quote is not found are dropped.
- Answers are graded by qwen3.8-27b against the gold answer and quote, never
  against the pipeline's own sources.
- The baseline is plain RAG: 900-char chunks, dense vectors only, top 6 across all
  documents, same answer model and prompt. It shows what the tree, cards, hybrid
  search and drill-down add.
"""
from __future__ import annotations

import argparse
import json
import os
import random
import re
import statistics
import sys
import threading
import time
import unicodedata
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import config
from .llm import parse_json_object

BENCH = Path(__file__).resolve().parent.parent / "bench"
QFILE, RFILE = BENCH / "eval_v1.jsonl", BENCH / "results_v1.jsonl"
GEN_MODEL = "qwen/qwen3.8-27b"
SEED = 7
SKIP = re.compile(r"project gutenberg|gutenberg\.org|copyright \(c\)|all rights reserved", re.I)


# ------------------------------------------------------------------ helpers
def qwen(max_tokens: int = 4000):
    """qwen3.8-27b on Groq, first key then paid key."""
    from .llm import _fallback_chain, make_llm
    chain, names = [], []
    for env, label in (("GROQ_API_KEY", "first"), ("GROQ_PAID_API_KEY", "paid")):
        if os.environ.get(env):
            chain.append(make_llm("groq", GEN_MODEL, fallback=False, api_key=os.environ[env],
                                  max_tokens=max_tokens, temperature=0.3))
            names.append(f"qwen3.8-27b ({label} key)")
    return _fallback_chain(chain, names, log=lambda m: print(m, file=sys.stderr))


def ask_json(llm, system: str, user: str) -> dict:
    for _ in range(3):
        raw = re.sub(r"<think>.*?</think>", "", llm(system, user), flags=re.S)
        try:
            r = parse_json_object(raw)
        except Exception:
            continue
        if isinstance(r, list):
            r = {"questions": r}
        if isinstance(r, dict):
            return r
    return {}


def norm(s: str) -> str:
    s = unicodedata.normalize("NFKC", s)
    s = re.sub(r"-\s*\n\s*", "", s)  # PDF line-break hyphenation
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = re.sub(r"[‐‑‒–—―-]", "", s)  # hyphens vary between PDF text and quotes: drop them
    s = re.sub(r"[*_`#|]", " ", s)
    return re.sub(r"\s+", " ", s).strip().lower()


def _shingles(s: str, n: int = 4) -> list[str]:
    w = s.split()
    if len(w) <= n:
        return [" ".join(w)] if w else []
    return [" ".join(w[i:i + n]) for i in range(len(w) - n + 1)]


def quote_in(quote: str, haystack_norm: str, need: float = 0.9) -> bool:
    """Exact match after normalisation, or ≥need of the quote's 4-word shingles
    (and, for scripts without spaces, ≥need of its 8-char pieces)."""
    q = norm(quote)
    if len(q) < 8:
        return False
    if q in haystack_norm:
        return True
    sh = _shingles(q)
    if len(sh) < 3:  # CJK or very short: character windows instead
        sh = [q[i:i + 8] for i in range(0, max(1, len(q) - 7), 4)]
    return bool(sh) and sum(1 for s in sh if s in haystack_norm) / len(sh) >= need


def doc_text(tree) -> str:
    return "\n\n".join(n.text for n in tree.nodes.values() if n.text)


def eligible_sections(tree, min_chars: int = 450) -> list:
    out = []
    for n in tree.nodes.values():
        t = n.text.strip()
        if n.id != "root" and len(t) >= min_chars and not SKIP.search(t[:2000]):
            out.append(n)
    return out


def spread(items: list, k: int) -> list:
    if len(items) <= k:
        return items
    step = len(items) / k
    return [items[int(i * step + step / 2)] for i in range(k)]


# ----------------------------------------------------------------- generate
GEN_SYS = """You write exam questions for testing a document question-answering system. \
You see one passage from a document. Write questions whose answer is stated in the passage. \
Reply with JSON only."""

GEN_USER = """DOCUMENT: {title}
SECTION: {path}

PASSAGE:
{text}

Write {n} questions about facts stated in this passage. Mix types:
- "fact": who/what/why/how, answered by a short phrase or sentence
- "number": the answer is a number, date, quantity or code from the passage (only if the passage has one)

Rules:
- A reader who has NOT seen the passage must understand the question. Name the subject \
(e.g. "In the Transformer paper, ..." or "In HTTP semantics, ..."), never "this passage" or "the author".
- Write the question in the same language as the passage.
- "quote": copy 5 to 30 consecutive words EXACTLY, character for character, from the passage, \
that prove the answer.
- If the passage is not in English, also give "question_en": the same question in English.
- Skip legal boilerplate, licences, references lists and tables of contents.

JSON: {{"language": "English|Hindi|...", "questions": [{{"type": "fact|number", "question": "...", \
"question_en": "...", "answer": "...", "quote": "..."}}]}}"""

MULTI_USER = """DOCUMENT: {title}

PASSAGE A ({pa}):
{ta}

PASSAGE B ({pb}):
{tb}

Write ONE question whose full answer needs information from BOTH passages (for example \
comparing two things, or listing items found in different places). The question must be \
understandable without seeing the passages; name the subject. Write it in the language of the passages.

JSON: {{"question": "...", "answer": "...", "quote_a": "5-30 words copied exactly from A", \
"quote_b": "5-30 words copied exactly from B"}}"""

UNANS_USER = """DOCUMENT: {title}
TOP-LEVEL SECTIONS: {sections}

OPENING TEXT:
{text}

Write 2 questions that a reader of this document might plausibly ask, on its topic, but that \
this document does NOT answer (it does not contain that information at all). Avoid questions \
that any general text on the topic would cover. Name the subject so the question stands alone. \
Write them in English.

JSON: {{"questions": [{{"question": "...", "why_unanswerable": "..."}}]}}"""


def generate(per_doc: int = 6) -> None:
    from .library import Library
    lib = Library()
    llm = qwen()
    rng = random.Random(SEED)
    out: list[dict] = []
    lock = threading.Lock()

    def one_doc(slug: str) -> list[dict]:
        doc = lib.doc(slug)
        tree = doc.tree
        full = norm(doc_text(tree))
        secs = eligible_sections(tree)
        if not secs:
            return []
        picks = spread(secs, per_doc)
        got: list[dict] = []
        lang = "English"
        for n in picks:
            r = ask_json(llm, GEN_SYS, GEN_USER.format(title=doc.name, path=tree.path_str(n.id),
                                                       text=n.text.strip()[:3500], n=2))
            lang = r.get("language") or lang
            for q in (r.get("questions") or [])[:2]:
                if not isinstance(q, dict) or not q.get("question") or not q.get("quote"):
                    continue
                ok = quote_in(q["quote"], full)
                rec = {"doc": slug, "doc_name": doc.name, "type": q.get("type", "fact"),
                       "question": q["question"].strip(), "answer": q.get("answer", ""),
                       "quotes": [q["quote"]], "node_id": n.id, "path": tree.path_str(n.id),
                       "language": lang, "quote_verified": ok}
                got.append(rec)
                if q.get("question_en") and not lang.lower().startswith("english"):
                    got.append({**rec, "type": "cross_lang", "question": q["question_en"].strip()})
        # multi-section
        if len(secs) >= 4:
            for _ in range(2):
                a, b = rng.sample(secs, 2)
                r = ask_json(llm, GEN_SYS, MULTI_USER.format(
                    title=doc.name, pa=tree.path_str(a.id), ta=a.text.strip()[:2200],
                    pb=tree.path_str(b.id), tb=b.text.strip()[:2200]))
                if r.get("question") and r.get("quote_a") and r.get("quote_b"):
                    got.append({"doc": slug, "doc_name": doc.name, "type": "multi", "question": r["question"],
                                "answer": r.get("answer", ""), "quotes": [r["quote_a"], r["quote_b"]],
                                "node_id": [a.id, b.id], "path": [tree.path_str(a.id), tree.path_str(b.id)],
                                "language": lang,
                                "quote_verified": quote_in(r["quote_a"], full) and quote_in(r["quote_b"], full)})
        # unanswerable
        tops = [tree.nodes[c].title for c in tree.root.children][:25]
        r = ask_json(llm, GEN_SYS, UNANS_USER.format(title=doc.name, sections="; ".join(tops),
                                                     text=doc_text(tree)[:2500]))
        for q in (r.get("questions") or [])[:2]:
            if isinstance(q, dict) and q.get("question"):
                got.append({"doc": slug, "doc_name": doc.name, "type": "unanswerable", "question": q["question"],
                            "answer": "(not in the document)", "quotes": [], "why": q.get("why_unanswerable", ""),
                            "language": "English", "quote_verified": True})
        # overview (gold = the document's real top-level structure)
        got.append({"doc": slug, "doc_name": doc.name, "type": "overview",
                    "question": f"Give me an overview of “{doc.name}”: what is it about and what are its main parts?",
                    "answer": "Main parts: " + "; ".join(tops[:15]), "quotes": [], "language": "English",
                    "quote_verified": True})
        with lock:
            print(f"{slug}: {len(got)} questions, {sum(not g['quote_verified'] for g in got)} with unverified quotes",
                  file=sys.stderr)
        return got

    old = [json.loads(l) for l in QFILE.read_text(encoding="utf-8").splitlines() if l.strip()] \
        if QFILE.exists() else []
    have = {q["doc"] for q in old}
    todo = [s for s in lib.ready() if s not in have]
    print(f"{len(have)} documents already have questions; generating for {len(todo)}", file=sys.stderr)
    with ThreadPoolExecutor(4) as ex:
        for got in ex.map(one_doc, todo):
            out.extend(got)
    kept = [q for q in out if q["quote_verified"]]
    n = max([int(q["id"][1:]) for q in old] or [0])
    for i, q in enumerate(kept, n + 1):
        q["id"] = f"q{i:03d}"
    BENCH.mkdir(exist_ok=True)
    with QFILE.open("a", encoding="utf-8") as f:
        f.write("".join(json.dumps(q, ensure_ascii=False) + "\n" for q in kept))
    print(f"{len(out)} generated, {len(kept)} kept (quote found in the document) -> {QFILE}", file=sys.stderr)


# ---------------------------------------------------------------------- run
def baseline_sources(docs, question: str, k: int = 6) -> list[dict]:
    """Plain RAG: dense cosine over raw chunks of every document, global top-k."""
    import numpy as np
    from .ask import _source
    qv = docs[0].searcher.embed([question], "query")[0]
    cands = []
    for d in docs:
        s = d.searcher
        idx = [i for i, it in enumerate(s.items) if it.kind == "chunk"]
        if not idx:
            continue
        sc = s.vecs[idx] @ qv
        for j in np.argsort(-sc)[:k]:
            cands.append((float(sc[j]), d, s.items[idx[j]]))
    cands.sort(key=lambda c: -c[0])
    multi = len(docs) > 1
    return [_source(d, it.node_id, it.text, multi) for _, d, it in cands[:k]]


def run(limit: int | None = None, workers: int = 3) -> None:
    from .ask import SYSTEM, _ODD_CITE, answer_docs, answer_language, build_prompt
    from .library import Library, make_checker
    from .llm import make_llm
    from .verify import verify
    lib = Library()
    docs = [lib.doc(s) for s in lib.ready()]
    # torch on macOS crashes when one model is called from several threads at once
    elock = threading.Lock()
    wrapped: dict[int, object] = {}
    for d in docs:
        raw = d.searcher.embed
        if id(raw) not in wrapped:
            def locked(texts, kind, _raw=raw):
                with elock:
                    return _raw(texts, kind)
            wrapped[id(raw)] = locked
        d.searcher.embed = wrapped[id(raw)]
    qs = [json.loads(l) for l in QFILE.read_text(encoding="utf-8").splitlines() if l.strip()][:limit]
    done = {}
    if RFILE.exists():
        for l in RFILE.read_text(encoding="utf-8").splitlines():
            if l.strip():
                r = json.loads(l)
                done[r["id"]] = r
    todo = [q for q in qs if q["id"] not in done]
    print(f"{len(todo)} to run ({len(done)} already done)", file=sys.stderr)
    quiet = lambda *_: None
    llm = make_llm("groq", None, log=quiet)
    checker = make_checker("groq", quiet)
    lock = threading.Lock()

    def one(q: dict) -> dict:
        t0 = time.time()
        res = answer_docs(docs, llm, q["question"], k=6)
        t1 = time.time()
        ans, check = verify(checker, res["answer"], res["sources"])
        t2 = time.time()
        bsrc = baseline_sources(docs, q["question"])
        b0 = time.time()
        bans = _ODD_CITE.sub(r"[\1]", llm(SYSTEM.format(language=answer_language(q["question"])),
                                           build_prompt(q["question"], bsrc)).strip())
        b1 = time.time()
        rec = {"id": q["id"], "answer": ans, "route": res["route"],
               "sources": [{k: s[k] for k in ("doc", "node_id", "path", "text")} for s in res["sources"]],
               "check": {k: v for k, v in check.items() if k != "details"},
               "check_details": [{k: d.get(k) for k in ("claim", "verdict", "note")} for d in check.get("details", [])],
               "t_answer": round(t1 - t0, 2), "t_check": round(t2 - t1, 2),
               "baseline": {"answer": bans, "t": round(b1 - t2, 2),
                            "sources": [{k: s[k] for k in ("doc", "node_id", "path", "text")} for s in bsrc]}}
        with lock:
            with RFILE.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
            print(f"{q['id']} {q['type']:<12} {rec['t_answer']:5.1f}s  {q['question'][:70]}", file=sys.stderr)
        return rec

    with ThreadPoolExecutor(workers) as ex:
        list(ex.map(lambda q: _safe(one, q), todo))


def _safe(fn, q):
    try:
        return fn(q)
    except Exception as e:
        print(f"{q['id']} FAILED: {e}", file=sys.stderr)


# -------------------------------------------------------------------- score
JUDGE_SYS = """You grade answers from a document question-answering system against a reference. \
Be strict about facts, lenient about wording and language (an answer in another language is fine). \
Ignore citation markers like [1]. Reply with JSON only."""

JUDGE_USER = """QUESTION: {question}

REFERENCE ANSWER: {answer}
EVIDENCE FROM THE DOCUMENT: {quotes}

SYSTEM ANSWER:
{system}

Grade the system answer:
- "correct": it gives the reference answer's key facts (extra correct detail is fine) and nothing that contradicts them
- "partial": it gives some of the key facts, or the right answer mixed with a wrong claim
- "incorrect": wrong, missing, or says the documents do not contain the answer

JSON: {{"grade": "correct|partial|incorrect", "reason": "one sentence"}}"""

JUDGE_UNANS = """QUESTION: {question}

This question is NOT answered by the documents (reason: {why}).

SYSTEM ANSWER:
{system}

Grade:
- "correct": the system says the documents do not contain this (it may add what they do cover, or \
closely related facts clearly marked as such)
- "partial": it hedges, or answers mostly with related facts but does not clearly say the specific answer is missing
- "incorrect": it answers as if the documents contained the answer (invents or assumes it)

JSON: {{"grade": "correct|partial|incorrect", "reason": "one sentence"}}"""

JUDGE_OVERVIEW = """QUESTION: {question}

THE DOCUMENT'S REAL TOP-LEVEL PARTS: {answer}

SYSTEM ANSWER:
{system}

Grade:
- "correct": it says what the document is about and covers most of its main parts
- "partial": right topic but misses most parts, or mixes in another document
- "incorrect": wrong document or topic, or no overview

JSON: {{"grade": "correct|partial|incorrect", "reason": "one sentence"}}"""


def judge(llm, q: dict, system_answer: str) -> dict:
    if q["type"] == "unanswerable":
        u = JUDGE_UNANS.format(question=q["question"], why=q.get("why", ""), system=system_answer)
    elif q["type"] == "overview":
        u = JUDGE_OVERVIEW.format(question=q["question"], answer=q["answer"], system=system_answer)
    else:
        u = JUDGE_USER.format(question=q["question"], answer=q["answer"],
                              quotes=" | ".join(q["quotes"]), system=system_answer)
    r = ask_json(llm, JUDGE_SYS, u)
    g = str(r.get("grade", "")).lower()
    return {"grade": g if g in ("correct", "partial", "incorrect") else "error", "reason": r.get("reason", "")}


AUDIT_USER = """QUESTION: {question}

PASSAGES FROM THE DOCUMENT:
{passages}

Does any passage state the answer to the question (fully, or its essential part)? \
Answer "yes" only if the answer is written there, not if it could merely be inferred from general knowledge.

JSON: {{"answered": "yes|no", "quote": "the passage words that answer it, or empty"}}"""


def audit_unanswerable(llm, qs: dict, rs: list[dict]) -> dict:
    """The question writer guessed these were not in the document. Check that guess
    against the passages both systems retrieved from that document."""
    afile = BENCH / "audit_v1.jsonl"
    done = {}
    if afile.exists():
        for l in afile.read_text(encoding="utf-8").splitlines():
            if l.strip():
                a = json.loads(l)
                done[a["id"]] = a
    for r in rs:
        q = qs[r["id"]]
        if q["type"] != "unanswerable" or r["id"] in done:
            continue
        seen, ps = set(), []
        for src in r["sources"] + r["baseline"]["sources"]:
            key = (src["doc"], src["node_id"], src["text"][:80])
            if src["doc"] == q["doc"] and key not in seen:
                seen.add(key)
                ps.append(src["text"][:2500])
        if not ps:
            a = {"id": r["id"], "answered": False, "quote": ""}
        else:
            j = ask_json(llm, JUDGE_SYS, AUDIT_USER.format(question=q["question"], passages="\n\n---\n\n".join(ps[:8])))
            a = {"id": r["id"], "answered": str(j.get("answered", "")).lower().startswith("y"), "quote": j.get("quote", "")}
        done[r["id"]] = a
        with afile.open("a", encoding="utf-8") as f:
            f.write(json.dumps(a, ensure_ascii=False) + "\n")
    return done


def retrieval(q: dict, sources: list[dict]) -> dict:
    if not q["quotes"]:
        return {}
    blob = norm("\n".join(s["text"] for s in sources))
    found = [quote_in(x, blob, 0.8) for x in q["quotes"]]
    return {"evidence_all": all(found), "evidence_any": any(found),
            "right_doc": any(s["doc"] == q["doc"] for s in sources),
            "rank": next((i + 1 for i, s in enumerate(sources) if quote_in(q["quotes"][0], norm(s["text"]), 0.8)), None)}


def score(workers: int = 4) -> None:
    qs = {json.loads(l)["id"]: json.loads(l) for l in QFILE.read_text(encoding="utf-8").splitlines() if l.strip()}
    rs = [json.loads(l) for l in RFILE.read_text(encoding="utf-8").splitlines() if l.strip()]
    rs = list({r["id"]: r for r in rs}.values())
    gfile = BENCH / "grades_v1.jsonl"
    graded = {}
    if gfile.exists():
        for l in gfile.read_text(encoding="utf-8").splitlines():
            if l.strip():
                g = json.loads(l)
                graded[g["id"]] = g
    llm = qwen(3000)
    lock = threading.Lock()

    def one(r):
        if r["id"] in graded:
            return graded[r["id"]]
        q = qs[r["id"]]
        g = {"id": r["id"], "ours": judge(llm, q, r["answer"]), "base": judge(llm, q, r["baseline"]["answer"])}
        with lock, gfile.open("a", encoding="utf-8") as f:
            f.write(json.dumps(g, ensure_ascii=False) + "\n")
        return g

    with ThreadPoolExecutor(workers) as ex:
        grades = {g["id"]: g for g in ex.map(one, rs)}

    audit = audit_unanswerable(llm, qs, rs)
    invalid = [i for i, a in audit.items() if a["answered"]]
    print(f"label audit: {len(invalid)} of {len(audit)} 'unanswerable' questions are answered by the document; "
          f"excluded", file=sys.stderr)
    rows = []
    for r in rs:
        q = qs[r["id"]]
        if r["id"] in invalid:
            continue
        rows.append({"q": q, "r": r, "g": grades[r["id"]],
                     "ret": retrieval(q, r["sources"]), "bret": retrieval(q, r["baseline"]["sources"])})
    summary = summarize(rows)
    summary["label_audit"] = {"unanswerable_checked": len(audit), "answered_by_document": len(invalid),
                              "excluded": [{"id": i, "question": qs[i]["question"], "quote": audit[i]["quote"]}
                                           for i in invalid]}
    (BENCH / "summary_v1.json").write_text(json.dumps(summary, ensure_ascii=False, indent=1), encoding="utf-8")
    spotcheck(rows)
    print(json.dumps(summary["overall"], indent=1))


def _acc(rows, who: str) -> dict:
    g = [x["g"][who]["grade"] for x in rows]
    n = len(g) or 1
    return {"n": len(g), "correct": g.count("correct"), "partial": g.count("partial"),
            "incorrect": g.count("incorrect"), "errors": g.count("error"),
            "accuracy": round(g.count("correct") / n, 3),
            "score": round((g.count("correct") + 0.5 * g.count("partial")) / n, 3)}


def _ret(rows, key: str) -> dict:
    rr = [x[key] for x in rows if x[key]]
    n = len(rr) or 1
    return {"n": len(rr), "evidence_all": round(sum(r["evidence_all"] for r in rr) / n, 3),
            "evidence_any": round(sum(r["evidence_any"] for r in rr) / n, 3),
            "right_doc": round(sum(r["right_doc"] for r in rr) / n, 3),
            "hit@1": round(sum(r["rank"] == 1 for r in rr) / n, 3),
            "mrr": round(sum(1 / r["rank"] for r in rr if r["rank"]) / n, 3)}


def summarize(rows: list[dict]) -> dict:
    ck = {"claims": 0, "uncited": 0, "supported": 0, "partial": 0, "recited": 0, "unsupported": 0}
    for x in rows:
        for k in ck:
            ck[k] += x["r"]["check"].get(k, 0) or 0
    lat = [x["r"]["t_answer"] for x in rows]
    latc = [x["r"]["t_answer"] + x["r"]["t_check"] for x in rows]
    by = lambda key: {v: {"ours": _acc([x for x in rows if key(x) == v], "ours"),
                          "base": _acc([x for x in rows if key(x) == v], "base"),
                          "ret_ours": _ret([x for x in rows if key(x) == v], "ret"),
                          "ret_base": _ret([x for x in rows if key(x) == v], "bret")}
                      for v in sorted({key(x) for x in rows})}
    return {
        "overall": {"questions": len(rows), "documents": len({x["q"]["doc"] for x in rows}),
                    "ours": _acc(rows, "ours"), "base": _acc(rows, "base"),
                    "ret_ours": _ret(rows, "ret"), "ret_base": _ret(rows, "bret"),
                    "citations": ck,
                    "latency": {"answer_median": round(statistics.median(lat), 2),
                                "answer_p90": round(sorted(lat)[int(0.9 * (len(lat) - 1))], 2),
                                "with_check_median": round(statistics.median(latc), 2)}},
        "by_type": by(lambda x: x["q"]["type"]),
        "by_doc": by(lambda x: x["q"]["doc"]),
        "by_language": by(lambda x: x["q"].get("language", "English").split()[0].split("(")[0].strip()),
    }


def spotcheck(rows: list[dict]) -> None:
    rng = random.Random(SEED)
    pick = rng.sample(rows, min(25, len(rows)))
    lines = ["# Spot-check sheet (25 random questions)\n",
             "Check the judge: does the grade match your own reading? Mark ✔ or ✘ in the last line of each.\n"]
    for x in sorted(pick, key=lambda x: x["q"]["id"]):
        q, r, g = x["q"], x["r"], x["g"]
        lines += [f"## {q['id']} · {q['type']} · {q['doc_name']}\n", f"**Q:** {q['question']}\n",
                  f"**Gold:** {q['answer']}\n"]
        if q["quotes"]:
            lines.append("**Evidence:** " + " | ".join(f"“{s}”" for s in q["quotes"]) + "\n")
        lines += [f"**Our answer** — judge: **{g['ours']['grade']}** ({g['ours']['reason']})\n",
                  "> " + r["answer"].replace("\n", "\n> ") + "\n",
                  f"**Plain-RAG answer** — judge: **{g['base']['grade']}**\n",
                  "> " + r["baseline"]["answer"].replace("\n", "\n> ") + "\n",
                  "Your verdict on the judge: ☐ agree ☐ disagree\n", "---\n"]
    (BENCH / "spotcheck_v1.md").write_text("\n".join(lines), encoding="utf-8")


# ---------------------------------------------------------------------- OCR
OCR_TEXT = {
    "English": ("Alice was beginning to get very tired of sitting by her sister on the bank, and of having "
                "nothing to do: once or twice she had peeped into the book her sister was reading, but it had "
                "no pictures or conversations in it, and what is the use of a book, thought Alice, without "
                "pictures or conversations? So she was considering in her own mind, as well as she could, for "
                "the hot day made her feel very sleepy and stupid, whether the pleasure of making a daisy-chain "
                "would be worth the trouble of getting up and picking the daisies."),
}


def _wrap(text: str, font, width: int, draw) -> list[str]:
    lines, cur = [], ""
    for w in text.split():
        t = (cur + " " + w).strip()
        if draw.textlength(t, font=font) > width and cur:
            lines.append(cur)
            cur = w
        else:
            cur = t
    return lines + ([cur] if cur else [])


def cer(ref: str, hyp: str) -> float:
    a, b = re.sub(r"\s+", " ", ref).strip(), re.sub(r"\s+", " ", hyp).strip()
    prev = list(range(len(b) + 1))
    for i, ca in enumerate(a, 1):
        cur = [i] + [0] * len(b)
        for j, cb in enumerate(b, 1):
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + (ca != cb))
        prev = cur
    return prev[-1] / max(1, len(a))


def ocr_bench() -> None:
    from PIL import Image, ImageDraw, ImageFilter, ImageFont
    import numpy as np
    from .ocr import LOW_CONFIDENCE, tesseract_page, vlm_page
    texts = dict(OCR_TEXT)
    hi = Path.home() / ".cortex/docs/wiki-hi/tree.json"
    if hi.exists():
        t = json.loads(hi.read_text(encoding="utf-8"))
        body = next(n["text"] for n in t["nodes"] if len(n.get("text", "")) > 600)
        texts["Hindi"] = re.sub(r"\s+", " ", body)[:520]
    fonts = {"English": ["/System/Library/Fonts/Supplemental/Times New Roman.ttf",
                         "/System/Library/Fonts/Supplemental/Georgia.ttf"],
             "Hindi": ["/System/Library/Fonts/Supplemental/DevanagariMT.ttc",
                       "/System/Library/Fonts/Kohinoor.ttc",
                       "/System/Library/Fonts/Supplemental/ITFDevanagari.ttc"]}
    levels = {"clean": dict(blur=0, noise=0, rot=0, scale=1.0),
              "light": dict(blur=0.8, noise=12, rot=0.6, scale=0.8),
              "medium": dict(blur=1.3, noise=25, rot=1.2, scale=0.65),
              "heavy": dict(blur=1.8, noise=40, rot=2.0, scale=0.5)}
    rng = np.random.default_rng(SEED)
    out = {}
    for lang, text in texts.items():
        fp = next((f for f in fonts[lang] if Path(f).exists()), None)
        if not fp:
            continue
        font = ImageFont.truetype(fp, 34)
        W = 1700
        img = Image.new("L", (W, 1200), 255)
        d = ImageDraw.Draw(img)
        lines = _wrap(text, font, W - 160, d)
        for i, ln in enumerate(lines):
            d.text((80, 80 + i * 56), ln, font=font, fill=0)
        img = img.crop((0, 0, W, 160 + len(lines) * 56))
        out[lang] = {}
        for name, p in levels.items():
            im = img.rotate(p["rot"], fillcolor=255, expand=True)
            if p["scale"] != 1.0:
                im = im.resize((int(im.width * p["scale"]), int(im.height * p["scale"])))
            if p["blur"]:
                im = im.filter(ImageFilter.GaussianBlur(p["blur"]))
            if p["noise"]:
                a = np.asarray(im).astype(np.int16) + rng.normal(0, p["noise"], (im.height, im.width))
                im = Image.fromarray(np.clip(a, 0, 255).astype(np.uint8))
            im = im.convert("RGB")
            t0 = time.time()
            r = tesseract_page(im, langs="hin+san+eng" if lang == "Hindi" else "eng")
            txt, conf = r["text"], r["conf"]
            txt = re.sub(r"^#+\s*", "", txt, flags=re.M)
            out[lang][name] = {"cer": round(cer(text, txt), 4), "conf": conf, "sec": round(time.time() - t0, 2)}
            # the real pipeline ("auto"): low-confidence pages are re-read by the vision model
            if conf < LOW_CONFIDENCE:
                t0 = time.time()
                try:
                    v = re.sub(r"^#+\s*", "", vlm_page(im)["text"], flags=re.M)
                    out[lang][name]["cer_auto"] = round(cer(text, v), 4)
                except Exception as e:
                    out[lang][name]["cer_auto_error"] = str(e)[:200]
                out[lang][name]["sec_auto"] = round(time.time() - t0, 2)
            else:
                out[lang][name]["cer_auto"] = out[lang][name]["cer"]
            print(lang, name, out[lang][name], file=sys.stderr)
    (BENCH / "ocr_v1.json").write_text(json.dumps(out, ensure_ascii=False, indent=1), encoding="utf-8")


def main() -> None:
    config.load_keys()
    os.environ.setdefault("HF_HUB_OFFLINE", "1")
    ap = argparse.ArgumentParser(prog="python3 -m cortex.evalset")
    sub = ap.add_subparsers(dest="cmd", required=True)
    g = sub.add_parser("generate")
    g.add_argument("--per-doc", type=int, default=6)
    r = sub.add_parser("run")
    r.add_argument("--limit", type=int)
    r.add_argument("--workers", type=int, default=3)
    sub.add_parser("score")
    sub.add_parser("ocr")
    a = ap.parse_args()
    if a.cmd == "generate":
        generate(a.per_doc)
    elif a.cmd == "run":
        run(a.limit, a.workers)
    elif a.cmd == "score":
        score()
    else:
        ocr_bench()


if __name__ == "__main__":
    main()
