# Document Cortex — what's built and what's left

Compared against the [architecture doc](https://claude.ai/artifact/XxoW4vc2PWxUPmDRwJDVbk) (2026-11-13).

## Built

| Layer | Status |
|---|---|
| Parse: PDF (outline / font headings / parts), DOCX, MD, TXT, wiki `==` headings, numbered headings | ✅ |
| OCR: Tesseract (160+ languages, script auto-detect) → vision model (Qwen3.8-27B) for low-confidence pages, e.g. handwriting; running headers removed; per-page cache | ✅ |
| Structure tree with page ranges | ✅ (PDF sections split at page granularity) |
| Index cards (summary, terms, entities, questions, titles) — bottom-up, cached, quota-aware | ✅ |
| Hybrid search (BM25 + prefix stems + CJK bigrams, dense vectors, RRF, one hit per section) | ✅ |
| Answer with citations; long sections send their best chunks | ✅ |
| Citation check (small model; supported / partial / re-cited / not found; list items) | ✅ |
| Free → paid Groq key fallback, model fallback on daily quota | ✅ |
| Benchmarks: card writers, retrieval; 14-question multilingual test set | ✅ |

## Built in this round

| Gap | What gets built |
|---|---|
| One document at a time | **Library**: many documents, ask one or all of them |
| Retrieval quality | **Reranker / better embedder**, adopted only if the benchmark shows a gain |
| Fixed pipeline for every question | **Router**: overview questions use the document's top-level cards (map-reduce); others use search |
| Parent section hit, answer in a child | **Drill-down**: a thin parent hit expands to its best-matching children |
| Concept graph | **Entity index** from the cards (entity → sections, co-occurring entities); questions that name entities pull their sections in |
| Episodic memory | **Query log + 👍/👎 feedback**; `consolidate` adds well-answered questions to the cards they hit, so retrieval improves with use |
| No UI | **Web UI**: upload, ingest progress, library, chat with citations, source viewer, check badges, feedback |
| Hard to run | **Package**: `pip install -e .`, `cortex serve`, `.env` for keys, data under `~/.cortex` |

## Benchmark notes

- Embedders are within noise on the 33-question set (MRR: Qwen3-Embedding-0.6B 0.70, bge-m3 0.69,
  e5-small 0.67); e5-small stays the default (smallest, fastest).
- Qwen3-Reranker-0.6B made results much worse (MRR 0.25) and slow; likely our integration
  (long Devanagari inputs hit the 1,024-token limit and truncate the yes/no prompt suffix).
  Not adopted until fixed.
- Cross-document ranking uses absolute similarity: relevant sections scored 0.85–0.88 vs
  0.78–0.82 for unrelated documents; candidates more than 0.05 below the best are dropped.

## Later

- Typed knowledge graph in Neo4j with schema plugins (the Ayurveda herb schema as plugin #1).
- PDF sections split at the heading's position on the page, not whole pages.
- Table extraction as structured data (rows/columns), figure captions.
- Auto-generated evaluation questions per document.
- Full agentic navigator (multi-step tool use over the tree) for multi-hop questions.

## Independent benchmark (v1, 2026-11-13)

272 questions over 15 documents (`bench/`, `cortex/evalset.py`). Document Cortex 86.4% correct vs
81.2% for plain RAG (p = 0.034). What it found:

- Fixed: library-wide overview questions used the first six documents instead of the one named
  (overview accuracy 27% → 67%).
- Open: two-section questions (73% vs 86% for plain RAG) and unanswerable questions (77% vs 85%):
  the answer model sometimes fills gaps from general knowledge. Ideas: stricter refusal prompt,
  drop sentences the citation check marks "not found".
- Open: Tesseract is over-confident on blurred Hindi (confidence 79, 13% character error), so the
  page never escalates to the vision model. Escalate Indic pages on a lower bar or on a second signal.
- To do: a person audits `bench/spotcheck_v1.md` to measure how often the grader is wrong.

### Fixes after v1 (checked on a held-out set, v2)

- Compound questions are split into parts and each part is searched (two-section: 73% → 86%; held-out 83% vs 60%).
- Answer prompt forbids facts not in the sources and says which part is missing (unanswerable: 77% → 80%;
  held-out 92% vs 88%). On v1 plain RAG still refuses slightly more often (88%).
- OCR: escalate below 80 (85 for Indic scripts); the vision model corrects Tesseract's draft (Hindi medium blur
  22% → 6.5%); repetitive vision output is rejected and the page marked unreadable. Image clean-up
  (upscale/denoise/binarise) and a script-only hint were tried and made results worse.
- Grader reasons before grading (it had contradicted itself).
