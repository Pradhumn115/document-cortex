"""Hybrid index over cards and chunks: BM25 + dense vectors, fused with
reciprocal rank fusion (RRF).

Chunks are embedded with their tree path in front ("contextual chunks"), and
cards are embedded as title + summary + questions + terms, so a question can
match a card's anticipated questions directly.
"""
from __future__ import annotations

import json
import math
import re
from collections import Counter
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Sequence

import numpy as np

from .tree import Tree

TOKEN_SPLIT = re.compile(r"[\s।॥|,.;:!?()\[\]{}\"'“”‘’\-–—/*#=+<>०-९0-9]+")
PREFIX_LEN = 5  # crude stem for inflected Sanskrit/Hindi forms (हरीतकी / हरीतक्या)


CJK = re.compile(r"[぀-ヿ㐀-䶿一-鿿가-힯]+")
CJK_PUNCT = re.compile(r"[。、！？「」『』（）・：；]+")


def tokenize(text: str) -> list[str]:
    text = CJK_PUNCT.sub(" ", text.lower())
    toks = [t for t in TOKEN_SPLIT.split(text) if len(t) > 1]
    out: list[str] = []
    for t in toks:
        runs = CJK.findall(t)
        if not runs:
            out.append(t)
            if len(t) > PREFIX_LEN:
                out.append(t[:PREFIX_LEN])  # crude stem for inflected forms
            continue
        # Chinese/Japanese/Korean have no spaces between words: index character
        # bigrams (standard for CJK search), plus any non-CJK remainder.
        for r in runs:
            out.extend(r[i:i + 2] for i in range(max(1, len(r) - 1)))
        rest = CJK.sub(" ", t).split()
        out.extend(x for x in rest if len(x) > 1)
    return out


class BM25:
    def __init__(self, docs: Sequence[list[str]], k1: float = 1.4, b: float = 0.75):
        self.k1, self.b = k1, b
        self.n = len(docs)
        self.len = np.array([len(d) for d in docs], dtype=np.float32)
        self.avg = float(self.len.mean()) if self.n else 0.0
        self.postings: dict[str, list[tuple[int, int]]] = {}
        for i, d in enumerate(docs):
            for t, f in Counter(d).items():
                self.postings.setdefault(t, []).append((i, f))
        self.idf = {t: math.log(1 + (self.n - len(p) + 0.5) / (len(p) + 0.5))
                    for t, p in self.postings.items()}

    def scores(self, query: list[str]) -> np.ndarray:
        out = np.zeros(self.n, dtype=np.float32)
        for t in set(query):
            idf = self.idf.get(t)
            if idf is None:
                continue
            for i, f in self.postings[t]:
                norm = self.k1 * (1 - self.b + self.b * self.len[i] / self.avg)
                out[i] += idf * f * (self.k1 + 1) / (f + norm)
        return out


Embedder = Callable[[list[str], str], np.ndarray]  # (texts, "query"|"passage") -> (n, d) unit vectors


def e5_embedder(model_name: str = "intfloat/multilingual-e5-small", batch: int = 32) -> Embedder:
    import torch
    from transformers import AutoModel, AutoTokenizer

    tok = AutoTokenizer.from_pretrained(model_name)
    model = AutoModel.from_pretrained(model_name).eval()

    def embed(texts: list[str], role: str) -> np.ndarray:
        vecs = []
        for i in range(0, len(texts), batch):
            b = tok([f"{role}: {t}" for t in texts[i:i + batch]], padding=True,
                    truncation=True, max_length=512, return_tensors="pt")
            with torch.no_grad():
                h = model(**b).last_hidden_state
            m = b["attention_mask"][..., None]
            e = (h * m).sum(1) / m.sum(1)
            vecs.append(torch.nn.functional.normalize(e, dim=-1).numpy())
        return np.vstack(vecs).astype(np.float32) if vecs else np.zeros((0, 384), np.float32)
    return embed


@dataclass
class Item:
    id: str
    kind: str  # card | chunk
    node_id: str
    path: str
    title: str
    text: str  # what gets indexed


def card_search_text(card: dict) -> str:
    return "\n".join([
        card["path"], card["title"], card.get("summary", ""),
        " ".join(card.get("questions", [])),
        " ".join(card.get("learned_questions", [])),  # added by Library.consolidate()
        " ".join(card.get("key_terms", [])), " ".join(card.get("entities", [])),
    ])


def build_items(tree: Tree, cards: dict[str, dict]) -> list[Item]:
    items = [Item(f"card:{nid}", "card", nid, c["path"], c["title"], card_search_text(c))
             for nid, c in cards.items() if nid in tree.nodes]
    for ch in tree.chunks:
        path = tree.path_str(ch.node_id)
        items.append(Item(f"chunk:{ch.id}", "chunk", ch.node_id, path,
                          tree.nodes[ch.node_id].title, f"{path}\n{ch.text}"))
    return items


def build_index(tree: Tree, cards: dict[str, dict], out_dir: Path, embed: Embedder) -> None:
    out_dir.mkdir(parents=True, exist_ok=True)
    items = build_items(tree, cards)
    vecs = embed([it.text for it in items], "passage")
    np.save(out_dir / "vectors.npy", vecs)
    (out_dir / "items.jsonl").write_text(
        "\n".join(json.dumps(it.__dict__, ensure_ascii=False) for it in items), encoding="utf-8")


def rrf(rankings: list[list[int]], k: int = 60) -> dict[int, float]:
    scores: dict[int, float] = {}
    for ranking in rankings:
        for rank, idx in enumerate(ranking):
            scores[idx] = scores.get(idx, 0.0) + 1.0 / (k + rank + 1)
    return scores


class Searcher:
    def __init__(self, index_dir: Path, embed: Embedder, reranker=None, rerank_depth: int = 20):
        lines = (index_dir / "items.jsonl").read_text(encoding="utf-8").splitlines()
        self.items = [Item(**json.loads(l)) for l in lines if l.strip()]
        self.vecs = np.load(index_dir / "vectors.npy")
        self.bm25 = BM25([tokenize(it.text) for it in self.items])
        self.embed = embed
        self.reranker = reranker
        self.rerank_depth = rerank_depth
        self._by_node: dict[str, list[int]] = {}
        for i, it in enumerate(self.items):
            self._by_node.setdefault(it.node_id, []).append(i)

    def node_items(self, node_id: str) -> list[int]:
        """Indexes of the card and chunks belonging to one section."""
        return self._by_node.get(node_id, [])

    def search(self, query: str, k: int = 8, kind: str = "all", depth: int = 50,
               dedupe: bool = True, mode: str = "hybrid") -> list[dict]:
        """mode: hybrid (BM25 + dense, RRF) | bm25 | dense.
        dedupe: keep only the best-ranked item per tree node, so a section's
        card and its chunks don't fill several of the top-k slots.
        If a reranker is set, the top `rerank_depth` candidates are re-scored."""
        mask = np.array([kind == "all" or it.kind == kind for it in self.items])
        lex_rank: list[int] = []
        dense_rank: list[int] = []
        if mode in ("hybrid", "bm25"):
            lex = np.where(mask, self.bm25.scores(tokenize(query)), -np.inf)
            lex_rank = [int(i) for i in np.argsort(-lex)[:depth] if np.isfinite(lex[i]) and lex[i] > 0]
        if mode in ("hybrid", "dense"):
            dense = np.where(mask, self.vecs @ self.embed([query], "query")[0], -np.inf)
            dense_rank = [int(i) for i in np.argsort(-dense)[:depth] if np.isfinite(dense[i])]
        fused = rrf([r for r in (lex_rank, dense_rank) if r])
        ordered = sorted(fused, key=fused.get, reverse=True)
        if dedupe:
            seen: set[str] = set()
            ordered = [i for i in ordered
                       if not (self.items[i].node_id in seen or seen.add(self.items[i].node_id))]
        if self.reranker is not None and ordered:
            head = ordered[:self.rerank_depth]
            scores = self.reranker(query, [self.items[i].text for i in head])
            head = [head[j] for j in np.argsort(-scores)]
            fused.update({i: float(s) for i, s in zip(head, sorted(scores, reverse=True))})
            ordered = head + ordered[self.rerank_depth:]
        top = ordered[:k]
        return [{
            "id": self.items[i].id, "kind": self.items[i].kind, "node_id": self.items[i].node_id,
            "path": self.items[i].path, "score": round(fused[i], 4),
            "bm25_rank": lex_rank.index(i) + 1 if i in lex_rank else None,
            "dense_rank": dense_rank.index(i) + 1 if i in dense_rank else None,
            "text": self.items[i].text,
        } for i in top]
