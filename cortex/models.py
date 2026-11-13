"""Embedding and reranking models, loaded with plain `transformers`
(the global sentence-transformers install is broken by a peft version clash).

Embedders:  e5-small (intfloat/multilingual-e5-small, HF cache)
            bge-m3   (BAAI/bge-m3, CLS pooling)
            qwen3-emb-0.6b (Qwen/Qwen3-Embedding-0.6B, last-token pooling + query instruction)
Rerankers:  qwen3-rr-0.6b (Qwen/Qwen3-Reranker-0.6B, P("yes") from a causal LM)
            bge-rr-m3     (BAAI/bge-reranker-v2-m3, cross-encoder logit)

Local weights live in ~/.cache/cortex-models/<org>/<repo> (see scripts/hf_fetch.sh);
e5-small comes from the regular Hugging Face cache.
"""
from __future__ import annotations

import os
from pathlib import Path
from typing import Callable

import numpy as np

LOCAL = Path(os.environ.get("CORTEX_MODELS", Path.home() / ".cache/cortex-models"))
QUERY_TASK = ("Given a question about an Ayurvedic or other reference text, "
              "retrieve the passage that answers it")

Embedder = Callable[[list[str], str], np.ndarray]
Reranker = Callable[[str, list[str]], np.ndarray]


def _device():
    import torch
    return "mps" if torch.backends.mps.is_available() else "cpu"


def _path(repo: str) -> str:
    p = LOCAL / repo
    return str(p) if p.exists() else repo


def make_embedder(name: str, batch: int = 16, max_length: int = 512) -> Embedder:
    import torch
    from transformers import AutoModel, AutoTokenizer

    dev = _device()
    if name == "e5-small":
        repo, pooling, side = "intfloat/multilingual-e5-small", "mean", "right"
    elif name == "bge-m3":
        repo, pooling, side = "BAAI/bge-m3", "cls", "right"
    elif name == "qwen3-emb-0.6b":
        repo, pooling, side = "Qwen/Qwen3-Embedding-0.6B", "last", "left"
    else:
        raise ValueError(f"unknown embedder {name!r}")

    tok = AutoTokenizer.from_pretrained(_path(repo), padding_side=side)
    model = AutoModel.from_pretrained(_path(repo)).to(dev).eval()

    def fmt(t: str, role: str) -> str:
        if name == "e5-small":
            return f"{role}: {t}"
        if name == "qwen3-emb-0.6b" and role == "query":
            return f"Instruct: {QUERY_TASK}\nQuery:{t}"
        return t

    def embed(texts: list[str], role: str) -> np.ndarray:
        out = []
        for i in range(0, len(texts), batch):
            b = tok([fmt(t, role) for t in texts[i:i + batch]], padding=True,
                    truncation=True, max_length=max_length, return_tensors="pt").to(dev)
            with torch.no_grad():
                h = model(**b).last_hidden_state
            if pooling == "mean":
                m = b["attention_mask"][..., None]
                e = (h * m).sum(1) / m.sum(1)
            elif pooling == "cls":
                e = h[:, 0]
            else:  # last token; left padding puts it at position -1
                e = h[:, -1]
            out.append(torch.nn.functional.normalize(e.float(), dim=-1).cpu().numpy())
        return np.vstack(out).astype(np.float32)
    return embed


def make_reranker(name: str, batch: int = 8, max_length: int = 1024) -> Reranker:
    import torch
    from transformers import (AutoModelForCausalLM, AutoModelForSequenceClassification,
                              AutoTokenizer)

    dev = _device()
    if name == "bge-rr-m3":
        repo = "BAAI/bge-reranker-v2-m3"
        tok = AutoTokenizer.from_pretrained(_path(repo))
        model = AutoModelForSequenceClassification.from_pretrained(_path(repo)).to(dev).eval()

        def score(query: str, docs: list[str]) -> np.ndarray:
            out = []
            for i in range(0, len(docs), batch):
                b = tok([[query, d] for d in docs[i:i + batch]], padding=True, truncation=True,
                        max_length=max_length, return_tensors="pt").to(dev)
                with torch.no_grad():
                    out.append(model(**b).logits.view(-1).float().cpu().numpy())
            return np.concatenate(out) if out else np.zeros(0)
        return score

    if name == "qwen3-rr-0.6b":
        repo = "Qwen/Qwen3-Reranker-0.6B"
        tok = AutoTokenizer.from_pretrained(_path(repo), padding_side="left")
        model = AutoModelForCausalLM.from_pretrained(_path(repo)).to(dev).eval()
        yes, no = tok.convert_tokens_to_ids("yes"), tok.convert_tokens_to_ids("no")
        prefix = ("<|im_start|>system\nJudge whether the Document meets the requirements based on "
                  "the Query and the Instruct provided. Note that the answer can only be \"yes\" or "
                  "\"no\".<|im_end|>\n<|im_start|>user\n")
        suffix = "<|im_end|>\n<|im_start|>assistant\n<think>\n\n</think>\n\n"

        def score(query: str, docs: list[str]) -> np.ndarray:
            out = []
            for i in range(0, len(docs), batch):
                texts = [f"{prefix}<Instruct>: {QUERY_TASK}\n<Query>: {query}\n<Document>: {d}{suffix}"
                         for d in docs[i:i + batch]]
                b = tok(texts, padding=True, truncation=True, max_length=max_length,
                        return_tensors="pt").to(dev)
                with torch.no_grad():
                    logits = model(**b).logits[:, -1, :]
                pair = torch.stack([logits[:, no], logits[:, yes]], dim=1).float()
                out.append(torch.log_softmax(pair, dim=1)[:, 1].exp().cpu().numpy())
            return np.concatenate(out) if out else np.zeros(0)
        return score

    raise ValueError(f"unknown reranker {name!r}")
