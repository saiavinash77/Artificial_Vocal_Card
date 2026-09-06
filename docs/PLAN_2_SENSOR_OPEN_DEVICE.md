# AVC Open Device — 2-Sensor Migration Plan (Mic + Piezo)

Branch: `feature/2-sensor-open-device` · Plan date: 06 Sep 2026
Source of truth: `AVC_Open_Device_Technical_Document.docx` (SATHVANI, v1.0, 05 Sep 2026)

---

## 0. Why this change

The **Open Device** (current hardware track) uses exactly **two sensors**:

| Sensor | Part | Interface | Role |
|---|---|---|---|
| PVDF piezo film | TE **LDT2-028K** | OPA140 charge amp → 7SEMI **ADS126x** 32-bit ADC → SPI | Dominant signal (60–70% SHAP importance) |
| MEMS mic | **ICS-43434** | I2S (16 kHz) | Secondary (spl_db, d_spl_dB) |

Removed from open device: IMU (never soldered), temp/humidity, and **pressure + airflow
(XGZP6897D / FS3000 — closed-device variant only)**. The existing 4-sensor pipeline stays
on `master` as the closed-device reference; this branch strips it to the open configuration.

**No contract break:** the wire format (`<IIB>` header + per-sensor `<H>` count + int16 LE
+ CRC16, ascending bit order) is unchanged. A 2-sensor packet is simply a packet with
`sensor_mask = 0x03`. Missing sensors yield empty windows and zero-filled descriptors —
already the tested behavior.

---

## 1. What changes, layer by layer

### Layer 1 — Packet / ingest (`services/ingest.py`, `firmware/main/packet.{h,c}`)

1. `packet.h`: `AVC_SENSOR_MIC|PIEZO` unchanged; `AVC_PRESSURE_RATE_HZ`,
   `AVC_AIRFLOW_RATE_HZ`, `AVC_PRESSURE_WINDOW_MAX`, `AVC_AIRFLOW_WINDOW_MAX` stay
   (closed-device compat) — **only** `AVC_PIEZO_RATE_HZ` changes: `1000 → 8000`
   (ADS126x 8 kSPS, doc §7; board jumpers OFF = 8 kSPS).
   `AVC_PIEZO_WINDOW_MAX`: `500 → 4000` (8 kHz × 0.5 s).
2. `ingest.py` `_SENSOR_BITS` rate for piezo: `1_000 → 8_000` (mirror; pin both sides
   in one commit so golden vectors regenerate together).
3. `windows_from_packets` already handles absent `pressure`/`airflow` streams — verify
   with new tests, no code change expected.
4. Regenerate golden vectors that embed piezo counts (VEC_ALL) via the Python side
   recipe in `firmware/README.md`.

### Layer 2 — Features (`services/features.py`)

* `base_descriptors`: `pressure_pa` and `velocity_ms` = 0.0 for every window
  (empty arrays) — matches doc SHAP: those features score 0.0000 on real data anyway.
* `spl_vel_ratio` and `pressure_rms_ratio` become degenerate ratios
  (SPL/eps≈SPL·1e9 / 0/eps=0). **Decision needed** (see §4-Q1): keep the 13-vector
  shape (zeros) vs. drop to an 11-vector. Plan default: **keep 13, zero-fill** —
  model input contract stays identical, closed-device upgrade is a non-event.
* Segmentation: mic energy stays the trigger (as implemented); piezo RMS can be added
  as an OR-trigger later (doc's RMS thresholds are firmware-side).

### Layer 2.5 — Firmware drivers (the real work; `firmware/main/main.c`)

Replace synthetic stubs with real drivers per doc §4:

| Driver | Pins (doc §4.2–4.4) | Notes |
|---|---|---|
| **ADS126x SPI (piezo)** | SCK=11, MOSI=13, MISO=12, CS=10, DRDY=9, RST=8, START=7, VCC=3V3, 5V=5V, AIN0=PIEZO_ADC, AIN1=GND | DRDY-triggered reads; 32-bit samples scaled to int16 |
| **ICS-43434 I2S (mic)** | BCLK=5, DOUT=6, LRCL=4, SEL=GND, VDD=3V3 | ICS-43434 outputs 24-bit in 32-bit frame; take top 16 bits |
| Transport | **USB serial 921600 → Python CSV logger** (doc §8 MVP), keep UDP as alternative | Doc pins CSV for the data-collection phase |

Also port the doc's speech onset/offset detector into firmware (noise floor 20,000 RMS;
onset 50,000 RMS; 20-frame hangover) as a Kconfig-selectable pre-filter.

New Kconfig: `AVC_SENSOR_SET` = `open2` (default) | `closed4`; `AVC_TRANSPORT` =
`usb_serial` (default) | `udp`.

### Layer 3 — AI pipeline

* Doc §9 uses a **Hybrid CTC + Attention** recognizer; our current head is the TRD's
  CNN-BiLSTM-Attention. **Not blocking M2** — training architecture is a training-time
  choice; the 13-descriptor input stays.
* SHAP-informed sanity: rms_amplitude + d_rms (piezo) and spl_dB + d_spl_dB (mic)
  must dominate after training; if not, suspect driver scaling.

### Layer 4 — TTS / delivery

* Unchanged for M2 (synthesis is M5). Doc's FastSpeech2 + HiFi-GAN matches our plan;
  Firebase/mobile is production-later.

---

## 2. What does NOT change

* Wire format, CRC16, ascending-bit block order, 500 ms windows.
* 13-descriptor vector order (LOCKED in `schemas.py`).
* 40-class phoneme vocab, decoder, LM re-rank, TTS interface.
* `master` branch (closed-device 4-sensor reference stays intact).

## 3. Milestones on this branch

| ID | Deliverable | Acceptance |
|---|---|---|
| **M2.1** | Python: piezo rate 8 kHz + zero-fill tests; CLI demo emits 2-sensor packets | all tests green; `capture/demo.hex` regenerated as mic+piezo only |
| **M2.2** | Firmware: ADS126x SPI driver (piezo) | reads DRDY interrupts at 8 kSPS on DevKitC; CRC-valid packets on USB |
| **M2.3** | Firmware: ICS-43434 I2S driver (mic, 16 kHz) | mic + piezo in ONE 500 ms packet (dual-core split per doc §7) |
| **M2.4** | `scripts/csv_logger.py` (921600 baud, onset-gated, word-label prompts) | logged CSV parses into packets; dataset-ready columns |
| **M2.5** | End-to-end: board → CSV/packets → pipeline → text | demo run with real board data on the desk |

Blocked-until-hardware: M2.2–M2.3 need the DevKitC + ADS126x module wired per doc §4.3
(your friend's board — status says mic driver done, ADS126x pending).

## 4. Open questions for the user

1. **Feature vector shape** — keep 13 descriptors (zero-fill pressure/airflow) or trim
   to 11 for the open device? *(Plan default: keep 13.)*
2. **Golden vectors** — regenerate VEC_ALL with 8 kHz piezo now, or keep 1 kHz legacy
   vectors as closed-device fixtures in a separate file? *(Default: regenerate + move
   old ones to `firmware/test/vectors_closed.h`.)*
3. **Transport priority** — doc says USB serial CSV for the data-collection phase;
   our firmware currently speaks UDP. Build CSV first (aligns with dataset milestone)
   or keep UDP and add CSV later? *(Default: CSV first on this branch.)*
4. **License for the repo** — still unanswered; needed before outside contributors /
   dataset sharing. MIT vs Apache-2.0?

## 5. Risk notes

* ADS126x 32-bit → int16 scaling: piezo is the dominant feature; wrong scale destroys
  the model. Mitigation: log raw 32-bit counts in CSV during M2.4, decide scaling
  after looking at real amplitudes.
* OPA140 fixes pending on the board side (bias to +5V, TVS restore, doc §10) — piezo
  signal may be noisy until then; synthetic mode remains the fallback.
* ICMR ethics approval gates human data collection (doc §11) — does not block code.
