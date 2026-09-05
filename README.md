# AVC — Artificial Vocal Card

Silent-speech-to-speech pipeline: an ESP32-S3 reads four throat/airway
sensors, streams packets to a gateway, and a Python pipeline turns them
into phonemes → words → synthesized speech.

```
ESP32-S3 (mic 16 kHz, piezo 1 kHz, pressure 100 Hz, airflow 100 Hz)
   │  one UDP datagram per 500 ms window (pinned wire format + CRC16)
   ▼
services.pipeline: ingest → segment → 13 descriptors → classifier
                   → CMU lexicon beam search → LM re-rank → TTS
```

**Status:** Milestone 1 — runnable end-to-end software reference
(71 tests green) + firmware skeleton with lockstep-tested packet format
and synthetic-sensor mode. See [`IMPLEMENTATION_PLAN.md`](IMPLEMENTATION_PLAN.md)
for architecture, locked contracts, and the roadmap to the >85% / <500 ms
TRD targets.

## Quickstart (software only)

Python 3.11+, numpy — nothing else required:

```bash
python -m unittest discover -s tests        # 71 tests
```

Run the pipeline on a capture file (one hex packet per line):

```bash
python -m services.pipeline capture/seq_7.hex --backend demo --tts silent --report
```

With a trained model (later milestones), `--backend onnx` and
`--tts onnx` pick up `models/avc_phoneme.onnx` / `models/tts.onnx`
without any code changes (see `models/README.md`).

## Live capture from the board

```bash
python scripts/udp_capture.py --port 7777 --out capture
```

Each validated packet lands in `capture/seq_<n>.hex`, directly
replayable through the pipeline command above.

## Firmware

See [`firmware/README.md`](firmware/README.md) — ESP-IDF v5.x build,
wire-format spec, and the host-runnable lockstep test
(`firmware/test/test_packet.c`). Until sensor pinouts are locked, the
default build streams deterministic synthetic data so the whole
firmware→UDP→Python path can be exercised with only a board.

## Layout

| Path | Contents |
|---|---|
| `services/` | Pipeline layers 2–4 (ingest, features, inference, decoder, LM, TTS, orchestrator + CLI) |
| `tests/` | Unittest suite — stdlib + numpy only |
| `firmware/` | ESP32-S3 firmware (packet serializer, acquisition + UDP, host lockstep test) |
| `scripts/` | Gateway-side utilities (`udp_capture.py`) |
| `models/` | Model file conventions (tracked; weights are not) |
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

1. ESP32-S3 board + pinout for the four sensors
2. Packet protocol of the existing physical device (TRD vs. this format)
3. Dataset: reuse the 48-instance research set or collect new sessions
4. License (TBD)

## License

TBD — decision pending (tracked in `IMPLEMENTATION_PLAN.md` §8).
