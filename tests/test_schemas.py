"""Tests for the locked schema constants (single source of truth)."""

import unittest

from services.schemas import (
    DESCRIPTOR_NAMES,
    PHONEME_CLASSES,
    PHONEME_TO_ID,
    PHONEME_VOCAB,
    class_name_of,
    phoneme_class_of,
    strip_stress,
)


class TestVocab(unittest.TestCase):
    def test_vocab_size_and_uniqueness(self):
        # 39 canonical ARPAbet symbols + SIL = 40 output classes.
        self.assertEqual(len(PHONEME_VOCAB), 40)
        self.assertEqual(len(set(PHONEME_VOCAB)), 40)

    def test_hh_present(self):
        # Regression: HH was accidentally dropped, making HELLO/HELP
        # undecodable. It must stay in both vocab and class map.
        self.assertIn("HH", PHONEME_VOCAB)
        self.assertEqual(class_name_of("HH"), "fricative")

    def test_full_arpabet_coverage(self):
        # The 39 canonical CMU ARPAbet symbols (stress-stripped).
        canonical = set(
            "AA AE AH AO AW AY B CH D DH EH ER EY F G HH IH IY JH K L M N NG "
            "OW OY P R S SH T TH UH UW V W Y Z ZH".split())
        self.assertTrue(canonical.issubset(set(PHONEME_VOCAB)))

    def test_id_map_inverse(self):
        self.assertEqual(len(PHONEME_TO_ID), len(PHONEME_VOCAB))
        for i, p in enumerate(PHONEME_VOCAB):
            self.assertEqual(PHONEME_TO_ID[p], i)

    def test_sil_last(self):
        # Position of SIL is part of the ONNX/TFLite contract.
        self.assertEqual(PHONEME_VOCAB[-1], "SIL")


class TestStressAndClass(unittest.TestCase):
    def test_strip_stress(self):
        self.assertEqual(strip_stress("AA1"), "AA")
        self.assertEqual(strip_stress("AA0"), "AA")
        self.assertEqual(strip_stress("AA2"), "AA")
        self.assertEqual(strip_stress("T"), "T")
        self.assertEqual(strip_stress(" HH1 "), "HH")  # whitespace-safe

    def test_class_names(self):
        self.assertEqual(class_name_of("AA"), "vowel")
        self.assertEqual(class_name_of("T"), "stop")
        self.assertEqual(class_name_of("S"), "fricative")
        self.assertEqual(class_name_of("M"), "nasal")
        self.assertEqual(class_name_of("L"), "liquid")
        self.assertEqual(class_name_of("W"), "glide")
        self.assertEqual(class_name_of("CH"), "affricate")

    def test_class_ids(self):
        # Class NAME -> id mapping is coherent, and every class name is
        # reachable as an unknown-phoneme fallback label.
        for name, cid in PHONEME_CLASSES.items():
            self.assertGreaterEqual(cid, 0)
        self.assertEqual(PHONEME_CLASSES["vowel"], 1)
        self.assertEqual(PHONEME_CLASSES["stop"], 2)
        self.assertEqual(phoneme_class_of("unknown"), 0)
        self.assertEqual(class_name_of("AA"), "vowel")  # phoneme, not class name

    def test_stressed_phoneme_gets_class(self):
        # Regression: "AA1" used to fall through to unknown(0).
        self.assertEqual(phoneme_class_of("AA1"), PHONEME_CLASSES["vowel"])
        self.assertEqual(phoneme_class_of("SIL"), 0)
        self.assertEqual(class_name_of("XYZ"), "unknown")


class TestDescriptorContract(unittest.TestCase):
    def test_names_ordered(self):
        self.assertEqual(DESCRIPTOR_NAMES[0], "rms_amplitude")
        self.assertEqual(DESCRIPTOR_NAMES[5], "energy_ratio")
        self.assertEqual(DESCRIPTOR_NAMES[9], "delta_rms")
        self.assertEqual(DESCRIPTOR_NAMES[12], "phoneme_class")


if __name__ == "__main__":
    unittest.main()
