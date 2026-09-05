"""End-to-end pipeline orchestrator (Layers 2-4).

    packets/packet-file  ->  SensorPacket list
                        ->  PhonemeWindow list (segmentation)
                        ->  FeatureSet (13 descriptors)
                        ->  List[Prediction] (phoneme classifier)
                        ->  List[DecodedCandidate] (CMU decode + beam)
                        ->  text (LM re-rank)
                        ->  WAV bytes (TTS)

Design principle: every stage is swappable via the module factories
(``get_classifier``, ``get_tts``), and segmentation is an explicit,
testable function rather than hidden inside ingestion (a TRD gap —
segmentation was unspecified).
"""

from __future__ import annotations

import time
from dataclasses import dataclass, field
from typing import List, Optional, Sequence

import numpy as np

from services.decoder import PhonemeDecoder
from services.features import features_from_windows
from services.inference import PhonemeClassifier, get_classifier
from services.ingest import parse_packet
from services.lm_rerank import SentenceScorer, UniformScorer
from services.schemas import (
    FeatureSet, PhonemeWindow, Prediction, SensorPacket, DecodedCandidate, PipelineResult,
)
from services.tts import TextToSpeech, get_tts


# ---------------------------------------------------------------------------
# Segmentation (Layer 2 prerequisite — TRD unspecified; pinned here)
# ---------------------------------------------------------------------------

def segment_by_energy(
    mic: np.ndarray,
    rate_hz: int,
    min_ms: float = 40.0,
    threshold_db: float = -45.0,
) -> List[tuple]:
    """Energy-based voiced/unvoiced segmentation of the mic stream.

    Frames of 20 ms with 10 ms hop; a frame is voiced when its RMS (in
    dB relative to the session peak) exceeds ``threshold_db``. Adjacent
    voiced frames merge; runs shorter than ``min_ms`` are dropped.

    Returns [(start_ms, end_ms), ...] in time order.
    """
    if mic.size == 0:
        return []
    frame = max(1, int(rate_hz * 0.02))
    hop = max(1, int(rate_hz * 0.01))
    n_frames = max(1, (mic.size - frame) // hop + 1)
    rms = np.array([
        float(np.sqrt(np.mean(mic[i * hop:i * hop + frame].astype(np.float64) ** 2)))
        for i in range(n_frames)
    ])
    peak = float(rms.max()) if rms.size else 0.0
    if peak <= 0:
        return []
    thr = peak * (10 ** (threshold_db / 20.0))
    voiced = rms > thr

    spans = []
    start = None
    for i, v in enumerate(voiced):
        if v and start is None:
            start = i
        elif not v and start is not None:
            spans.append((start * hop * 1000.0 / rate_hz, i * hop * 1000.0 / rate_hz))
            start = None
    if start is not None:
        spans.append((start * hop * 1000.0 / rate_hz, n_frames * hop * 1000.0 / rate_hz))
    return [(s, e) for s, e in spans if e - s >= min_ms]


def windows_from_packets(
    packets: Sequence[SensorPacket],
    min_ms: float = 40.0,
) -> List[PhonemeWindow]:
    """Build PhonemeWindows from packets using energy segmentation.

    All streams are sliced by the same [start_ms, end_ms) window, with
    per-sensor rate conversion. Windows are tagged with the packet's
    timeline (single packet = single 500 ms acquisition window).
    """
    out: List[PhonemeWindow] = []
    for pkt in packets:
        mic_stream = pkt.stream("mic")
        if mic_stream is None or mic_stream.samples.size == 0:
            continue
        mic_rate = mic_stream.rate_hz
        spans = segment_by_energy(mic_stream.samples, mic_rate, min_ms=min_ms)
        get = lambda name: pkt.stream(name)  # noqa: E731
        for s_ms, e_ms in spans:
            sl = lambda arr, rate: arr[int(s_ms * rate / 1000):int(e_ms * rate / 1000)]  # noqa: E731
            out.append(PhonemeWindow(
                start_ms=s_ms, end_ms=e_ms,
                mic=sl(mic_stream.samples, mic_rate),
                piezo=sl(get("piezo").samples, get("piezo").rate_hz) if get("piezo") else np.zeros(0, np.float32),
                pressure=sl(get("pressure").samples, get("pressure").rate_hz) if get("pressure") else np.zeros(0, np.float32),
                airflow=sl(get("airflow").samples, get("airflow").rate_hz) if get("airflow") else np.zeros(0, np.float32),
            ))
    return out


# ---------------------------------------------------------------------------
# Orchestrator
# ---------------------------------------------------------------------------

@dataclass
class PipelineConfig:
    classifier: str = "auto"          # "auto" | "onnx" | "demo"
    tts: str = "auto"                 # "auto" | "silent" | "onnx"
    lexicon_path: Optional[str] = None   # cmudict-0.7b path; None = mini
    min_phoneme_ms: float = 40.0
    beam_width: int = 3
    topk_words: int = 5


def run_pipeline(
    packets: Sequence[SensorPacket],
    labels: Optional[List[Optional[str]]] = None,
    config: Optional[PipelineConfig] = None,
    classifier: Optional[PhonemeClassifier] = None,
    scorer: Optional[SentenceScorer] = None,
    tts: Optional[TextToSpeech] = None,
    out_wav: Optional[str] = None,
) -> PipelineResult:
    """Run layers 2-4 for a list of packets. Returns a PipelineResult.

    ``labels`` (optional) ground-truth phonemes per window for eval.
    """
    cfg = config or PipelineConfig()
    stage_ms = {}

    t0 = time.perf_counter()
    windows = windows_from_packets(packets, min_ms=cfg.min_phoneme_ms)
    stage_ms["segment"] = (time.perf_counter() - t0) * 1000

    t0 = time.perf_counter()
    feats: FeatureSet = features_from_windows(windows, labels=labels)
    stage_ms["features"] = (time.perf_counter() - t0) * 1000

    if classifier is None:
        classifier = get_classifier(None if cfg.classifier == "auto" else cfg.classifier)
    t0 = time.perf_counter()
    preds: List[Prediction] = classifier.predict(feats)
    stage_ms["inference"] = (time.perf_counter() - t0) * 1000

    decoder = PhonemeDecoder(beam_width=cfg.beam_width, topk_words=cfg.topk_words)
    t0 = time.perf_counter()
    phonemes = [p.phoneme for p in preds]
    candidates: List[DecodedCandidate] = decoder.decode(phonemes)
    stage_ms["decode"] = (time.perf_counter() - t0) * 1000

    if scorer is None:
        scorer = UniformScorer()
    t0 = time.perf_counter()
    ranked = scorer.rerank(candidates)
    stage_ms["rerank"] = (time.perf_counter() - t0) * 1000

    text = ranked[0].text if ranked else ""
    wav_path = None
    if tts is None and cfg.tts != "none":
        try:
            tts = get_tts(None if cfg.tts == "auto" else cfg.tts)
        except FileNotFoundError:
            tts = None  # no bundle yet; skip synthesis
    if tts is not None:
        t0 = time.perf_counter()
        wav = tts.synthesize(text)
        stage_ms["tts"] = (time.perf_counter() - t0) * 1000
        if out_wav:
            with open(out_wav, "wb") as fh:
                fh.write(wav)
            wav_path = out_wav

    return PipelineResult(
        phonemes=phonemes, predictions=preds, candidates=ranked,
        text=text, wav_path=wav_path, stage_ms=stage_ms)


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> None:  # pragma: no cover - thin shell
    import argparse
    import os
    import sys

    ap = argparse.ArgumentParser(
        prog="avc",
        description="AVC pipeline: sensor packets -> text (+ optional WAV)")
    ap.add_argument("packet_file", help="file with length-prefixed packets, or '-' for stdin (hex)")
    ap.add_argument("--wav", default=None, help="write synthesized audio here")
    ap.add_argument("--backend", default="auto", choices=["auto", "demo", "onnx"],
                    help="phoneme classifier backend")
    ap.add_argument("--tts", default="auto", choices=["auto", "silent", "onnx"])
    ap.add_argument("--cmudict", default=None, help="path to cmudict-0.7b")
    ap.add_argument("--report", action="store_true", help="print stage timing report")
    args = ap.parse_args()

    # packet file format: one packet per line, hex-encoded, '#' comments
    raw = sys.stdin.read() if args.packet_file == "-" else open(args.packet_file, "r").read()
    packets = []
    for line in raw.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        packets.append(parse_packet(bytes.fromhex(line)))

    if not packets:
        print("no packets parsed", file=sys.stderr)
        sys.exit(1)

    config = PipelineConfig(classifier=args.backend, tts=args.tts)
    if args.cmudict:
        config.lexicon_path = args.cmudict
    result = run_pipeline(packets, config=config, out_wav=args.wav)

    print("phonemes:", " ".join(result.phonemes))
    print("text    :", result.text or "(none)")
    if args.report:
        for k, v in result.stage_ms.items():
            print(f"  {k:10s} {v:8.2f} ms")
        print(f"  {'e2e':10s} {result.e2e_ms:8.2f} ms")
    if result.wav_path:
        print("wav     :", result.wav_path)


if __name__ == "__main__":
    _cli()
