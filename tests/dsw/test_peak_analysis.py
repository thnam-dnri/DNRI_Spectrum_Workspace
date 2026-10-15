"""Numerical checks against known peak shapes and difficult ROI boundaries."""
import unittest

import numpy as np

from peak_analysis import analyze_roi, automatic_roi


class PeakAnalysis(unittest.TestCase):
    def setUp(self):
        self.x = np.arange(4096) + .5
        self.calibration = (0., 1.)

    def gaussian(self, center=1000., sigma=20., amplitude=10000., background=100.):
        return np.rint(background + amplitude * np.exp(-.5*((self.x-center)/sigma)**2)).astype(np.uint32)

    def test_gaussian_width_area_centroid_and_resolution(self):
        counts = self.gaussian()
        original = counts.copy()
        result = analyze_roi(counts, (880, 1120), self.calibration)
        expected = 2 * np.sqrt(2 * np.log(2)) * 20
        self.assertAlmostEqual(result['fwhm_keV'], expected, delta=.03)
        self.assertAlmostEqual(result['centroid_keV'], 1000., places=8)
        self.assertAlmostEqual(result['resolution_percent'], expected/10, delta=.003)
        self.assertEqual(result['gross_counts'], int(counts[880:1120].sum()))
        self.assertAlmostEqual(result['net_counts'], 10000*20*np.sqrt(2*np.pi), delta=20)
        np.testing.assert_array_equal(counts, original)

    def test_linear_background_and_calibration_scaling(self):
        counts = self.gaussian(background=0).astype(np.uint64) + np.arange(4096)
        a = analyze_roi(counts, (880, 1120), (0., 1.))
        b = analyze_roi(counts, (450, 570), (10., .5))
        self.assertAlmostEqual(a['centroid_keV'], 1000., delta=.01)
        self.assertAlmostEqual(b['centroid_keV'], 510., delta=.01)
        self.assertAlmostEqual(b['fwhm_keV'], a['fwhm_keV']/2, places=8)
        self.assertEqual(a['gross_counts'], b['gross_counts'])
        self.assertAlmostEqual(a['net_counts'], b['net_counts'], places=6)

    def test_automatic_selection_finds_clicked_weaker_peak(self):
        counts = self.gaussian(center=600., sigma=12., amplitude=15000., background=0)
        counts += self.gaussian(center=1100., sigma=20., amplitude=4000.)
        strongest = automatic_roi(counts, self.calibration)
        selected = automatic_roi(counts, self.calibration, target=1105., search_radius=80.)
        self.assertTrue(strongest[0] < 600 < strongest[1])
        self.assertTrue(selected[0] < 1100 < selected[1])
        self.assertGreater(selected[0], 700)
        measured = analyze_roi(counts, selected, self.calibration)
        self.assertAlmostEqual(measured['fwhm_keV'], 2*np.sqrt(2*np.log(2))*20, delta=.3)
        with self.assertRaises(ValueError):
            automatic_roi(counts, self.calibration, target=2000., search_radius=50.)

    def test_automatic_roi_stops_before_neighbor(self):
        counts = self.gaussian(center=1000., sigma=10., amplitude=10000., background=0)
        counts += self.gaussian(center=1060., sigma=10., amplitude=8000., background=30.)
        selected = automatic_roi(counts, self.calibration, target=1000., search_radius=80.)
        self.assertLess(selected[1], 1060.)
        self.assertGreater(selected[1], 1015.)
        self.assertAlmostEqual(analyze_roi(counts, selected, self.calibration)['centroid_keV'], 1000., delta=1.)

    def test_noisy_gaussian(self):
        rng = np.random.default_rng(120)
        counts = rng.poisson(self.gaussian()).astype(np.uint32)
        selected = automatic_roi(counts, self.calibration, target=1003., search_radius=80.)
        result = analyze_roi(counts, selected, self.calibration, smoothing=5)
        self.assertAlmostEqual(result['centroid_keV'], 1000., delta=.5)
        self.assertAlmostEqual(result['fwhm_keV'], 2*np.sqrt(2*np.log(2))*20, delta=2.)

    def test_invalid_empty_flat_and_truncated_rois(self):
        for counts in (np.zeros(4096, dtype=np.uint32), np.full(4096, 20, dtype=np.uint32)):
            self.assertIsNone(analyze_roi(counts, (100, 200), self.calibration)['fwhm_keV'])
            with self.assertRaises(ValueError):
                automatic_roi(counts, self.calibration)
        short = analyze_roi(self.gaussian(), (1000, 1001), self.calibration)
        self.assertIsNone(short['fwhm_keV'])
        cut = analyze_roi(self.gaussian(), (1000, 1100), self.calibration)
        self.assertIsNone(cut['fwhm_keV'])
        self.assertIn('cut off', cut['status'])
        with self.assertRaises(ValueError):
            analyze_roi(self.gaussian(), (1100, 1000), self.calibration)
        with self.assertRaises(ValueError):
            analyze_roi(self.gaussian(), (900, 1100), (0., -1.))

    def test_one_bin_peak_is_sampling_limited_and_smoothing_is_explicit(self):
        counts = np.zeros(4096, dtype=np.uint32)
        counts[1000] = 100000
        roi = automatic_roi(counts, self.calibration, target=1000., search_radius=50.)
        raw = analyze_roi(counts, roi, self.calibration)
        smooth = analyze_roi(counts, roi, self.calibration, smoothing=5)
        self.assertAlmostEqual(raw['fwhm_keV'], 1.)
        self.assertIn('sampling-limited', raw['warning'])
        self.assertGreater(smooth['fwhm_keV'], raw['fwhm_keV'])
        self.assertEqual(raw['gross_counts'], smooth['gross_counts'])
        self.assertEqual(raw['net_counts'], smooth['net_counts'])
        self.assertEqual(raw['centroid_keV'], smooth['centroid_keV'])

    def test_energy_range_and_large_counts(self):
        counts = np.zeros(4096, dtype=np.uint32)
        counts[:20] = np.iinfo(np.uint32).max
        result = analyze_roi(counts, (0, 100), (-10., 1.))
        self.assertEqual(result['gross_counts'], 10 * np.iinfo(np.uint32).max)
        # Counts beyond the plot range must not contribute to ROI area.
        counts[4000:] = np.iinfo(np.uint32).max
        self.assertEqual(analyze_roi(counts, (3990, 4100), (0., 1.))['gross_counts'], 0)

    def test_gaussian_fit_recovers_known_parameters(self):
        counts = self.gaussian(center=1000., sigma=20., amplitude=10000., background=100.)
        fit = analyze_roi(counts, (880, 1120), self.calibration, method='gaussian')
        expected_fwhm = 2 * np.sqrt(2 * np.log(2)) * 20
        self.assertAlmostEqual(fit['centroid_keV'], 1000., delta=0.05)
        self.assertAlmostEqual(fit['fwhm_keV'], expected_fwhm, delta=0.05)
        self.assertAlmostEqual(fit['net_counts'], 10000. * 20. * np.sqrt(2 * np.pi), delta=20.)
        self.assertLess(fit['chi2_red'], 0.01)
        self.assertEqual(fit['method'], 'Gaussian fit (linear background)')
        self.assertIn('fit_curve', fit)
        x_dense, y_dense = fit['fit_curve']
        self.assertEqual(len(x_dense), len(y_dense))
        self.assertGreater(len(x_dense), 100)

    def test_gaussian_statistical_errors_and_degrees_of_freedom(self):
        counts = self.gaussian()
        a = analyze_roi(counts, (880, 1120), self.calibration, method='gaussian')
        b = analyze_roi(counts*4, (880, 1120), self.calibration, method='gaussian')
        self.assertTrue(a['converged'])
        self.assertEqual(a['degrees_of_freedom'], 235)
        self.assertGreater(a['fwhm_uncertainty_keV'], 0)
        self.assertGreater(a['net_counts_uncertainty'], 0)
        self.assertAlmostEqual(b['centroid_uncertainty_keV']/a['centroid_uncertainty_keV'], .5, delta=.02)
        self.assertAlmostEqual(b['net_counts_uncertainty']/a['net_counts_uncertainty'], 2., delta=.05)
        covariance = np.array(a['parameter_covariance'])
        np.testing.assert_allclose(covariance, covariance.T, rtol=1e-6, atol=1e-8)
        self.assertTrue(np.all(np.linalg.eigvalsh(covariance) >= -1e-8))
        short = analyze_roi(counts, (998, 1003), self.calibration, method='gaussian')
        self.assertIsNone(short['fwhm_keV'])
        self.assertFalse(short['converged'])

    def test_gaussian_fit_noisy_and_cutoff(self):
        rng = np.random.default_rng(42)
        counts = rng.poisson(self.gaussian()).astype(np.uint32)
        fit = analyze_roi(counts, (880, 1120), self.calibration, method='gaussian')
        expected_fwhm = 2 * np.sqrt(2 * np.log(2)) * 20
        self.assertAlmostEqual(fit['centroid_keV'], 1000., delta=0.5)
        self.assertAlmostEqual(fit['fwhm_keV'], expected_fwhm, delta=1.5)
        self.assertAlmostEqual(fit['chi2_red'], 1.0, delta=0.3)

        # Cut off peak
        cut = analyze_roi(counts, (1000, 1120), self.calibration, method='gaussian')
        self.assertIn('cut off', cut['status'])
        self.assertIsNone(cut['fwhm_keV'])

        # Empty / flat
        empty = analyze_roi(np.zeros(4096, dtype=np.uint32), (880, 1120), self.calibration, method='gaussian')
        self.assertIn('No counts', empty['status'])


if __name__ == '__main__':
    unittest.main()
