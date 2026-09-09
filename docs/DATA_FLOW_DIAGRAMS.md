# AVC Data Flow — Diagrams

Two views of the same pipeline: the **user's view** (what it feels like)
and the **engineer's view** (what actually happens). Current hardware
track: the **open device** — mic + piezo only
(branch `feature/2-sensor-open-device`; `master` keeps the 4-sensor
closed-device reference).

---

## 1. Normal view — user's perspective

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

**Step by step — what you experience vs. what is actually happening:**

| # | What you experience 🙂 | What is actually happening ⚙️ |
|---|---|---|
| 1 | You mouth a word — no sound needed | Mic + throat sensor record a **500 ms window** of vibrations (8,000 mic + 4,000 piezo samples) |
| 2 | A small device sits on your throat | The board checks the window is **speech, not silence** (onset gate) and stamps it with an error-check code (CRC) |
| 3 | Data streams down the cable to the laptop | One CRC-checked **CSV line per window** over USB serial (base64-encoded samples) |
| 4 | The laptop "thinks" for a moment | Software measures **13 signal descriptors** → classifies **phonemes** (sound units) → dictionary + beam search build the most likely **words** |
| 5 | The speaker says "hello" | The best sentence is re-ranked by a language model and **synthesized to audio** — target under **0.5 s** end to end |

**One-liner:** you mouth → device feels → laptop thinks → speaker speaks.

**Why two sensors?** The piezo film (on the throat) feels the *muscle and
air* movement — it carries most of the signal (~60–70% importance). The
tiny mic catches *residual sound shape*. Pressure/airflow sensors exist
only on the closed-device variant; on the open device their features are
zero-filled so the model contract stays identical.

---

## 2. Technical view — engineer's perspective

```mermaid
flowchart TB
    subgraph DEVICE["ESP32-S3 — open device (mic + piezo)"]
        MIC["ICS-43434 MEMS mic<br/>I2S @ 16 kHz → 8,000 int16 / window"]
        PZ["LDT2-028K PVDF film → OPA140 → ADS126x<br/>SPI @ 8 kSPS → 4,000 int16 / window"]
        ACQ["acquisition_task — main.c<br/>500 ms window, mic-clocked pacing"]
        ONG["onset gate — onset.c (Kconfig, optional)<br/>20 ms frames · RMS onset 50k / floor 20k · 20-frame hangover"]
        CSVL["csv_build_line — main.c<br/>AVC1 line: base64 int16 + CRC16"]
        PKB["avc_packet_build — packet.c<br/>binary: sensor_mask 0x03 + CRC16 footer"]
    end

    subgraph TRANSPORT["Transport — AVC_TRANSPORT (Kconfig)"]
        USB["USB serial @ 921600<br/>(default — doc §8)"]
        UDP["UDP datagram ≈ 24 KB<br/>(alternative)"]
    end

    subgraph GATEWAY["Gateway (PC / laptop)"]
        CAP["scripts/udp_capture.py<br/>CRC validate → seq_N.hex"]
        LOG["scripts/csv_logger.py<br/>CRC validate · onset split into reps · word labels · session CSVs + manifest"]
        PAR["services/ingest.py — parse_packet<br/>streams + pinned rates (16k / 8k)"]
        SEG["services/pipeline.py — segment_by_energy<br/>20 ms frames / 10 ms hop · −45 dB rel-peak · ≥40 ms"]
        FEAT["services/features.py<br/>13 descriptors (pressure/airflow zero-filled)"]
        INF["services/inference.py<br/>phoneme classifier — 40 ARPAbet classes (demo → ONNX)"]
        DEC["services/decoder.py<br/>CMU-dict lexicon + beam search (width 3)"]
        LMR["services/lm_rerank.py<br/>sentence re-rank (swappable scorer)"]
        TTSN["services/tts.py<br/>synthesizer (silent demo → ONNX later)"]
        OUT["PipelineResult<br/>text + WAV + stage timings"]
    end

    MIC --> ACQ
    PZ --> ACQ
    ACQ --> ONG
    ONG --> CSVL --> USB --> LOG
    ONG --> PKB --> UDP --> CAP
    LOG -->|"replay: csv → build_packet → parse_packet"| PAR
    CAP --> PAR
    PAR --> SEG --> FEAT --> INF --> DEC --> LMR --> TTSN --> OUT
```

**Stage latency budget (TRD 5.x — end-to-end target < 500 ms):**

| Stage | Budget | Module | Notes |
|---|---|---|---|
| Ingest / parse | < 5 ms | `services/ingest.py` | CRC16 + struct unpack |
| Features | < 10 ms | `services/features.py` | 13 descriptors, dialogue-normalized |
| Inference | < 50 ms | `services/inference.py` | ONNX runtime once trained (M3) |
| Decode | < 100 ms | `services/decoder.py` | lexicon + beam search |
| LM re-rank | < 100 ms | `services/lm_rerank.py` | swappable scorer |
| TTS | remainder | `services/tts.py` | will dominate the budget (M5) |
| **Measured (demo backends)** | **~8–14 ms** | whole pipeline | plumbing only — no accuracy claims |

**Locked contracts (do not change one side without the other):**

| Contract | Defined in | Value |
|---|---|---|
| Binary wire format | `ingest.py` ↔ `packet.c` | `<IIB` header · per-sensor `<H` + int16 LE · CRC16-CCITT footer |
| CSV line format | `csv_logger.py` ↔ `main.c` | `AVC1,<seq>,<ts>,<mask>,<onset>,<label>,<b64…>,<crc16>` |
| Sample rates | `ingest.py` ↔ `packet.h` | mic 16 kHz · piezo 8 kHz (ADS126x) · pressure/airflow 100 Hz |
| 13 descriptors | `schemas.py` `DESCRIPTOR_NAMES` | order is part of the (B, T, 13) model input |
| Phoneme vocab | `schemas.py` `PHONEME_VOCAB` | 40 classes (39 ARPAbet incl. HH + SIL) |
| Onset thresholds | `onset.c` ↔ `onset.py` | 50k / 20k / 20-frame hangover (raw-count scale) + provisional int16 3000/1200 |

**Data sizes:** one open-device window = 8,000 + 4,000 int16 = 24,000
samples ≈ 24.4 KB binary (or ~32.5 KB as a base64 CSV line) — fits one
UDP datagram and the 921600-baud serial line with headroom.
