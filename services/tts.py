"""Layer 4c: text-to-speech — SWAPPABLE MODULE (TRD 6.3, revised).

Interface: phonemes/words -> 16-bit PCM WAV @ 22.05 kHz (TRD output
spec). Speaker personalization via a 256-dim d-vector/x-vector
extracted from ~5 min of pre-loss audio (TRD "Speaker Embedding").

Implementations shipped:
- :class:`SilentTTS`  — returns silence; default zero-dep baseline.
- :class:`VocoderTTS` — FastSpeech2/HiFi-GAN via a local ONNX bundle
                        (optional onnxruntime dep, exactly like the
                        classifier backend).

No cloud TTS API calls anywhere — offline-first is a product
requirement (see OFFLINE_CAPABILITY_ANALYSIS.md).
"""

from __future__ import annotations

import io
import os
import struct
import wave
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional, Sequence

SAMPLE_RATE = 22_050   # TRD 6.3
SAMPLE_WIDTH = 2       # 16-bit


@dataclass
class SpeakerEmbedding:
    """256-dim speaker identity vector (d-vector / x-vector)."""
    vector: Sequence[float]

    def __post_init__(self):
        if len(self.vector) != 256:
            raise ValueError(f"speaker embedding must be 256-dim, got {len(self.vector)}")


class TextToSpeech(ABC):
    """Contract: text (and optional speaker) -> WAV bytes."""

    @abstractmethod
    def synthesize(self, text: str, speaker: Optional[SpeakerEmbedding] = None) -> bytes:
        """Return 16-bit PCM WAV file content."""

    def synthesize_to_file(self, text: str, path: str,
                           speaker: Optional[SpeakerEmbedding] = None) -> str:
        wav = self.synthesize(text, speaker)
        with open(path, "wb") as fh:
            fh.write(wav)
        return path


def _wav_bytes(pcm: bytearray) -> bytes:
    """Wrap raw 16-bit LE PCM in a WAV container."""
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(SAMPLE_WIDTH)
        w.setframerate(SAMPLE_RATE)
        w.writeframes(bytes(pcm))
    return buf.getvalue()


class SilentTTS(TextToSpeech):
    """Zero-dependency baseline: fixed 300 ms of silence.

    Keeps the end-to-end pipeline testable (latency, file plumbing,
    WAV format correctness) without any neural TTS.
    """

    DURATION_S = 0.3

    def synthesize(self, text: str, speaker: Optional[SpeakerEmbedding] = None) -> bytes:
        n = int(SAMPLE_RATE * self.DURATION_S)
        pcm = bytearray(n * SAMPLE_WIDTH)
        return _wav_bytes(pcm)


class VocoderTTS(TextToSpeech):
    """FastSpeech2 (acoustic model) + HiFi-GAN (vocoder) via ONNX.

    Expected bundle layout (produce with services/export_tts.py later):
        models/tts/fastspeech2.onnx     inputs: phoneme ids (int64, (N,)),
                                        speaker embedding (float32, (256,))
        models/tts/hifigan.onnx          inputs: mel (float32, (80, T'))
    """

    def __init__(self, acoustic_path: str = "models/tts/fastspeech2.onnx",
                 vocoder_path: str = "models/tts/hifigan.onnx"):
        import onnxruntime as ort  # lazy optional dep
        if not (os.path.isfile(acoustic_path) and os.path.isfile(vocoder_path)):
            raise FileNotFoundError(
                f"TTS ONNX bundle not found ({acoustic_path}, {vocoder_path}). "
                "Use SilentTTS until the TTS milestone lands.")
        self.am = ort.InferenceSession(acoustic_path, providers=["CPUExecutionProvider"])
        self.vc = ort.InferenceSession(vocoder_path, providers=["CPUExecutionProvider"])
        self._am_in = [i.name for i in self.am.get_inputs()]
        self._vc_in = [i.name for i in self.vc.get_inputs()]

    def synthesize(self, text: str, speaker: Optional[SpeakerEmbedding] = None) -> bytes:
        import numpy as np
        ids = self._phoneme_ids(text)
        spk = np.zeros(256, np.float32) if speaker is None else np.asarray(speaker.vector, np.float32)
        feeds = {self._am_in[0]: ids.astype(np.int64)}
        if len(self._am_in) > 1:
            feeds[self._am_in[1]] = spk
        mel = self.am.run(None, feeds)[0]
        pcm = self.vc.run(None, {self._vc_in[0]: mel.astype(np.float32)})[0]
        pcm = (np.clip(pcm, -1.0, 1.0) * 32767.0).astype("<i2").tobytes()
        return _wav_bytes(bytearray(pcm))

    @staticmethod
    def _phoneme_ids(text: str) -> "np.ndarray":
        """G2P is a later milestone; placeholder maps A-Z to 1-26."""
        import numpy as np
        return np.array([max(1, ord(c) - 64) for c in text.upper() if c.isalpha()], dtype=np.int64)


def get_tts(name: Optional[str] = None) -> TextToSpeech:
    """Factory: 'silent' (default) or 'onnx' when the bundle exists."""
    if name is None:
        name = "onnx" if (os.path.isfile("models/tts/fastspeech2.onnx")
                          and os.path.isfile("models/tts/hifigan.onnx")) else "silent"
    if name == "silent":
        return SilentTTS()
    if name == "onnx":
        return VocoderTTS()
    raise ValueError(f"unknown TTS backend: {name!r} (use 'silent' or 'onnx')")
