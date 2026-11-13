"""Fill report/template.html with the benchmark results -> report/report.html.

    python3 scripts/build_report.py
"""
from __future__ import annotations

import json
import random
import statistics
import time
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BENCH = ROOT / "bench"
LIB = Path.home() / ".cortex" / "docs"

LANG = {"wiki-hi": "Hindi", "wiki-fr": "French", "wiki-ja": "Japanese", "wiki-ar": "Arabic",
        "bhav-guduchi": "Hindi / Sanskrit", "nadi-darshan-p40-47-scan": "Hindi"}
SHORT = {"wiki-hi": "आयुर्वेद (Hindi wiki)", "wiki-fr": "Photosynthèse (French wiki, DOCX)",
         "wiki-ja": "光合成 (Japanese wiki)", "wiki-ar": "تمثيل ضوئي (Arabic wiki)",
         "bhav-guduchi": "Bhavaprakasha: गुडूची", "nadi-darshan-p40-47-scan": "Nadi Darshan (scanned Hindi)",
         "Ml-mini": "Handwritten ML notes (scan)", "gift-of-the-magi": "The Gift of the Magi",
         "rfc2324-htcpcp": "RFC 2324 (HTCPCP)", "rfc9110-http-semantics": "RFC 9110 (HTTP Semantics)",
         "raptor-2401.18059": "RAPTOR paper", "attention-is-all-you-need-1706.03762": "Attention Is All You Need",
         "alice-in-wonderland": "Alice in Wonderland", "wiki-en-photosynthesis": "Photosynthesis (English wiki)",
         "wiki-en-ayurveda": "Ayurveda (English wiki)"}
FORMAT = {".pdf": "PDF", ".txt": "Plain text", ".md": "Markdown", ".docx": "Word (DOCX)"}
SCANNED = {"Ml-mini": "Scanned PDF, handwriting", "nadi-darshan-p40-47-scan": "Scanned PDF"}


def load(p: Path) -> list[dict]:
    return [json.loads(l) for l in p.read_text(encoding="utf-8").splitlines() if l.strip()]


def main() -> None:
    summary = json.loads((BENCH / "summary_v1.json").read_text(encoding="utf-8"))
    ocr = json.loads((BENCH / "ocr_v1.json").read_text(encoding="utf-8"))
    qs = {q["id"]: q for q in load(BENCH / "eval_v1.jsonl")}
    rs = {r["id"]: r for r in load(BENCH / "results_v1.jsonl")}
    gs = {g["id"]: g for g in load(BENCH / "grades_v1.jsonl")}
    asked = [q for q in qs.values() if q["id"] in rs]
    per_doc = {}
    for q in asked:
        per_doc[q["doc"]] = per_doc.get(q["doc"], 0) + 1

    docs = []
    for slug in sorted(per_doc, key=lambda s: SHORT.get(s, s)):
        meta = json.loads((LIB / slug / "meta.json").read_text(encoding="utf-8"))
        tree = json.loads((LIB / slug / "tree.json").read_text(encoding="utf-8"))
        ext = Path(meta.get("file", "")).suffix.lower()
        root = next(n for n in tree["nodes"] if n["id"] == "root")
        st = root.get("meta", {}).get("structure") or meta.get("structure") or "headings"
        docs.append({"slug": slug, "short": SHORT.get(slug, slug), "name": SHORT.get(slug, meta.get("name", slug)),
                     "format": SCANNED.get(slug, FORMAT.get(ext, ext)), "language": LANG.get(slug, "English"),
                     "structure": str(st)[:60], "sections": len(tree["nodes"]) - 1, "questions": per_doc[slug]})

    # examples: where the pipeline's extra steps mattered, plus one it got wrong
    rng = random.Random(3)
    win = [i for i in rs if gs.get(i) and gs[i]["ours"]["grade"] == "correct" and gs[i]["base"]["grade"] == "incorrect"]
    lose = [i for i in rs if gs.get(i) and gs[i]["ours"]["grade"] == "incorrect"]
    by_type = {}
    for i in win:
        by_type.setdefault(qs[i]["type"], []).append(i)
    pick = [rng.choice(v) for t, v in sorted(by_type.items())][:4] + (rng.sample(lose, 1) if lose else [])
    examples = [{"type": qs[i]["type"], "doc": SHORT.get(qs[i]["doc"], qs[i]["doc"]), "question": qs[i]["question"],
                 "gold": qs[i]["answer"][:220], "ours": rs[i]["answer"][:700],
                 "g_ours": gs[i]["ours"]["grade"], "g_base": gs[i]["base"]["grade"]} for i in pick]

    limits = json.loads((ROOT / "report" / "limits.json").read_text(encoding="utf-8"))
    data = {"summary": summary, "ocr": ocr, "docs": docs, "examples": examples, "limits": limits,
            "generated": len(qs) + int((BENCH / "dropped_v1.txt").read_text().strip() or 0)
            if (BENCH / "dropped_v1.txt").exists() else len(qs),
            "langs": len({d["language"].split(" /")[0] for d in docs}) + 1,  # + Sanskrit
            "base_latency": round(statistics.median(r["baseline"]["t"] for r in rs.values()), 2),
            "date": time.strftime("%d %B %Y"),
            "sig": json.loads((BENCH / "significance_v1.json").read_text()),
            "audit": summary.get("label_audit", {})}
    html = (ROOT / "report" / "template.html").read_text(encoding="utf-8")
    html = html.replace("/*__DATA__*/null", json.dumps(data, ensure_ascii=False).replace("</", "<\\/"))
    (ROOT / "report" / "report.html").write_text(html, encoding="utf-8")
    print("report/report.html", len(html) // 1024, "KB")


if __name__ == "__main__":
    main()
