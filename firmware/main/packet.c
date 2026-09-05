/*
 * AVC packet serializer — byte-for-byte mirror of services/ingest.py.
 *
 * Both implementations are kept in lockstep by firmware/test/test_packet.c,
 * which replays Python-generated golden vectors (hex + expected CRC) and
 * requires an exact byte match. If you change either side, regenerate the
 * vectors from the Python side (services/ingest.py build_packet) and update
 * the embedded copies together.
 */
#include "packet.h"

/* ---- CRC16-CCITT (poly 0x1021, init 0xFFFF) ------------------------- */

uint16_t avc_crc16_update(uint16_t crc, const uint8_t *data, size_t len)
{
    for (size_t i = 0; i < len; ++i) {
        crc ^= (uint16_t)(data[i] << 8);
        for (int b = 0; b < 8; ++b) {
            if (crc & 0x8000u) {
                crc = (uint16_t)((crc << 1) ^ 0x1021u);
            } else {
                crc = (uint16_t)(crc << 1);
            }
        }
    }
    return crc;
}

uint16_t avc_crc16(const uint8_t *data, size_t len)
{
    return avc_crc16_update(0xFFFFu, data, len);
}

/* ---- little-endian writers (ESP32-S3 is little-endian, but be explicit) */

static void put_u16le(uint8_t *p, uint16_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)(v >> 8);
}

static void put_u32le(uint8_t *p, uint32_t v)
{
    p[0] = (uint8_t)(v & 0xFFu);
    p[1] = (uint8_t)((v >> 8) & 0xFFu);
    p[2] = (uint8_t)((v >> 16) & 0xFFu);
    p[3] = (uint8_t)((v >> 24) & 0xFFu);
}

static void put_i16le(uint8_t *p, int16_t v)
{
    put_u16le(p, (uint16_t)(int32_t)v);
}

/* ---- packet builder ----------------------------------------------- */

size_t avc_packet_build(uint32_t seq_no,
                        uint32_t timestamp_ms,
                        const avc_sensor_block_t *blocks,
                        uint8_t n_blocks,
                        uint8_t *out,
                        size_t cap)
{
    if (out == NULL || blocks == NULL || n_blocks == 0 || n_blocks > 4) {
        return 0;
    }
    /* blocks must be single-bit flags in strictly ascending bit order
     * (the same order in which services/ingest.py writes the payload) */
    for (uint8_t i = 0; i < n_blocks; ++i) {
        if (blocks[i].sensor_bit == 0 ||
            (blocks[i].sensor_bit & (blocks[i].sensor_bit - 1)) != 0) {
            return 0; /* not a single-bit flag */
        }
        if (i > 0 && blocks[i].sensor_bit <= blocks[i - 1].sensor_bit) {
            return 0;
        }
    }

    size_t need = AVC_PACKET_HEADER_SIZE + AVC_PACKET_FOOTER_SIZE +
                  (size_t)n_blocks * 2u;
    for (uint8_t i = 0; i < n_blocks; ++i) {
        need += (size_t)blocks[i].count * 2u;
        if (blocks[i].samples == NULL && blocks[i].count > 0) {
            return 0;
        }
    }
    if (cap < need) {
        return 0;
    }

    uint8_t mask = 0;
    for (uint8_t i = 0; i < n_blocks; ++i) {
        mask |= blocks[i].sensor_bit;
    }

    size_t off = 0;

    /* header: <IIB  seq_no, timestamp_ms, sensor_mask */
    put_u32le(&out[off], seq_no);
    off += 4;
    put_u32le(&out[off], timestamp_ms);
    off += 4;
    out[off++] = mask;

    /* payload: per block <H count + int16 LE samples. The caller-supplied
     * ascending bit order equals Python's _SENSOR_BITS iteration order. */
    for (uint8_t i = 0; i < n_blocks; ++i) {
        put_u16le(&out[off], blocks[i].count);
        off += 2;
        for (uint16_t s = 0; s < blocks[i].count; ++s) {
            put_i16le(&out[off], blocks[i].samples[s]);
            off += 2;
        }
    }

    /* footer: CRC16-CCITT over header+payload, little-endian */
    put_u16le(&out[off], avc_crc16(out, off));
    off += AVC_PACKET_FOOTER_SIZE;

    return off; /* == need */
}
