"""AVC (Artificial Vocal Card) — cloud-side software pipeline.

Layer 2 (Feature Engineering), Layer 3 (Inference) and Layer 4
(Language & Synthesis) of the AVC architecture, as a pure-Python/NumPy
reference implementation that runs with zero heavy dependencies.

Layer 1 (ESP32-S3 sensor acquisition) lives in ``firmware/`` and speaks
the packet format parsed by :mod:`services.ingest`.

Spec source: AVC_TRD_Technical_Requirements_Document (sections 4-6).
"""

__version__ = "0.1.0"

from services.schemas import (  # noqa: F401
    DESCRIPTOR_NAMES,
    PHONEME_CLASSES,
    SensorPacket,
    PhonemeWindow,
    FeatureRow,
    FeatureSet,
    Prediction,
    PipelineResult,
)
