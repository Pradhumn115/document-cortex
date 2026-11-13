"""Citation check: does each cited claim actually appear in the source it cites?

1. Split the answer into claims (sentences / bullet items) with their [n] citations.
2. One call to a small model judges every claim against the numbered sources:
   supported | partial | unsupported, plus which sources really support it.
3. Apply the verdicts to the answer:
     unsupported but another source supports it -> citation replaced ("re-cited")
     unsupported and no source supports it      -> marked ⚠ not in sources
     cited source wrong, others right            -> citation corrected
   Claims without any citation are reported too.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field

from .llm import LLM, parse_json_object

CITE = re.compile(r"\[(\d+)\]")
SOURCE_CHARS = 2500

SYSTEM = """You check citations in an answer against numbered sources.
For each numbered CLAIM, decide whether the sources it cites support it:
  "supported"   the cited source(s) state this (paraphrase is fine)
  "partial"     the cited source(s) support part of it; the rest is missing or stretched
  "unsupported" the cited source(s) do not support it
Also list "supported_by": the numbers of ALL sources that do support the claim
(may differ from the cited ones; empty if none do).
Judge only against the source text, not your own knowledge.
Return ONLY JSON: {"verdicts": [{"claim": 1, "verdict": "...", "supported_by": [..],
"note": "<10 words on what is missing, if not supported>"}, ...]}"""


@dataclass
class Claim:
    idx: int
    text: str
    cites: list[int]
    start: int  # character span in the answer
    end: int
    verdict: str = ""
    supported_by: list[int] = field(default_factory=list)
    note: str = ""
    block: int = 0
    inherited: bool = False  # citation taken from the end of its list/paragraph


def recite(text: str, nums: list[int]) -> str:
    """Replace a sentence's citations, placing the new ones before its final punctuation."""
    body = re.sub(r"\s+([.!?।])", r"\1", CITE.sub("", text)).rstrip()
    cites = "".join(f"[{n}]" for n in nums)
    m = re.search(r"[.!?।]+$", body)
    return f"{body[:m.start()]} {cites}{m.group()}" if m else f"{body} {cites}"


ABBREV = re.compile(r"(?:\b(?:e\.g|i\.e|et al|vs|fig|figs|eq|etc|cf|approx|no|vol|pp|dr|mr|mrs|ms|st)"
                    r"|\b[A-Z])\.$", re.I)
# Sentence end: . ! ? । 。 ！ ？ (plus any citations right after), then space or end of line.
# CJK enders need no following space.
END = re.compile(r"(?:[.!?।]+(?:\s*\[\d+\])*(?=\s|$)|[。！？](?:\s*\[\d+\])*)")
# Heading-ish lines: "## Foo", "**Foo**", or a short lead-in ending in a colon
# ("The eight branches are:"); any script, so no \w (it misses Devanagari vowel signs).
HEADING_LINE = re.compile(r"^\s*(#{1,6}\s.*|\*\*[^*]+\*\*\s*[:：]?|[^.!?।。\n\[\]]{1,80}[:：])\s*$")


def _sentences(line: str):
    """(start, end) spans of sentences in one line, not splitting after
    abbreviations (e.g., et al.) or inside numbers (82.6)."""
    start = 0
    for m in END.finditer(line):
        if m.group().startswith(".") and ABBREV.search(line[start:m.start() + 1]):
            continue
        yield start, m.end()
        start = m.end()
    if line[start:].strip():
        yield start, len(line)


def split_claims(answer: str) -> list[Claim]:
    """Sentences and bullet items with real text; code blocks and heading-only
    lines are skipped. Citations written just before the sentence end
    ("... [1]。") stay with their sentence."""
    claims: list[Claim] = []
    block_cites: dict[int, list[int]] = {}  # citation-only lines, e.g. a lone "[1]" after a list
    pos, in_code, block = 0, False, 0
    for line in answer.splitlines(keepends=True):
        start_line = pos
        pos += len(line)
        if not line.strip():
            block += 1  # blank line ends a paragraph / list
            continue
        if line.lstrip().startswith(("```", "~~~")):
            in_code = not in_code
            continue
        if in_code or HEADING_LINE.match(line):
            continue
        if not re.search(r"\w{2}", CITE.sub("", line)):
            if CITE.search(line):
                block_cites.setdefault(block, []).extend(int(c) for c in CITE.findall(line))
            continue
        for s, e in _sentences(line.rstrip("\n")):
            text = line[s:e].strip()
            if len(re.sub(r"\[\d+\]|[\W_]", "", text)) < 6:
                continue
            claims.append(Claim(len(claims) + 1, text, [int(c) for c in CITE.findall(text)],
                                start_line + s, start_line + e, block=block))
    # Lists and paragraphs often cite once at the end ("... [1][2]" or a lone "[1]").
    # Uncited sentences inherit the next citation in the same block.
    # If a block has no citation at all, a short closing block right after it
    # (a lone "[1]", or one sentence like "described in source [5]") covers it.
    per_block: dict[int, list[Claim]] = {}
    for c in claims:
        per_block.setdefault(c.block, []).append(c)

    def closing_cites(b: int) -> list[int] | None:
        if b in block_cites:
            return block_cites[b]
        nxt = per_block.get(b, [])
        if len(nxt) == 1 and nxt[0].cites and len(nxt[0].text) <= 160:
            return nxt[0].cites
        return None

    own = {b for b, cs in per_block.items() if any(c.cites for c in cs)} | set(block_cites)
    for i, c in enumerate(claims):
        if c.cites:
            continue
        nxt = next((d.cites for d in claims[i + 1:] if d.block == c.block and d.cites and not d.inherited),
                   None)
        nxt = nxt or block_cites.get(c.block)
        if not nxt and c.block not in own:
            nxt = closing_cites(c.block + 1)
        if nxt:
            c.cites, c.inherited = list(dict.fromkeys(nxt)), True
    return claims


def verify(llm: LLM, answer: str, sources: list[dict]) -> tuple[str, dict]:
    claims = split_claims(answer)
    cited = [c for c in claims if c.cites]
    report = {"claims": len(claims), "uncited": sum(1 for c in claims if not c.cites),
              "supported": 0, "partial": 0, "recited": 0, "unsupported": 0, "details": []}
    if not cited:
        return answer, report

    src = "\n\n".join(f"[{i}] {s['path']}\n{s['text'][:SOURCE_CHARS]}" for i, s in enumerate(sources, 1))
    listing = "\n".join(f"CLAIM {c.idx} (cites {', '.join(f'[{n}]' for n in c.cites)}): "
                        f"{CITE.sub('', c.text).strip()}" for c in cited)
    try:
        data = parse_json_object(llm(SYSTEM, f"SOURCES:\n\n{src}\n\nCLAIMS:\n{listing}"))
        verdicts = {int(v["claim"]): v for v in data.get("verdicts", [])}
    except Exception as e:  # checking failed: say so, never block the answer
        report["error"] = str(e)[:200]
        return answer, report

    valid = set(range(1, len(sources) + 1))
    edits: list[tuple[int, int, str]] = []
    for c in cited:
        v = verdicts.get(c.idx, {})
        c.verdict = str(v.get("verdict", "unchecked")).lower()
        c.supported_by = [int(n) for n in v.get("supported_by", []) if str(n).isdigit() and int(n) in valid]
        c.note = str(v.get("note", ""))[:120]
        new = c.text
        if c.verdict == "supported":
            report["supported"] += 1
        elif c.verdict == "partial":
            report["partial"] += 1
            new = c.text + " ⚠ partly supported"
        elif c.supported_by:
            report["recited"] += 1
            new = recite(c.text, c.supported_by)
            c.verdict = "re-cited"
        else:
            report["unsupported"] += 1
            new = c.text + " ⚠ not found in the sources"
        if new != c.text:
            edits.append((c.start, c.end, new))
        report["details"].append({"claim": CITE.sub("", c.text).strip()[:100], "cited": c.cites,
                                  "verdict": c.verdict, "supported_by": c.supported_by, "note": c.note})

    out = answer
    for start, end, new in sorted(edits, reverse=True):
        seg = out[start:end]
        lead = seg[:len(seg) - len(seg.lstrip())]
        out = out[:start] + lead + new + out[end:]
    return out, report


def summary_line(report: dict) -> str:
    if report.get("error"):
        return f"Citation check failed: {report['error']}"
    checked = report["supported"] + report["partial"] + report["recited"] + report["unsupported"]
    if not checked:
        return "Citation check: no cited claims."
    parts = [f"{report['supported']} supported"]
    if report["partial"]:
        parts.append(f"{report['partial']} partly supported")
    if report["recited"]:
        parts.append(f"{report['recited']} re-cited to the right source")
    if report["unsupported"]:
        parts.append(f"{report['unsupported']} not found in sources")
    if report["uncited"]:
        parts.append(f"{report['uncited']} sentences without a citation")
    return f"Citation check ({checked} claims): " + ", ".join(parts)
