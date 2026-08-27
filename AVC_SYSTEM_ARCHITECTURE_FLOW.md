# AVC (Artificial Vocal Cord) - Complete System Architecture Flow

## High-Level System Overview

```
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    AVC SYSTEM - END-TO-END DATA FLOW                              │
└─────────────────────────────────────────────────────────────────────────────────┘

                    USER WEARS MASK → SPEAKS INTENT
                              │
                              ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                          LAYER 1: WEARABLE EDGE DEVICE                            │
│                         (On-Mask Processing & Streaming)                          │
└─────────────────────────────────────────────────────────────────────────────────┘

┌────────────────┐  ┌────────────────┐  ┌────────────────┐  ┌────────────────┐
│   PVDF PIEZO   │  │   MEMS MIC     │  │ PRESSURE SENSOR│  │ AIRFLOW SENSOR  │
│   (Vibration)  │  │   (Acoustic)   │  │   (Breath)     │  │   (Velocity)   │
│                │  │                │  │                │  │                │
│  32-bit ADC    │  │   I2S @16kHz   │  │   I2C @100Hz   │  │   I2C @100Hz   │
│  @1kHz         │  │   16-bit       │  │   ±2kPa range  │  │   0-30 m/s     │
└────────┬───────┘  └────────┬───────┘  └────────┬───────┘  └────────┬───────┘
         │                   │                   │                   │
         └───────────────────┼───────────────────┼───────────────────┘
                             │                   │
                             ▼                   ▼
                    ┌─────────────────────────────────────────┐
                    │           ESP32-S3 MICROCONTROLLER        │
                    │  (Xtensa LX7 Dual-Core @240MHz)          │
                    │                                         │
                    │  • 512KB SRAM + 8MB PSRAM               │
                    │  • Multi-sensor synchronization          │
                    │  • 500ms ring buffer                    │
                    │  • Timestamp alignment                  │
                    │  • WiFi 4 + BLE 5.0 connectivity        │
                    └─────────────────┬───────────────────────┘
                                      │
                                      ▼
                    ┌─────────────────────────────────────────┐
                    │          DATA STREAMING PROTOCOL        │
                    │                                         │
                    │  • UDP over WiFi (low latency)          │
                    │  • DTLS 1.3 encryption                  │
                    │  • ~50 KB/s aggregate rate             │
                    │  • Application-layer reliability       │
                    │  • CRC16 error detection               │
                    └─────────────────┬───────────────────────┘
                                      │
                                      ▼
                              CLOUD INFRASTRUCTURE
                              (AWS / Cloud Platform)
                                      │
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    LAYER 2: CLOUD SIGNAL & FEATURE ENGINEERING                   │
│                       (Data Ingestion & Feature Extraction)                        │
└─────────────────────────────────────────────────────────────────────────────────┘

                    ┌─────────────────────────────────────────┐
                    │         AWS KINESIS / APACHE KAFKA       │
                    │         (Real-time Stream Ingestion)     │
                    │                                         │
                    │  • Protobuf packet parsing              │
                    │  • Sequence number validation            │
                    │  • Sensor frame de-interleaving         │
                    │  • Timestamp synchronization            │
                    └─────────────────┬───────────────────────┘
                                      │
                                      ▼
                    ┌─────────────────────────────────────────┐
                    │      FEATURE ENGINEERING PIPELINE         │
                    │          (AWS Lambda / Python)           │
                    │                                         │
                    │  INPUT: Raw sensor streams               │
                    │  OUTPUT: 13-descriptor feature vector    │
                    │                                         │
                    │  ┌───────────────────────────────────┐  │
                    │  │  13 ENGINEERED DESCRIPTORS:       │  │
                    │  │                                   │  │
                    │  │  1. RMS Amplitude                 │  │
                    │  │  2. SPL (dB)                      │  │
                    │  │  3. Pressure (Pa)                 │  │
                    │  │  4. Velocity (m/s)                │  │
                    │  │  5. Duration (ms)                 │  │
                    │  │  6. Energy Ratio                  │  │
                    │  │  7. Duration Norm                 │  │
                    │  │  8. SPL/Velocity Ratio             │  │
                    │  │  9. Pressure/RMS Ratio            │  │
                    │  │  10. ΔRMS (temporal change)       │  │
                    │  │  11. ΔSPL (temporal change)       │  │
                    │  │  12. ΔPressure (temporal change)  │  │
                    │  │  13. Phoneme-Class (categorical)  │  │
                    │  └───────────────────────────────────┘  │
                    │                                         │
                    │  • Z-score / Min-Max normalization      │
                    │  • <100ms processing latency             │
                    │  • Per-phoneme window computation        │
                    └─────────────────┬───────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    LAYER 3: CLOUD INFERENCE (AI/ML PIPELINE)                      │
│                   (Deep Learning Phoneme Reconstruction)                           │
└─────────────────────────────────────────────────────────────────────────────────┘

                    ┌─────────────────────────────────────────┐
                    │       CNN-BiLSTM-ATTENTION MODEL         │
                    │       (~5.5M trainable parameters)      │
                    │                                         │
                    │  INPUT: (Batch, Time, 13 descriptors)    │
                    │  OUTPUT: Phoneme probability distribution │
                    └─────────────────┬───────────────────────┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          │                           │                           │
          ▼                           ▼                           ▼
┌─────────────────────┐   ┌─────────────────────┐   ┌─────────────────────┐
│   INPUT PROJECTION  │   │   CNN FEATURE       │   │   BILSTM TEMPORAL   │
│   Linear(13→32)     │   │   EXTRACTOR         │   │   MODELING          │
│   + GELU + LayerNorm│   │   3× Conv1d blocks  │   │   3× BiLSTM layers  │
│   ~448 params       │   │   64→128→256 ch     │   │   256 hidden units  │
│                     │   │   k=3, BN+GELU+Drop │   │   bidirectional     │
│   (B,T,13) → (B,T,32)│   │   ~129K params      │   │   ~4.2M params      │
└─────────┬───────────┘   └─────────┬───────────┘   └─────────┬───────────┘
          │                         │                         │
          └─────────────────────────┼─────────────────────────┘
                                    │
                                    ▼
                    ┌─────────────────────────────────────────┐
                    │     SELF-ATTENTION MECHANISM             │
                    │                                         │
                    │  • 4 Multi-Head Attention (d_k=128)      │
                    │  • Residual connections                  │
                    │  • Layer Normalization                   │
                    │  • ~1.05M parameters                      │
                    │                                         │
                    │  PURPOSE: Contextual refinement of       │
                    │           latent representations         │
                    └─────────────────┬───────────────────────┘
                                      │
                                      ▼
                    ┌─────────────────────────────────────────┐
                    │      CLASSIFICATION HEAD                 │
                    │                                         │
                    │  • LayerNorm + FC(512→256) + GELU        │
                    │  • Dropout + FC(256→C) where C = classes │
                    │  • Softmax activation                    │
                    │  • Label smoothing (ε=0.1)               │
                    │  • ~132K parameters                      │
                    │                                         │
                    │  OUTPUT: Phoneme probabilities per       │
                    │          time step                        │
                    └─────────────────┬───────────────────────┘
                                      │
                                      ▼
                    ┌─────────────────────────────────────────┐
                    │      MODEL TRAINING CONFIGURATION         │
                    │                                         │
                    │  • Optimizer: AdamW (LR=2e-3, WD=1e-4)   │
                    │  • Loss: Weighted Cross-Entropy          │
                    │  • Augmentation: Gaussian noise (40x)     │
                    │  • Regularization: Dropout(0.2), BN, LN   │
                    │  • Hardware: NVIDIA A10G/T4 GPU          │
                    │  • Framework: PyTorch 2.x / ONNX Runtime │
                    └─────────────────┬───────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    LAYER 4: LANGUAGE & SYNTHESIS LAYER                            │
│              (Phoneme-to-Word Conversion & Speech Synthesis)                       │
└─────────────────────────────────────────────────────────────────────────────────┘

                    ┌─────────────────────────────────────────┐
                    │        PHONEME-TO-WORD RECONSTRUCTION    │
                    │                                         │
                    │  INPUT: Predicted phoneme sequence       │
                    │  OUTPUT: Candidate word sequences        │
                    └─────────────────┬───────────────────────┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          │                           │                           │
          ▼                           ▼                           ▼
┌─────────────────────┐   ┌─────────────────────┐   ┌─────────────────────┐
│   CMU DICTIONARY     │   │   EDIT-DISTANCE      │   │   BEAM SEARCH       │
│   ALIGNMENT          │   │   RECOVERY          │   │   (width=3)         │
│                     │   │                     │   │                     │
│ • Dynamic programming│   │ • Levenshtein      │   │ • Top-k candidates   │
│ • Optimal segmentation│   │   distance         │   │ • Sequence compose   │
│ • Phonetic matching  │   │ • Approximate match  │   │ • Pruning by score   │
└─────────┬───────────┘   └─────────┬───────────┘   └─────────┬───────────┘
          │                         │                         │
          └─────────────────────────┼─────────────────────────┘
                                    │
                                    ▼
                    ┌─────────────────────────────────────────┐
                    │      LANGUAGE MODEL RE-RANKING           │
                    │                                         │
                    │  MODEL: GPT-2 / DistilGPT-2 / Indic-LM   │
                    │  (HuggingFace Transformers)             │
                    │                                         │
                    │  PROCESS:                                │
                    │  • Score = -Loss_LM(sentence)            │
                    │  • Lower perplexity = higher plausibility│
                    │  • Top-1 candidate selection             │
                    │                                         │
                    │  PURPOSE: Ensure linguistic plausibility  │
                    └─────────────────┬───────────────────────┘
                                      │
                                      ▼
                    ┌─────────────────────────────────────────┐
                    │      TEXT-TO-SPEECH SYNTHESIS            │
                    │                                         │
                    │  INPUT: Selected word sequence            │
                    │  OUTPUT: Personalized audio waveform      │
                    └─────────────────┬───────────────────────┘
                                      │
          ┌───────────────────────────┼───────────────────────────┐
          │                           │                           │
          ▼                           ▼                           ▼
┌─────────────────────┐   ┌─────────────────────┐   ┌─────────────────────┐
│   FASTSPEECH2       │   │   HIFI-GAN          │   │   SPEAKER           │
│   (Acoustic Model)  │   │   (Vocoder)         │   │   EMBEDDING         │
│                     │   │                     │   │                     │
│ • Non-autoregressive│   │ • GAN-based         │   │ • d-vector/x-vector  │
│ • Parallel mel-gen  │   │ • Waveform synth    │   │ • 256-dim vector    │
│ • Controllable prosody│   │ • Real-time capable │   │ • 5-min sample      │
│ • <100ms latency    │   │ • Near-human quality│   │ • Personalization   │
└─────────┬───────────┘   └─────────┬───────────┘   └─────────┬───────────┘
          │                         │                         │
          └─────────────────────────┼─────────────────────────┘
                                    │
                                    ▼
                    ┌─────────────────────────────────────────┐
                    │      FINAL AUDIO OUTPUT                  │
                    │                                         │
                    │  • Format: 16-bit PCM WAV                │
                    │  • Sample rate: 22.05 kHz                │
                    │  • Quality: MOS ≥3.5/5.0                 │
                    │  • Latency: <200ms per utterance         │
                    │  • Runtime: ONNX Runtime / TensorRT      │
                    └─────────────────┬───────────────────────┘
                                      │
                                      ▼
┌─────────────────────────────────────────────────────────────────────────────────┐
│                    LAYER 5: OUTPUT & USER EXPERIENCE                              │
│                      (Mobile App & Clinician Dashboard)                           │
└─────────────────────────────────────────────────────────────────────────────────┘

                    ┌─────────────────────────────────────────┐
                    │      ANDROID COMPANION APP               │
                    │                                         │
                    │  CORE FEATURES:                          │
                    │  • Real-time audio playback              │
                    │  • BLE 5.0 device pairing                │
                    │  • Voice profile management              │
                    │  • Device calibration                    │
                    │  • OTA firmware updates                  │
                    │  • Offline phrase cache (50 phrases)     │
                    │                                         │
                    │  PERFORMANCE:                            │
                    │  • End-to-end latency: <500ms            │
                    │  • Multi-language support (Hindi, Telugu)│
                    │  • UI/UX: Kotlin/Java native             │
                    └─────────────────┬───────────────────────┘
                                      │
                                      ▼
                    ┌─────────────────────────────────────────┐
                    │      CLINICIAN DASHBOARD                 │
                    │                                         │
                    │  ANALYTICS & MONITORING:                  │
                    │  • Daily usage time tracking              │
                    │  • Phoneme accuracy trends               │
                    │  • Error log analysis                    │
                    │  • Patient progress monitoring           │
                    │  • Calibration tools                     │
                    │                                         │
                    │  COMPLIANCE:                             │
                    │  • HIPAA/GDPR compliant data handling     │
                    │  • Anonymized research data export        │
                    │  • Role-based access control             │
                    └─────────────────��───────────────────────┘

## Complete Data Flow Timeline

```
USER SPEAKING INTENT → SENSOR CAPTURE (0-10ms)
                         ↓
        ESP32-S3 PROCESSING & BUFFERING (10-50ms)
                         ↓
        WiFi UDP STREAMING WITH ENCRYPTION (50-100ms)
                         ↓
        CLOUD INGESTION & PACKET PROCESSING (100-150ms)
                         ↓
        FEATURE ENGINEERING (13 descriptors) (150-250ms)
                         ↓
        CNN-BiLSTM-ATTENTION INFERENCE (250-350ms)
                         ↓
        PHONEME-TO-WORD RECONSTRUCTION (350-400ms)
                         ↓
        LANGUAGE MODEL RE-RANKING (400-420ms)
                         ↓
        FASTSPEECH2 + HIFI-GAN SYNTHESIS (420-450ms)
                         ↓
        PERSONALIZED VOICE APPLICATION (450-480ms)
                         ↓
        AUDIO TRANSMISSION TO MOBILE APP (480-500ms)
                         ↓
        SPEECH OUTPUT TO USER (END-TO-END: <500ms)
```

## Infrastructure & Technology Stack

### Cloud Infrastructure (AWS)
```
┌─────────────────────────────────────────────────────────────┐
│                     AWS CLOUD ARCHITECTURE                     │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │ API GATEWAY  │  │  KINESIS/    │  │   LAMBDA     │       │
│  │ 10K req/day  │→ │  KAFKA       │→ │  Feature     │       │
│  │ Rate limiting│  │  Ingestion   │  │  Engineering │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
│         │                                    │                │
│         ▼                                    ▼                │
│  ┌──────────────┐                    ┌──────────────┐        │
│  │  ECS GPU     │←──────────────────│  ECS CPU     │        │
│  │  A10G/T4     │   Model Inference │  GPT-2       │        │
│  │  PyTorch/    │                   │  Scoring     │        │
│  │  ONNX        │                   └──────────────┘        │
│  └──────────────┘                                             │
│         │                                                      │
│         ▼                                                      │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │  POSTGRESQL  │  │    REDIS     │  │     S3       │       │
│  │  User data   │  │   Cache      │  │  Audio/Models│       │
│  │  100GB       │  │   Session    │  │  5TB encrypted│       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
│                                                               │
│  ┌──────────────┐  ┌──────────────┐  ┌──────────────┐       │
│  │ PROMETHEUS   │  │   GRAFANA    │  │ CLOUDWATCH   │       │
│  │  Metrics     │  │  Dashboards  │  │  Monitoring  │       │
│  └──────────────┘  └──────────────┘  └──────────────┘       │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

### Security & Compliance Layers
```
┌─────────────────────────────────────────────────────────────┐
│                   SECURITY ARCHITECTURE                       │
├─────────────────────────────────────────────────────────────┤
│                                                               │
│  DEVICE → CLOUD: DTLS 1.3 / AES-256-GCM encryption          │
│  APP → CLOUD: TLS 1.3 encryption                             │
│  DATA AT REST: AES-256-GCM                                   │
│  AUTHENTICATION: OAuth 2.0 + JWT (RS256)                     │
│  AUTHORIZATION: RBAC (Patient/Clinician/Admin/Researcher)   │
│  AUDIT LOGGING: Immutable WORM storage (7 years)             │
│  ANONYMIZATION: k-anonymity + differential privacy           │
│                                                               │
│  COMPLIANCE:                                                  │
│  • India: DPDP Act 2023                                      │
│  • US: HIPAA                                                 │
│  • EU: GDPR                                                  │
│  • Medical: ISO 13485 + IEC 62304                            │
│                                                               │
└─────────────────────────────────────────────────────────────┘
```

## Key Performance Metrics

### Latency Budget Breakdown
- **Sensor Processing**: 0-50ms
- **Network Transmission**: 50-100ms  
- **Cloud Processing**: 100-400ms
- **TTS Synthesis**: 400-450ms
- **Audio Delivery**: 450-500ms
- **Total End-to-End**: <500ms (P0), <300ms (P1 stretch goal)

### Accuracy Targets
- **Phoneme Reconstruction**: >85% on real patient data (P0), >90% (P1)
- **Speech Naturalness**: MOS ≥3.5/5.0
- **Device Uptime**: ≥99.5% during 6-hour sessions
- **Cloud Availability**: ≥99.9% SLA

### Scalability Requirements
- **Year 1**: 2,300+ concurrent devices
- **Cloud Throughput**: ≥100 requests/second per inference node
- **Data Volume**: ~50 KB/s per device × 2,300 devices = ~115 MB/s aggregate

## Future Edge Computing Roadmap

### Phase 2 (2027): Partial Edge Inference
```
ESP32-S3 QUANTIZATION:
• CNN-BiLSTM → INT8 quantization
• ONNX Runtime + TFLite Micro
• Top-3 phoneme candidates on edge
• Full refinement in cloud
• Model size: ≤2MB
• Benefit: Reduced cloud dependency
```

### Phase 3 (2028): Full Edge Inference
```
ADVANCED EDGE HARDWARE:
• ESP32-P4 or ARM Cortex-M55 + Ethos-U55 NPU
• Full phoneme classification on edge
• <100ms latency for 50 common phrases
• Cloud reserved for personalization & analytics
• Benefit: Near-instant response, offline capability
```

## Critical Success Factors

1. **Sensor Signal Quality**: Validation on real patients is critical for AI model reliability
2. **AI Model Generalization**: Must achieve >85% accuracy on diverse patient data
3. **Clinical Validation**: 10-patient pilot at AIIMS gates regulatory submissions
4. **Regulatory Approval**: CDSCO license (India) and FDA clearance (US) for market entry
5. **Manufacturing Scale**: Cost optimization to achieve ₹65K retail price (₹25K BOM)

This architecture provides a complete foundation for restoring natural speech to voiceless individuals through advanced AI and edge-cloud computing.