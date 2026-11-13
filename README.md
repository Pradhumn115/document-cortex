# Document Cortex

Put in any document and ask anything about it. Answers cite the exact section and page,
and every citation is checked against its source.

Works with PDFs (including scanned books and **handwritten notes**), Word files, Markdown,
plain text and images, in any language — tested on English, Hindi, Sanskrit, French,
Japanese and Arabic.

![flow](https://img.shields.io/badge/ingest-structure%20%E2%86%92%20cards%20%E2%86%92%20index-0f6b5c)
![answers](https://img.shields.io/badge/answers-cited%20%2B%20checked-8a5a00)

## Demo (1 minute)

https://github.com/user-attachments/assets/af0f2e2a-fb4c-473c-a60f-72984fbacc9a

Every shot is the real app: it answers a question about the Transformer paper (2/2 claims supported,
cited to page 7), a French Wikipedia article and a scanned Hindi book, then shows the held-out
benchmark (91% vs 82% for plain RAG). If the player doesn't load:
[silent preview](docs/media/demo-preview.gif) ·
[download the MP4](https://github.com/Pradhumn115/document-cortex/raw/main/docs/media/document-cortex-demo.mp4).
Made with [HyperFrames](https://hyperframes.heygen.com); music from Apple Loops.

## Quick start (macOS / Linux)

```bash
git clone https://github.com/Pradhumn115/document-cortex.git
cd document-cortex
python3 -m venv .venv && source .venv/bin/activate
pip install -e .
brew install tesseract tesseract-lang      # OCR for scans (Linux: apt install tesseract-ocr tesseract-ocr-hin ...)

mkdir -p ~/.cortex && cp .env.example ~/.cortex/.env   # then put your Groq key in it
cortex serve                                            # opens http://127.0.0.1:8765
```

A free Groq key (https://console.groq.com/keys) is enough. The first run downloads a small
multilingual embedding model (~470 MB).

## Use it

**Web UI** — `cortex serve`: chats with history (new, rename, pin, search, export as Markdown,
clear or delete with undo), projects that group documents and their chats, a library with
folders, search and bulk actions, follow-up questions ("what is its dose?"), a picker for which
documents a chat searches, click any `[n]` to see the exact source passage, each document's
outline, the live process trace, settings (theme, keys, models) — on desktop and phone.
See [docs/UX.md](docs/UX.md).

**Command line**

```bash
cortex add report.pdf notes.docx scan.png      # add to the library (~/.cortex)
cortex list
cortex ask "What are the main findings?"        # all documents
cortex ask "What is the dose?" --doc my-herbs --show-check
cortex history ; cortex feedback <id> up ; cortex consolidate
cortex remove <slug> ; cortex keys
```

## How it works

```
file ──► parse ──► structure tree ──► index cards ──► hybrid index
         │         (outline, headings,   (small model:     (BM25 + vectors,
         │          numbered sections,    summary, terms,    RRF, one hit per
         │          or titled parts)      entities, Qs)      section)
         └─ scans: Tesseract (script auto-detect, 160+ languages);
            unsure pages: a vision model corrects Tesseract's draft
            (or reads handwriting from scratch); runaway output rejected

question ──► router ──► overview: top-level cards of each document
                 └───► search: hybrid search per document + entity index,
                       cross-document ranking by absolute similarity,
                       drill-down into long or parent sections
         ──► large model answers only from the sources, citing [n]
         ──► small model checks every cited sentence:
             supported · ⚠ partly · re-cited · ⚠ not in sources
         ──► memory: every Q&A logged; 👍 answers teach the cards (consolidate)
```

| Role | Default model | Notes |
|---|---|---|
| Index cards, answers | `openai/gpt-oss-120b` on Groq | won our card-writer benchmark |
| Citation check | `openai/gpt-oss-20b` on Groq | small and fast |
| OCR of handwriting / bad scans | `qwen/qwen3.8-27b` (vision) on Groq | or any local VLM via `CORTEX_VLM_BASE_URL` |
| Printed scans | Tesseract | `--ocr-engine surya` for Surya |
| Embeddings | `intfloat/multilingual-e5-small` (local) | `--embedder bge-m3 / qwen3-emb-0.6b` |

When the first Groq key's daily quota is used up, a second key (`GROQ_PAID_API_KEY`) takes
over automatically, and cards already written are cached, so nothing is paid for twice.
Any OpenAI-compatible server works too (`--provider openai`, e.g. LM Studio), as does Claude
(`--provider anthropic`).

## Tested

| document | format / language | result |
|---|---|---|
| RAPTOR paper (arXiv 2401.18059) | PDF, 23 pp | 7/7 questions |
| Wikipedia: आयुर्वेद / Photosynthèse / 光合成 / تمثيل ضوئي | TXT · DOCX; Hindi, French, Japanese, Arabic | 8/8 |
| RFC 2324 (HTCPCP) | numbered sections | 2/2 |
| *The Gift of the Magi* | no headings | 2/2 |
| Bhavaprakasha (गुडूची) | Hindi/Sanskrit Markdown | 2/2 |
| Handwritten ML notes | scanned PDF, 19 pp | 3/3 |
| *Nadi Darshan* (Hindi book) | scanned PDF | 2/2 |

`python3 scripts/fetch_samples.py` downloads the public test documents;
`bench/multidoc_questions.tsv` has the questions and expected answers.

### Independent benchmark (15 documents, 6 languages)

Questions written by a different model family (Qwen) from the raw text; every gold answer
carries a quote that code checks is in the document; answers graded by Qwen; every question
asked against the whole library. Baseline = plain RAG (same chunks, vectors only, top 6,
same answer model and prompt). Full write-up: `report/report.html` (`python3 scripts/build_report.py`).

| | main set (271) | held-out set (160, written after the fixes) |
|---|---|---|
| Document Cortex, fully correct | **87.5%** | **91.2%** |
| plain RAG, fully correct | 80.4% | 81.9% |
| paired: only we / only RAG right | 32 / 13 (p = 0.007) | 19 / 4 (p = 0.003) |
| evidence found by search | **90%** vs 79% | **89%** vs 81% |
| two-section questions | **86%** vs 82% | **83%** vs 60% |
| cross-language questions | **74%** vs 61% | **79%** vs 50% |
| unanswerable, correctly refused | 80% vs **88%** | **92%** vs 88% |

OCR character error on synthetic scans (3 pages per level; Tesseract → pipeline):
English medium blur 28% → 0.8%, heavy 100% → 14%; Hindi medium blur 22% → 6.5%
(the vision model alone: 40%); heavy Hindi stays unreadable and is marked so.

```bash
python3 -m cortex.evalset generate && python3 -m cortex.evalset run && python3 -m cortex.evalset score
python3 -m cortex.evalset --tag v2 generate --per-doc 4   # held-out set
python3 -m cortex.evalset ocr
```

Benchmarks (33 gold questions over 4 Bhavaprakasha herbs):

| card writer | hit@1 | MRR | | search | MRR |
|---|---|---|---|---|---|
| gpt-oss-120b | 0.61 | 0.67 | | hybrid (e5-small) | 0.67 |
| qwen3.8-27b | 0.58 | 0.65 | | BM25 only | 0.62 |
| gpt-oss-20b | 0.52 | 0.61 | | vectors only | 0.59 |
| no cards | 0.39 | 0.55 | | | |

## Project layout

```
cortex/
  ingest.py     any file -> structure tree (outline / headings / numbered / parts), OCR hookup
  ocr.py        Tesseract, Surya, vision-model OCR; page cache; running-header removal
  tree.py       sections, page ranges, chunking (danda- and CJK-aware)
  cards.py      bottom-up index cards; cached; daily-quota aware
  search.py     BM25 (+prefix stems, CJK bigrams), vectors, RRF, dedupe
  models.py     embedders and rerankers
  ask.py        router, multi-document gather, drill-down, entity index, cited answers
  verify.py     citation check
  library.py    library, ingest pipeline, history, feedback, consolidation
  web.py        FastAPI app + static/index.html (the UI)
  llm.py        Groq / OpenAI-compatible / Anthropic, key and model fallback
  bench.py      benchmarks; profiles/ has the Bhavaprakasha corpus loader
  evalset.py    independent benchmark: generate, run, score, OCR error rate
report/         project report (template + build script output)
  workspace.py  projects and chats (JSON files next to the library)
tests/          42 tests, no network needed
```

See [ROADMAP.md](ROADMAP.md) for what is built and what is next.
