/*
 * AVC speech onset/offset gate — implementation. Pure C99, no ESP-IDF
 * dependencies, so it stays host-testable next to packet.c.
 * Lockstep mirror: services/onset.py (see tests/test_onset.py).
 */
#include <math.h>

#include "onset.h"

void avc_onset_init(avc_onset_t *st)
{
    st->hangover = 0;
    st->active = 0;
}

int avc_onset_frame(avc_onset_t *st, int32_t frame_rms)
{
    if (frame_rms >= AVC_ONSET_ONSET_RMS) {
        st->active = 1;
        st->hangover = AVC_ONSET_HANGOVER;
        return 1;
    }
    if (frame_rms < AVC_ONSET_NOISE_FLOOR) {
        /* collapsed below the floor: speech ends immediately */
        st->active = 0;
        st->hangover = 0;
        return 0;
    }
    /* between floor and onset: in speech only while hangover lasts */
    if (st->active && st->hangover > 0) {
        st->hangover -= 1;
        return 1;
    }
    st->active = 0;
    return 0;
}

int32_t avc_onset_frame_rms(const int16_t *samples, size_t n)
{
    if (n == 0u || samples == NULL) {
        return 0;
    }
    int64_t acc = 0;
    for (size_t i = 0; i < n; ++i) {
        int64_t s = samples[i];
        acc += s * s;
    }
    return (int32_t)sqrt((double)acc / (double)n);
}

int avc_onset_window(const int16_t *samples, size_t n, uint32_t rate_hz)
{
    if (samples == NULL || n == 0u || rate_hz == 0u) {
        return 0;
    }
    avc_onset_t st;
    avc_onset_init(&st);

    const size_t frame = (size_t)(rate_hz * AVC_ONSET_FRAME_MS / 1000u);
    if (frame == 0u) {
        return 0;
    }
    for (size_t i = 0; i < n; i += frame) {
        size_t len = (n - i < frame) ? (n - i) : frame;
        if (avc_onset_frame(&st, avc_onset_frame_rms(samples + i, len))) {
            return 1;
        }
    }
    return 0;
}
