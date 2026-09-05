"""Layer 4a: phoneme sequence -> words (TRD section 6.1).

CMU Pronouncing Dictionary alignment with dynamic programming +
Levenshtein edit distance, top-k candidate generation, beam search
composition. A tiny built-in dictionary subset keeps the module
testable offline; :func:`load_cmu_dict` swaps in the real
cmudict-0.7b for production.

Phonemes are ARPAbet, stress-stripped (AA vs AA1 both -> "AA").
"""

from __future__ import annotations

import heapq
from functools import lru_cache
from typing import Dict, Iterable, List, Optional, Sequence, Tuple

from services.schemas import DecodedCandidate, strip_stress

Word = str
#: word -> tuple of stress-stripped phonemes
PhonemeIndex = Dict[Word, Tuple[str, ...]]


# ---------------------------------------------------------------------------
# Dictionary
# ---------------------------------------------------------------------------

#: Minimal built-in lexicon so the decoder is runnable + testable with
#: zero downloads. Production MUST call load_cmu_dict().
_MINI_LEXICON: Dict[Word, Tuple[str, ...]] = {
    "HELLO": ("HH", "AH", "L", "OW"),
    "AVC": ("EY", "V", "IY", "S", "IY"),
    "VOCAL": ("V", "OW", "K", "AH", "L"),
    "CARD": ("K", "AA", "R", "D"),
    "ARTIFICIAL": ("AA", "R", "T", "IH", "F", "IH", "SH", "AH", "L"),
    "TEST": ("T", "EH", "S", "T"),
    "IS": ("IH", "Z"),
    "WORKING": ("W", "ER", "K", "IH", "NG"),
    "YES": ("Y", "EH", "S"),
    "NO": ("N", "OW"),
    "WATER": ("W", "AO", "T", "ER"),
    "HELP": ("HH", "EH", "L", "P"),
    "ME": ("M", "IY"),
    "PLEASE": ("P", "L", "IY", "Z"),
    "THANK": ("TH", "AE", "NG", "K"),
    "YOU": ("Y", "UW"),
}


def load_cmu_dict(path: str) -> PhonemeIndex:
    """Parse cmudict-0.7b (comments with ;;;, entries 'WORD  PH1 PH2').

    Stress markers stripped; duplicate pronunciations (WORD(2)) all map.
    """
    idx: PhonemeIndex = {}
    with open(path, "r", encoding="latin-1") as fh:
        for line in fh:
            if line.startswith(";;;") or not line.strip():
                continue
            parts = line.strip().split()
            word, phons = parts[0], tuple(strip_stress(p) for p in parts[1:])
            base = word.rstrip("()0123456789")  # WORD(2) -> WORD
            idx.setdefault(base, phons)
    return idx


# ---------------------------------------------------------------------------
# Edit distance (Levenshtein over phoneme symbols)
# ---------------------------------------------------------------------------

@lru_cache(maxsize=200_000)
def _phoneme_edit_cached(a: Tuple[str, ...], b: Tuple[str, ...]) -> int:
    la, lb = len(a), len(b)
    if la == 0:
        return lb
    if lb == 0:
        return la
    prev = list(range(lb + 1))
    for i in range(1, la + 1):
        cur = [i] + [0] * lb
        for j in range(1, lb + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            cur[j] = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
        prev = cur
    return prev[lb]


def phoneme_edit_distance(a: Sequence[str], b: Sequence[str]) -> int:
    """Levenshtein distance between phoneme sequences (sub=del=ins=1)."""
    return _phoneme_edit_cached(tuple(a), tuple(b))


# ---------------------------------------------------------------------------
# Decoder
# ---------------------------------------------------------------------------

class PhonemeDecoder:
    """Segment + match a phoneme stream against a lexicon.

    Strategy per TRD 6.1:
    1. DP segmentation: for every start position, try every lexicon
       word whose pronunciation is within ``max_word_edits`` of the
       consumed span (greedy span growth capped by ``max_span``).
    2. Collect top-k word candidates per span (k = ``topk_words``).
    3. Beam search over segmentation paths (beam = ``beam_width``),
       keeping a few full-utterance hypotheses.
    """

    def __init__(self, lexicon: Optional[PhonemeIndex] = None,
                 max_span: int = 12, max_word_edits: int = 1,
                 topk_words: int = 5, beam_width: int = 3):
        self.lexicon: PhonemeIndex = dict(_MINI_LEXICON) if lexicon is None else dict(lexicon)
        self.max_span = max_span
        self.max_word_edits = max_word_edits
        self.topk_words = topk_words
        self.beam_width = beam_width

    # -- span matching -----------------------------------------------------

    def _word_candidates(self, span: Tuple[str, ...]) -> List[Tuple[Word, int]]:
        """(word, edits) for lexicon entries within max_word_edits of span,
        sorted by edits then alphabetically; at most topk_words."""
        scored: List[Tuple[int, str, int]] = []
        for word, phons in self.lexicon.items():
            d = _phoneme_edit_cached(span, phons)
            if d <= self.max_word_edits:
                scored.append((d, word, d))
        scored.sort(key=lambda t: (t[0], t[1]))
        return [(w, d) for d, w, _ in scored[: self.topk_words]]

    # -- beam search -------------------------------------------------------

    def decode(self, phonemes: Sequence[str]) -> List[DecodedCandidate]:
        """Return up to beam_width sentence hypotheses, best first."""
        seq = [strip_stress(p) for p in phonemes if p and p != "SIL"]
        n = len(seq)
        if n == 0:
            return [DecodedCandidate(text="", words=[], lm_logprob=0.0, edit_cost=0.0)]

        # beam state: (position, words, edit_cost) kept per start set
        beams: List[Tuple[int, List[Word], float]] = [(0, [], 0.0)]
        finished: List[Tuple[int, List[Word], float]] = []
        # bound memory: beam search is approximate by design (TRD beam=3);
        # keep a modest multiple to avoid starving the finished set.
        max_live = max(1, self.beam_width * 5)

        # frontier expansion: sort states by (edit_cost) asc, keep beam
        while beams:
            # pick the most promising state deterministically
            beams.sort(key=lambda s: (s[2], s[0]))
            beams = beams[:max_live]
            state = beams.pop(0)
            pos, words, cost = state
            if pos >= n:
                finished.append(state)
                if len(finished) >= self.beam_width:
                    break
                continue
            progressed = False
            for span_len in range(1, min(self.max_span, n - pos) + 1):
                span = tuple(seq[pos:pos + span_len])
                for word, edits in self._word_candidates(span):
                    beams.append((pos + span_len, words + [word], cost + edits))
                    progressed = True
            if not progressed:
                # dead end: skip this position as an unrecognized phoneme
                beams.append((pos + 1, words, cost + 1.0))

        # A path consuming everything always exists via dead-end skips,
        # but an all-skip path carries no words: surface the raw phoneme
        # stream instead of an empty hypothesis so callers can see what
        # was heard (and why nothing matched).
        finished.sort(key=lambda s: (s[2], -len(s[1])))
        if not finished or not finished[0][1]:
            return [DecodedCandidate(text=" ".join(seq), words=list(seq),
                                     lm_logprob=0.0, edit_cost=float(n))]

        finished.sort(key=lambda s: (s[2], -len(s[1])))
        out: List[DecodedCandidate] = []
        for pos, words, cost in finished[: self.beam_width]:
            out.append(DecodedCandidate(
                text=" ".join(words), words=words, lm_logprob=0.0, edit_cost=float(cost)))
        return out
