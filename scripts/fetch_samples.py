"""Download the public test documents used in bench/multidoc_questions.tsv into samples/.

    python3 scripts/fetch_samples.py && cortex add samples/*

Wikipedia articles (CC BY-SA, via the Wikipedia API as plain text), RFC 2324,
O. Henry's "The Gift of the Magi" (Project Gutenberg), the RAPTOR paper (arXiv),
and a DOCX made from the French article to exercise the DOCX path.
"""
from __future__ import annotations

import json
import re
import urllib.parse
import urllib.request
from pathlib import Path

UA = {"User-Agent": "document-cortex-samples/0.1"}
OUT = Path(__file__).resolve().parent.parent / "samples"
WIKI = {"hi": "आयुर्वेद", "fr": "Photosynthèse", "ja": "光合成", "ar": "تمثيل ضوئي"}
WIKI_EN = {"wiki-en-photosynthesis.txt": "Photosynthesis", "wiki-en-ayurveda.txt": "Ayurveda"}
FILES = {
    "rfc2324-htcpcp.txt": "https://www.rfc-editor.org/rfc/rfc2324.txt",
    "gift-of-the-magi.txt": "https://www.gutenberg.org/cache/epub/7256/pg7256.txt",
    "raptor-2401.18059.pdf": "https://arxiv.org/pdf/2401.18059",
    # long / differently structured documents for the benchmark (bench/eval_v1.jsonl)
    "rfc9110-http-semantics.txt": "https://www.rfc-editor.org/rfc/rfc9110.txt",
    "attention-is-all-you-need-1706.03762.pdf": "https://arxiv.org/pdf/1706.03762",
    "alice-in-wonderland.txt": "https://www.gutenberg.org/cache/epub/11/pg11.txt",
}


def get(url: str) -> bytes:
    return urllib.request.urlopen(urllib.request.Request(url, headers=UA), timeout=120).read()


def main() -> None:
    OUT.mkdir(exist_ok=True)
    for lang, title in WIKI.items():
        q = urllib.parse.urlencode({"action": "query", "prop": "extracts", "explaintext": 1,
                                    "titles": title, "format": "json", "redirects": 1})
        page = next(iter(json.loads(get(f"https://{lang}.wikipedia.org/w/api.php?{q}"))["query"]["pages"].values()))
        (OUT / f"wiki-{lang}.txt").write_text(page.get("extract", ""), encoding="utf-8")
        print(f"wiki-{lang}.txt")
    for name, title in WIKI_EN.items():
        q = urllib.parse.urlencode({"action": "query", "prop": "extracts", "explaintext": 1,
                                    "titles": title, "format": "json", "redirects": 1})
        page = next(iter(json.loads(get(f"https://en.wikipedia.org/w/api.php?{q}"))["query"]["pages"].values()))
        (OUT / name).write_text(page.get("extract", ""), encoding="utf-8")
        print(name)
    for name, url in FILES.items():
        (OUT / name).write_bytes(get(url))
        print(name)
    try:
        import docx
        d = docx.Document()
        d.add_heading("Photosynthèse", 0)
        for block in re.split(r"\n\s*\n", (OUT / "wiki-fr.txt").read_text(encoding="utf-8")):
            m = re.match(r"^(={2,6})\s*(.*?)\s*\1\s*$", block.strip())
            if m:
                d.add_heading(m.group(2), level=len(m.group(1)) - 1)
            elif block.strip():
                d.add_paragraph(block.strip())
        d.save(OUT / "wiki-fr.docx")
        print("wiki-fr.docx")
    except ImportError:
        pass


if __name__ == "__main__":
    main()
