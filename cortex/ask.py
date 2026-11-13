"""Answer a question from one or more ingested documents, with citations.

1. Route: overview questions ("summarize", "main points", "सारांश", "要約"...) are
   answered from each document's top-level cards (a map of the whole document);
   everything else goes through search.
2. Search each document (hybrid BM25 + vectors, one hit per section), add sections
   whose card entities are named in the question (entity index), optionally
   rerank, and keep the best k across all documents.
3. Expand every hit to text the model can read: short sections whole; long
   sections, and thin parent sections (drill-down), as their best-matching passages.
4. A large model answers only from those sources, citing [1], [2]...
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np

from .llm import LLM
from .search import BM25, Searcher, tokenize
from .trace import NullTrace
from .tree import Tree

SYSTEM = """You answer questions about documents using ONLY the numbered sources \
provided. Every fact in your answer must be stated in a source: never add facts from your own \
knowledge, even well-known ones, and never fill in numbers, names, dates or mechanisms the sources \
do not give. Cite every claim with its source number in square brackets, exactly like [1] \
or [2][3] — no other citation style, no line numbers. If the question has several parts, answer \
each part. If the sources answer only part of it, answer that part and say plainly which part \
they do not cover. If they do not contain the answer at all, say so plainly and say what they do \
cover. Write the answer in {language}. Be concise: a few sentences or a short list."""

SOURCE_CHARS = 3000
MULTI_DOC_MARGIN = 0.05  # cosine gap (e5) that separated relevant from unrelated documents in testing
_ODD_CITE = re.compile(r"【(\d+)[^】]*】")
OVERVIEW = re.compile(
    r"\b(summari[sz]e|summary|overview|main (points|ideas|topics|themes)|key (points|takeaways|findings|ideas)"
    r"|what is (this|the) (document|paper|book|text|report|article) about|table of contents|outline of"
    r"|tl;?dr)\b|सारांश|मुख्य बिंदु|संक्षेप में|résum|要約|概要|ملخص|موجز", re.I)


@dataclass
class Doc:
    slug: str
    name: str
    tree: Tree
    searcher: Searcher
    cards: dict = field(default_factory=dict)
    entities: dict = field(default_factory=dict)  # normalised entity -> [node ids]

    @classmethod
    def load(cls, work: Path, embed, name: str | None = None, reranker=None) -> "Doc":
        from .cards import load_cards
        tree = Tree.load(work / "tree.json")
        cards = {k: v for k, v in load_cards(work / "cards.jsonl").items() if k in tree.nodes}
        ents = json.loads((work / "entities.json").read_text()) if (work / "entities.json").exists() \
            else build_entity_index(cards)
        return cls(work.name, name or tree.root.title, tree, Searcher(work / "index", embed, reranker=reranker),
                   cards, ents)


# ------------------------------------------------------------------ helpers
def answer_language(question: str) -> str:
    """Pick the answer language from the question's script (models guess badly)."""
    if re.search(r"[ऀ-ॿ]", question):
        return "Hindi (Devanagari), keeping Sanskrit terms as written"
    if re.search(r"[^\x00-\x7F]", question) and not re.search(r"[A-Za-z]{3}", question):
        return "the same language as the question"
    return "English"


def pages_str(pages) -> str:
    if not pages:
        return ""
    return f"p. {pages[0]}" if pages[0] == pages[1] else f"p. {pages[0]}–{pages[1]}"


def route(question: str) -> str:
    return "overview" if OVERVIEW.search(question) else "search"


def _norm_entity(e: str) -> str:
    return re.sub(r"\s+", " ", re.sub(r"\(.*?\)", "", e)).strip().lower()


def build_entity_index(cards: dict) -> dict[str, list[str]]:
    """Entity -> sections whose card names it. A lightweight concept graph: two
    sections are linked when their cards share an entity."""
    idx: dict[str, list[str]] = {}
    for nid, c in cards.items():
        for e in c.get("entities", []) + [t for t in c.get("key_terms", []) if len(t) > 3]:
            k = _norm_entity(str(e))
            if len(k) >= 3 and nid not in idx.setdefault(k, []):
                idx[k].append(nid)
    return idx


def entity_hits(doc: Doc, question: str, limit: int = 3) -> list[str]:
    q = question.lower()
    found = [(len(k), ids) for k, ids in doc.entities.items() if k in q and len(ids) <= 25]
    out: list[str] = []
    for _, ids in sorted(found, key=lambda x: -x[0]):  # longest (most specific) names first
        for nid in sorted(ids, key=lambda n: -doc.tree.depth(n)):  # deepest section first
            if nid not in out:
                out.append(nid)
    return out[:limit]


def _subtree(tree: Tree, nid: str) -> set[str]:
    out, stack = set(), [nid]
    while stack:
        n = stack.pop()
        out.add(n)
        stack.extend(tree.nodes[n].children)
    return out


def best_passages(doc: Doc, node_ids: set[str], question: str, qvec) -> str:
    """Chunks from these sections ranked by relevance (dense + BM25 rank fusion),
    returned in document order within SOURCE_CHARS."""
    s, tree = doc.searcher, doc.tree
    idx = [i for i, it in enumerate(s.items) if it.kind == "chunk" and it.node_id in node_ids]
    if not idx:
        return "\n\n".join(tree.nodes[n].text for n in node_ids)[:SOURCE_CHARS]
    chunks = {c.id: c for c in tree.chunks if c.node_id in node_ids}
    order = {n: i for i, n in enumerate(tree.nodes)}  # document order of nodes
    dense = s.vecs[idx] @ qvec
    lex = BM25([tokenize(s.items[i].text) for i in idx]).scores(tokenize(question))
    score = np.zeros(len(idx))
    for ranking in (np.argsort(-dense), np.argsort(-lex)):
        for r, j in enumerate(ranking):
            score[j] += 1 / (60 + r + 1)
    picked, used = [], 0
    for j in np.argsort(-score):
        cid = s.items[idx[j]].id.split(":", 1)[1]
        ch = chunks.get(cid)
        text = ch.text if ch else s.items[idx[j]].text
        if used + len(text) > SOURCE_CHARS and picked:
            break
        head = "" if (ch is None or len(node_ids) == 1) else f"({tree.nodes[ch.node_id].title}) "
        picked.append(((order.get(ch.node_id, 0), ch.ord) if ch else (0, j), head + text))
        used += len(text)
    return "\n[…]\n".join(t for _, t in sorted(picked))


def expand(doc: Doc, nid: str, question: str, qvec) -> tuple[str, str]:
    """Text the answer model reads for one hit, and how it was chosen."""
    node = doc.tree.nodes[nid]
    text = node.text.strip()
    if node.children and len(text) < 600:  # thin parent: drill down into its subtree
        sub = _subtree(doc.tree, nid)
        return (((text + "\n\n") if text else "") + best_passages(doc, sub, question, qvec),
                f"drill-down: best passages from {len(sub) - 1} subsections")
    if len(text) > SOURCE_CHARS:
        return best_passages(doc, {nid}, question, qvec), f"long section ({len(text):,} chars): best-matching passages"
    if len(text) < 200 and not node.children:
        # A heading with (almost) no text of its own: common in OCR'd notes and flat
        # outlines, where the body ends up under the next heading. Read on.
        order = list(doc.tree.nodes)
        i, extra, used = order.index(nid), [], 0
        for nxt in order[i + 1:i + 6]:
            n2 = doc.tree.nodes[nxt]
            if not n2.text.strip():
                continue
            extra.append(f"{n2.title}\n{n2.text.strip()}")
            used += len(n2.text)
            if used + len(text) >= 1200:
                break
        if extra:
            return ((text + "\n\n") if text else "") + "\n\n".join(extra)[:SOURCE_CHARS], \
                f"short section ({len(text)} chars) + the {len(extra)} section(s) that follow it"
    return text, f"whole section ({len(text):,} chars)"


def _source(doc: Doc, nid: str, text: str, multi: bool) -> dict:
    path = doc.tree.path_str(nid)
    return {"doc": doc.slug, "doc_name": doc.name, "node_id": nid,
            "path": path if not multi else path, "pages": doc.tree.nodes[nid].meta.get("pages"),
            "text": text.strip()[:SOURCE_CHARS + 600]}


# ------------------------------------------------------------------- gather
def _name_key(s: str) -> str:
    s = re.sub(r"\.(pdf|docx?|md|txt|png|jpe?g)$", "", s.strip(), flags=re.I)
    return re.sub(r"[\W_]+", " ", s.lower()).strip()


def pick_overview_docs(docs: list[Doc], question: str, k: int = 6, trace=None) -> list[Doc]:
    """Which documents an overview question is about: the ones it names; otherwise,
    when there are more than k, the k whose whole-document card is closest to it."""
    trace = trace or NullTrace()
    if len(docs) == 1:
        return docs
    q = _name_key(question)
    named = [d for d in docs if any(len(n) >= 4 and n in q for n in {_name_key(d.name), _name_key(d.slug),
                                                                      _name_key(d.tree.root.title)})]
    if named:
        trace.step("select", f"The question names {len(named)} document{'s' if len(named) > 1 else ''}",
                   ", ".join(d.name for d in named))
        return named
    if len(docs) <= k:
        return docs
    scored = []
    for d in docs:
        idx = [i for i in d.searcher.node_items("root") if d.searcher.items[i].kind == "card"]
        if not idx:
            continue
        qv = d.searcher.embed([question], "query")[0]
        scored.append((float(max(d.searcher.vecs[i] @ qv for i in idx)), d))
    scored.sort(key=lambda x: -x[0])
    keep = [d for _, d in scored[:k]]
    trace.step("select", f"Picked the {len(keep)} documents closest to the question",
               ", ".join(f"{d.name} ({s:.2f})" for s, d in scored[:k]))
    return keep


def gather_overview(docs: list[Doc], k: int = 6, trace=None, question: str = "") -> list[dict]:
    """One source per document: its root card plus its top-level section cards."""
    trace = trace or NullTrace()
    out = []
    for d in pick_overview_docs(docs, question, k, trace):
        root = d.cards.get("root", {})
        lines = [root.get("summary", "")] if root else []
        for cid in d.tree.root.children[:40]:
            c = d.cards.get(cid, {})
            pg = pages_str(d.tree.nodes[cid].meta.get("pages"))
            lines.append(f"- {d.tree.nodes[cid].title}{f' ({pg})' if pg else ''}: {c.get('summary', '')[:400]}")
        out.append(_source(d, "root", "\n".join(l for l in lines if l), len(docs) > 1))
        trace.step("sources", f"Map of “{d.name}”: whole-document card + {min(40, len(d.tree.root.children))} "
                              f"top-level section cards")
    return out[:k]


_SPLIT = re.compile(
    r"\?\s+(?=\S)|[?？]\s*|。\s*(?:また|さらに)、?|;\s+|,?\s+and\s+(?=(?:what|which|how|why|who|when|where|does|is|are)\b)"
    r"|,?\s+et\s+(?=(?:quel|quelle|quels|quelles|comment|pourquoi|qui|où)\b)|\s+और\s+(?=(?:क्या|कौन|कैसे|क्यों|कितन))", re.I)
_LINKED = re.compile(r"\b(relate|relation|compare|comparison|differ|difference|versus|vs\.?|both|between|while|"
                     r"as well as|in addition)\b|および|と比較|の関係|तुलना|संबंध|مقارنة|comparer|rapport entre", re.I)
DECOMPOSE = """Split the question into the separate things a search engine must look up to answer it. \
If it asks one thing, return it unchanged as the only item. At most 3 items, each a short standalone \
search query in the question's language. Reply with JSON only: {"queries": ["...", "..."]}"""


def sub_questions(question: str, llm=None) -> list[str]:
    """Parts of a compound question, or [] if it asks one thing. Cheap patterns first;
    a long question that links two topics ("how does X relate to Y") goes to the model."""
    parts = [p.strip(" ,;。、") for p in _SPLIT.split(question) if p and len(p.strip(" ,;。、")) >= 12]
    if len(parts) >= 2:
        return parts[:3]
    if llm is not None and len(question) >= 90 and _LINKED.search(question):
        try:
            from .llm import parse_json_object
            qs = parse_json_object(llm(DECOMPOSE, question)).get("queries") or []
            qs = [q.strip() for q in qs if isinstance(q, str) and len(q.strip()) >= 8]
            if len(qs) >= 2:
                return qs[:3]
        except Exception:
            pass
    return []


def _search_all(docs: list[Doc], question: str, k: int, trace, qvecs: dict, label: str = "") -> list:
    """Ranked (score, doc, node_id, why) candidates for one query across the documents."""
    multi = len(docs) > 1
    cands: list[tuple[float, Doc, str, str]] = []

    def qvec(d: Doc):
        key = (id(d.searcher.embed), question)
        if key not in qvecs:
            qvecs[key] = d.searcher.embed([question], "query")[0]
        return qvecs[key]

    with trace.timed("search", f"Hybrid search{label}: keywords (BM25) + meaning (vectors), fused by rank") as st:
        lines = []
        for d in docs:
            hits = d.searcher.search(question, k=k)
            nodes = [(h["score"], h["node_id"], "search") for h in hits]
            if hits:
                h = hits[0]
                lines.append(f"{d.name}: {len(hits)} sections; best “{d.tree.nodes[h['node_id']].title[:60]}” "
                             f"(keyword rank {h['bm25_rank'] or '–'}, meaning rank {h['dense_rank'] or '–'})")
            floor = min((s for s, _, _ in nodes), default=0.0)
            seen = {n for _, n, _ in nodes}
            ents = [n for n in entity_hits(d, question) if n not in seen]  # named in the question
            if ents:
                trace.step("entities", f"Entity index: question names things listed on {len(ents)} more "
                                       f"section card(s) in “{d.name}”",
                           items=[d.tree.path_str(n).split(" › ", 1)[-1][:90] for n in ents])
            nodes += [(floor * 0.99, n, "entity") for n in ents]
            for rrf_score, nid, why in nodes:
                score = rrf_score
                if multi:
                    # Rank-based scores are only comparable within one document. Across
                    # documents use absolute similarity (best card/chunk of the section),
                    # with the in-document rank as a small tie-breaker.
                    s = d.searcher
                    idx = s.node_items(nid)
                    cos = float(np.max(s.vecs[idx] @ qvec(d))) if idx else 0.0
                    score = cos + 0.5 * rrf_score
                cands.append((score, d, nid, why))
        st["items"] = lines
        st["detail"] = f"Searched {sum(len(d.searcher.items) for d in docs):,} index cards and chunks."
    cands.sort(key=lambda c: -c[0])
    if multi and cands:  # drop sections far less relevant than the best match
        best = cands[0][0]
        kept = [c for c in cands if c[0] >= best - MULTI_DOC_MARGIN]
        kept = kept if len(kept) >= 2 else cands[:2]
        dropped_docs = sorted({c[1].name for c in cands} - {c[1].name for c in kept})
        trace.step("rank", f"Cross-document ranking: kept {len(kept)} of {len(cands)} candidates",
                   f"Ranked by absolute similarity to the question; anything more than {MULTI_DOC_MARGIN} below "
                   f"the best match ({best:.3f}) was dropped as unrelated.",
                   items=[f"not relevant: {n}" for n in dropped_docs][:12])
        cands = kept
    return cands


def gather(docs: list[Doc], question: str, k: int = 6, trace=None, llm=None) -> tuple[list[dict], str]:
    trace = trace or NullTrace()
    mode = route(question)
    if mode == "overview":
        m = OVERVIEW.search(question)
        trace.step("route", "Route: overview of the whole document",
                   f"The question asks for an overview (“{m.group(0)}”), so the answer is built from each "
                   f"document's top-level index cards instead of searching for one passage.")
        return gather_overview(docs, k, trace=trace, question=question), mode
    trace.step("route", f"Route: search {'across ' + str(len(docs)) + ' documents' if len(docs) > 1 else 'the document'}",
               "Not an overview question, so look for the specific sections that answer it.")
    multi = len(docs) > 1
    qvecs: dict = {}
    parts = sub_questions(question, llm)
    if parts:
        trace.step("decompose", f"The question asks {len(parts)} things; each part is searched too", items=parts)
    lists = [(question, _search_all(docs, question, k, trace, qvecs, " for the whole question" if parts else ""))]
    for i, p in enumerate(parts, 1):
        lists.append((p, _search_all(docs, p, k, trace, qvecs, f" for part {i}")))

    def qv(d: Doc, q: str):
        key = (id(d.searcher.embed), q)
        if key not in qvecs:
            qvecs[key] = d.searcher.embed([q], "query")[0]
        return qvecs[key]

    # take the best remaining candidate from each list in turn, so every part gets sources
    order, pos = [], [0] * len(lists)
    while any(pos[i] < len(c) for i, (_, c) in enumerate(lists)):
        for i, (q, c) in enumerate(lists):
            if pos[i] < len(c):
                order.append((q, c[pos[i]]))
                pos[i] += 1
    out, used, how = [], set(), []
    for q, (_, d, nid, why) in order:
        if (d.slug, nid) in used:
            continue
        used.add((d.slug, nid))
        text, method = expand(d, nid, q, qv(d, q))
        out.append(_source(d, nid, text, multi))
        pg = pages_str(d.tree.nodes[nid].meta.get("pages"))
        how.append(f"[{len(out)}] {(d.name + ' › ') if multi else ''}"
                   f"{d.tree.path_str(nid).split(' › ', 1)[-1][:80]}{(' · ' + pg) if pg else ''} — {method}"
                   + (" (via entity index)" if why == "entity" else ""))
        if len(out) >= k:
            break
    trace.step("sources", f"Selected {len(out)} sources for the answer", items=how)
    return out, mode


def build_prompt(question: str, sources: list[dict]) -> str:
    multi = len({s["doc"] for s in sources}) > 1
    blocks = []
    for i, s in enumerate(sources, 1):
        where = (f"{s['doc_name']} › " if multi and not s["path"].startswith(s["doc_name"]) else "") + s["path"]
        if s["pages"]:
            where += f" ({pages_str(s['pages'])})"
        blocks.append(f"[{i}] {where}\n{s['text']}")
    return f"QUESTION: {question}\n\nSOURCES:\n\n" + "\n\n---\n\n".join(blocks)


def answer_docs(docs: list[Doc], llm: LLM, question: str, k: int = 6, trace=None) -> dict:
    trace = trace or NullTrace()
    sources, mode = gather(docs, question, k, trace, llm)
    lang = answer_language(question)
    prompt = build_prompt(question, sources)
    with trace.timed("answer", f"Writing the answer with {getattr(llm, 'label', 'the answer model')}") as st:
        text = llm(SYSTEM.format(language=lang), prompt).strip()
        st["title"] = f"Answer written by {getattr(llm, 'label', 'the answer model')}"
        st["detail"] = (f"The model saw only the {len(sources)} sources ({len(prompt):,} characters) and was told to "
                        f"cite every claim, answer in {lang.split(' (')[0]}, and say so if the sources lack the answer.")
    return {"answer": _ODD_CITE.sub(r"[\1]", text), "sources": sources, "route": mode}


def answer(tree: Tree, searcher: Searcher, llm: LLM, question: str, k: int = 6) -> dict:
    """Single-document convenience wrapper (kept for the CLI and benchmarks)."""
    doc = Doc("doc", tree.root.title, tree, searcher)
    return answer_docs([doc], llm, question, k)
