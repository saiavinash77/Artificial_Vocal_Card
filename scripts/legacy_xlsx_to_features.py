"""Legacy research dataset converter (M3 data prep).

Converts the 48-instance legacy research format (5 descriptors per
phoneme window, e.g. `avc_aum_namah_shivaya.xlsx`) into the LOCKED
13-descriptor format used by csv_logger --featurize, so M3 training
reads ONE format regardless of whether data came from the legacy
research set or from live csv_logger sessions.

Legacy input (xlsx sheets, or .csv mirroring Phoneme_Data):
    Phoneme_Data         dialogue, start_time_s, end_time_s, duration_ms,
                         phoneme, rms_amplitude, spl_dB, pressure_Pa,
                         velocity_m_s
    Word_Discrete        word timing + phoneme_sequence (optional)
    Wrong_Phoneme_Labels correct/predicted phonemes (optional)

Output per dialogue: <out>/<dialogue>.features.csv with the header
    word,<DESCRIPTOR_NAMES...>          (identical to csv_logger)

Descriptor reconstruction from the legacy 5:
    1 rms_amplitude     given
    2 spl_db           RECOMPUTED as 20*log10(rms/P_REF) — the legacy
                       sheet's spl column uses an unknown/uncalibrated
                       reference (not internally consistent with its own
                       RMS column), so it is replaced with the repo's
                       pinned dB-relative formula (IMPLEMENTATION_PLAN
                       §3.7: absolute SPL awaits mic calibration)
    3 pressure_pa      given
    4 velocity_ms      given
    5 duration_ms      given
    6 energy_ratio     rms / (mu_rms + eps)      mu per dialogue
    7 duration_norm    dur / (mu_dur + eps)       mu per dialogue
    8 spl_vel_ratio    spl / (|vel| + eps)
    9 pressure_rms_ratio  pressure / (rms + eps)
    10-12 delta_*      value_t - value_{t-1}; first window = 0.0
                       (pinned convention, tests/test_features.py)
    13 phoneme_class   phoneme_class_of(label) — from the CORRECT label

Also prints (xlsx only, when the sheets exist):
    eval   phoneme accuracy (stress-stripped) + class accuracy +
           confusion pairs, from Wrong_Phoneme_Labels
    decode ground-truth and predicted phoneme streams through
           services.decoder.PhonemeDecoder with a lexicon built FROM
           Word_Discrete's phoneme_sequence column (stress-stripped) —
           works for any vocabulary, no manual lexicon needed

Usage:
    python scripts/legacy_xlsx_to_features.py legacy.xlsx --out-dir data/legacy
    python scripts/legacy_xlsx_to_features.py legacy.csv   --out-dir out

xlsx support needs openpyxl (optional dep, lazy import — the repo core
stays stdlib+numpy). CSV mode needs nothing extra.
"""
from __future__ import annotations

import argparse
import csv as csvmod
import json
import math
import sys
from pathlib import Path
from typing import Dict, List, Optional

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from services.decoder import PhonemeDecoder  # noqa: E402
from services.schemas import (  # noqa: E402
    DESCRIPTOR_NAMES, EPS, P_REF, SPL_FLOOR_DB, class_name_of,
    phoneme_class_of, strip_stress,
)

IDX = {name: i for i, name in enumerate(DESCRIPTOR_NAMES)}

LEGACY_HEADER = ["dialogue", "start_time_s", "end_time_s", "duration_ms",
                 "phoneme", "rms_amplitude", "spl_dB", "pressure_Pa",
                 "velocity_m_s"]
WORD_HEADER = ["word", "start_time_s", "end_time_s", "duration_ms",
               "phoneme_sequence", "avg_rms_amplitude", "avg_spl_dB",
               "avg_pressure_Pa", "avg_velocity_m_s", "num_phonemes"]
WRONG_HEADER = ["dialogue", "start_time_s", "end_time_s", "duration_ms",
                "correct_phoneme", "predicted_phoneme", "error_type",
                "rms_amplitude", "spl_dB", "pressure_Pa", "velocity_m_s"]


# ---------------------------------------------------------------------------
# Input loading
# ---------------------------------------------------------------------------

def _sheet_rows(ws) -> List[List[object]]:
    rows = []
    for row in ws.iter_rows(values_only=True):
        rows.append(list(row))
    return rows


def load_legacy_xlsx(path: Path) -> Dict[str, List[List[object]]]:
    """Read the three legacy sheets; missing ones come back empty."""
    try:
        from openpyxl import load_workbook  # lazy optional dep
    except ImportError as exc:
        raise SystemExit(
            "xlsx input needs openpyxl (pip install openpyxl), or export "
            "the Phoneme_Data sheet as CSV and pass that instead") from exc
    wb = load_workbook(path, data_only=True)
    out: Dict[str, List[List[object]]] = {}
    for name in ("Phoneme_Data", "Word_Discrete", "Wrong_Phoneme_Labels"):
        ws = wb[name] if name in wb.sheetnames else None
        rows = _sheet_rows(ws) if ws is not None else []
        if rows and rows[0] and str(rows[0][0]).strip().lower() in (
                LEGACY_HEADER[0], WORD_HEADER[0], WRONG_HEADER[0]):
            rows = rows[1:]  # drop header row
        out[name] = [r for r in rows if any(v is not None and v != "" for v in r)]
    return out


def load_legacy_csv(path: Path) -> Dict[str, List[List[object]]]:
    """CSV mode: Phoneme_Data only (converted as one 'csv' dialogue)."""
    with open(path, newline="", encoding="utf-8-sig") as fh:
        reader = csvmod.reader(fh)
        rows = [r for r in reader if r and any(v.strip() for v in r)]
    if rows and rows[0][0].strip().lower() == LEGACY_HEADER[0]:
        rows = rows[1:]
    return {"Phoneme_Data": rows, "Word_Discrete": [], "Wrong_Phoneme_Labels": []}


def load_legacy(path: Path) -> Dict[str, List[List[object]]]:
    if path.suffix.lower() == ".csv":
        return load_legacy_csv(path)
    return load_legacy_xlsx(path)


# ---------------------------------------------------------------------------
# Core conversion
# ---------------------------------------------------------------------------

def _f(v, default=0.0) -> float:
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _spl(rms: float) -> float:
    """Pinned dB-relative SPL (schemas.P_REF); floor at SPL_FLOOR_DB."""
    if rms <= 0.0:
        return SPL_FLOOR_DB
    return 20.0 * math.log10(rms / P_REF)


def convert_rows(rows: List[List[object]],
                 word_map: Optional[List[Dict]] = None) -> List[Dict]:
    """Legacy Phoneme_Data rows -> list of 14-col feature rows.

    Deltas/means are computed per contiguous dialogue group (rows are
    expected time-ordered; a dialogue-name change starts a new group).
    """
    out: List[Dict] = []
    group: List[Dict] = []
    prev_dialogue = None

    def flush(group: List[Dict]) -> None:
        if not group:
            return
        mu_rms = float(np.mean([g["rms"] for g in group]))
        mu_dur = float(np.mean([g["dur"] for g in group]))
        prev_rms = prev_spl = prev_pres = None
        for g in group:
            v = np.zeros(len(DESCRIPTOR_NAMES), dtype=np.float64)
            rms, spl, pres, vel, dur = (
                g["rms"], g["spl"], g["pres"], g["vel"], g["dur"])
            v[IDX["rms_amplitude"]] = rms
            v[IDX["spl_db"]] = spl
            v[IDX["pressure_pa"]] = pres
            v[IDX["velocity_ms"]] = vel
            v[IDX["duration_ms"]] = dur
            v[IDX["energy_ratio"]] = rms / (mu_rms + EPS)
            v[IDX["duration_norm"]] = dur / (mu_dur + EPS)
            v[IDX["spl_vel_ratio"]] = spl / (abs(vel) + EPS)
            v[IDX["pressure_rms_ratio"]] = pres / (rms + EPS)
            # pinned convention: first window's deltas are 0.0
            v[IDX["delta_rms"]] = 0.0 if prev_rms is None else rms - prev_rms
            v[IDX["delta_spl"]] = 0.0 if prev_spl is None else spl - prev_spl
            v[IDX["delta_pressure"]] = 0.0 if prev_pres is None else pres - prev_pres
            v[IDX["phoneme_class"]] = float(phoneme_class_of(g["phoneme"]))
            out.append({
                "dialogue": g["dialogue"],
                "word": g["word"],
                "phoneme": strip_stress(g["phoneme"]),
                "vector": v,
            })
            prev_rms, prev_spl, prev_pres = rms, spl, pres

    for r in rows:
        dialogue = str(r[0])
        if prev_dialogue is not None and dialogue != prev_dialogue:
            flush(group)
            group = []
        prev_dialogue = dialogue
        start, end = _f(r[1]), _f(r[2])
        dur = _f(r[3]) if len(r) > 3 and r[3] not in (None, "") else (end - start) * 1000
        rms = _f(r[5])
        group.append({
            "dialogue": dialogue, "start": start, "end": end, "dur": dur,
            "phoneme": str(r[4]), "rms": rms,
            # recompute SPL with the pinned formula (see module docstring)
            "spl": _spl(rms),
            "pres": _f(r[7]) if len(r) > 7 else 0.0,
            "vel": _f(r[8]) if len(r) > 8 else 0.0,
            "word": "unlabeled",
        })
    flush(group)

    # attach word labels from the Word_Discrete timing, when available
    if word_map:
        for row in out:
            row["word"] = _word_for(row, word_map)
    return out


def _word_for(row: Dict, word_map: List[Dict]) -> str:
    t = row["start_t"] if "start_t" in row else None
    # fall back: match by order-in-dialogue via phoneme index is not
    # robust; timing is the join key.
    return "unlabeled"


def word_map_from_rows(word_rows: List[List[object]]) -> List[Dict]:
    """Word_Discrete rows -> [{word, start, end}] per dialogue."""
    out = []
    for r in word_rows:
        out.append({"word": str(r[0]), "start": _f(r[1]), "end": _f(r[2]),
                    "phonemes": str(r[4]) if len(r) > 4 else ""})
    return out


def attach_words(features: List[Dict], word_map: List[Dict],
                 timings: List[float]) -> None:
    """Assign word labels by window start time inside [start, end).

    ``timings`` parallel to features: each feature row's start_time_s.
    """
    for feat, t in zip(features, timings):
        label = "unlabeled"
        for w in word_map:
            if w["start"] <= t < w["end"]:
                label = w["word"]
                break
        feat["word"] = label


def write_features_csv(path: Path, features: List[Dict]) -> None:
    """M3-ready CSV — byte-compatible with csv_logger --featurize."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", newline="\n") as fh:
        fh.write("word," + ",".join(DESCRIPTOR_NAMES) + "\n")
        for f in features:
            fh.write(f["word"] + "," +
                     ",".join(f"{x:.6g}" for x in f["vector"]) + "\n")


# ---------------------------------------------------------------------------
# Eval + decode (xlsx sheets)
# ---------------------------------------------------------------------------

def evaluate(wrong_rows: List[List[object]]) -> Dict:
    """Accuracy report from the Wrong_Phoneme_Labels sheet."""
    n = 0
    exact = 0
    class_ok = 0
    confusions: Dict[str, int] = {}
    for r in wrong_rows:
        correct, predicted = str(r[4]), str(r[5])
        n += 1
        c_ok = strip_stress(correct) == strip_stress(predicted)
        k_ok = class_name_of(correct) == class_name_of(predicted)
        exact += c_ok
        class_ok += k_ok
        if not c_ok:
            key = f"{strip_stress(correct)}->{strip_stress(predicted)}"
            confusions[key] = confusions.get(key, 0) + 1
    return {
        "n_windows": n,
        "phoneme_accuracy": round(exact / n, 4) if n else None,
        "class_accuracy": round(class_ok / n, 4) if n else None,
        "confusion_pairs": dict(sorted(confusions.items(),
                                       key=lambda kv: -kv[1])),
    }


def lexicon_from_words(word_map: List[Dict]) -> Dict[str, tuple]:
    """Build a decoder lexicon FROM the data (stress-stripped tuples)."""
    lex = {}
    for w in word_map:
        seq = tuple(strip_stress(p) for p in w["phonemes"].split("-") if p)
        if seq:
            lex[w["word"].upper()] = seq
    return lex


def decode_stream(phonemes: List[str], lexicon: Dict[str, tuple],
                  beam_width: int = 3) -> List[Dict]:
    dec = PhonemeDecoder(lexicon=lexicon, beam_width=beam_width)
    return [{"text": c.text, "edit_cost": c.edit_cost}
            for c in dec.decode(phonemes)]


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def _cli() -> int:  # pragma: no cover - thin shell
    ap = argparse.ArgumentParser(
        prog="legacy_xlsx_to_features",
        description="Legacy 5-descriptor research data -> locked 13-descriptor "
                    "M3 format (same CSV as csv_logger --featurize)")
    ap.add_argument("input", help="legacy .xlsx or .csv (Phoneme_Data export)")
    ap.add_argument("--out-dir", default="data/legacy",
                    help="output directory for *.features.csv")
    ap.add_argument("--json", action="store_true",
                    help="print the full report as JSON (files still written)")
    args = ap.parse_args()

    path = Path(args.input)
    sheets = load_legacy(path)
    features = convert_rows(sheets["Phoneme_Data"])
    word_map = word_map_from_rows(sheets["Word_Discrete"])
    if word_map and features:
        # join key: window start time (column 1 of Phoneme_Data)
        timings = [_f(r[1]) for r in sheets["Phoneme_Data"]]
        attach_words(features, word_map, timings)

    out_dir = Path(args.out_dir)
    written = []
    by_dialogue: Dict[str, List[Dict]] = {}
    for f in features:
        by_dialogue.setdefault(f["dialogue"], []).append(f)
    for dialogue, rows in by_dialogue.items():
        slug = "".join(c if c.isalnum() else "_" for c in dialogue.strip()) or "session"
        out_path = out_dir / f"{slug}.features.csv"
        write_features_csv(out_path, rows)
        written.append(str(out_path))

    report: Dict = {"input": str(path), "features_written": written,
                    "n_windows": len(features),
                    "n_words": len(word_map)}

    if sheets["Wrong_Phoneme_Labels"]:
        truth = [str(r[4]) for r in sheets["Wrong_Phoneme_Labels"]]
        preds = [str(r[5]) for r in sheets["Wrong_Phoneme_Labels"]]
        report["eval"] = evaluate(sheets["Wrong_Phoneme_Labels"])
        lex = lexicon_from_words(word_map)
        if lex:
            report["decode"] = {
                "ground_truth": decode_stream(truth, lex),
                "predicted": decode_stream(preds, lex),
            }

    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"windows: {report['n_windows']}  words: {report['n_words']}")
        for w in written:
            print(f"  wrote {w}")
        if "eval" in report:
            ev = report["eval"]
            print(f"eval: phoneme {ev['phoneme_accuracy']*100:.1f}% | "
                  f"class {ev['class_accuracy']*100:.1f}% "
                  f"({ev['n_windows']} windows)")
            top = list(ev["confusion_pairs"].items())[:5]
            print("  top confusions: " +
                  ", ".join(f"{k}×{v}" for k, v in top))
        if "decode" in report:
            for mode in ("ground_truth", "predicted"):
                best = report["decode"][mode][0]
                print(f"decode[{mode}]: {best['text']!r} "
                      f"(edit_cost {best['edit_cost']})")
    return 0


if __name__ == "__main__":
    raise SystemExit(_cli())
