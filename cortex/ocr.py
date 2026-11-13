"""OCR for scanned PDFs and images.

Engines
  tesseract  default; fast, 160+ languages (hin, san, ara, jpn, chi_sim, ...),
             per-word confidence. Language is picked per document from
             Tesseract's script detection unless given.
  surya      higher quality on hard scans and Indic scripts, slower (local model).

Output per page: text with "## " on lines set in noticeably larger type
(so scans get an outline too), the page's mean confidence, and the engine.
Pages are cached as JSON under <workdir>/ocr/, so re-runs never redo OCR.
"""
from __future__ import annotations

import io
import json
import re
import statistics
import subprocess
from pathlib import Path

LOW_CONFIDENCE = 80.0  # mean word confidence (0-100) below which a page goes to the vision model
# (blurred English scored 61-63 while getting 12-51% of characters wrong; clean print scores 93-96)
# Tesseract is over-confident on blurred Indic scripts: a damaged Hindi page scored 79 while getting
# 13% of characters wrong, and clean printed Hindi scans score 87-90. So Indic pages escalate below 85.
# A page Tesseract mostly read (confidence >= DRAFT_MIN) goes to the vision model with Tesseract's text
# as a draft to correct: on blurred Hindi that beat both Tesseract and the vision model alone, while
# the vision model reading Hindi from scratch did worse than Tesseract. Handwriting scores below 50,
# so it keeps the plain prompt.
DRAFT_MIN = 50.0
INDIC = {"hin", "san", "mar", "nep", "ben", "asm", "guj", "pan", "ori", "tam", "tel", "kan", "mal", "sin"}
LOW_CONFIDENCE_INDIC = 85.0


def looks_runaway(text: str) -> bool:
    """A vision model that cannot read a page sometimes repeats itself for thousands of
    characters. Such text compresses far better than real prose; reject it."""
    import zlib
    b = text.encode("utf-8")
    return len(b) > 1500 and len(zlib.compress(b)) / len(b) < 0.12


def escalation_threshold(langs: str) -> float:
    return LOW_CONFIDENCE_INDIC if INDIC & set(langs.split("+")) else LOW_CONFIDENCE

SCRIPT_LANGS = {
    "Devanagari": "hin+san+eng", "Latin": "eng", "Arabic": "ara+eng", "Han": "chi_sim+eng",
    "HanS": "chi_sim+eng", "HanT": "chi_tra+eng", "Japanese": "jpn+eng", "Hangul": "kor+eng",
    "Cyrillic": "rus+eng", "Bengali": "ben+eng", "Tamil": "tam+eng", "Telugu": "tel+eng",
    "Gujarati": "guj+eng", "Gurmukhi": "pan+eng", "Kannada": "kan+eng", "Malayalam": "mal+eng",
    "Greek": "ell+eng", "Hebrew": "heb+eng", "Thai": "tha+eng",
}
IMAGE_EXT = {".png", ".jpg", ".jpeg", ".tif", ".tiff", ".webp", ".bmp"}


# ------------------------------------------------------------------- inputs
def render_pdf_page(pdf_path: Path, page_no: int, dpi: int = 200):
    import fitz
    from PIL import Image
    doc = fitz.open(pdf_path)
    pix = doc[page_no - 1].get_pixmap(dpi=dpi)
    return Image.open(io.BytesIO(pix.tobytes("png"))).convert("RGB")


def load_image(path: Path):
    from PIL import Image
    return Image.open(path).convert("RGB")


# ----------------------------------------------------------- language detect
def detect_langs(image) -> str:
    """Tesseract OSD script detection -> a language string for Tesseract."""
    import pytesseract
    try:
        osd = pytesseract.image_to_osd(image, config="--psm 0")
        script = re.search(r"Script:\s*(\S+)", osd).group(1)
        conf = float(re.search(r"Script confidence:\s*([\d.]+)", osd).group(1))
        if conf >= 1.0 and script in SCRIPT_LANGS:
            return SCRIPT_LANGS[script]
    except Exception:
        pass
    return "eng+hin"  # safe default for this project's documents


# ---------------------------------------------------------------- tesseract
def _mark_headings(lines: list[tuple[str, float]]) -> str:
    """lines: (text, line height px). Lines clearly taller than body text,
    short, and not sentence-like become '## ' headings."""
    heights = [h for t, h in lines if len(t) > 15]
    body = statistics.median(heights) if heights else 0
    out = []
    for text, h in lines:
        short = len(text) <= 80 and len(text.split()) <= 12 and not text.rstrip().endswith((".", ",", ";", "।"))
        if body and h >= body * 1.35 and short and re.search(r"[^\W\d_]{2}", text):
            out.append("## " + text)
        else:
            out.append(text)
    return "\n".join(out)


def tesseract_page(image, langs: str) -> dict:
    import pytesseract
    from pytesseract import Output
    d = pytesseract.image_to_data(image, lang=langs, config="--psm 3", output_type=Output.DICT)
    lines: dict[tuple, dict] = {}
    confs = []
    for i, word in enumerate(d["text"]):
        if not word.strip():
            continue
        key = (d["block_num"][i], d["par_num"][i], d["line_num"][i])
        ln = lines.setdefault(key, {"words": [], "h": 0, "block": d["block_num"][i]})
        ln["words"].append(word)
        ln["h"] = max(ln["h"], d["height"][i])
        c = float(d["conf"][i])
        if c >= 0:
            confs.append(c)
    ordered, prev_block = [], None
    for key in sorted(lines):
        ln = lines[key]
        if prev_block is not None and ln["block"] != prev_block:
            ordered.append(("", 0))  # paragraph break between blocks
        ordered.append((" ".join(ln["words"]), ln["h"]))
        prev_block = ln["block"]
    text = _mark_headings([(t, h) for t, h in ordered if t]) if ordered else ""
    # re-insert blank lines between blocks (headings logic ignores them)
    if ordered:
        parts, buf = [], []
        for t, h in ordered:
            if not t:
                parts.append(buf)
                buf = []
            else:
                buf.append((t, h))
        parts.append(buf)
        marked = iter(text.split("\n"))
        text = "\n\n".join("\n".join(next(marked) for _ in p) for p in parts if p)
    return {"text": text, "conf": round(statistics.mean(confs), 1) if confs else 0.0,
            "engine": f"tesseract:{langs}"}


# -------------------------------------------------------------------- surya
_SURYA = {}


def surya_page(image, langs: str = "") -> dict:
    if not _SURYA:
        from surya.detection import DetectionPredictor
        from surya.foundation import FoundationPredictor
        from surya.recognition import RecognitionPredictor
        _SURYA["rec"] = RecognitionPredictor(FoundationPredictor())
        _SURYA["det"] = DetectionPredictor()
    res = _SURYA["rec"]([image], det_predictor=_SURYA["det"])[0]
    lines, confs = [], []
    for tl in res.text_lines:
        x0, y0, x1, y1 = tl.bbox
        lines.append((tl.text.strip(), y1 - y0))
        confs.append(float(getattr(tl, "confidence", 1.0) or 0) * 100)
    return {"text": _mark_headings([l for l in lines if l[0]]),
            "conf": round(statistics.mean(confs), 1) if confs else 0.0, "engine": "surya"}


# ---------------------------------------------------------------------- vlm
VLM_PROMPT = ("/no_think\nTranscribe ALL text on this page exactly as written, in its original language "
              "and script (handwritten or printed). Keep lists, numbering and line breaks. Mark a line "
              "with '## ' ONLY if it is a real section title that introduces the text below it (usually "
              "underlined, boxed or larger); never mark list items, formulas, labels or short lines. "
              "Do not translate, summarise or add commentary. If the page has no text, output nothing.")
SCRIPT_NAMES = {"hin": "Hindi (Devanagari script)", "san": "Sanskrit (Devanagari script)", "mar": "Marathi (Devanagari)",
                "ben": "Bengali", "tam": "Tamil", "tel": "Telugu", "guj": "Gujarati", "pan": "Punjabi (Gurmukhi)",
                "kan": "Kannada", "mal": "Malayalam", "ara": "Arabic", "jpn": "Japanese", "chi_sim": "Chinese",
                "rus": "Russian", "eng": "English"}


def vlm_prompt(langs: str | None = None, draft: str | None = None) -> str:
    p = VLM_PROMPT
    names = [SCRIPT_NAMES[l] for l in (langs or "").split("+") if l in SCRIPT_NAMES and l != "eng"]
    if names:
        p += f" The page is in {' / '.join(dict.fromkeys(names))}."
    p += " If a word cannot be read, write [?] instead of guessing."
    if draft and draft.strip():
        p += ("\n\nAn OCR program produced this draft of the page. It may contain errors; correct it "
              "against the image, keeping words it got right:\n\n" + draft.strip()[:6000])
    return p


VLM_DEFAULT = "qwen/qwen3.8-27b"  # on Groq; reads handwriting (tested on handwritten notes)


def vlm_page(image, model: str = VLM_DEFAULT, base_url: str | None = None,
             langs: str | None = None, draft: str | None = None) -> dict:
    """OCR one page with a vision-language model over an OpenAI-compatible API.
    Default: Groq (free key, then paid key on daily quota). For a local model
    set CORTEX_VLM_BASE_URL (e.g. mlx_vlm.server / LM Studio) and CORTEX_VLM_MODEL."""
    import base64
    import os

    import openai

    from .llm import DailyLimit, _DAILY

    base_url = base_url or os.environ.get("CORTEX_VLM_BASE_URL") or "https://api.groq.com/openai/v1"
    model = os.environ.get("CORTEX_VLM_MODEL", model)
    keys = [os.environ.get(k) for k in ("GROQ_API_KEY", "GROQ_PAID_API_KEY")] if "groq" in base_url \
        else [os.environ.get("OPENAI_API_KEY", "local")]
    keys = [k for k in keys if k]
    if not keys:
        raise RuntimeError("vlm OCR needs GROQ_API_KEY (or CORTEX_VLM_BASE_URL for a local model)")
    im = image.copy()
    im.thumbnail((1600, 1600))
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=88)
    url = "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()
    last = None
    for key in keys:
        client = openai.OpenAI(base_url=base_url, api_key=key, timeout=120, max_retries=0)
        for attempt in range(5):
            try:
                r = client.chat.completions.create(model=model, max_tokens=4000, temperature=0.0, messages=[
                    {"role": "user", "content": [{"type": "text", "text": vlm_prompt(langs, draft)},
                                                 {"type": "image_url", "image_url": {"url": url}}]}])
                text = (r.choices[0].message.content or "").strip()
                text = re.sub(r"^<think>.*?</think>\s*", "", text, flags=re.S)
                text = re.sub(r"^```\w*\n|\n```$", "", text)
                return {"text": text, "conf": 85.0, "engine": f"vlm:{model}"}
            except Exception as e:
                last = e
                if _DAILY.search(str(e)):
                    break  # this key is done for today; try the next key
                import time
                time.sleep(min(30, 3 * 2 ** attempt))
    raise DailyLimit(f"vision OCR failed on every key: {last}") if last and _DAILY.search(str(last)) \
        else RuntimeError(f"vision OCR failed: {last}")


# --------------------------------------------------------------------- main
def ocr_pages(images: list[tuple[int, callable]], cache_dir: Path, engine: str = "auto",
              langs: str | None = None, log=print) -> list[dict]:
    """images: (page_no, loader) pairs; loader() returns a PIL image.
    engine: auto (tesseract, then the vision model for low-confidence pages,
    e.g. handwriting) | tesseract | surya | vlm.
    Returns [{page, text, conf, engine}], using the cache where possible."""
    cache_dir.mkdir(parents=True, exist_ok=True)
    out = []
    escalated = 0
    for n, (page, load) in enumerate(images, 1):
        cfile = cache_dir / f"page_{page:04d}.{engine}.json"
        if cfile.exists():
            out.append(json.loads(cfile.read_text(encoding="utf-8")))
            continue
        img = load()
        if engine in ("tesseract", "auto"):
            if langs is None:
                langs = detect_langs(img)
                log(f"    OCR language: {langs} (script detected on page {page} by Tesseract)")
            res = tesseract_page(img, langs)
            limit = escalation_threshold(langs)
            if engine == "auto" and res["conf"] < limit:
                try:
                    log(f"    page {page}: Tesseract confidence {res['conf']:.0f} < {limit:.0f} "
                        f"(handwriting or poor scan) -> re-reading with the vision model"
                        + (" (correcting Tesseract's draft)" if res["conf"] >= DRAFT_MIN else ""))
                    use_draft = res["conf"] >= DRAFT_MIN and len(res["text"].strip()) >= 40
                    v = vlm_page(img, langs=langs if use_draft else None, draft=res["text"] if use_draft else None)
                    v["tesseract_conf"] = res["conf"]
                    if looks_runaway(v["text"]):
                        log(f"    page {page}: the vision model's text repeats itself; page marked unreadable")
                        v = {**v, "text": "", "conf": 0.0, "unreadable": True}
                    v["mode"] = "corrected Tesseract draft" if use_draft else "read from scratch"
                    res = v
                    escalated += 1
                except Exception as e:  # keep the tesseract text for now; the page stays flagged
                    log(f"    page {page}: vision OCR unavailable ({str(e)[:80]}); keeping tesseract text")
                    res["page"] = page
                    out.append(res)  # not cached, so the next run tries the vision model again
                    continue
        elif engine == "surya":
            res = surya_page(img)
        elif engine == "vlm":
            res = vlm_page(img)
        else:
            raise ValueError(f"unknown OCR engine {engine!r}")
        res["page"] = page
        cfile.write_text(json.dumps(res, ensure_ascii=False), encoding="utf-8")
        out.append(res)
        if n % 5 == 0 or n == len(images):
            log(f"    OCR {n}/{len(images)} pages" + (f" ({escalated} re-read by the vision model)"
                                                       if escalated else ""))
    return out


def tesseract_available() -> bool:
    try:
        subprocess.run(["tesseract", "--version"], capture_output=True, check=True)
        return True
    except Exception:
        return False
