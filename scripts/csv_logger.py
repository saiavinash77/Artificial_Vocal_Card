"""Gateway-side CSV logger for the AVC open device (M2.4).

Reads the firmware's USB-serial CSV stream (one line per 500 ms window),
validates it, splits it into labeled repetitions, and writes a session
directory that is immediately replayable through the pipeline.

Firmware line format (AVC1 — gateway and firmware build/parse this
together; mirror of the binary packet, minus the sensor blocks):

    AVC1,<seq>,<ts_ms>,<mask_hex>,<onset>,<label>,<b64 per enabled
    sensor in ascending bit order>,<crc16_hex>

  - samples are the int16 raw counts exactly as the firmware holds
    them, base64-encoded little-endian (24 KB binary -> ~32 KB text per
    full open2 window; at 921600 baud that is ~64 KB/s, inside the
    ~92 KB/s line rate with headroom)
  - <crc16_hex> is the SAME CRC16-CCITT as the packet footer
    (services.ingest.crc16_ccitt) over the ASCII prefix before the
    final comma — catches serial corruption before it lands in data
  - <label> is normally empty from the firmware; the logger attaches
    word labels (prompted mode) and never writes raw commas (sanitized)

Session layout (``--out data``)::

    data/session_YYYYmmdd_HHMMSS/
        manifest.json        # device, rates, onset thresholds, rep index
        rep_000_<label>.csv  # one row per window: dataset-ready columns
        rep_000_<label>.features.csv  (with --featurize: 13 descriptors)

Replay (proof that logged CSV parses into packets — M2.4 acceptance)::

    python scripts/csv_logger.py --replay data/session_.../rep_000_<label>.csv

Self-test with no hardware and no serial port::

    python scripts/csv_logger.py --self-test

Live capture (needs pyserial + the board)::

    python scripts/csv_logger.py --port COM5 --baud 921600 --speaker sai \\
        --prompt --out data
"""
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.ingest import (  # noqa: E402
    PacketError, build_packet, crc16_ccitt, parse_packet,
)
from services.onset import (  # noqa: E402
    HANGOVER_FRAMES, INT16_NOISE_FLOOR, INT16_ONSET_RMS, NOISE_FLOOR,
    ONSET_RMS, OnsetTracker,
)

MAGIC = "AVC1"
FORMAT_ID = "avc-csv-1"

#: sensor bit -> name; MUST mirror services/ingest._SENSOR_BITS order.
SENSOR_BITS: List[tuple] = [
    (0x01, "mic"),
    (0x02, "piezo"),
    (0x04, "pressure"),
    (0x08, "airflow"),
]
RATES: Dict[str, int] = {"mic": 16_000, "piezo": 8_000,
                         "pressure": 100, "airflow": 100}


class CsvError(ValueError):
    """Raised when a CSV line fails structural or CRC validation."""


def _sanitize_label(label: str) -> str:
    """Labels live inside comma-separated lines: no commas/newlines."""
    label = str(label).strip()          # strip whitespace FIRST —
    out = ""                            # replacing '_' would otherwise
    for c in label:                     # turn '  ' into '__'
        out += c if c.isalnum() or c in "-_'" else "_"
    return out or "unlabeled"


def csv_line(seq: int, ts_ms: int, streams: Dict[str, np.ndarray],
             onset: int = 0, label: str = "") -> str:
    """Serialize one window (name -> int16 raw-count array) to AVC1 CSV.

    This is the exact line the firmware emits (main.c csv_build_line);
    kept here so tests and the self-test generate byte-identical input.
    """
    mask = 0
    for bit, name in SENSOR_BITS:
        if name in streams:
            mask |= bit
    label = _sanitize_label(label) if label else ""
    fields = [MAGIC, str(int(seq)), str(int(ts_ms)), f"{mask:02x}",
              str(int(onset)), label]
    for bit, name in SENSOR_BITS:
        if mask & bit:
            arr = np.asarray(streams[name], dtype=np.int16).ravel()
            fields.append(base64.b64encode(arr.astype("<i2").tobytes()).decode("ascii"))
    prefix = ",".join(fields)
    return f"{prefix},{crc16_ccitt(prefix.encode('ascii')):04x}"


def parse_csv_line(line: str) -> Dict:
    """Validate + decode one AVC1 line. Raises CsvError on any defect.

    Returns {seq, ts_ms, mask, onset, label, streams: {name: int16 array},
    gateway_onset: bool} — gateway_onset is recomputed here from the raw
    mic counts (SATHVANI §5 semantics, services.onset) so rep splitting
    works even when the firmware gate is disabled.
    """
    line = line.strip()
    if not line.startswith(MAGIC + ","):
        raise CsvError(f"bad magic: {line[:12]!r}")
    parts = line.split(",")
    if len(parts) < 8:  # magic+5 header fields + >=1 sample + crc
        raise CsvError(f"too few fields: {len(parts)}")
    prefix, crc_s = ",".join(parts[:-1]), parts[-1]
    try:
        crc_recv = int(crc_s, 16)
    except ValueError as exc:
        raise CsvError(f"bad crc field: {crc_s!r}") from exc
    if crc16_ccitt(prefix.encode("ascii")) != crc_recv:
        raise CsvError(f"CRC mismatch on seq {parts[1]}")

    magic, seq_s, ts_s, mask_s, onset_s, label = parts[:6]
    try:
        seq, ts_ms, mask, onset = int(seq_s), int(ts_s), int(mask_s, 16), int(onset_s)
    except ValueError as exc:
        raise CsvError(f"bad numeric header: {exc}") from exc
    if mask == 0 or mask & ~0x0F:
        raise CsvError(f"invalid sensor_mask: {mask:#04x}")

    b64s = parts[6:-1]
    want = bin(mask).count("1")
    if len(b64s) != want:
        raise CsvError(f"expected {want} sample fields for mask {mask:#04x}, got {len(b64s)}")

    streams: Dict[str, np.ndarray] = {}
    for (bit, name), b64 in zip((s for s in SENSOR_BITS if mask & s[0]), b64s):
        try:
            raw = base64.b64decode(b64, validate=True)
        except Exception as exc:  # binascii.Error etc.
            raise CsvError(f"bad base64 for {name}: {exc}") from exc
        if len(raw) % 2:
            raise CsvError(f"odd byte count for {name}: {len(raw)}")
        streams[name] = np.frombuffer(raw, dtype="<i2").astype(np.int16)

    tracker = OnsetTracker(onset_rms=INT16_ONSET_RMS,
                           noise_floor=INT16_NOISE_FLOOR)
    gateway_onset = bool(tracker.window(streams["mic"], RATES["mic"])) \
        if "mic" in streams else False
    return {"seq": seq, "ts_ms": ts_ms, "mask": mask, "onset": onset,
            "label": label, "streams": streams, "gateway_onset": gateway_onset}


def csv_to_packets(lines: List[str]) -> List:
    """Convert validated AVC1 lines into real SensorPackets.

    Routes through build_packet + parse_packet (the actual wire path) so
    a replay exercises exactly what a UDP capture would: int16 counts ->
    float /32767 -> quantize -> CRC -> parse. Round-trip is exact.
    """
    packets = []
    for line in lines:
        rec = parse_csv_line(line)
        floats = {name: arr.astype(np.float32) / 32767.0
                  for name, arr in rec["streams"].items()}
        packets.append(parse_packet(build_packet(rec["seq"], rec["ts_ms"], floats)))
    return packets


# ---------------------------------------------------------------------------
# Session writing
# ---------------------------------------------------------------------------

REP_COLUMNS = ["seq", "ts_ms", "mask", "onset", "gateway_onset", "label",
               "mic_n", "piezo_n", "mic_rms_raw", "piezo_rms_raw",
               "mic_b64", "piezo_b64"]


def _rep_row(rec: Dict) -> Dict:
    streams = rec["streams"]
    def rms_raw(name):
        arr = streams.get(name)
        return 0 if arr is None or arr.size == 0 else int(
            np.sqrt(np.mean(arr.astype(np.int64) ** 2)))
    return {
        "seq": rec["seq"], "ts_ms": rec["ts_ms"], "mask": f"{rec['mask']:02x}",
        "onset": rec["onset"], "gateway_onset": int(rec["gateway_onset"]),
        "label": rec["label"] or "unlabeled",
        "mic_n": streams.get("mic", np.zeros(0, np.int16)).size,
        "piezo_n": streams.get("piezo", np.zeros(0, np.int16)).size,
        "mic_rms_raw": rms_raw("mic"), "piezo_rms_raw": rms_raw("piezo"),
        "mic_b64": base64.b64encode(
            streams.get("mic", np.zeros(0, np.int16)).astype("<i2").tobytes()).decode(),
        "piezo_b64": base64.b64encode(
            streams.get("piezo", np.zeros(0, np.int16)).astype("<i2").tobytes()).decode(),
    }


def split_reps(records: List[Dict], silence_windows: int = 2) -> List[List[Dict]]:
    """Group records into repetitions.

    A rep is a run of gateway-active windows; `silence_windows`
    consecutive inactive windows end it. Leading/trailing inactive
    windows are dropped (they are silence by definition).
    """
    reps: List[List[Dict]] = []
    cur: List[Dict] = []
    quiet = 0
    for rec in records:
        if rec["gateway_onset"]:
            cur.append(rec)
            quiet = 0
        else:
            if cur:
                quiet += 1
                if quiet >= silence_windows:
                    reps.append(cur)
                    cur, quiet = [], 0
    if cur:
        reps.append(cur)
    return reps


def write_session(out_root: Path, records: List[Dict], speaker: str = "",
                  labels: Optional[List[str]] = None,
                  featurize: bool = False) -> Path:
    """Write one session directory (manifest + per-rep CSVs).

    ``labels`` parallel to the reps from split_reps; missing entries
    fall back to the line's label or 'unlabeled'.
    """
    reps = split_reps(records)
    session = out_root / f"session_{time.strftime('%Y%m%d_%H%M%S')}"
    session.mkdir(parents=True, exist_ok=True)

    manifest = {
        "format": FORMAT_ID, "created": time.strftime("%Y-%m-%dT%H:%M:%S"),
        "device": "open2", "speaker": speaker,
        "rates": RATES,
        "onset": {"onset_rms": ONSET_RMS, "noise_floor": NOISE_FLOOR,
                  "hangover_frames": HANGOVER_FRAMES,
                  "gateway_int16_onset_rms": INT16_ONSET_RMS,
                  "gateway_int16_noise_floor": INT16_NOISE_FLOOR},
        "reps": [],
    }
    for i, rep in enumerate(reps):
        label = _sanitize_label(
            (labels[i] if labels and i < len(labels) and labels[i]
             else rep[0]["label"] or "unlabeled"))
        name = f"rep_{i:03d}_{label}.csv"
        path = session / name
        with open(path, "w", newline="\n") as fh:
            fh.write(",".join(REP_COLUMNS) + "\n")
            for rec in rep:
                fh.write(",".join(str(x) for x in
                                  _rep_row(rec).values()) + "\n")
        manifest["reps"].append({
            "file": name, "label": label, "n_windows": len(rep),
            "seq_range": [rep[0]["seq"], rep[-1]["seq"]],
            "ts_range": [rep[0]["ts_ms"], rep[-1]["ts_ms"]]})
        if featurize:
            _write_features(session / name.replace(".csv", ".features.csv"),
                            rep, label)
    with open(session / "manifest.json", "w") as fh:
        json.dump(manifest, fh, indent=2)
    return session


def _write_features(path: Path, rep: List[Dict], label: str) -> None:
    """13-descriptor rows for one rep — M3-ready training rows."""
    from services.features import DESCRIPTOR_NAMES, features_from_windows
    from services.pipeline import windows_from_packets
    packets = csv_to_packets([csv_line(r["seq"], r["ts_ms"], r["streams"],
                                        r["onset"], r["label"]) for r in rep])
    wins = windows_from_packets(packets)
    feats = features_from_windows(wins)
    with open(path, "w", newline="\n") as fh:
        fh.write("word," + ",".join(DESCRIPTOR_NAMES) + "\n")
        for row in feats.rows:
            fh.write(label + "," +
                    ",".join(f"{v:.6g}" for v in row.vector) + "\n")


def load_rep_csv(path: Path) -> List[str]:
    """Read a rep CSV back into AVC1 lines (for --replay)."""
    with open(path) as fh:
        rows = [ln for ln in fh.read().splitlines()
                if ln and not ln.startswith(REP_COLUMNS[0])]
    lines = []
    for row in rows:
        cols = dict(zip(REP_COLUMNS, row.split(",")))
        streams = {}
        for name, col in (("mic", "mic_b64"), ("piezo", "piezo_b64")):
            raw = base64.b64decode(cols[col])
            if raw:
                streams[name] = np.frombuffer(raw, dtype="<i2").astype(np.int16)
        lines.append(csv_line(int(cols["seq"]), int(cols["ts_ms"]), streams,
                              int(cols["onset"]), cols["label"]))
    return lines


# ---------------------------------------------------------------------------
# Live serial capture
# ---------------------------------------------------------------------------

def capture(port: str, baud: int, out_root: Path, speaker: str,
            prompt: bool, featurize: bool) -> int:
    try:
        import serial  # lazy: optional dependency for live capture only
    except ImportError:
        print("pyserial is required for live capture: pip install pyserial",
              file=sys.stderr)
        return 2
    ser = serial.Serial(port, baud, timeout=2)
    print(f"listening on {port} @ {baud}; Ctrl-C to stop")
    records: List[Dict] = []
    labels: List[str] = []
    next_label = ""
    n_bad = 0
    try:
        while True:
            raw = ser.readline()
            if not raw:
                continue
            line = raw.decode("ascii", errors="replace").strip()
            if not line.startswith(MAGIC + ","):
                n_bad += 1  # log noise / boot chatter
                continue
            try:
                rec = parse_csv_line(line)
            except CsvError as e:
                n_bad += 1
                print(f"[BAD ] {e}")
                continue
            if prompt and not next_label:
                next_label = input("next word (Enter=finish): ").strip()
                if not next_label:
                    break
                labels.append(next_label)
            records.append(rec)
            print(f"[OK  ] seq={rec['seq']} ts={rec['ts_ms']} "
                  f"mask={rec['mask']:#04x} onset={rec['gateway_onset']}")
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()
    session = write_session(out_root, records, speaker=speaker,
                            labels=labels or None, featurize=featurize)
    print(f"\n{len(records)} windows ({n_bad} bad lines) -> {session}")
    return 0


# ---------------------------------------------------------------------------
# Self-test (no hardware, no serial)
# ---------------------------------------------------------------------------

def _synth_window(seq: int, voiced: bool, seed: int) -> Dict:
    """Deterministic synthetic window, open2 channels, doc-scale RMS."""
    rng = np.random.default_rng(seed)
    mic = (0.30 * np.sin(2 * np.pi * 180 * np.arange(8000) / 16_000)
           + 0.02 * rng.normal(size=8000)) if voiced else \
          (0.004 * rng.normal(size=8000))
    piezo = (0.25 * np.sin(2 * np.pi * 90 * np.arange(4000) / 8_000)
             + 0.02 * rng.normal(size=4000)) if voiced else \
            (0.004 * rng.normal(size=4000))
    return {"mic": (mic * 32767).astype(np.int16),
            "piezo": (piezo * 32767).astype(np.int16)}


def self_test(out_root: Path) -> int:
    """Generate 2 'utterances', run the full logger path, replay pipeline."""
    from services.pipeline import PipelineConfig, run_pipeline, windows_from_packets
    from services.tts import SilentTTS

    seq = 0
    records: List[Dict] = []
    plan = []  # (voiced windows, silent windows)
    for utterance, n_voiced in ((3, 4), (3, 2)):
        for v in range(n_voiced):
            records.append(parse_csv_line(
                csv_line(seq, seq * 500, _synth_window(seq, True, seq))))
            seq += 1
        for s in range(utterance):
            records.append(parse_csv_line(
                csv_line(seq, seq * 500, _synth_window(seq, False, seq))))
            seq += 1
        plan.append(n_voiced)

    reps = split_reps(records)
    assert [len(r) for r in reps] == plan, \
        f"rep split got {[len(r) for r in reps]}, want {plan}"

    session = write_session(out_root, records, speaker="selftest",
                            labels=["hello", "avc"], featurize=True)
    manifest = json.loads((session / "manifest.json").read_text())
    assert [r["label"] for r in manifest["reps"]] == ["hello", "avc"]

    for rep in manifest["reps"]:
        lines = load_rep_csv(session / rep["file"])
        packets = csv_to_packets(lines)
        assert all(p.sensor_mask == 0x03 for p in packets)
        assert all(p.stream("piezo").rate_hz == 8_000 for p in packets)
        res = run_pipeline(packets, config=PipelineConfig(classifier="demo"),
                          tts=SilentTTS())
        wins = windows_from_packets(packets)
        assert len(res.predictions) == len(wins)
        print(f"[REPLAY] {rep['file']}: {len(packets)} packets -> "
              f"{len(res.predictions)} predictions, e2e "
              f"{res.e2e_ms:.2f} ms, text={res.text[:40]!r}")
    print(f"SELF-TEST PASS -> {session}")
    return 0


def _cli() -> int:  # pragma: no cover - thin shell
    ap = argparse.ArgumentParser(
        prog="csv_logger",
        description="AVC open-device CSV logger (SATHVANI doc §8 MVP)")
    ap.add_argument("--port", help="serial port for live capture (e.g. COM5)")
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--out", default="data", help="session output root")
    ap.add_argument("--speaker", default="", help="speaker id for the manifest")
    ap.add_argument("--prompt", action="store_true",
                    help="prompt for a word label per repetition")
    ap.add_argument("--featurize", action="store_true",
                    help="also write 13-descriptor rows per rep (M3-ready)")
    ap.add_argument("--replay", metavar="REP_CSV",
                    help="replay a logged rep CSV through the pipeline")
    ap.add_argument("--self-test", action="store_true",
                    help="no hardware needed: end-to-end logger path check")
    args = ap.parse_args()

    if args.self_test:
        return self_test(Path(args.out))
    if args.replay:
        from services.pipeline import PipelineConfig, run_pipeline
        from services.tts import SilentTTS
        packets = csv_to_packets(load_rep_csv(Path(args.replay)))
        res = run_pipeline(packets, config=PipelineConfig(classifier="demo"),
                           tts=SilentTTS())
        print("phonemes:", " ".join(res.phonemes))
        print("text    :", res.text or "(none)")
        for k, v in res.stage_ms.items():
            print(f"  {k:10s} {v:8.2f} ms")
        return 0
    if args.port:
        return capture(args.port, args.baud, Path(args.out),
                       args.speaker, args.prompt, args.featurize)
    ap.print_help()
    return 1


if __name__ == "__main__":
    raise SystemExit(_cli())
