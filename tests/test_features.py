"""Tests for Layer 2: the 13-descriptor feature engineering."""

import math
import unittest

import numpy as np

from services.features import (
    IDX, base_descriptors, features_from_windows, normalize_zscore,
)


def make_window(mic, start_ms=0.0, end_ms=40.0, pressure=None, airflow=None):
    from services.schemas import PhonemeWindow
    z = np.zeros(0, np.float32)
    return PhonemeWindow(
        start_ms=start_ms, end_ms=end_ms,
        mic=np.asarray(mic, np.float32),
        piezo=z,
        pressure=z if pressure is None else np.asarray(pressure, np.float32),
        airflow=z if airflow is None else np.asarray(airflow, np.float32),
    )


class TestBaseDescriptors(unittest.TestCase):
    def test_rms_and_spl(self):
        # constant 0.5 amplitude -> RMS 0.5
        w = make_window([0.5, -0.5, 0.5, -0.5])
        v = base_descriptors(w)
        self.assertAlmostEqual(v[IDX["rms_amplitude"]], 0.5, places=6)
        expected_spl = 20 * math.log10(0.5 / 2e-5)
        self.assertAlmostEqual(v[IDX["spl_db"]], expected_spl, places=4)

    def test_pressure_velocity_duration(self):
        w = make_window([0.1] * 8, start_ms=100.0, end_ms=140.0,
                        pressure=[2.0, 4.0], airflow=[0.1, 0.3])
        v = base_descriptors(w)
        self.assertAlmostEqual(v[IDX["pressure_pa"]], 3.0, places=6)
        self.assertAlmostEqual(v[IDX["velocity_ms"]], 0.2, places=6)
        self.assertAlmostEqual(v[IDX["duration_ms"]], 40.0, places=6)

    def test_ratio_guards(self):
        # zero mic, zero flow, zero pressure -> no NaN, no inf
        w = make_window(np.zeros(8))
        v = base_descriptors(w)
        self.assertTrue(np.all(np.isfinite(v)))
        self.assertEqual(v[IDX["spl_db"]], -120.0)  # SPL floor
        self.assertEqual(v[IDX["rms_amplitude"]], 0.0)

    def test_empty_window_safe(self):
        w = make_window(np.zeros(0, np.float32), 0.0, 0.0)
        v = base_descriptors(w)
        self.assertTrue(np.all(np.isfinite(v)))
        self.assertEqual(v[IDX["duration_ms"]], 0.0)


class TestFeatureSet(unittest.TestCase):
    def _two_windows(self):
        w1 = make_window([0.5, -0.5, 0.5, -0.5], 0.0, 40.0,
                         pressure=[1.0], airflow=[0.1])
        w2 = make_window([0.25, -0.25], 40.0, 120.0,
                         pressure=[3.0], airflow=[0.2])
        return w1, w2

    def test_matrix_shape(self):
        fs = features_from_windows(list(self._two_windows()))
        m = fs.matrix()
        self.assertEqual(m.shape, (2, 13))
        self.assertEqual(m.dtype, np.float32)

    def test_dialogue_normalized_descriptors(self):
        # mu_rms = (0.5 + 0.25)/2 = 0.375; mu_dur = (40 + 80)/2 = 60
        fs = features_from_windows(list(self._two_windows()))
        r0, r1 = fs.rows[0].vector, fs.rows[1].vector
        self.assertAlmostEqual(r0[IDX["energy_ratio"]], 0.5 / 0.375, places=5)
        self.assertAlmostEqual(r1[IDX["energy_ratio"]], 0.25 / 0.375, places=5)
        self.assertAlmostEqual(r0[IDX["duration_norm"]], 40.0 / 60.0, places=5)
        self.assertAlmostEqual(r1[IDX["duration_norm"]], 80.0 / 60.0, places=5)

    def test_first_deltas_are_zero(self):
        # Pinned convention: first phoneme's deltas (10-12) are 0.0.
        fs = features_from_windows(list(self._two_windows()))
        for name in ("delta_rms", "delta_spl", "delta_pressure"):
            self.assertEqual(fs.rows[0].vector[IDX[name]], 0.0, name)

    def test_second_deltas(self):
        fs = features_from_windows(list(self._two_windows()))
        r1 = fs.rows[1].vector
        self.assertAlmostEqual(r1[IDX["delta_rms"]], 0.25 - 0.5, places=5)
        self.assertAlmostEqual(r1[IDX["delta_pressure"]], 3.0 - 1.0, places=5)

    def test_labels_and_unknown(self):
        w1, w2 = self._two_windows()
        fs = features_from_windows([w1, w2], labels=["T", None])
        self.assertAlmostEqual(fs.rows[0].vector[IDX["phoneme_class"]], 2.0)  # stop
        self.assertAlmostEqual(fs.rows[1].vector[IDX["phoneme_class"]], 0.0)  # unknown
        self.assertEqual(fs.phonemes(), ["T", None])

    def test_labels_length_mismatch(self):
        w1, _ = self._two_windows()
        with self.assertRaises(ValueError):
            features_from_windows([w1], labels=["A", "B"])

    def test_empty(self):
        fs = features_from_windows([])
        self.assertEqual(fs.matrix().shape, (0, 13))


class TestNormalize(unittest.TestCase):
    def test_zscore_basic(self):
        m = np.array([[0.0, 0.0], [2.0, 2.0]])
        n, mean, std = normalize_zscore(m)
        np.testing.assert_allclose(mean, [1.0, 1.0])
        np.testing.assert_allclose(std, [1.0, 1.0])
        np.testing.assert_allclose(n, [[-1.0, -1.0], [1.0, 1.0]])

    def test_zero_std_column_guarded(self):
        m = np.array([[5.0, 0.0], [5.0, 2.0]])
        n, _, std = normalize_zscore(m)
        self.assertEqual(std[0], 1.0)   # degenerate std replaced by 1
        np.testing.assert_allclose(n[:, 0], [0.0, 0.0])
        self.assertTrue(np.all(np.isfinite(n)))

    def test_train_stats_reused_at_inference(self):
        train = np.random.default_rng(0).normal(size=(100, 13))
        _, mean, std = normalize_zscore(train)
        probe = np.random.default_rng(1).normal(size=(5, 13))
        n2, m2, s2 = normalize_zscore(probe, mean=mean, std=std)
        np.testing.assert_allclose(m2, mean)
        np.testing.assert_allclose(s2, std)
        np.testing.assert_allclose(n2, (probe - mean) / std)


if __name__ == "__main__":
    unittest.main()
