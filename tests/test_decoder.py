"""Tests for Layer 4a: phoneme -> word decoding (CMU-style lexicon match)."""

import os
import tempfile
import unittest

from services.decoder import (
    PhonemeDecoder, load_cmu_dict, phoneme_edit_distance, strip_stress,
)


class TestEditDistance(unittest.TestCase):
    def test_known_distances(self):
        self.assertEqual(phoneme_edit_distance([], ["A"]), 1)
        self.assertEqual(phoneme_edit_distance(["A"], []), 1)
        self.assertEqual(phoneme_edit_distance([], []), 0)
        self.assertEqual(phoneme_edit_distance(["AA", "T"], ["AA", "T"]), 0)
        self.assertEqual(phoneme_edit_distance(["AA", "T"], ["AA", "D"]), 1)
        self.assertEqual(
            phoneme_edit_distance(["HH", "AH", "L", "OW"], ["HH", "AH", "L"]), 1)

    def test_cache_hits_same_result(self):
        a, b = ("P", "L", "IY", "Z"), ("P", "L", "IY", "S")
        self.assertEqual(phoneme_edit_distance(a, b),
                         phoneme_edit_distance(list(a), list(b)))


class TestStripStress(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(strip_stress("AA1"), "AA")
        self.assertEqual(strip_stress("OW2"), "OW")
        self.assertEqual(strip_stress("T"), "T")


class TestDecode(unittest.TestCase):
    def setUp(self):
        self.dec = PhonemeDecoder()

    def test_empty_stream(self):
        cands = self.dec.decode([])
        self.assertEqual(len(cands), 1)
        self.assertEqual(cands[0].text, "")

    def test_exact_word(self):
        cands = self.dec.decode(["HH", "AH", "L", "OW"])
        self.assertEqual(cands[0].text, "HELLO")
        self.assertEqual(cands[0].edit_cost, 0.0)

    def test_stress_markers_ignored(self):
        cands = self.dec.decode(["HH1", "AH0", "L", "OW1"])
        self.assertEqual(cands[0].text, "HELLO")

    def test_sil_filtered(self):
        cands = self.dec.decode(["SIL", "HH", "AH", "L", "OW", "SIL"])
        self.assertEqual(cands[0].text, "HELLO")

    def test_two_words(self):
        cands = self.dec.decode(["HH", "AH", "L", "OW", "T", "EH", "S", "T"])
        self.assertEqual(cands[0].text, "HELLO TEST")
        self.assertEqual(cands[0].edit_cost, 0.0)

    def test_single_edit_tolerance(self):
        # One corrupted phoneme should still recover the word (max_word_edits=1).
        cands = self.dec.decode(["HH", "AH", "L", "AA"])  # OW -> AA
        self.assertEqual(cands[0].text, "HELLO")
        self.assertEqual(cands[0].edit_cost, 1.0)

    def test_dead_end_skips_cost_one(self):
        # Unrecognized leading phoneme -> skipped, +1 edit cost.
        cands = self.dec.decode(["ZZ", "HH", "AH", "L", "OW"])
        self.assertEqual(cands[0].text, "HELLO")
        self.assertEqual(cands[0].edit_cost, 1.0)

    def test_beam_bounds(self):
        cands = self.dec.decode(["HH", "AH", "L", "OW", "T", "EH", "S", "T"])
        self.assertLessEqual(len(cands), self.dec.beam_width)
        self.assertGreaterEqual(len(cands), 1)

    def test_all_junk_falls_back(self):
        cands = self.dec.decode(["Q", "Q", "Q"])
        # No lexicon path possible: raw-symbol fallback with cost == n.
        self.assertEqual(cands[0].edit_cost, 3.0)
        self.assertTrue(cands[0].text)


class TestLoadCmuDict(unittest.TestCase):
    def _write(self, text):
        fd, path = tempfile.mkstemp(suffix=".dict", text=True)
        with os.fdopen(fd, "w", encoding="latin-1") as fh:
            fh.write(text)
        self.addCleanup(os.unlink, path)
        return path

    def test_parse_and_variants(self):
        path = self._write(
            ";;; comment line\n"
            "HELLO  HH AH0 L OW1\n"
            "HELLO(2)  HH EH1 L OW0\n"
            "\n"
            "WATER  W AO1 T ER0\n")
        idx = load_cmu_dict(path)
        self.assertEqual(idx["HELLO"], ("HH", "AH", "L", "OW"))
        self.assertEqual(idx["WATER"], ("W", "AO", "T", "ER"))
        self.assertNotIn("HELLO(2)", idx)  # variant folded into base word

    def test_custom_lexicon_used(self):
        dec = PhonemeDecoder(lexicon={"SIX": ("S", "IH", "K", "S")})
        cands = dec.decode(["S", "IH", "K", "S"])
        self.assertEqual(cands[0].text, "SIX")


if __name__ == "__main__":
    unittest.main()
