# AVC System - Quick Visual Flow Guide

## 🎯 Simple 5-Layer Architecture Flow

```
┌─────────────────────────────────────────────────────────────────────┐
│                        USER SPEAKS INTENT                             │
│                    (Wears mask, attempts speech)                      │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 1: WEARABLE DEVICE (On-Mask)                                   │
│                                                                       │
│  ┌──────────┐ ┌──────────┐ ┌──────────┐ ┌──────────┐               │
│  │  PIEZO   │ │   MIC    │ │ PRESSURE │ │ AIRFLOW  │               │
│  │ Sensor   │ │ Sensor   │ │ Sensor   │ │ Sensor   │               │
│  └────┬─────┘ └────┬─────┘ └────┬─────┘ └────┬─────┘               │
│       │            │            │            │                      │
│       └────────────┼────────────┼────────────┘                      │
│                    ▼            ▼                                     │
│              ┌──────────────────────────┐                            │
│              │      ESP32-S3 MCU        │                            │
│              │  • Syncs all sensors     │                            │
│              │  • Buffers data (500ms)   │                            │
│              │  • WiFi + BLE streaming   │                            │
│              └──────────┬───────────────┘                            │
│                         │                                            │
│                         ▼                                            │
│              Encrypted UDP stream to cloud (~50KB/s)                 │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 2: CLOUD FEATURE ENGINEERING                                   │
│                                                                       │
│              ┌──────────────────────────┐                            │
│              │   AWS Kinesis/Kafka       │                            │
│              │   (Data Ingestion)        │                            │
│              └──────────┬───────────────┘                            │
│                         │                                            │
│                         ▼                                            │
│              ┌──────────────────────────┐                            │
│              │   Feature Engineering    │                            │
│              │   (13 descriptors)       │                            │
│              │                          │                            │
│              │  1. RMS Amplitude        │                            │
│              │  2. SPL (dB)             │                            │
│              │  3. Pressure (Pa)        │                            │
│              │  4. Velocity (m/s)       │                            │
│              │  5. Duration (ms)        │                            │
│              │  6-12. Ratios & Changes  │                            │
│              │  13. Phoneme Class       │                            │
│              └──────────┬───────────────┘                            │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 3: AI INFERENCE (Deep Learning)                               │
│                                                                       │
│              ┌──────────────────────────┐                            │
│              │  CNN-BiLSTM-Attention     │                            │
│              │  Neural Network          │                            │
│              │                          │                            │
│              │  Input: 13 descriptors   │                            │
│              │  ↓                        │                            │
│              │  CNN Feature Extractor   │                            │
│              │  (64→128→256 channels)   │                            │
│              │  ↓                        │                            │
│              │  BiLSTM Temporal Model    │                            │
│              │  (3 layers, bidirectional)│                            │
│              │  ↓                        │                            │
│              │  Self-Attention Mechanism  │                            │
│              │  (4 heads, context refinement)│                         │
│              │  ↓                        │                            │
│              │  Classification Head      │                            │
│              │  (Softmax over phonemes)  │                            │
│              └──────────┬───────────────┘                            │
│                         │                                            │
│                         ▼                                            │
│              Predicted phoneme sequence                             │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 4: LANGUAGE & SYNTHESIS                                       │
│                                                                       │
│              ┌──────────────────────────┐                            │
│              │  Phoneme → Word          │                            │
│              │  (CMU Dictionary +        │                            │
│              │   Edit Distance)         │                            │
│              └──────────┬───────────────┘                            │
│                         │                                            │
│                         ▼                                            │
│              ┌──────────────────────────┐                            │
│              │  Language Model          │                            │
│              │  (GPT-2 Re-ranking)      │                            │
│              │  Selects best sentence   │                            │
│              └──────────┬───────────────┘                            │
│                         │                                            │
│                         ▼                                            │
│              ┌──────────────────────────┐                            │
│              │  Text-to-Speech          │                            │
│              │  (FastSpeech2 + HiFi-GAN) │                            │
│              │  + Personal Voice        │                            │
│              └──────────┬───────────────┘                            │
│                         │                                            │
│                         ▼                                            │
│              Personalized audio waveform                            │
└────────────────────────────────┬────────────────────────────────────┘
                                 │
                                 ▼
┌─────────────────────────────────────────────────────────────────────┐
│  LAYER 5: USER OUTPUT                                                │
│                                                                       │
│              ┌──────────────────────────┐                            │
│              │  Android Companion App   │                            │
│              │  • Plays audio           │                            │
│              │  • Device management      │                            │
│              │  • Voice profiles         │                            │
│              └──────────┬───────────────┘                            │
│                         │                                            │
│                         ▼                                            │
│              ┌──────────────────────────┐                            │
│              │  CLINICIAN DASHBOARD     │                            │
│              │  • Usage analytics       │                            │
│              │  • Progress tracking     │                            │
│              │  • Calibration tools     │                            │
│              └──────────┬───────────────┘                            │
│                         │                                            │
│                         ▼                                            │
│                    USER HEARS THEIR VOICE                            │
│                    (Natural, personalized speech)                      │
└─────────────────────────────────────────────────────────────────────┘
```

## ⚡ Real-Time Timeline (Target: <500ms total)

```
0ms    ──► User speaks (breath + vocal intent)
10ms   ──► Sensors capture physiological signals
50ms   ──► ESP32-S3 processes and buffers data
100ms  ──► Encrypted data transmitted to cloud
150ms  ──► Cloud receives and parses data
250ms  ──► Feature engineering completes (13 descriptors)
350ms  ──► AI model predicts phonemes
400ms  ──► Phonemes converted to words
420ms  ──► Language model selects best sentence
450ms  ──► Text-to-speech generates audio
480ms  ──► Personalized voice applied
500ms  ──► Audio transmitted to phone
520ms  ──► USER HEARS THEIR NATURAL VOICE
```

## 🔧 Technology Stack Summary

### Hardware (Device)
- **Sensors**: PVDF Piezo, MEMS Mic, Pressure, Airflow
- **Processor**: ESP32-S3 (dual-core 240MHz)
- **Connectivity**: WiFi 4 + BLE 5.0
- **Power**: Li-Po 2000mAh (6hr battery life)

### Cloud (AWS)
- **Ingestion**: Kinesis/Kafka
- **Processing**: Lambda (feature engineering)
- **AI/ML**: ECS GPU (PyTorch/ONNX)
- **Database**: PostgreSQL + Redis
- **Storage**: S3 (encrypted)
- **Monitoring**: Prometheus + Grafana

### AI Models
- **Phoneme Recognition**: CNN-BiLSTM-Attention (5.5M params)
- **Language Model**: GPT-2 / DistilGPT-2
- **Speech Synthesis**: FastSpeech2 + HiFi-GAN
- **Voice Personalization**: d-vector/x-vector embeddings

### Software
- **Mobile App**: Android (Kotlin/Java)
- **Firmware**: FreeRTOS + ESP-IDF
- **Backend**: Python + FastAPI
- **Security**: OAuth 2.0 + JWT, TLS 1.3, AES-256

## 🎯 Key Performance Indicators

| Metric | Target | Current Status |
|--------|--------|----------------|
| End-to-end Latency | <500ms | Prototype testing |
| Phoneme Accuracy | >85% | Research data: 100% |
| Speech Naturalness | MOS ≥3.5/5.0 | TTS development |
| Device Battery | ≥6 hours | Hardware validation |
| Cloud Uptime | ≥99.9% | Infrastructure setup |
| Patient Comfort | ≥4/5 rating | Ergonomic design |

## 🚀 Implementation Phases

**Phase 1** (2025): ✅ Foundation & Data Collection
- Hardware design finalized
- Baseline AI model trained
- Provisional patent filed

**Phase 2** (Q1-Q2 2026): 🔨 Prototype & AI v1
- First wearable prototype
- Android companion app
- AI model v1 (>80% accuracy)

**Phase 3** (Q3-Q4 2026): 🏥 Clinical Validation
- 10-patient pilot at AIIMS
- Real patient data collection
- AI model v2 (>85% accuracy)

**Phase 4** (2027-2028): 📈 Commercial Launch
- Regulatory approvals (CDSCO/FDA)
- Manufacturing scale-up
- Market entry (2,300+ devices)

## 🔒 Security & Compliance

- **Data Encryption**: TLS 1.3 (transit), AES-256 (rest)
- **Authentication**: OAuth 2.0 + JWT
- **Compliance**: HIPAA (US), DPDP Act (India), GDPR (EU)
- **Medical Standards**: ISO 13485, IEC 62304
- **Audit Trail**: 7-year immutable logging

---

**This complete system enables voiceless individuals to speak naturally in real-time using advanced AI and physiological signal processing.**