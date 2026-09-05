"""Layer 4b: language-model re-ranking — SWAPPABLE MODULE (TRD 6.2, revised).

DECISION LOCKED (2026-09, project owner): GPT-2 is dropped entirely —
licensing ambiguity + inference cost. This module defines the
:class:`SentenceScorer` interface and ships three implementations:

- :class:`UniformScorer`      — no-op baseline (always 0.0); default so
                                the pipeline runs with zero deps.
- :class:`NgramScorer`        — tiny built-in bigram scorer over a
                                supplied word-frequency table; offline,
                                deterministic, patent/IP-clean.
- :class:`HuggingFaceScorer`   — any causal-LM (e.g. DistilGPT-2 or an
                                Indic LM like AI4Bharat IndicBERT) via
                                transformers; optional dependency.

The production plan (IMPLEMENTATION_PLAN.md) picks the concrete model
after the language(s) of the target population are pinned.
"""

from __future__ import annotations

import math
from abc import ABC, abstractmethod
from typing import Iterable, List, Optional, Sequence

from services.schemas import DecodedCandidate


class SentenceScorer(ABC):
    """log P(sentence) — any monotone score works; higher is better."""

    @abstractmethod
    def logprob(self, words: Sequence[str]) -> float:
        ...

    def rerank(self, candidates: List[DecodedCandidate]) -> List[DecodedCandidate]:
        """Return candidates sorted best-first by (lm_logprob - edit_cost)."""
        for c in candidates:
            c.lm_logprob = self.logprob(c.words)
        return sorted(candidates, key=lambda c: c.score, reverse=True)


class UniformScorer(SentenceScorer):
    """Baseline no-op scorer (default). Keeps decoder order stable."""

    def logprob(self, words: Sequence[str]) -> float:
        return 0.0


class NgramScorer(SentenceScorer):
    """Simple add-lambda bigram scorer from a word list.

    ``wordlist``: iterable of whitespace-tokenized sentences used to
    build unigram + bigram counts. Deterministic, tiny, offline — good
    enough to prefer "HELLO WORLD" over "HELLO WO RLD"-style junk while
    a real LM is being selected.
    """

    def __init__(self, wordlist: Iterable[str], smoothing: float = 1.0):
        self.uni: dict = {}
        self.bi: dict = {}
        self.total = 0
        self.lam = smoothing
        for line in wordlist:
            toks = ["<s>"] + line.upper().split() + ["</s>"]
            for a, b in zip(toks, toks[1:]):
                self.uni[a] = self.uni.get(a, 0) + 1
                self.uni[b] = self.uni.get(b, 0) + 1
                self.bi[(a, b)] = self.bi.get((a, b), 0) + 1
                self.total += 1
        self.vocab = len(self.uni) or 1

    def logprob(self, words: Sequence[str]) -> float:
        toks = ["<s>"] + [w.upper() for w in words] + ["</s>"]
        lp = 0.0
        for a, b in zip(toks, toks[1:]):
            p = (self.bi.get((a, b), 0) + self.lam) / (self.uni.get(a, 0) + self.lam * self.vocab)
            lp += math.log(p)
        return lp / max(1, len(toks) - 1)  # length-normalized


class HuggingFaceScorer(SentenceScorer):
    """Any causal LM via transformers (optional dep). Score = -mean
    token NLL of the sentence, i.e. TRD "Score = -Loss_LM".

    DistilGPT-2 and Mistral-7B both work; pick per deployment budget.
    """

    def __init__(self, model_name: str = "distilgpt2"):
        from transformers import AutoModelForCausalLM, AutoTokenizer  # lazy
        self.tok = AutoTokenizer.from_pretrained(model_name)
        self.model = AutoModelForCausalLM.from_pretrained(model_name)
        self.model.eval()
        self._name = model_name

    def logprob(self, words: Sequence[str]) -> float:
        text = " ".join(words).lower()
        import torch
        enc = self.tok(text, return_tensors="pt")
        with torch.no_grad():
            out = self.model(**enc).logits
        logp = torch.log_softmax(out[:, :-1], dim=-1)
        tgt = enc.input_ids[:, 1:]
        tok_lp = logp.gather(2, tgt.unsqueeze(-1)).squeeze(-1)
        return -float(tok_lp.mean())  # negative mean NLL
