"""Speech onset/offset gate — Python mirror of firmware/main/onset.c.

Ports the SATHVANI doc v1.0 §5 pre-filter with identical semantics, so
the gateway and the firmware make the same gating decisions:

    frame    = 20 ms of mic samples (320 @ 16 kHz)
    ONSET    = 50,000 raw-count RMS  -> speech
    NOISE_FLOOR = 20,000 raw-count RMS -> below this, speech ends at once
    hangover = 20 frames of grace between ONSET and FLOOR

State machine per frame:
    rms >= ONSET            active=True,  hangover reset to 20
    FLOOR <= rms < ONSET    active while hangover > 0, hangover -= 1
    rms < FLOOR             active=False, hangover = 0

Thresholds are raw-count scale (the doc's 32-bit ADC pipeline). With the
synthetic int16 generator they never fire — recalibrate after the real
ADS126x/ICS-43434 drivers land (see plan risk §5).
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import List, Optional, Sequence

import numpy as np

#: doc §5 constants — RAW 32-bit-ADC COUNTS (the SATHVANI device's own
#: scale). Keep in lockstep with firmware/main/onset.h. NOTE: these can
#: NEVER fire on int16-count data (onset 50,000 > int16 full scale
#: 32,767) — they are the canonical device-side numbers only.
ONSET_RMS = 50_000
NOISE_FLOOR = 20_000
HANGOVER_FRAMES = 20
FRAME_MS = 20

#: Provisional INT16-scale thresholds for gateway gating (the CSV/packet
#: mic channel is int16 by construction). Derived from the doc §5 RATIO
#: (onset:floor = 50,000:20,000 = 2.5) mapped onto int16 full scale:
#: floor ≈ 3.7% FS, onset ≈ 9% FS. PROVISIONAL — recalibrate against
#: real ICS-43434 amplitudes once M2.3 lands (plan risk §5).
INT16_ONSET_RMS = 3_000
INT16_NOISE_FLOOR = 1_200


@dataclass
class OnsetTracker:
    """Streaming gate over 20 ms frames — mirrors avc_onset_t.

    Thresholds default to the doc §5 raw-count values; pass
    ``onset_rms``/``noise_floor`` to gate on another scale (e.g. the
    provisional INT16_* constants for int16-count streams).
    """

    hangover: int = 0
    active: bool = False
    onset_rms: int = ONSET_RMS
    noise_floor: int = NOISE_FLOOR

    def frame(self, rms: float) -> bool:
        """Feed one frame's raw-count RMS; returns speech-active."""
        if rms >= self.onset_rms:
            self.active = True
            self.hangover = HANGOVER_FRAMES
            return True
        if rms < self.noise_floor:
            self.active = False
            self.hangover = 0
            return False
        if self.active and self.hangover > 0:
            self.hangover -= 1
            return True
        self.active = False
        return False

    def window(self, mic_int16: np.ndarray, rate_hz: int = 16_000) -> bool:
        """True if ANY 20 ms frame of the buffer is speech-active."""
        frame = int(rate_hz * FRAME_MS / 1000)
        if mic_int16.size == 0 or frame == 0:
            return False
        for i in range(0, mic_int16.size, frame):
            chunk = mic_int16[i:i + frame]
            rms = int(np.sqrt(np.mean(chunk.astype(np.int64) ** 2)))
            if self.frame(rms):
                return True
        return False


def frame_rms_raw(mic_int16: np.ndarray) -> int:
    """Raw-count RMS of a 20 ms frame (int64 accumulation, like the C)."""
    if mic_int16.size == 0:
        return 0
    return int(np.sqrt(np.mean(mic_int16.astype(np.int64) ** 2)))


def split_frames(mic_int16: np.ndarray, rate_hz: int = 16_000) -> List[np.ndarray]:
    """Cut a buffer into 20 ms frames (last one may be short)."""
    frame = int(rate_hz * FRAME_MS / 1000)
    if mic_int16.size == 0 or frame == 0:
        return []
    return [mic_int16[i:i + frame] for i in range(0, mic_int16.size, frame)]
