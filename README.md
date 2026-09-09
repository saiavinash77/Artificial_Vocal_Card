# AVC — Artificial Vocal Card

Silent-speech-to-speech pipeline: you mouth a word without voicing it, a
small throat device feels the vibrations, and a Python pipeline turns
them into phonemes → words → synthesized speech.

**Hardware track (this branch):** the **open device** — two sensors,
mic + piezo. The `master` branch keeps the 4-sensor closed-device
reference; the wire format and model contract are identical for both.

## How it works — user's view 🙂

```mermaid
flowchart LR
    U["🙂 You mouth a word<br/>(silently — no voice needed)"]
    S["🎛️ Throat device<br/>feels the vibrations"]
    C["💻 Laptop<br/>checks + saves the data"]
    B["🧠 AVC software<br/>sounds → phonemes → word"]
    W["📄 Text<br/>'hello'"]
    SP["🔊 Speaker / screen<br/>says it out loud"]

    U --> S
    S -->|"USB cable — 500 ms chunks"| C
    C -->|"under half a second"| B
    B --> W
    W --> SP
```

One sentence: **you mouth → device feels → laptop thinks → speaker speaks.**
The piezo film on the throat carries most of the signal (~60–70%
importance); the mic catches residual sound shape.

## How it works — technical data flow ⚙️

```mermaid
flowchart TB
    subgraph DEVICE["ESP32-S3 — open device (mic + piezo)"]
        MIC["ICS-43434 MEMS mic<br/>I2S @ 16 kHz → 8,000 int16 / window"]
        PZ["LDT2-028K PVDF film → OPA140 → ADS126x<br/>SPI @ 8 kSPS → 4,000 int16 / window"]
        ACQ["acquisition_task — main.c<br/>500 ms window, mic-clocked pacing"]
        ONG["onset gate — onset.c (optional)<br/>20 ms frames · onset 50k / floor 20k RMS · hangover"]
        CSVL["csv_build_line — main.c<br/>AVC1 line: base64 int16 + CRC16"]
        PKB["avc_packet_build — packet.c<br/>binary: sensor_mask 0x03 + CRC16 footer"]
    end

    subgraph TRANSPORT["Transport — AVC_TRANSPORT (Kconfig)"]
        USB["USB serial @ 921600<br/>(default — data collection)"]
        UDP["UDP datagram ≈ 24 KB<br/>(alternative)"]
    end

    subgraph GATEWAY["Gateway (PC / laptop)"]
        CAP["scripts/udp_capture.py<br/>CRC validate → seq_N.hex"]
        LOG["scripts/csv_logger.py<br/>CRC validate · onset split into reps · word labels · session CSVs + manifest"]
        PAR["services/ingest.py — parse_packet<br/>streams + pinned rates (16k / 8k)"]
        SEG["services/pipeline.py — segment_by_energy<br/>20 ms frames / 10 ms hop · −45 dB rel-peak · ≥40 ms"]
        FEAT["services/features.py<br/>13 descriptors (pressure/airflow zero-filled)"]
        INF["services/inference.py<br/>phoneme classifier — 40 ARPAbet classes (demo → ONNX)"]
        DEC["services/decoder.py<br/>CMU-dict lexicon + beam search"]
        LMR["services/lm_rerank.py<br/>sentence re-rank (swappable scorer)"]
        TTSN["services/tts.py<br/>synthesizer (silent demo → ONNX later)"]
        OUT["PipelineResult<br/>text + WAV + stage timings"]
    end

    MIC --> ACQ
    PZ --> ACQ
    ACQ --> ONG
    ONG --> CSVL --> USB --> LOG
    ONG --> PKB --> UDP --> CAP
    LOG -->|"replay: csv → packets"| PAR
    CAP --> PAR
    PAR --> SEG --> FEAT --> INF --> DEC --> LMR --> TTSN --> OUT
```

Latency budget: **< 500 ms end-to-end** (TRD 5.x) — ingest <5 ·
features <10 · inference <50 · decode <100 · LM <100 · TTS the rest.
Measured with demo backends: ~8–14 ms plumbing.

Full annotated versions with step tables, locked contracts, and
data-size notes: [`docs/DATA_FLOW_DIAGRAMS.md`](docs/DATA_FLOW_DIAGRAMS.md).

**Status:** runnable end-to-end software reference (86 tests green) +
firmware skeleton with lockstep-tested packet format, synthetic-sensor
mode, USB-serial CSV transport, and the data-collection logger. Open-device
migration **M2.1 + M2.4 done**; sensor drivers (M2.2/M2.3) await the
physical board. See [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md)
and [`docs/PLAN_2_SENSOR_OPEN_DEVICE.md`](docs/PLAN_2_SENSOR_OPEN_DEVICE.md)
for the roadmap to the >85% / <500 ms TRD targets.

## Quickstart (software only)

Python 3.11+, numpy — nothing else required:

```bash
python -m unittest discover -s tests        # 86 tests
```

Run the pipeline on a capture file (one hex packet per line; the default
`capture/demo.hex` is an **open-device 2-sensor capture**, mic + piezo):

```bash
python -m services.pipeline capture/demo.hex --backend demo --tts silent --report
```

With a trained model (later milestones), `--backend onnx` and
`--tts onnx` pick up `models/avc_phoneme.onnx` / `models/tts.onnx`
without any code changes (see `models/README.md`).

## Live capture from the board

**Data-collection phase (default transport `usb_serial`):**

```bash
python scripts/csv_logger.py --port COM5 --baud 921600 --speaker sai --prompt --out data
```

The logger validates every line (CRC16), splits windows into labeled
repetitions (gateway-side onset gate, SATHVANI §5 semantics), writes
`data/session_*/rep_*_<word>.csv` + `manifest.json`, and replays any
rep through the pipeline:

```bash
python scripts/csv_logger.py --replay data/session_*/rep_000_hello.csv
```

No hardware? `--self-test` runs the whole logger path on synthetic
windows (generate → validate → split → write → replay → pipeline).

**Binary packets (transport `udp`):**

```bash
python scripts/udp_capture.py --port 7777 --out capture
```

Each validated packet lands in `capture/seq_<n>.hex`, directly
replayable through the pipeline command above.

## Firmware

See [`firmware/README.md`](firmware/README.md) — ESP-IDF v5.x build,
wire-format spec (binary packets + CSV lines), Kconfig transport/sensor
set, and the host-runnable lockstep test (`firmware/test/test_packet.c`).
Until sensor pinouts are locked, the default build streams deterministic
synthetic data so the whole firmware→transport→Python path can be
exercised with only a board.

## Layout

| Path | Contents |
|---|---|
| `services/` | Pipeline layers 2–4 (ingest, features, inference, decoder, LM, TTS, orchestrator + CLI) |
| `tests/` | Unittest suite — stdlib + numpy only |
| `firmware/` | ESP32-S3 firmware (packet serializer, acquisition + UDP, host lockstep test) |
| `scripts/` | Gateway-side utilities (`csv_logger.py`, `udp_capture.py`) |
| `models/` | Model file conventions (tracked; weights are not) |
| `docs/` | Plans + [`DATA_FLOW_DIAGRAMS.md`](docs/DATA_FLOW_DIAGRAMS.md) (user + technical flow diagrams) |
| `AVC_TRD_*.txt` | Original requirements document (spec source) |

## Design rules

- **Zero-install core:** stdlib + numpy run everything; onnxruntime,
  torch are lazy/optional imports that fail over to demo backends.
- **One source of truth:** descriptor list, phoneme vocab (40 classes),
  and phoneme classes live in `services/schemas.py` only.
- **Python ↔ C lockstep:** the wire format is pinned and enforced both
  ways by golden-vector tests (`tests/test_ingest.py`,
  `firmware/test/test_packet.c`).
- **Swappable stages:** classifier, LM scorer, and TTS are factory-
  selected; the demo backends exist to test plumbing, never for
  accuracy claims.

## Open questions

1. ESP32-S3 board + pinout (mic + piezo drivers, M2.2/M2.3)
2. Dataset: reuse the 48-instance research set or collect new sessions
3. Protocol of the existing physical device (TRD vs. this format)

License: **Apache-2.0** (resolved) — see [`LICENSE`](LICENSE).
