# AVC Implementation Plan

**Project:** Artificial Vocal Card (AVC) — silent-speech-to-speech pipeline
**Spec source:** `AVC_TRD_Technical_Requirements_Document.txt` (TRD)
**Scope lock (2026):** software + firmware only. No Android client, no
cloud services. All four TRD sensors implemented in code; swappable LM
module (GPT-2 dropped from baseline).

---

## 1. Architecture (as built)

```
ESP32-S3 firmware            Gateway (PC / hub)
┌───────────────────┐  UDP   ┌──────────────────────────────────────────┐
│ mic   16 kHz ─┐   │ 7 kB   │ Layer 2  ingest.py      parse + CRC16    │
│ piezo  1 kHz ─┤   │ ─────► │          pipeline.py    segmentation     │
│ pressure100Hz─┤500│        │          features.py    13 descriptors    │
│ airflow 100Hz─┘ms │        │ Layer 3  inference.py  classifier        │
│ packet.c → CRC16  │        │          model_torch.py CNN-BiLSTM-Attn   │
└───────────────────┘        │          decoder.py    CMU lexicon + beam│
                              │          lm_rerank.py   swappable scorer │
                              │ Layer 4  tts.py         synthesizer      │
                              └──────────────────────────────────────────┘
```

- **Layer 1 (device):** `firmware/` — 500 ms windows, pinned wire format,
  CRC16 footer, UDP datagrams. Synthetic-sensor mode lets the full link
  be validated before sensors are wired.
- **Layer 2 (ingest/features):** `services/ingest.py` + `services/features.py`.
- **Layer 3 (inference):** `services/inference.py` (ONNX runtime, lazy) +
  `services/model_torch.py` (training head, optional torch).
- **Layer 4 (decode/TTS):** `services/decoder.py` (CMU-dict lexicon +
  beam search) → `services/lm_rerank.py` → `services/tts.py`.

Every stage is behind a factory (`get_classifier`, `get_tts`,
`SentenceScorer`), so heavier backends drop in without touching callers.

## 2. Locked contracts (do not change unilaterally)

| Contract | Defined in | Notes |
|---|---|---|
| 13 descriptors + order | `services/schemas.py` `DESCRIPTOR_NAMES` | Model input (B,T,13) |
| Phoneme vocab (40 classes) | `services/schemas.py` `PHONEME_VOCAB` | 39 ARPAbet **incl. HH** + SIL last; ONNX/TFLite heads must match |
| Phoneme classes (descriptor 13) | `services/schemas.py` `PHONEME_CLASSES` | 0 reserved for "unknown" |
| Wire format | `services/ingest.py` ↔ `firmware/main/packet.c` | `<IIB` header, per-sensor `<H`+int16 LE, CRC16-CCITT footer |
| First-window deltas = 0.0 | `services/features.py` | Explicit pinned convention, tested |
| Segmentation params | `services/pipeline.py` `segment_by_energy` | 20 ms frames / 10 ms hop / −45 dB rel-peak / ≥40 ms |

Lockstep between Python and firmware is enforced by golden vectors
embedded in `firmware/test/test_packet.c` (regeneration recipe in
`firmware/README.md`).

## 3. TRD gaps addressed (decisions pinned here)

1. **Wire format underspecified** (TRD "8-byte header" didn't fix a bit
   layout) → pinned the concrete 9-byte-header format above.
2. **Segmentation unspecified** → energy-based segmentation pinned in
   `pipeline.py` (parameters above).
3. **Descriptor 13 "phoneme class" is circular at prediction time**
   (needs the label it's predicting) → class 0 "unknown" at inference;
   decoder receives raw phoneme symbols which carry the information.
4. **First-window deltas undefined** (NaN or value−0?) → 0.0, tested.
5. **Vocab drift risk** (original spec list was missing HH, mislabeled
   "39 symbols") → canonical 40-class vocab in one place, derived
   `N_CLASSES` everywhere.
6. **Junk-input decode behavior** → when the best finished beam has no
   words, decoder returns raw phoneme symbols (visible "what was
   heard"), never empty text.
7. **Absolute SPL needs mic calibration** → SPL descriptor documented
   as dB-relative until calibration lands (see §6, M4).

## 4. Current status

| Milestone | State |
|---|---|
| M0 repo cleanup, spec lock | ✅ done |
| M1 software reference (Layers 2–4) | ✅ done — 71 tests green, CLI smoke 3.72 ms e2e (demo backends) |
| M1 firmware skeleton | ✅ done — packet.c + lockstep tests + synthetic-mode acquisition/UDP |
| M2 real sensor drivers + board bring-up | ⏸ blocked on hardware answers |
| M3 dataset + real classifier training | ⏸ blocked on dataset answer |
| M4 calibration + accuracy eval | pending M3 |
| M5 TFLite export + on-device decode | groundwork only |

## 5. Milestones forward

- **M2 — hardware bring-up.** Replace synthetic drivers in
  `firmware/main/main.c` (I2S mic, ADC piezo/pressure/airflow); validate
  500 ms pacing with real clocks; measure UDP loss on target LAN.
- **M3 — dataset + training.** Collect/ingest sessions → `FeatureSet`
  rows → train `model_torch.py`; export ONNX → `models/avc_phoneme.onnx`;
  eval: **>85% phoneme accuracy** (TRD 5.x) on held-out speakers.
- **M4 — calibration.** Mic sensitivity constant → absolute SPL;
  pressure/flow calibration against reference instruments.
- **M5 — deployment.** TFLite export of the trained head; firmware-side
  decode (beam only, no LM) for an offline mode; latency target
  **<500 ms** e2e (currently: plumbing stages 3.72 ms; TTS will dominate).

## 6. Performance budgets (TRD 5.x)

- e2e utterance latency: **<500 ms** (budget split: ingest <5 ms,
  features <10 ms, inference <50 ms, decode <100 ms, LM <100 ms,
  TTS remainder).
- Phoneme accuracy: **>85%** (real eval, M3 — demo classifier is
  plumbing only, never for accuracy claims).
- Packet: ≤17 KB per 500 ms window, one UDP datagram; CRC16 catches link
  corruption before features.

## 7. Testing & parity strategy

- **Python:** `python -m unittest discover -s tests` — 71 tests, no
  network, no model files; numpy is the only third-party dep.
- **Firmware:** `firmware/test/test_packet.c` — C99 host test, golden
  vectors + argument validation (no ESP-IDF required, run on any CI
  with a C compiler).
- **Golden vectors:** generated from the Python side only; both sides
  must be regenerated together (recipe in `firmware/README.md`).

## 8. Open questions (blocking M2/M3)

1. ESP32-S3 dev board model + pinout for the 4 sensors.
2. Protocol emitted by the existing physical device — does it already
   speak a packet format, or is firmware free to use this repo's pinned
   format?
3. Dataset: 48-instance research data vs. new collection (count,
   speakers, sessions, labels).
4. License for the repo (pending user choice).
