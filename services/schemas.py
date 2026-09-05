"""Shared data schemas and canonical constants for the AVC pipeline.

The 13-descriptor feature vector and its ordering are LOCKED here
(TRD section 4.2). Every downstream component (features, inference,
training exports, firmware parity tests) must reference these names —
never hardcode the list elsewhere.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence

import numpy as np

# ---------------------------------------------------------------------------
# Canonical constants
# ---------------------------------------------------------------------------

#: Order is part of the wire/model contract. (B, T, 13) input to the
#: CNN-BiLSTM-Attention model uses exactly this ordering.
DESCRIPTOR_NAMES: List[str] = [
    "rms_amplitude",       # 1. sqrt(mean(x^2)) over phoneme window
    "spl_db",              # 2. 20*log10(RMS / P_REF)
    "pressure_pa",         # 3. mean differential pressure, Pa
    "velocity_ms",         # 4. mean thermal airflow velocity, m/s
    "duration_ms",        # 5. end - start
    "energy_ratio",        # 6. RMS / (mu_RMS + eps)      (dialogue-normalized)
    "duration_norm",       # 7. Duration / (mu_Dur + eps) (dialogue-normalized)
    "spl_vel_ratio",       # 8. SPL / (Velocity + eps)
    "pressure_rms_ratio",  # 9. Pressure / (RMS + eps)
    "delta_rms",           # 10. RMS_t - RMS_{t-1}
    "delta_spl",           # 11. SPL_t - SPL_{t-1}
    "delta_pressure",      # 12. Pressure_t - Pressure_{t-1}
    "phoneme_class",       # 13. categorical encoding (see PHONEME_CLASSES)
]

N_DESCRIPTORS = len(DESCRIPTOR_NAMES)

#: Reference pressure for SPL (20 uPa, standard acoustic reference).
#: NOTE: absolute SPL requires a calibrated mic; with an uncalibrated
#: ADC stream this yields dB relative to P_REF, not physical SPL.
P_REF = 2e-5

#: Numerical guard used in every ratio formula (TRD: "+ epsilon").
EPS = 1e-9

#: Floor for SPL when a window is silent (avoid log10(0)).
SPL_FLOOR_DB = -120.0

#: Phoneme-class encoding (TRD descriptor 13). 0 is reserved for
#: "unknown" so the value can be produced at inference time (see
#: IMPLEMENTATION_PLAN.md — "phoneme-class circular input" gap).
PHONEME_CLASSES: Dict[str, int] = {
    "unknown": 0,
    "vowel": 1,
    "stop": 2,
    "fricative": 3,
    "nasal": 4,
    "liquid": 5,
    "glide": 6,
    "affricate": 7,
}

_ARPABET_CLASS: Dict[str, str] = {
    **{p: "vowel" for p in (
        "AA AE AH AO AW AY EH ER EY IH IY OW OY UH UW".split())},
    **{p: "stop" for p in "P T K B D G".split()},
    **{p: "fricative" for p in "F V TH DH S Z SH ZH HH".split()},
    **{p: "nasal" for p in "M N NG".split()},
    **{p: "liquid" for p in "L R".split()},
    **{p: "glide" for p in "W Y".split()},
    **{p: "affricate" for p in "CH JH".split()},
}


def class_name_of(phoneme: str) -> str:
    """Phoneme-class NAME for an ARPAbet symbol ('AA1' -> 'vowel')."""
    return _ARPABET_CLASS.get(strip_stress(phoneme).upper(), "unknown")


def phoneme_class_of(phoneme: str) -> int:
    """Categorical class id for an ARPAbet phoneme (stress-stripped)."""
    return PHONEME_CLASSES.get(class_name_of(phoneme), 0)


def strip_stress(phoneme: str) -> str:
    """'AA1' -> 'AA', 'T' -> 'T' (drops CMU stress digits 0/1/2)."""
    return phoneme.strip().rstrip("012")


#: Classifier output vocabulary: the 39 canonical ARPAbet symbols
#: (incl. HH) plus SIL, so C = 40 output classes. Order is part of the
#: model contract (ONNX head + firmware TFLite must match exactly).
PHONEME_VOCAB: List[str] = [
    "AA", "AE", "AH", "AO", "AW", "AY", "EH", "ER", "EY", "IH", "IY",
    "OW", "OY", "UH", "UW",
    "P", "T", "K", "B", "D", "G",
    "F", "V", "TH", "DH", "S", "Z", "SH", "ZH", "HH",
    "M", "N", "NG", "L", "R", "W", "Y", "CH", "JH",
    "SIL",
]

#: symbol -> vocab index (the inverse is materialized where needed).
PHONEME_TO_ID: Dict[str, int] = {p: i for i, p in enumerate(PHONEME_VOCAB)}


# ---------------------------------------------------------------------------
# Data objects
# ---------------------------------------------------------------------------

@dataclass
class SensorStream:
    """One sensor's contiguous samples over a packet window."""
    name: str                      # "mic" | "piezo" | "pressure" | "airflow"
    rate_hz: int
    samples: np.ndarray            # float32 view of the raw samples


@dataclass
class SensorPacket:
    """Parsed Layer-1 packet (TRD section 4.1)."""
    seq_no: int
    timestamp_ms: int
    sensor_mask: int
    streams: Dict[str, SensorStream] = field(default_factory=dict)

    def stream(self, name: str) -> Optional[SensorStream]:
        return self.streams.get(name)


@dataclass
class PhonemeWindow:
    """One segmented phoneme instance with aligned per-sensor slices."""
    start_ms: float
    end_ms: float
    mic: np.ndarray                # float32 mic samples in window
    piezo: np.ndarray
    pressure: np.ndarray
    airflow: np.ndarray

    @property
    def duration_ms(self) -> float:
        return self.end_ms - self.start_ms


@dataclass
class FeatureRow:
    """The 13-descriptor vector for one phoneme window (+ provenance)."""
    vector: np.ndarray             # shape (13,), order == DESCRIPTOR_NAMES
    phoneme: Optional[str] = None  # ground-truth label if known
    window: Optional[PhonemeWindow] = None

    def as_dict(self) -> Dict[str, float]:
        return dict(zip(DESCRIPTOR_NAMES, (float(v) for v in self.vector)))


@dataclass
class FeatureSet:
    """All feature rows for one utterance, in time order."""
    rows: List[FeatureRow] = field(default_factory=list)

    def matrix(self) -> np.ndarray:
        """(T, 13) matrix in canonical descriptor order."""
        if not self.rows:
            return np.zeros((0, N_DESCRIPTORS), dtype=np.float32)
        return np.vstack([r.vector for r in self.rows]).astype(np.float32)

    def phonemes(self) -> List[Optional[str]]:
        return [r.phoneme for r in self.rows]


@dataclass
class Prediction:
    """Classifier output for one phoneme window."""
    phoneme: str                   # ARPAbet symbol, stress-stripped
    class_name: str                # one of PHONEME_CLASSES keys
    confidence: float              # [0, 1]

    def __post_init__(self) -> None:
        if not 0.0 <= self.confidence <= 1.0:
            raise ValueError(f"confidence out of range: {self.confidence}")


@dataclass
class DecodedCandidate:
    """One sentence hypothesis from the decoder + LM re-rank."""
    text: str
    words: List[str]
    lm_logprob: float
    edit_cost: float               # total phoneme edit distance paid

    @property
    def score(self) -> float:
        return self.lm_logprob - self.edit_cost


@dataclass
class PipelineResult:
    """End-to-end result for one utterance."""
    phonemes: List[str]
    predictions: List[Prediction]
    candidates: List[DecodedCandidate]
    text: Optional[str] = None
    wav_path: Optional[str] = None
    stage_ms: Dict[str, float] = field(default_factory=dict)

    @property
    def e2e_ms(self) -> float:
        return float(sum(self.stage_ms.values()))
