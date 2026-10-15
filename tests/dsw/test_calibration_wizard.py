import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
from PySide6 import QtWidgets
from calibration_wizard import calculate_two_point_calibration, TwoPointCalibrationDialog


class TestCalibrationWizard(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    def test_calculate_two_point_calibration(self):
        # Na-22: 511 keV at bin 2092.5, 1274.54 keV at bin 5218.0
        offset, gain = calculate_two_point_calibration(2092.5, 511.0, 5218.0, 1274.54)
        self.assertAlmostEqual(gain, (1274.54 - 511.0) / (5218.0 - 2092.5), places=6)
        self.assertAlmostEqual(offset, 511.0 - gain * 2092.5, places=6)

        # Equal channels
        with self.assertRaises(ValueError):
            calculate_two_point_calibration(100.0, 500.0, 100.0, 1000.0)

        # Negative gain
        with self.assertRaises(ValueError):
            calculate_two_point_calibration(100.0, 1000.0, 200.0, 500.0)

    def test_dialog_interaction(self):
        callback_called = [False]
        def get_centroid(ch):
            return 661.66 if ch == 1 else None

        dialog = TwoPointCalibrationDialog(1, (0.0, 0.24414), get_centroid)
        self.assertTrue(dialog.apply_button.isEnabled())

        # Select isotope preset for point 1
        # Index 6 is Cs-137 (661.66 keV)
        dialog.source_menus[1].setCurrentIndex(6)
        self.assertEqual(dialog.energy_spinboxes[1].value(), 661.66)

        # Capture ROI
        dialog.capture_roi(1)
        expected_bin = 661.66 / 0.24414
        self.assertAlmostEqual(dialog.bin_spinboxes[1].value(), expected_bin, places=2)

        # Test apply signal emission
        emitted = []
        dialog.calibration_applied.connect(lambda ch, off, g: emitted.append((ch, off, g)))
        dialog.apply_calibration()
        self.assertEqual(len(emitted), 1)
        self.assertEqual(emitted[0][0], 1)
        self.assertAlmostEqual(emitted[0][1], dialog.computed[0])
        self.assertAlmostEqual(emitted[0][2], dialog.computed[1])
        dialog.close()


if __name__ == '__main__':
    unittest.main()
