"""Generic profile: one markdown file, or a folder of them (one group per file)."""
from __future__ import annotations

from pathlib import Path

from ..tree import Node, Tree, parse_markdown


def build(src: Path, title: str | None = None) -> Tree:
    if src.is_file():
        tree = Tree(title or src.stem, meta={"profile": "markdown"})
        parse_markdown(src.read_text(encoding="utf-8"), tree, tree.root, "n")
        return tree
    tree = Tree(title or src.name, meta={"profile": "markdown"})
    files = sorted(p for p in src.rglob("*") if p.suffix.lower() in (".md", ".txt", ".markdown"))
    for i, p in enumerate(files, start=1):
        group = tree.add(Node(id=f"f{i:03d}", title=p.stem, kind="group", meta={"source_file": str(p)}), tree.root)
        parse_markdown(p.read_text(encoding="utf-8"), tree, group, group.id)
    return tree
