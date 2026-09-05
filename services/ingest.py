"""Layer 1 → Layer 2 boundary: parse ESP32-S3 sensor packets.

Implements the TRD section 4.1 packet structure::

    [Header: 8 bytes] [Payload: interleaved sensor frames] [Footer: 2-byte CRC16]
    Header fields (little-endian):
        uint32 seq_no
        uint32 timestamp_ms
        (sensor_mask is carried in byte 4 of timestamp's high nibble OR
         a separate byte — see NOTE below)

NOTE — format pinning: the TRD text specifies "Header: 8 bytes (seq_no,
timestamp_ms, sensor_mask)" which does not uniquely fix a bit layout.
We pin this repository's concrete, testable layout:

    Header (9 bytes):
        uint32 seq_no
        uint32 timestamp_ms
        uint8  sensor_mask          # bit0 mic, bit1 piezo, bit2 pressure, bit3 airflow

    Payload: for each enabled sensor (ascending bit order), a block of
    interleaved frames: ``uint16 frame_count`` followed by
    ``frame_count * frame_size`` int16 LE samples, where frame_size is
    per-sensor (mic 1, others 1). All four streams are sampled at their
    own rates; a block simply carries whatever elapsed in the 500ms
    window (mic 16kHz -> 8000 samples, piezo 1kHz -> 500, others 100Hz
    -> 50).

    Footer: CRC16-CCITT (poly 0x1021, init 0xFFFF) over header+payload.

This layout is deterministic, round-trippable, and mirrored by the
firmware packer in ``firmware/src/packet.c`` so both sides can be kept
in lockstep via a golden-vector test.
"""

from __future__ import annotations

import struct
from typing import List, Optional

import numpy as np

from services.schemas import SensorPacket, SensorStream

# sensor bit positions in sensor_mask
SENSOR_MIC = 0x01
SENSOR_PIEZO = 0x02
SENSOR_PRESSURE = 0x04
SENSOR_AIRFLOW = 0x08

#: sensor_mask bit -> (name, nominal rate). TRD section 4.1 rates.
_SENSOR_BITS: List[tuple] = [
    (SENSOR_MIC, "mic", 16_000),
    (SENSOR_PIEZO, "piezo", 1_000),
    (SENSOR_PRESSURE, "pressure", 100),
    (SENSOR_AIRFLOW, "airflow", 100),
]

HEADER_SIZE = 9   # 4 + 4 + 1
FOOTER_SIZE = 2   # CRC16


class PacketError(ValueError):
    """Raised when a packet fails structural or CRC validation."""


def crc16_ccitt(data: bytes, crc: int = 0xFFFF) -> int:
    """CRC16-CCITT (Kermit-style poly order used by the firmware, too).

    Poly 0x1021, init 0xFFFF, no reflection, no final xor — matches the
    reference C implementation in firmware/src/packet.c.
    """
    for byte in data:
        crc ^= byte << 8
        for _ in range(8):
            if crc & 0x8000:
                crc = ((crc << 1) ^ 0x1021) & 0xFFFF
            else:
                crc = (crc << 1) & 0xFFFF
    return crc


def build_packet(
    seq_no: int,
    timestamp_ms: int,
    streams: dict,
    rates: Optional[dict] = None,
) -> bytes:
    """Serialize streams (name -> float array) into the wire format.

    Floats are scaled by 32767 clamped (int16). Firmware performs the
    inverse. Exists for tests + offline replay of synthetic sessions.
    """
    rates = rates or {"mic": 16_000, "piezo": 1_000, "pressure": 100, "airflow": 100}
    mask = 0
    for bit, name, _ in _SENSOR_BITS:
        if name in streams:
            mask |= bit
    body = bytearray()
    body += struct.pack("<IIB", seq_no & 0xFFFFFFFF, timestamp_ms & 0xFFFFFFFF, mask)
    for bit, name, rate in _SENSOR_BITS:
        if mask & bit:
            arr = np.asarray(streams[name], dtype=np.float32).ravel()
            ints = np.clip(np.round(arr * 32767.0), -32768, 32767).astype("<i2")
            body += struct.pack("<H", len(ints))
            body += ints.tobytes()
    crc = crc16_ccitt(bytes(body))
    body += struct.pack("<H", crc)
    return bytes(body)


def parse_packet(data: bytes) -> SensorPacket:
    """Parse + validate one packet. Raises PacketError on any defect."""
    if len(data) < HEADER_SIZE + FOOTER_SIZE:
        raise PacketError(f"packet too short: {len(data)} bytes")
    if len(data) > 64 * 1024:
        raise PacketError(f"packet too large: {len(data)} bytes")

    crc_recv = struct.unpack("<H", data[-FOOTER_SIZE:])[0]
    crc_calc = crc16_ccitt(data[:-FOOTER_SIZE])
    if crc_recv != crc_calc:
        raise PacketError(f"CRC mismatch: received {crc_recv:#06x}, computed {crc_calc:#06x}")

    seq_no, timestamp_ms, mask = struct.unpack("<IIB", data[:HEADER_SIZE])
    if mask == 0 or mask & ~0x0F:
        raise PacketError(f"invalid sensor_mask: {mask:#04x}")

    offset = HEADER_SIZE
    streams = {}
    for bit, name, rate in _SENSOR_BITS:
        if not (mask & bit):
            continue
        if offset + 2 > len(data) - FOOTER_SIZE:
            raise PacketError("truncated frame-count field")
        count = struct.unpack_from("<H", data, offset)[0]
        offset += 2
        nbytes = count * 2
        if offset + nbytes > len(data) - FOOTER_SIZE:
            raise PacketError(f"truncated {name} block: need {nbytes} bytes")
        ints = np.frombuffer(data, dtype="<i2", count=count, offset=offset)
        offset += nbytes
        floats = ints.astype(np.float32) / 32767.0
        streams[name] = SensorStream(name=name, rate_hz=rate, samples=floats)

    if offset != len(data) - FOOTER_SIZE:
        raise PacketError(f"trailing garbage: consumed {offset}, payload ends {len(data) - FOOTER_SIZE}")

    return SensorPacket(seq_no=seq_no, timestamp_ms=timestamp_ms, sensor_mask=mask, streams=streams)
