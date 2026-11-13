"""Bhavaprakasha profile: builds the tree from the Ayurveda-AI pipeline output.

Input:
  herbs_dir  folder of corrected per-herb markdown files (herbs_final_split/),
             each starting with metadata lines such as "# Lines: 25-256".
  fulltext   bhav_full_text.txt; its running page headers ("गुडूच्यादिवर्गः")
             tell us which varga (chapter) a line range belongs to.

Output tree: Bhavaprakasha > varga > herb > markdown subsections.
This file is the only Ayurveda-specific part of milestone 1.
"""
from __future__ import annotations

import re
from collections import Counter
from pathlib import Path

from ..tree import HEADING_RE, Node, Tree, clean_title, parse_markdown

# "गुडूच्यादिवर्गः" / "अथ वटादिवर्गः" / OCR'd visarga "वटादिवर्ग:"; Devanagari name only.
VARGA_RE = re.compile(r"^\s*['\"\-]?\s*(?:अथ\s*)?([ऀ-ॿ]{2,}?)\s*वर्ग[ः:]")
META_RE = re.compile(r"^#\s*(Lines|Corrected|Herb|Split|Source)\b", re.I)
LINES_RE = re.compile(r"^#\s*Lines:\s*(\d+)\s*-\s*(\d+)", re.M)
HERB_META_RE = re.compile(r"^#\s*Herb:\s*(.+)$", re.M)

# OCR variants of the same varga name -> one spelling
VARGA_ALIASES = {
    "बटादि": "वटादि",
    "टादि": "वटादि",
    "हरितक्यादि": "हरीतक्यादि",
    "घात्वादि": "धात्वादि",
    "तंक": "तक्र",
    "धातूपधातुरसो": "धात्वादि",
    "अनेकार्थं": "अनेकार्थनाम",
}
APPENDIX = "परिशिष्ट (appendix)"


def normalize_varga(name: str) -> str:
    name = name.strip("'\"").replace(" ", "")
    return VARGA_ALIASES.get(name, name)


def varga_headers(fulltext: str) -> list[tuple[int, str]]:
    """(1-based line number, varga name) for every varga header line."""
    out = []
    for i, line in enumerate(fulltext.splitlines(), start=1):
        # Headers are short; long lines merely mention a varga. "…वर्गः समाप्तः"
        # marks a chapter's end, not its start.
        if len(line) > 60 or "समाप्त" in line:
            continue
        m = VARGA_RE.match(line)
        if m:
            out.append((i, normalize_varga(m.group(1))))
    return out


def assign_varga(start: int, end: int, headers: list[tuple[int, str]]) -> str | None:
    inside = Counter(v for ln, v in headers if start <= ln <= end)
    if inside:
        return inside.most_common(1)[0][0]
    before = [v for ln, v in headers if ln < start]
    return before[-1] if before else None


def smooth(labels: list[str | None]) -> list[str]:
    """Fix isolated outliers: a herb whose neighbours agree takes their varga."""
    out = list(labels)
    for i in range(len(out)):
        prev = next((out[j] for j in range(i - 1, -1, -1) if out[j]), None)
        nxt = next((labels[j] for j in range(i + 1, len(labels)) if labels[j]), None)
        if out[i] is None:
            out[i] = prev or nxt or APPENDIX
        elif prev and prev == nxt and out[i] != prev:
            out[i] = prev
    return out


def herb_title(path: Path, body: str) -> str:
    m = HERB_META_RE.search(body)
    if m:
        return clean_title(m.group(1))
    for line in body.splitlines():
        h = HEADING_RE.match(line)
        if h and not META_RE.match(line):
            return clean_title(h.group(2))
    return path.stem.split("_", 1)[-1] or path.stem


def strip_meta(body: str, title: str) -> str:
    """Drop metadata lines and the first heading if it just repeats the title."""
    lines, out, dropped_title = body.splitlines(), [], False
    for line in lines:
        if META_RE.match(line):
            continue
        h = HEADING_RE.match(line)
        if h and not dropped_title and clean_title(h.group(2)) == title:
            dropped_title = True
            continue
        out.append(line)
    return "\n".join(out)


def build(herbs_dir: Path, fulltext_path: Path, limit_groups: int | None = None,
          only_groups: list[str] | None = None) -> Tree:
    files = sorted(p for p in herbs_dir.glob("*.txt") if not p.name.startswith("."))
    headers = varga_headers(fulltext_path.read_text(encoding="utf-8"))

    records = []
    for p in files:
        body = p.read_text(encoding="utf-8")
        m = LINES_RE.search(body)
        span = (int(m.group(1)), int(m.group(2))) if m else None
        varga = assign_varga(*span, headers) if span else None
        records.append({"path": p, "body": body, "span": span, "varga": varga})

    # Files without a line span are the hand-split appendix (453a…): keep them
    # out of smoothing so they don't pull real vargas around.
    spanned = [r for r in records if r["span"]]
    for r, v in zip(spanned, smooth([r["varga"] for r in spanned])):
        r["varga"] = v
    for r in records:
        if not r["span"]:
            r["varga"] = APPENDIX

    tree = Tree("भावप्रकाश निघण्टु (Bhavaprakasha)", meta={"profile": "bhavaprakasha"})
    groups: dict[str, Node] = {}
    order: list[str] = []
    for r in records:
        v = r["varga"]
        if v not in groups:
            order.append(v)
            if only_groups and v not in only_groups:
                groups[v] = None  # type: ignore[assignment]
                continue
            if limit_groups is not None and sum(1 for g in groups.values() if g) >= limit_groups:
                groups[v] = None  # type: ignore[assignment]
                continue
            title = v if v == APPENDIX else f"{v}वर्ग"
            groups[v] = tree.add(Node(id=f"g{len(order):02d}", title=title, kind="group"), tree.root)
        group = groups[v]
        if group is None:
            continue
        stem = r["path"].stem
        title = herb_title(r["path"], r["body"])
        meta = {"source_file": r["path"].name}
        if r["span"]:
            meta["lines"] = list(r["span"])
        herb = tree.add(Node(id=f"h{stem.split('_', 1)[0]}", title=title, kind="section", meta=meta), group)
        parse_markdown(strip_meta(r["body"], title), tree, herb, herb.id)
    return tree
