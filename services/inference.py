"""Layer 3: phoneme inference — swappable backends behind one interface.

The TRD model (CNN-BiLSTM-Attention, ~5.5M params, PyTorch 2.x trained,
ONNX Runtime served) is loaded via :class:`ONNXClassifier` once weights
exist. Until then, :class:`DemoHeuristicClassifier` keeps the pipeline
runnable end-to-end for plumbing tests — it is a deterministic
placeholder, NOT a trained model, and must never be used for accuracy
evaluation.

Model topology for training lives in :mod:`services.model_torch`
(optional PyTorch dependency — never imported by the runtime pipeline).
"""

from __future__ import annotations

import os
from abc import ABC, abstractmethod
from typing import List, Optional

import numpy as np

from services.schemas import (
    FeatureSet, PHONEME_TO_ID, PHONEME_VOCAB, Prediction, class_name_of,
)

#: Default ONNX weights location (override with AVC_MODEL_PATH).
DEFAULT_MODEL_PATH = os.environ.get("AVC_MODEL_PATH", "models/phoneme_cnn_bilstm_attn.onnx")

#: ARPAbet output classes (stress-stripped, incl. HH) + SIL — C = 40.
#: Canonical order lives in services/schemas.py (PHONEME_VOCAB).
_ID2PHONEME = {i: p for i, p in enumerate(PHONEME_VOCAB)}


class PhonemeClassifier(ABC):
    """Contract every inference backend must honor."""

    @abstractmethod
    def predict(self, features: FeatureSet) -> List[Prediction]:
        """One Prediction per feature row, in time order."""

    @property
    def backend_name(self) -> str:
        return type(self).__name__


class ONNXClassifier(PhonemeClassifier):
    """Serves the TRD CNN-BiLSTM-Attention model via onnxruntime.

    Input  float32 (1, T, 13) — z-scored with the SAME constants used
    at training time (supply ``mean``/``std`` or a .npz stats file).
    Output float32 (1, T, C) logits/softmax over PHONEME_VOCAB.
    """

    def __init__(self, model_path: str = DEFAULT_MODEL_PATH,
                 mean: Optional[np.ndarray] = None, std: Optional[np.ndarray] = None,
                 stats_path: Optional[str] = None):
        import onnxruntime as ort  # lazy: optional dependency
        if not os.path.isfile(model_path):
            raise FileNotFoundError(
                f"ONNX model not found at {model_path!r}. Train via services/train.py "
                "or set AVC_MODEL_PATH.")
        if stats_path and (mean is None or std is None):
            stats = np.load(stats_path)
            mean, std = stats["mean"], stats["std"]
        self._session = ort.InferenceSession(model_path, providers=["CPUExecutionProvider"])
        self._input_name = self._session.get_inputs()[0].name
        self._mean = None if mean is None else np.asarray(mean, np.float32)
        self._std = None if std is None else np.asarray(std, np.float32)

    def predict(self, features: FeatureSet) -> List[Prediction]:
        matrix = features.matrix()          # (T, 13) raw descriptors
        if matrix.shape[0] == 0:
            return []
        if self._mean is not None:
            matrix = (matrix - self._mean) / self._std
        probs = self._session.run(
            None, {self._input_name: matrix[np.newaxis, ...].astype(np.float32)})[0]
        # probs: (1, T, C); if model emits log-softmax, normalize anyway
        probs = probs[0]
        if np.any(probs < 0) or abs(probs.sum(-1)[0] - 1.0) > 1e-3:
            probs = np.exp(probs - probs.max(-1, keepdims=True))
            probs = probs / probs.sum(-1, keepdims=True)
        out: List[Prediction] = []
        for row in probs:
            idx = int(np.argmax(row))
            phon = _ID2PHONEME.get(idx, "SIL")
            out.append(Prediction(phoneme=phon, class_name=class_name_of(phon),
                                   confidence=float(row[idx])))
        return out


class DemoHeuristicClassifier(PhonemeClassifier):
    """Deterministic placeholder for pipeline plumbing ONLY.

    Maps the phoneme-class descriptor (index 12) + energy cues to a
    representative phoneme per class. Accuracy is meaningless by
    construction — its job is to make the end-to-end path testable
    before real weights exist.
    """

    _REPRESENTATIVE = {
        0: "AA",   # unknown -> neutral vowel
        1: "AA",   # vowel
        2: "T",    # stop
        3: "S",     # fricative
        4: "N",     # nasal
        5: "L",     # liquid
        6: "W",     # glide
        7: "CH",    # affricate
    }
    _CLASS_NAME = {
        0: "unknown", 1: "vowel", 2: "stop", 3: "fricative",
        4: "nasal", 5: "liquid", 6: "glide", 7: "affricate",
    }

    def predict(self, features: FeatureSet) -> List[Prediction]:
        out: List[Prediction] = []
        for row in features.rows:
            v = row.vector
            cls_id = int(round(float(v[12])))
            cls_id = min(max(cls_id, 0), 7)
            # confidence grows with normalized energy, clipped to (0,1)
            energy = float(v[5])  # energy_ratio
            conf = float(np.clip(1.0 / (1.0 + np.exp(-(energy - 1.0))), 0.01, 0.99))
            out.append(Prediction(
                phoneme=self._REPRESENTATIVE[cls_id],
                class_name=self._CLASS_NAME[cls_id],
                confidence=conf))
        return out


def get_classifier(name: Optional[str] = None) -> PhonemeClassifier:
    """Factory. ``name`` in {"onnx", "demo"}; default: onnx if a model
    file is reachable, else demo."""
    if name is None:
        name = "onnx" if os.path.isfile(DEFAULT_MODEL_PATH) else "demo"
    if name == "onnx":
        return ONNXClassifier()
    if name == "demo":
        return DemoHeuristicClassifier()
    raise ValueError(f"unknown classifier backend: {name!r} (use 'onnx' or 'demo')")
