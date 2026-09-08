/*
 * AVC speech onset/offset gate — port of the SATHVANI doc v1.0 §5
 * pre-filter (Python mirror: services/onset.py — decisions must stay
 * in lockstep; golden scenarios live in tests/test_onset.py).
 *
 * Semantics (pinned; identical in Python):
 *   frame  = 20 ms of mic samples (320 @ 16 kHz)
 *   ONSET  = 50,000  raw-count RMS -> speech
 *   FLOOR  = 20,000  raw-count RMS -> below this, speech ends at once
 *   hangover = 20 frames of grace between ONSET and FLOOR
 *
 * State machine per frame:
 *   rms >= ONSET          speech=1, hangover reset to 20
 *   FLOOR <= rms < ONSET  speech = (hangover > 0), hangover--
 *   rms < FLOOR           speech=0, hangover=0
 *
 * Thresholds are RAW-COUNT scale (doc's 32-bit ADC pipeline). With the
 * synthetic int16 generator they will never fire; recalibrate after the
 * real ADS126x/ICS-43434 drivers land (M2.2/M2.3) — see plan risk §5.
 */
#ifndef AVC_ONSET_H
#define AVC_ONSET_H

#include <stddef.h>
#include <stdint.h>

#define AVC_ONSET_NOISE_FLOOR 20000
#define AVC_ONSET_ONSET_RMS  50000
#define AVC_ONSET_HANGOVER   20
#define AVC_ONSET_FRAME_MS   20u

/* Provisional int16-scale thresholds for the gateway-side gate over
 * CSV/packet mic data (max int16 RMS is 32767, so the doc's raw-count
 * onset 50000 can never fire there). Same doc §5 ratio (2.5): floor
 * ~3.7% FS, onset ~9% FS. Recalibrate after real drivers land. */
#define AVC_ONSET_INT16_NOISE_FLOOR 1200
#define AVC_ONSET_INT16_ONSET_RMS   3000

typedef struct {
    int      hangover;   /* grace frames remaining */
    uint8_t  active;     /* 1 while speech (incl. hangover) */
} avc_onset_t;

/* Reset the tracker (session start). */
void avc_onset_init(avc_onset_t *st);

/* Feed one frame's RMS (raw counts). Returns 1 if the frame is
 * speech-active (onset or hangover), 0 if silent. */
int avc_onset_frame(avc_onset_t *st, int32_t frame_rms);

/* RMS of one int16 frame in raw counts (int64 internally — a 20 ms
 * mic frame's sum of squares overflows int32). */
int32_t avc_onset_frame_rms(const int16_t *samples, size_t n);

/* True if ANY 20 ms frame of the window is speech-active. Windows are
 * 500 ms by contract, but any buffer length is accepted. */
int avc_onset_window(const int16_t *samples, size_t n, uint32_t rate_hz);

#endif /* AVC_ONSET_H */
