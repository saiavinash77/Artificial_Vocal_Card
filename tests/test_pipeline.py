"""Tests for the end-to-end pipeline (demo backend, silent TTS)."""

import os
import tempfile
import unittest

import numpy as np

from services.ingest import build_packet, parse_packet
from services.pipeline import (
    PipelineConfig, run_pipeline, segment_by_energy, windows_from_packets,
)
from services.tts import SilentTTS, SpeakerEmbedding, get_tts


def synth_stream(rate_hz, dur_ms, amp=0.5, freq_hz=200.0, seed=0):
    """Deterministic tone-ish stream: voiced bursts vs near-silence.

    Noise std 0.0005 stays below the -45 dB segmentation threshold
    (0.0056 x peak), so gaps are correctly treated as unvoiced.
    """
    rng = np.random.default_rng(seed)
    n = int(rate_hz * dur_ms / 1000)
    t = np.arange(n) / rate_hz
    sig = amp * np.sin(2 * np.pi * freq_hz * t) + 0.0005 * rng.normal(size=n)
    return sig.astype(np.float32)


def packet_with_bursts(seed=7, bursts=3, gap_ms=60, burst_ms=90):
    """One 16 kHz mic packet: `bursts` voiced spans separated by silence.

    Piezo 1 kHz, pressure/airflow 100 Hz constant-ish companions.
    """
    rate = 16_000
    parts = []
    for b in range(bursts):
        parts.append(synth_stream(rate, burst_ms, seed=seed + b))
        parts.append(np.zeros(int(rate * gap_ms / 1000), np.float32))
    mic = np.concatenate(parts)
    n_ms = mic.size / rate * 1000
    piezo = synth_stream(1_000, n_ms, amp=0.05, seed=seed + 100)
    pressure = np.full(int(100 * n_ms / 1000), 3.0, np.float32)
    airflow = np.full(int(100 * n_ms / 1000), 0.2, np.float32)
    streams = {"mic": mic, "piezo": piezo,
               "pressure": pressure, "airflow": airflow}
    return parse_packet(build_packet(1, 0, streams))


class TestSegmentation(unittest.TestCase):
    def test_burst_count(self):
        mic = packet_with_bursts().stream("mic").samples
        spans = segment_by_energy(mic, 16_000)
        self.assertEqual(len(spans), 3)
        for s, e in spans:
            self.assertGreaterEqual(e - s, 40.0)

    def test_silence_yields_nothing(self):
        self.assertEqual(segment_by_energy(np.zeros(1600, np.float32), 16_000), [])

    def test_min_ms_drops_short_clicks(self):
        # A 10 ms click must vanish (below the 40 ms floor).
        mic = np.zeros(1600, np.float32)
        mic[100:110] = 0.9
        self.assertEqual(segment_by_energy(mic, 16_000), [])


class TestWindowsFromPackets(unittest.TestCase):
    def test_windows_have_all_streams(self):
        pkt = packet_with_bursts()
        wins = windows_from_packets([pkt])
        self.assertEqual(len(wins), 3)
        for w in wins:
            self.assertEqual(w.mic.size > 0, True)
            self.assertEqual(w.piezo.size > 0, True)
            self.assertEqual(w.pressure.size > 0, True)
            self.assertEqual(w.airflow.size > 0, True)

    def test_no_mic_no_windows(self):
        pkt = parse_packet(build_packet(1, 0, {"piezo": np.zeros(10, np.float32)}))
        self.assertEqual(windows_from_packets([pkt]), [])


class TestRunPipeline(unittest.TestCase):
    def test_end_to_end_demo_silent(self):
        pkt = packet_with_bursts()
        res = run_pipeline([pkt], config=PipelineConfig(classifier="demo"),
                           tts=SilentTTS())
        self.assertEqual(len(res.phonemes), 3)
        self.assertEqual(len(res.predictions), 3)
        self.assertTrue(res.text is not None or res.text == "")
        for stage in ("segment", "features", "inference", "decode", "rerank"):
            self.assertIn(stage, res.stage_ms)
            self.assertGreaterEqual(res.stage_ms[stage], 0.0)
        self.assertGreater(res.e2e_ms, 0.0)

    def test_labels_flow_through(self):
        pkt = packet_with_bursts()
        res = run_pipeline([pkt],
                           labels=["T", "S", "M"],
                           config=PipelineConfig(classifier="demo"),
                           tts=SilentTTS())
        # demo classifier echoes class ids: stop->T, fricative->S, nasal->N
        self.assertEqual([p.phoneme for p in res.predictions], ["T", "S", "N"])
        self.assertEqual([p.class_name for p in res.predictions],
                         ["stop", "fricative", "nasal"])

    def test_wav_written(self):
        pkt = packet_with_bursts()
        fd, path = tempfile.mkstemp(suffix=".wav")
        os.close(fd)
        self.addCleanup(os.unlink, path)
        res = run_pipeline([pkt], config=PipelineConfig(classifier="demo", tts="silent"),
                           out_wav=path)
        self.assertEqual(res.wav_path, path)
        with open(path, "rb") as fh:
            head = fh.read(12)
        self.assertEqual(head[:4], b"RIFF")
        self.assertEqual(head[8:12], b"WAVE")


class TestSilentTTS(unittest.TestCase):
    def test_wav_container(self):
        wav = SilentTTS().synthesize("hello")
        self.assertEqual(wav[:4], b"RIFF")
        self.assertEqual(wav[8:12], b"WAVE")
        # 300 ms * 22050 Hz * 2 bytes = 13230 data bytes (44-byte header)
        self.assertEqual(len(wav), 44 + 22050 * 2 * 0.3)

    def test_speaker_embedding_validation(self):
        with self.assertRaises(ValueError):
            SpeakerEmbedding(vector=[0.0] * 255)
        SpeakerEmbedding(vector=[0.0] * 256)  # exactly 256 is valid

    def test_factory_default_silent(self):
        # No ONNX TTS bundle in repo -> auto must pick silent.
        tts = get_tts(None)
        self.assertIsInstance(tts, SilentTTS)


if __name__ == "__main__":
    unittest.main()
