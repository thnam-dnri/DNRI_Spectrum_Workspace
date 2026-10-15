import unittest
import numpy as np
from comparison import exposure_scale, project_reference, subtract_background
from acquisition import AcquisitionPreset


class Comparison(unittest.TestCase):
    def test_energy_rebin_conserves_counts_and_split_bin_covariance(self):
        projected, source, target, fraction = project_reference([100, 200], (0, 2), 4, (0, 1))
        np.testing.assert_allclose(projected, [50, 50, 100, 100])
        counts, noise = subtract_background(np.full(4, 300), [100, 200], (0, 1), (0, 2), 1)
        np.testing.assert_allclose(counts, [250, 250, 200, 200])
        # The two halves of each original reference bin are correlated.
        self.assertAlmostEqual(noise.covariance_of(np.ones(4))[0, 0], 1200+300)
        np.testing.assert_allclose(noise.diagonal, [325, 325, 350, 350])

    def test_partial_energy_overlap_and_noise_subset(self):
        counts, noise = subtract_background([100, 100, 100], [80, 40], (0, 1), (.5, 1), 2)
        np.testing.assert_allclose(counts, [20, -20, 60])
        selected = np.array([False, True, True])
        self.assertAlmostEqual(noise.subset(selected).covariance_of([1, 1])[0, 0], 200+4*(.5**2*80+40))

    def test_time_normalization_does_not_invent_live_time(self):
        scale, basis = exposure_scale({'live_time': 50, 'real_time': 60}, {'live_time': 100, 'real_time': 120})
        self.assertEqual(scale, .5)
        self.assertEqual(basis, 'detector live time')
        scale, basis = exposure_scale({'stats': {'acquisition_ticks': 50*50000000}}, {'live_time': 40, 'real_time': 100})
        self.assertEqual(scale, .5)
        self.assertIn('uncorrected', basis)
        with self.assertRaises(ValueError):
            exposure_scale({}, {})
        self.assertEqual(exposure_scale({}, {}, 2), (2, 'manual scale'))

    def test_presets_use_new_counts_and_request_stop_once(self):
        preset = AcquisitionPreset('counts', 50, 0)
        preset.arm(100, {1: 1000, 2: 2000})
        self.assertFalse(preset.progress(101, {1: 1020, 2: 2020})[1])
        self.assertTrue(preset.progress(102, {1: 1025, 2: 2025})[1])
        preset.stop_requested = True
        self.assertFalse(preset.progress(103, {1: 1030, 2: 2030})[1])
        preset = AcquisitionPreset('duration', 2.5)
        preset.arm(10, {})
        self.assertFalse(preset.progress(12, {})[1])
        self.assertTrue(preset.progress(12.5, {})[1])


if __name__ == '__main__':
    unittest.main()
