"""Tests for schemas + ingest (packet wire format + CRC16)."""

import struct
import unittest

import numpy as np

from services.schemas import (
    DESCRIPTOR_NAMES, N_DESCRIPTORS, PHONEME_CLASSES, Prediction,
)
from services.ingest import (
    HEADER_SIZE, PacketError, SENSOR_AIRFLOW, SENSOR_MIC, SENSOR_PIEZO, SENSOR_PRESSURE,
    build_packet, crc16_ccitt, parse_packet,
)


class TestSchemas(unittest.TestCase):
    def test_descriptor_contract(self):
        self.assertEqual(N_DESCRIPTORS, 13)
        self.assertEqual(len(set(DESCRIPTOR_NAMES)), 13)  # unique + ordered
        self.assertEqual(DESCRIPTOR_NAMES[0], "rms_amplitude")
        self.assertEqual(DESCRIPTOR_NAMES[12], "phoneme_class")

    def test_phoneme_classes(self):
        self.assertEqual(PHONEME_CLASSES["unknown"], 0)
        self.assertEqual(len(PHONEME_CLASSES), 8)

    def test_prediction_confidence_bounds(self):
        with self.assertRaises(ValueError):
            Prediction(phoneme="T", class_name="stop", confidence=1.5)
        with self.assertRaises(ValueError):
            Prediction(phoneme="T", class_name="stop", confidence=-0.1)


class TestCRC16(unittest.TestCase):
    def test_known_vectors(self):
        # CRC16-CCITT (poly 0x1021, init 0xFFFF, no reflect): "123456789" -> 0x29B1
        self.assertEqual(crc16_ccitt(b"123456789"), 0x29B1)
        self.assertEqual(crc16_ccitt(b""), 0xFFFF)
        self.assertEqual(crc16_ccitt(b"A") ^ crc16_ccitt(b"A"), 0)

    def test_incremental_equals_oneshot(self):
        # Chained continuation must equal the one-shot CRC over the full payload.
        data = b"AVC test payload"
        one = crc16_ccitt(data)
        c = crc16_ccitt(data[:5])          # first chunk
        c = crc16_ccitt(data[5:], crc=c)   # continue with the running value
        self.assertEqual(one, c)


class TestPacketRoundTrip(unittest.TestCase):
    def _streams(self):
        rng = np.random.default_rng(42)
        return {
            "mic": rng.normal(0, 0.1, 160).astype(np.float32),
            "piezo": rng.normal(0, 0.05, 10).astype(np.float32),
            "pressure": rng.normal(500, 10, 1).astype(np.float32),  # Pa-scale, clipped
            "airflow": rng.normal(0.2, 0.02, 1).astype(np.float32),
        }

    def test_round_trip_all_sensors(self):
        pkt = build_packet(7, 12345, self._streams())
        parsed = parse_packet(pkt)
        self.assertEqual(parsed.seq_no, 7)
        self.assertEqual(parsed.timestamp_ms, 12345)
        self.assertEqual(parsed.sensor_mask, 0x0F)
        self.assertEqual(set(parsed.streams), {"mic", "piezo", "pressure", "airflow"})
        # quantization is int16 round-trip: tolerance ~ 1/32767
        for name, arr in self._streams().items():
            got = parsed.streams[name].samples
            self.assertEqual(got.shape[0], arr.shape[0], name)
            np.testing.assert_allclose(got, np.clip(arr, -1, 1), atol=1.5 / 32767, err_msg=name)

    def test_partial_mask(self):
        streams = {"mic": np.zeros(100, np.float32), "airflow": np.zeros(10, np.float32)}
        parsed = parse_packet(build_packet(1, 0, streams))
        self.assertEqual(parsed.sensor_mask, SENSOR_MIC | SENSOR_AIRFLOW)
        self.assertIn("mic", parsed.streams)
        self.assertIn("airflow", parsed.streams)
        self.assertNotIn("piezo", parsed.streams)

    def test_rates_pinned(self):
        parsed = parse_packet(build_packet(1, 0, self._streams()))
        self.assertEqual(parsed.stream("mic").rate_hz, 16_000)
        self.assertEqual(parsed.stream("piezo").rate_hz, 1_000)
        self.assertEqual(parsed.stream("pressure").rate_hz, 100)
        self.assertEqual(parsed.stream("airflow").rate_hz, 100)

    def test_corrupt_crc_rejected(self):
        pkt = bytearray(build_packet(2, 99, self._streams()))
        pkt[-1] ^= 0xFF
        with self.assertRaises(PacketError):
            parse_packet(bytes(pkt))

    def test_truncated_rejected(self):
        pkt = build_packet(3, 5, self._streams())
        for cut in (0, 1, HEADER_SIZE, len(pkt) - 3):
            with self.assertRaises(PacketError):
                parse_packet(pkt[:cut])

    def test_trailing_garbage_rejected(self):
        pkt = build_packet(3, 5, {"mic": np.zeros(4, np.float32)})
        with self.assertRaises(PacketError):
            parse_packet(pkt + b"\x00\x00")

    def test_bad_mask_rejected(self):
        body = struct.pack("<IIB", 1, 0, 0x10) + struct.pack("<H", 1) + b"\x00\x00"
        crc = crc16_ccitt(body)
        with self.assertRaises(PacketError):
            parse_packet(body + struct.pack("<H", crc))


if __name__ == "__main__":
    unittest.main()
