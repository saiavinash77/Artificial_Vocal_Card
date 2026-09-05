# Model file conventions

Backends resolve models from this directory by fixed name
(see `services/inference.py` and `services/tts.py`):

| File | Backend | Purpose |
|---|---|---|
| `avc_phoneme.onnx` | `--backend onnx` | Phoneme classifier, input (B, T, 13) float32, output (B, T, 40) logits over `PHONEME_VOCAB` |
| `tts.onnx` | `--tts onnx` | Optional vocoder/synthesizer (M5+) |

Rules:

- Weights are **not committed** (`.gitignore` covers `*.onnx`, `*.pt`,
  `*.tflite`); distribute via release artifacts instead.
- Any exported head MUST use `services/schemas.py` `PHONEME_VOCAB`
  (40 classes, SIL last) — run `tests/test_inference.py` after export.
- Training topology lives in `services/model_torch.py` (TRD 5.1
  CNN-BiLSTM-Attention; import only where torch is installed).
  `python -m services.model_torch` runs a no-data smoke check that
  prints the parameter count (~5.5M) and a forward-pass shape.
