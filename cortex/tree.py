"""Document tree: the book's own hierarchy (book > group > section > subsection),
plus leaf chunks cut from each node's own text.

The tree is the skeleton every later stage hangs off: index cards are written
per node, chunks carry their node's path as context, and search results are
reported by path.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path

HEADING_RE = re.compile(r"^(#{1,6})\s+(.*?)\s*#*\s*$")


@dataclass
class Node:
    id: str
    title: str
    kind: str  # book | group | section | subsection
    text: str = ""  # the node's own text, excluding children
    children: list[str] = field(default_factory=list)
    parent: str | None = None
    meta: dict = field(default_factory=dict)


@dataclass
class Chunk:
    id: str
    node_id: str
    ord: int
    text: str


class Tree:
    def __init__(self, title: str, meta: dict | None = None):
        self.nodes: dict[str, Node] = {}
        self.chunks: list[Chunk] = []
        self.root = self.add(Node(id="root", title=title, kind="book", meta=meta or {}))

    # -- construction -------------------------------------------------
    def add(self, node: Node, parent: Node | None = None) -> Node:
        if node.id in self.nodes:
            raise ValueError(f"duplicate node id {node.id}")
        self.nodes[node.id] = node
        if parent is not None:
            node.parent = parent.id
            parent.children.append(node.id)
        return node

    # -- navigation ---------------------------------------------------
    def path(self, node_id: str) -> list[str]:
        out, cur = [], self.nodes.get(node_id)
        while cur is not None:
            out.append(cur.title)
            cur = self.nodes.get(cur.parent) if cur.parent else None
        return list(reversed(out))

    def path_str(self, node_id: str) -> str:
        return " › ".join(self.path(node_id))

    def depth(self, node_id: str) -> int:
        return len(self.path(node_id)) - 1

    def bottom_up(self) -> list[list[str]]:
        """Node ids grouped by depth, deepest first, so children are always
        processed before their parents."""
        by_depth: dict[int, list[str]] = {}
        for nid in self.nodes:
            by_depth.setdefault(self.depth(nid), []).append(nid)
        return [by_depth[d] for d in sorted(by_depth, reverse=True)]

    # -- persistence --------------------------------------------------
    def save(self, path: Path) -> None:
        data = {
            "nodes": [asdict(n) for n in self.nodes.values()],
            "chunks": [asdict(c) for c in self.chunks],
        }
        path.write_text(json.dumps(data, ensure_ascii=False, indent=1), encoding="utf-8")

    @classmethod
    def load(cls, path: Path) -> "Tree":
        data = json.loads(path.read_text(encoding="utf-8"))
        nodes = [Node(**n) for n in data["nodes"]]
        tree = cls.__new__(cls)
        tree.nodes = {n.id: n for n in nodes}
        tree.root = tree.nodes["root"]
        tree.chunks = [Chunk(**c) for c in data["chunks"]]
        return tree


def parse_markdown(text: str, tree: Tree, parent: Node, id_prefix: str) -> None:
    """Attach markdown headings under `parent` as nested nodes.

    Heading levels are relative: the shallowest heading in `text` becomes a
    direct child of `parent`. Text before the first heading belongs to `parent`.
    """
    lines = text.splitlines()
    # Lines inside ``` / ~~~ fences are code (e.g. "# install deps"), never headings.
    in_code, code = False, []
    for line in lines:
        if line.lstrip().startswith(("```", "~~~")):
            in_code = not in_code
            code.append(True)
        else:
            code.append(in_code)
    levels = [len(m.group(1)) for line, c in zip(lines, code) if not c and (m := HEADING_RE.match(line))]
    base = min(levels) if levels else 1

    stack: list[tuple[int, Node]] = [(base - 1, parent)]
    buf: list[str] = []
    counter = 0

    def flush():
        node = stack[-1][1]
        chunk = "\n".join(buf).strip()
        if chunk:
            node.text = (node.text + "\n\n" + chunk).strip() if node.text else chunk
        buf.clear()

    for line, is_code in zip(lines, code):
        m = None if is_code else HEADING_RE.match(line)
        if not m:
            buf.append(line)
            continue
        flush()
        level, title = len(m.group(1)), clean_title(m.group(2))
        while stack and stack[-1][0] >= level:
            stack.pop()
        counter += 1
        kind = "section" if stack[-1][1] is parent and parent.kind in ("book", "group") else "subsection"
        node = tree.add(Node(id=f"{id_prefix}.{counter}", title=title or "(untitled)", kind=kind), stack[-1][1])
        stack.append((level, node))
    flush()


def clean_title(s: str) -> str:
    s = re.sub(r"[*_`]+", "", s).strip().rstrip(":：").strip()
    return s


_SPLIT_SENT = re.compile(r"(?<=[।॥.!?])\s+")


def chunk_text(text: str, max_chars: int = 900) -> list[str]:
    """Pack paragraphs into chunks of at most ~max_chars; split long paragraphs
    on sentence/verse boundaries (danda included)."""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces: list[str] = []
    for p in paras:
        if len(p) <= max_chars:
            pieces.append(p)
            continue
        cur = ""
        for sent in _SPLIT_SENT.split(p):
            if cur and len(cur) + len(sent) + 1 > max_chars:
                pieces.append(cur)
                cur = ""
            while len(sent) > max_chars:  # pathological: no boundaries at all
                pieces.append(sent[:max_chars])
                sent = sent[max_chars:]
            cur = f"{cur} {sent}".strip()
        if cur:
            pieces.append(cur)

    chunks: list[str] = []
    cur = ""
    for piece in pieces:
        if cur and len(cur) + len(piece) + 2 > max_chars:
            chunks.append(cur)
            cur = piece
        else:
            cur = f"{cur}\n\n{piece}" if cur else piece
    if cur:
        chunks.append(cur)
    return chunks


def build_chunks(tree: Tree, max_chars: int = 900) -> None:
    tree.chunks = []
    for node in tree.nodes.values():
        for i, text in enumerate(chunk_text(node.text, max_chars)):
            tree.chunks.append(Chunk(id=f"{node.id}#c{i}", node_id=node.id, ord=i, text=text))
