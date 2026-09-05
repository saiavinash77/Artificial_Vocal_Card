"""Layer 2: 13-descriptor feature engineering (TRD section 4.2).

Computes the canonical DESCRIPTOR_NAMES vector from segmented
PhonemeWindow data. Formulas follow the TRD exactly:

    1.  RMS          sqrt(mean(x^2)) over phoneme window
    2.  SPL          20*log10(RMS / P_REF)           (floored at SPL_FLOOR_DB)
    3.  Pressure     mean differential pressure       [Pa]
    4.  Velocity     mean thermal airflow velocity    [m/s]
    5.  Duration     end_ms - start_ms               [ms]
    6.  EnergyRatio  RMS / (mu_RMS + eps)             mu over the dialogue
    7.  DurationNorm Duration / (mu_Dur + eps)        mu over the dialogue
    8.  SPL/Vel      SPL / (Velocity + eps)
    9.  Pres/RMS     Pressure / (RMS + eps)
    10. dRMS         RMS_t - RMS_{t-1}
    11. dSPL         SPL_t - SPL_{t-1}
    12. dPressure    Pressure_t - Pressure_{t-1}
    13. PhonemeClass categorical id (PHONEME_CLASSES; "unknown" when the
        label is not available at prediction time)

Design notes (gaps addressed, see IMPLEMENTATION_PLAN.md):
- Zero/empty windows are guarded (EPS, SPL floor) so no NaN ever enters
  the model.
- Dialogue-level means (descriptors 6-7) are computed contextually
  across the utterance, in a single pass in `features_from_windows`.
- Deltas (10-12) for the FIRST phoneme default to 0.0 (no previous),
  not NaN — an explicit, tested convention.
"""

from __future__ import annotations

import math
from typing import List, Optional

import numpy as np

from services.schemas import (
    DESCRIPTOR_NAMES,
    EPS,
    P_REF,
    SPL_FLOOR_DB,
    FeatureRow,
    FeatureSet,
    PhonemeWindow,
    phoneme_class_of,
)

IDX = {name: i for i, name in enumerate(DESCRIPTOR_NAMES)}


def _rms(x: np.ndarray) -> float:
    if x.size == 0:
        return 0.0
    return float(math.sqrt(float(np.mean(np.square(x.astype(np.float64))))))


def _spl(rms: float) -> float:
    if rms <= 0.0:
        return SPL_FLOOR_DB
    return 20.0 * math.log10(rms / P_REF)


def base_descriptors(window: PhonemeWindow) -> np.ndarray:
    """Descriptors 1-9 for one window (deltas + class added later)."""
    v = np.zeros(len(DESCRIPTOR_NAMES), dtype=np.float64)
    mic = window.mic if window.mic is not None else np.zeros(0, np.float32)
    pres = window.pressure if window.pressure is not None else np.zeros(0, np.float32)
    flow = window.airflow if window.airflow is not None else np.zeros(0, np.float32)

    rms = _rms(mic)
    spl = _spl(rms)
    pressure = float(np.mean(pres)) if pres.size else 0.0
    velocity = float(np.mean(flow)) if flow.size else 0.0

    v[IDX["rms_amplitude"]] = rms
    v[IDX["spl_db"]] = spl
    v[IDX["pressure_pa"]] = pressure
    v[IDX["velocity_ms"]] = velocity
    v[IDX["duration_ms"]] = max(0.0, window.duration_ms)
    v[IDX["spl_vel_ratio"]] = spl / (abs(velocity) + EPS)
    v[IDX["pressure_rms_ratio"]] = pressure / (rms + EPS)
    return v


def features_from_windows(
    windows: List[PhonemeWindow],
    labels: Optional[List[Optional[str]]] = None,
) -> FeatureSet:
    """Full 13-descriptor FeatureSet for one utterance (dialogue context).

    ``labels`` parallel to windows; None entries get class "unknown".
    """
    if labels is not None and len(labels) != len(windows):
        raise ValueError(f"labels length {len(labels)} != windows length {len(windows)}")

    n = len(windows)
    feats = FeatureSet()
    if n == 0:
        return feats

    bases = [base_descriptors(w) for w in windows]
    mu_rms = float(np.mean([b[IDX["rms_amplitude"]] for b in bases]))
    mu_dur = float(np.mean([b[IDX["duration_ms"]] for b in bases]))

    prev_rms: Optional[float] = None
    prev_spl: Optional[float] = None
    prev_pres: Optional[float] = None
    for i, (w, b) in enumerate(zip(windows, bases)):
        v = b.copy()
        rms = v[IDX["rms_amplitude"]]
        spl = v[IDX["spl_db"]]
        pres = v[IDX["pressure_pa"]]

        v[IDX["energy_ratio"]] = rms / (mu_rms + EPS)
        v[IDX["duration_norm"]] = v[IDX["duration_ms"]] / (mu_dur + EPS)
        # Pinned convention: the FIRST window has no predecessor, so its
        # deltas are 0.0 — NOT (value - 0), which would inject a spurious
        # spike proportional to the signal itself.
        v[IDX["delta_rms"]] = 0.0 if prev_rms is None else rms - prev_rms
        v[IDX["delta_spl"]] = 0.0 if prev_spl is None else spl - prev_spl
        v[IDX["delta_pressure"]] = 0.0 if prev_pres is None else pres - prev_pres

        label = labels[i] if labels is not None else None
        if label:
            v[IDX["phoneme_class"]] = float(phoneme_class_of(str(label)))
        else:
            v[IDX["phoneme_class"]] = 0.0  # "unknown"

        feats.rows.append(FeatureRow(vector=v.astype(np.float32), phoneme=label, window=w))
        prev_rms, prev_spl, prev_pres = rms, spl, pres

    return feats


def normalize_zscore(matrix: np.ndarray, mean: Optional[np.ndarray] = None,
                     std: Optional[np.ndarray] = None):
    """Per-dimension z-score (TRD preprocessing). Returns (norm, mean, std).

    Constants are computed from the data when not supplied, and returned
    so training-time stats can be applied consistently at inference.
    """
    m = np.atleast_2d(np.asarray(matrix, dtype=np.float64))
    if mean is None:
        mean = m.mean(axis=0)
    if std is None:
        std = m.std(axis=0)
    std_safe = np.where(std < EPS, 1.0, std)
    return (m - mean) / std_safe, mean, std_safe
