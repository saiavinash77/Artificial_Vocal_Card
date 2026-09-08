"""Tests for M2.4: CSV logger + speech onset gate (open device)."""

import base64
import json
import os
import tempfile
import unittest
from pathlib import Path

import numpy as np

from services.onset import (
    HANGOVER_FRAMES, INT16_NOISE_FLOOR, INT16_ONSET_RMS, OnsetTracker,
    frame_rms_raw, split_frames,
)

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))

from csv_logger import (  # noqa: E402
    CsvError, FORMAT_ID, REP_COLUMNS, _sanitize_label, csv_line,
    csv_to_packets, load_rep_csv, parse_csv_line, split_reps,
    write_session,
)


def synth_streams(seed=0, voiced=True, rate_mic=16_000, rate_piezo=8_000):
    rng = np.random.default_rng(seed)
    n_mic = rate_mic // 2  # 500 ms
    n_piezo = rate_piezo // 2
    if voiced:
        t = np.arange(n_mic) / rate_mic
        mic = 0.3 * np.sin(2 * np.pi * 180 * t) + 0.02 * rng.normal(size=n_mic)
        t2 = np.arange(n_piezo) / rate_piezo
        piezo = 0.25 * np.sin(2 * np.pi * 90 * t2) + 0.02 * rng.normal(size=n_piezo)
    else:
        mic = 0.004 * rng.normal(size=n_mic)
        piezo = 0.004 * rng.normal(size=n_piezo)
    return {"mic": (mic * 32767).astype(np.int16),
            "piezo": (piezo * 32767).astype(np.int16)}


class TestOnsetGate(unittest.TestCase):
    def test_onset_activates_and_hangover_decays(self):
        st = OnsetTracker(onset_rms=INT16_ONSET_RMS,
                          noise_floor=INT16_NOISE_FLOOR)
        self.assertFalse(st.frame(INT16_NOISE_FLOOR - 1))   # silent floor
        self.assertTrue(st.frame(INT16_ONSET_RMS))          # onset fires
        self.assertEqual(st.hangover, HANGOVER_FRAMES)
        mid = (INT16_NOISE_FLOOR + INT16_ONSET_RMS) // 2    # between-band
        for i in range(HANGOVER_FRAMES):
            self.assertTrue(st.frame(mid), f"hangover frame {i}")
        self.assertFalse(st.frame(mid))                     # grace expired

    def test_floor_collapses_immediately(self):
        st = OnsetTracker(onset_rms=INT16_ONSET_RMS,
                          noise_floor=INT16_NOISE_FLOOR)
        st.frame(INT16_ONSET_RMS)
        self.assertFalse(st.frame(INT16_NOISE_FLOOR - 1))   # no grace
        self.assertEqual(st.hangover, 0)

    def test_window_voiced_vs_silent(self):
        gate = dict(onset_rms=INT16_ONSET_RMS, noise_floor=INT16_NOISE_FLOOR)
        self.assertTrue(OnsetTracker(**gate).window(
            synth_streams(voiced=True)["mic"]))
        self.assertFalse(OnsetTracker(**gate).window(
            synth_streams(voiced=False)["mic"]))

    def test_frame_math(self):
        # constant-amplitude frames have trivial RMS
        frame = np.full(320, 3000, np.int16)
        self.assertEqual(frame_rms_raw(frame), 3000)
        self.assertEqual(split_frames(np.zeros(0, np.int16)), [])
        # 25 frames of 20 ms -> last is short
        fs = split_frames(np.zeros(16_000 // 2, np.int16))
        self.assertEqual(len(fs), 25)
        self.assertEqual(fs[-1].size, 16000 // 2 - 24 * 320)


class TestCsvLine(unittest.TestCase):
    def test_round_trip(self):
        streams = synth_streams(seed=1)
        line = csv_line(7, 3500, streams, onset=1, label="hello")
        rec = parse_csv_line(line)
        self.assertEqual(rec["seq"], 7)
        self.assertEqual(rec["ts_ms"], 3500)
        self.assertEqual(rec["mask"], 0x03)
        self.assertEqual(rec["onset"], 1)
        self.assertEqual(rec["label"], "hello")
        self.assertTrue(rec["gateway_onset"])
        for name, arr in streams.items():
            np.testing.assert_array_equal(rec["streams"][name], arr)

    def test_crc_rejects_corruption(self):
        line = csv_line(7, 3500, synth_streams())
        corrupt = line[:-1] + ("0" if line[-1] != "0" else "1")
        with self.assertRaises(CsvError):
            parse_csv_line(corrupt)

    def test_bad_fields_rejected(self):
        base = csv_line(1, 0, synth_streams())
        # bad magic
        with self.assertRaises(CsvError):
            parse_csv_line("XXX1" + base[4:])
        # missing a sample field (mic + piezo -> one b64)
        parts = base.split(",")
        with self.assertRaises(CsvError):
            parse_csv_line(",".join(parts[:-2] + parts[-1:]))

    def test_label_sanitized(self):
        self.assertEqual(_sanitize_label("hello, world"), "hello__world")
        self.assertEqual(_sanitize_label("  "), "unlabeled")
        self.assertEqual(_sanitize_label("it's"), "it's")


class TestRepSplit(unittest.TestCase):
    def _records(self, voiced_pattern):
        recs = []
        for i, v in enumerate(voiced_pattern):
            recs.append(parse_csv_line(
                csv_line(i, i * 500, synth_streams(seed=i, voiced=v))))
        return recs

    def test_groups_runs(self):
        recs = self._records([1, 1, 0, 0, 1, 0, 0, 0])
        reps = split_reps(recs, silence_windows=2)
        self.assertEqual([len(r) for r in reps], [2, 1])

    def test_all_silent_yields_nothing(self):
        recs = self._records([0, 0, 0])
        self.assertEqual(split_reps(recs), [])

    def test_labels_from_lines_used(self):
        recs = self._records([1, 0, 0, 1, 0, 0])
        reps = split_reps(recs)
        self.assertEqual(len(reps), 2)


class TestSessionWriteReplay(unittest.TestCase):
    def test_session_manifest_and_pipeline_replay(self):
        # M2.4 acceptance: logged CSV parses into packets and runs the
        # pipeline; columns are dataset-ready; manifest carries metadata.
        from services.pipeline import PipelineConfig, run_pipeline, windows_from_packets
        from services.tts import SilentTTS

        recs = []
        seq = 0
        for utterance in (3, 2):  # two reps: 3 then 2 voiced windows
            for _ in range(utterance):
                recs.append(parse_csv_line(csv_line(
                    seq, seq * 500, synth_streams(seed=seq, voiced=True))))
                seq += 1
            for _ in range(3):
                recs.append(parse_csv_line(csv_line(
                    seq, seq * 500, synth_streams(seed=seq, voiced=False))))
                seq += 1

        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: __import__("shutil").rmtree(tmp, ignore_errors=True))
        session = write_session(tmp, recs, speaker="tester",
                                labels=["hello", "avc"], featurize=True)

        manifest = json.loads((session / "manifest.json").read_text())
        self.assertEqual(manifest["format"], FORMAT_ID)
        self.assertEqual(manifest["device"], "open2")
        self.assertEqual(manifest["speaker"], "tester")
        self.assertEqual(manifest["rates"]["piezo"], 8_000)
        self.assertEqual([r["label"] for r in manifest["reps"]],
                         ["hello", "avc"])
        self.assertEqual([r["n_windows"] for r in manifest["reps"]], [3, 2])

        for i, rep_m in enumerate(manifest["reps"]):
            rep_path = session / rep_m["file"]
            header = rep_path.read_text().splitlines()[0]
            self.assertEqual(header.split(","), REP_COLUMNS)
            # dataset-ready rows
            rows = rep_path.read_text().splitlines()[1:]
            self.assertEqual(len(rows), rep_m["n_windows"])
            for row in rows:
                cols = row.split(",")
                self.assertEqual(len(cols), len(REP_COLUMNS))
                int(cols["seq".split(",").index("seq") if False else
                     REP_COLUMNS.index("seq")])
                int(cols[REP_COLUMNS.index("mic_n")])
                float(cols[REP_COLUMNS.index("mic_rms_raw")])

            # replay path: CSV -> AVC1 -> packets -> pipeline
            lines = load_rep_csv(rep_path)
            packets = csv_to_packets(lines)
            self.assertEqual(len(packets), rep_m["n_windows"])
            for p in packets:
                self.assertEqual(p.sensor_mask, 0x03)
                self.assertEqual(p.stream("piezo").rate_hz, 8_000)
                self.assertEqual(p.stream("mic").samples.size, 8000)
                self.assertEqual(p.stream("piezo").samples.size, 4000)
            res = run_pipeline(packets,
                               config=PipelineConfig(classifier="demo"),
                               tts=SilentTTS())
            self.assertEqual(len(res.predictions),
                             len(windows_from_packets(packets)))

            feats_path = session / rep_m["file"].replace(".csv", ".features.csv")
            feat_rows = feats_path.read_text().splitlines()
            self.assertEqual(len(feat_rows), rep_m["n_windows"] + 1)


if __name__ == "__main__":
    unittest.main()
