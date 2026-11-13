"""Turn any document into a Tree, using the best structure signal available.

Order of preference, per format:
  PDF   1. the PDF's own outline (bookmarks)
        2. headings detected from font size / bold spans
        3. fixed-size "parts" of a few pages each (the card writer names them)
        Scanned PDFs (no text layer) are reported; OCR them first.
  DOCX  1. Heading / Title paragraph styles
        2. short bold paragraphs used as headings
        3. fixed-size parts
  MD    markdown headings, else fixed-size parts
  TXT   markdown-style or numbered headings ("1.", "1.2", "Chapter 3"), else parts

Every node keeps where it came from (page range for PDFs) in node.meta so
answers can cite it.
"""
from __future__ import annotations

import re
import statistics
from pathlib import Path

from .ocr import IMAGE_EXT, LOW_CONFIDENCE as LOW_CONF
from .tree import Node, Tree, parse_markdown

PART_CHARS = 3500


class ScannedPDF(RuntimeError):
    pass


# --------------------------------------------------------------------- utils
def _parts(tree: Tree, parent: Node, blocks: list[tuple[str, dict]], prefix: str) -> None:
    """Group (text, meta) blocks into ~PART_CHARS parts under `parent`."""
    buf, meta, n = [], {}, 0

    def flush():
        nonlocal buf, meta, n
        if not buf:
            return
        n += 1
        text = "\n\n".join(buf)
        pages = meta.get("pages")
        title = f"Part {n}" + (f" (p. {pages[0]}–{pages[1]})" if pages and pages[0] != pages[1]
                               else f" (p. {pages[0]})" if pages else "")
        tree.add(Node(id=f"{prefix}{n}", title=title, kind="section", text=text,
                      meta={**meta, "synthetic_title": True}), parent)
        buf, meta = [], {}

    for text, m in blocks:
        if sum(len(b) for b in buf) + len(text) > PART_CHARS and buf:
            flush()
        buf.append(text)
        if "page" in m:
            p = meta.get("pages", [m["page"], m["page"]])
            meta["pages"] = [min(p[0], m["page"]), max(p[1], m["page"])]
    flush()


_NUM_HEADING = re.compile(r"^\s*((\d+(\.\d+){0,3})[.)]?|chapter\s+\d+|section\s+\d+|[IVX]+\.)\s+\S.{0,80}$", re.I)


# ----------------------------------------------------------------------- PDF
def _pdf(path: Path, ocr: str = "auto", ocr_engine: str = "auto", ocr_langs: str | None = None,
         cache_dir: Path | None = None, log=print) -> Tree:
    """ocr: auto (OCR pages without a text layer) | always | never."""
    import fitz

    doc = fitz.open(path)
    pages = [doc[i].get_text("text") for i in range(doc.page_count)]
    empty = [i + 1 for i, p in enumerate(pages) if len(p.strip()) < 30]
    need = list(range(1, doc.page_count + 1)) if ocr == "always" else (empty if ocr == "auto" else [])
    if need:
        from .ocr import ocr_pages, render_pdf_page
        log(f"    {len(need)} of {doc.page_count} pages have no text layer: running OCR ({ocr_engine})")
        res = ocr_pages([(n, (lambda n=n: render_pdf_page(path, n))) for n in need],
                        (cache_dir or path.parent / ".cortex_ocr") / path.stem, ocr_engine, ocr_langs, log)
        for r in res:
            pages[r["page"] - 1] = r["text"]
        tree = Tree(doc.metadata.get("title") or path.stem, meta={
            "source": str(path), "format": "pdf",
            "ocr": {"engine": res[0]["engine"] if res else ocr_engine, "pages": len(res),
                    "low_confidence_pages": [r["page"] for r in res if r["conf"] < LOW_CONF]},
        })
        if len(need) == doc.page_count or len(need) > doc.page_count // 2:
            return _from_paged_text(tree, pages)
    elif sum(len(p.strip()) for p in pages) < 50 * max(1, doc.page_count) * 0.2:
        raise ScannedPDF(f"{path.name}: {doc.page_count} pages but almost no text layer; "
                         f"ingest with OCR enabled (the default) instead of --ocr never")
    else:
        tree = Tree(doc.metadata.get("title") or path.stem, meta={"source": str(path), "format": "pdf"})

    toc = doc.get_toc()
    if len(toc) >= 2:
        tree.root.meta["structure"] = f"PDF outline (bookmarks): {len(toc)} entries"
        return _pdf_from_toc(tree, doc, toc, pages)
    heads = _pdf_detect_headings(doc)
    if len(heads) >= 2:
        tree.root.meta["structure"] = f"no outline; {len(heads)} headings detected from larger or bold type"
        return _pdf_from_headings(tree, doc, heads)
    tree.root.meta["structure"] = "no outline or headings: pages grouped into parts, titled by the card writer"
    _parts(tree, tree.root, [(p.strip(), {"page": i + 1}) for i, p in enumerate(pages) if p.strip()], "p")
    return tree


PAGE_MARK = re.compile(r"⟦p(\d+)⟧")


def _drop_page_furniture(pages: list[str]) -> list[str]:
    """Remove running headers/footers: short lines that repeat on several pages
    (book/chapter titles, 'M T W T F S S' on notebook paper, page labels)."""
    if len(pages) < 3:
        return pages

    def key(line: str) -> str:
        s = line.strip().lower()
        if s.startswith("#"):  # headings must repeat exactly ("Topic 1" vs "Topic 2" differ)
            return "#" + re.sub(r"[\W_]+", "", s)
        return re.sub(r"[\d०-९\W_]+", "", s)  # plain lines: ignore page numbers / punctuation

    counts: dict[str, int] = {}
    for p in pages:
        for k in {key(l) for l in p.splitlines() if 0 < len(l.strip()) <= 60}:
            if k:
                counts[k] = counts.get(k, 0) + 1
    limit = max(3, len(pages) // 4)
    repeated = {k for k, c in counts.items() if c >= limit}
    if not repeated:
        return pages
    return ["\n".join(l for l in p.splitlines() if not (len(l.strip()) <= 60 and key(l) in repeated))
            for p in pages]


def _from_paged_text(tree: Tree, pages: list[str]) -> Tree:
    """Build a tree from per-page text that may contain '## ' headings (OCR output),
    keeping page ranges per node."""
    pages = _drop_page_furniture(pages)
    md = "\n".join(f"⟦p{i + 1}⟧\n{t}" for i, t in enumerate(pages) if t.strip())
    n_heads = len(re.findall(r"^#{1,6}\s+\S", md, re.M))
    if n_heads >= 2:
        trial = Tree(tree.root.title, meta=dict(tree.root.meta))
        parse_markdown(md, trial, trial.root, "o")
        bodies = [len(PAGE_MARK.sub("", n.text).strip()) for nid, n in trial.nodes.items() if nid != "root"]
        empty = sum(1 for b in bodies if b < 40)
        if bodies and empty / len(bodies) <= 0.5:
            tree.nodes, tree.root = trial.nodes, trial.root
            tree.root.meta["structure"] = f"headings detected in OCR text: {n_heads}"
            current = 1
            for node in tree.nodes.values():  # creation order == document order
                found = [int(x) for x in PAGE_MARK.findall(node.text)]
                start = found[0] if found and node.text.lstrip().startswith("⟦") else current
                if found:
                    current = found[-1]
                node.text = PAGE_MARK.sub("", node.text).strip()
                if node.id != "root":
                    node.meta["pages"] = [start, max([start] + found)]
            return tree
        # Most "headings" have no text under them: the OCR over-marked headings
        # (list items, formulas). Treat them as ordinary lines instead.
        tree.root.meta["structure"] = (f"OCR marked {n_heads} lines as headings but {empty} of {len(bodies)} had no "
                                       f"text under them, so they were treated as ordinary lines; pages grouped "
                                       f"into parts, titled by the card writer")
        pages = [re.sub(r"^#{1,6}\s+", "", t, flags=re.M) for t in pages]
    else:
        tree.root.meta["structure"] = "no headings in OCR text: pages grouped into parts, titled by the card writer"
    _parts(tree, tree.root, [(t.strip(), {"page": i + 1}) for i, t in enumerate(pages) if t.strip()], "p")
    return tree


def _pdf_from_toc(tree: Tree, doc, toc, pages=None) -> Tree:
    # toc rows: [level, title, page(1-based)]; a section runs to the next entry's page.
    stack: list[tuple[int, Node]] = [(0, tree.root)]
    entries = []
    for i, (lvl, title, page) in enumerate(toc):
        while stack[-1][0] >= lvl:
            stack.pop()
        node = tree.add(Node(id=f"t{i + 1}", title=title.strip() or f"Section {i + 1}",
                             kind="section" if lvl == 1 else "subsection"), stack[-1][1])
        stack.append((lvl, node))
        entries.append((node, max(page, 1)))
    for j, (node, start) in enumerate(entries):
        nxt = entries[j + 1][1] if j + 1 < len(entries) else doc.page_count + 1
        last = min(max(start, nxt - 1), doc.page_count)  # same-page entries share that page
        node.text = "\n".join((pages[p - 1] if pages else doc[p - 1].get_text("text"))
                              for p in range(start, last + 1)).strip()
        node.meta["pages"] = [start, last]
    return tree


def _pdf_spans(doc):
    for pno in range(doc.page_count):
        for block in doc[pno].get_text("dict")["blocks"]:
            for line in block.get("lines", []):
                text = "".join(s["text"] for s in line["spans"]).strip()
                if not text:
                    continue
                size = max(s["size"] for s in line["spans"])
                bold = all(("Bold" in s["font"]) or (s["flags"] & 16) for s in line["spans"] if s["text"].strip())
                yield pno + 1, text, round(size, 1), bold


def _pdf_detect_headings(doc) -> list[tuple[int, int]]:
    """Return indices (into the span list) of heading lines: noticeably larger than
    body text or bold, short, and not ending like a sentence."""
    spans = list(_pdf_spans(doc))
    doc._cortex_spans = spans  # reuse below
    if not spans:
        return []
    body = statistics.median(s[2] for s in spans)
    heads = []
    for i, (_, text, size, bold) in enumerate(spans):
        short = len(text) <= 90 and not text.endswith((".", ",", ";")) and len(text.split()) <= 12
        if short and (size >= body * 1.15 or (bold and size >= body)) and re.search(r"[A-Za-zऀ-ॿ]", text):
            heads.append((i, 1 if size >= body * 1.4 else 2))
    # Too many "headings" means the signal is noise (e.g. a form or a table-heavy doc).
    return heads if len(heads) <= max(8, len(spans) // 6) else []


def _pdf_from_headings(tree: Tree, doc, heads) -> Tree:
    spans = doc._cortex_spans
    lines = []
    hset = dict(heads)
    for i, (page, text, _, _) in enumerate(spans):
        lines.append(("#" * hset[i] + " " + text) if i in hset else text)
    md = "\n".join(lines)
    parse_markdown(md, tree, tree.root, "h")
    # Attach page ranges by locating each heading's span index.
    for nid, (i, _) in zip([n for n in tree.nodes if n != "root"], heads):
        tree.nodes[nid].meta["pages"] = [spans[i][0], spans[i][0]]
    return tree


# ---------------------------------------------------------------------- DOCX
def _docx(path: Path) -> Tree:
    import docx

    d = docx.Document(str(path))
    tree = Tree(d.core_properties.title or path.stem, meta={"source": str(path), "format": "docx"})
    paras = [(p.text.strip(), p.style.name.lower(), p) for p in d.paragraphs if p.text.strip()]
    for t in d.tables:  # keep table text too, row by row
        for row in t.rows:
            cells = " | ".join(c.text.strip() for c in row.cells)
            if cells.strip(" |"):
                paras.append((cells, "table", None))

    def level(text, style, p):
        m = re.match(r"heading (\d)", style)
        if m:
            return int(m.group(1))
        if style == "title":
            return 1
        if p is not None and len(text) <= 80 and p.runs and all(r.bold for r in p.runs if r.text.strip()):
            return 2
        return 0

    levels = [level(*x) for x in paras]
    if sum(1 for l in levels if l) >= 2:
        md = "\n\n".join(("#" * l + " " + t) if l else t for (t, _, _), l in zip(paras, levels))
        tree.root.meta["structure"] = f"Word heading styles / bold heading lines: {sum(1 for l in levels if l)} headings"
        parse_markdown(md, tree, tree.root, "d")
    else:
        tree.root.meta["structure"] = "no headings in the Word file: paragraphs grouped into parts"
        _parts(tree, tree.root, [(t, {}) for t, _, _ in paras], "p")
    return tree


# ------------------------------------------------------------------ MD / TXT
def _text(path: Path) -> Tree:
    text = path.read_text(encoding="utf-8", errors="ignore")
    tree = Tree(path.stem, meta={"source": str(path), "format": path.suffix.lstrip(".") or "txt"})
    # MediaWiki / Wikipedia plain-text headings: "== History ==" -> "# History"
    wiki = re.compile(r"^(={2,6})\s*([^=\n].*?)\s*\1\s*$", re.M)
    if len(wiki.findall(text)) >= 2:
        tree.root.meta["structure"] = f"Wikipedia-style headings (== … ==): {len(wiki.findall(text))}"
        text = wiki.sub(lambda m: "#" * (len(m.group(1)) - 1) + " " + m.group(2), text)
    text = _strip_front_matter(text, tree)
    if re.search(r"^#{1,6}\s+\S", text, re.M):
        n = len(re.findall(r"^#{1,6}\s+\S", text, re.M))
        tree.root.meta.setdefault("structure", f"Markdown headings: {n}")
        parse_markdown(text, tree, tree.root, "m")
        return tree
    lines = text.splitlines()
    numbered = sum(1 for l in lines if _NUM_HEADING.match(l) and len(l) < 90)
    if numbered >= 2:
        def depth(l: str) -> int:  # "2.1.1 Foo" -> 3; "Chapter 2" / "IV." -> 1
            m = re.match(r"\s*(\d+(?:\.\d+)*)", l)
            return min(6, m.group(1).count(".") + 1) if m else 1
        md = "\n".join(("#" * depth(l) + " " + l.strip()) if _NUM_HEADING.match(l) and len(l) < 90 else l
                       for l in lines)
        tree.root.meta["structure"] = f"numbered headings (1., 2.1, …): {numbered}, nested by number depth"
        parse_markdown(md, tree, tree.root, "m")
        return tree
    tree.root.meta["structure"] = "no headings: paragraphs grouped into parts, titled by the card writer"
    _parts(tree, tree.root, [(p.strip(), {}) for p in re.split(r"\n\s*\n", text) if p.strip()], "p")
    return tree


_FRONT = re.compile(r"^(?:#\s*)?([A-Za-z][\w -]{0,30}):\s*(.+)$")


def _strip_front_matter(text: str, tree: Tree) -> str:
    """Leading 'Key: value' lines (YAML front matter, or '# Lines: 25-256' style
    metadata comments) go into tree meta instead of becoming headings."""
    lines = text.splitlines()
    if lines and lines[0].strip() == "---":  # YAML front matter
        end = next((i for i in range(1, min(len(lines), 60)) if lines[i].strip() == "---"), None)
        if end:
            for l in lines[1:end]:
                if (m := _FRONT.match(l.strip())):
                    tree.root.meta[m.group(1).strip().lower()] = m.group(2).strip()
            return "\n".join(lines[end + 1:])
    i = 0
    while i < min(len(lines), 10) and (m := _FRONT.match(lines[i].strip())) and lines[i].lstrip().startswith("#"):
        tree.root.meta[m.group(1).strip().lower()] = m.group(2).strip()
        i += 1
    return "\n".join(lines[i:])


def _image(path: Path, ocr_engine: str, ocr_langs: str | None, cache_dir: Path | None, log) -> Tree:
    from .ocr import load_image, ocr_pages
    res = ocr_pages([(1, lambda: load_image(path))], (cache_dir or path.parent / ".cortex_ocr") / path.stem,
                    ocr_engine, ocr_langs, log)
    tree = Tree(path.stem, meta={"source": str(path), "format": "image",
                                 "ocr": {"engine": res[0]["engine"], "pages": 1,
                                         "low_confidence_pages": [1] if res[0]["conf"] < LOW_CONF else []}})
    return _from_paged_text(tree, [res[0]["text"]])


def build_tree(path: Path, ocr: str = "auto", ocr_engine: str = "auto", ocr_langs: str | None = None,
               cache_dir: Path | None = None, log=print) -> Tree:
    ext = path.suffix.lower()
    if ext == ".pdf":
        return _pdf(path, ocr, ocr_engine, ocr_langs, cache_dir, log)
    if ext in IMAGE_EXT:
        return _image(path, ocr_engine, ocr_langs, cache_dir, log)
    if ext == ".docx":
        return _docx(path)
    if ext in (".md", ".markdown", ".txt", ".text", ".rst"):
        return _text(path)
    raise ValueError(f"unsupported file type: {path.suffix} "
                     f"(supported: pdf, docx, md, txt, {', '.join(sorted(e[1:] for e in IMAGE_EXT))})")
