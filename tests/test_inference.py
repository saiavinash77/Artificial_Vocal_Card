"""Tests for Layer 3/4 inference + LM re-rank interfaces (no ONNX needed)."""

import unittest

import numpy as np

from services.inference import (
    PHONEME_VOCAB, DemoHeuristicClassifier, get_classifier,
)
from services.lm_rerank import NgramScorer, UniformScorer
from services.schemas import DecodedCandidate, FeatureSet, class_name_of


def _fs_from_vectors(vectors):
    """Build a FeatureSet directly from raw 13-d descriptor rows."""
    from services.features import features_from_windows
    from services.schemas import FeatureRow
    fs = FeatureSet()
    for v in vectors:
        fs.rows.append(FeatureRow(vector=np.asarray(v, np.float32)))
    return fs


class TestVocab(unittest.TestCase):
    def test_vocab_importable_and_complete(self):
        # 39 ARPAbet + SIL = 40; HH regression guard.
        self.assertEqual(len(PHONEME_VOCAB), 40)
        self.assertIn("HH", PHONEME_VOCAB)
        self.assertIn("SIL", PHONEME_VOCAB)


class TestDemoClassifier(unittest.TestCase):
    def test_one_prediction_per_row(self):
        vecs = [np.full(13, 0.1), np.full(13, 0.1)]
        fs = _fs_from_vectors(vecs)
        preds = DemoHeuristicClassifier().predict(fs)
        self.assertEqual(len(preds), 2)
        for p in preds:
            self.assertTrue(0.0 <= p.confidence <= 1.0)
            self.assertIn(p.phoneme, PHONEME_VOCAB)
            self.assertNotEqual(p.class_name, "consonant")  # dead value removed

    def test_class_id_to_representative(self):
        # class id 2 (stop) must map to a stop phoneme.
        v = np.zeros(13)
        v[12] = 2.0
        (p,) = DemoHeuristicClassifier().predict(_fs_from_vectors([v]))
        self.assertEqual(class_name_of(p.phoneme), "stop")
        self.assertEqual(p.class_name, "stop")

    def test_empty_feature_set(self):
        self.assertEqual(DemoHeuristicClassifier().predict(FeatureSet()), [])


class TestFactory(unittest.TestCase):
    def test_demo_backend(self):
        self.assertIsInstance(get_classifier("demo"), DemoHeuristicClassifier)

    def test_unknown_backend_rejected(self):
        with self.assertRaises(ValueError):
            get_classifier("gpt4")

    def test_auto_falls_back_to_demo(self):
        # No models/ dir in this repo state -> auto must pick demo.
        import os
        from services.inference import DEFAULT_MODEL_PATH
        if not os.path.isfile(DEFAULT_MODEL_PATH):
            self.assertIsInstance(get_classifier(None), DemoHeuristicClassifier)


class TestNgramScorer(unittest.TestCase):
    def test_prefers_seen_bigram(self):
        scorer = NgramScorer(["hello help me", "hello world"])
        seen = DecodedCandidate(text="HELLO HELP", words=["HELLO", "HELP"],
                                lm_logprob=0.0, edit_cost=0.0)
        junk = DecodedCandidate(text="HELLO XQZJ", words=["HELLO", "XQZJ"],
                                lm_logprob=0.0, edit_cost=0.0)
        ranked = scorer.rerank([junk, seen])
        self.assertEqual(ranked[0].text, "HELLO HELP")

    def test_uniform_keeps_order(self):
        scorer = UniformScorer()
        a = DecodedCandidate("A", ["A"], 0.0, 0.0)
        b = DecodedCandidate("B", ["B"], 0.0, 1.0)
        ranked = scorer.rerank([a, b])
        self.assertEqual(ranked[0].text, "A")  # lower edit cost wins
        self.assertEqual(ranked[1].lm_logprob, 0.0)

    def test_ngram_case_insensitive(self):
        # Higher logprob = better; seen continuation must score higher.
        s = NgramScorer(["Hello Help Me"])
        self.assertGreater(s.logprob(["hello", "help"]), s.logprob(["hello", "zzz"]))


if __name__ == "__main__":
    unittest.main()
