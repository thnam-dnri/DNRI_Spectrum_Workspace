import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PySide6 import QtCore, QtTest, QtWidgets

from app import SpectrumPlot, Window
from spectrum_io import measurement, write_spectrum, write_session


class SharedSpectrumView(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    @classmethod
    def tearDownClass(cls):
        QtTest.QTest.qWait(120)
        cls.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def setUp(self):
        self.window = Window()
        self.temporary = tempfile.TemporaryDirectory()
        self.directory = Path(self.temporary.name)

    def tearDown(self):
        self.window.close()
        self.window.worker.thread.join(timeout=3)
        self.window.close()
        self.temporary.cleanup()

    def foreground(self, channel):
        self.window.foreground_menu.setCurrentIndex(self.window.foreground_menu.findData(channel))

    def background(self, channel):
        self.window.background_menu.setCurrentIndex(self.window.background_menu.findData(channel))

    def pair(self):
        self.window.load_frame(1, measurement(np.arange(1, 65), (0., 1.), real_time=10.), grouping=8)
        self.window.load_frame(2, measurement(np.full(32, 80), (0., 2.), real_time=10.), grouping=16)
        self.background(2)

    def test_one_plot_and_background_requires_a_distinct_foreground(self):
        self.assertEqual(len(self.window.findChildren(SpectrumPlot)), 1)
        self.assertIs(self.window.plots[1], self.window.plots[2])
        self.foreground(None)
        self.assertFalse(self.window.background_menu.isEnabled())
        self.background(2)
        self.assertIsNone(self.window.background_menu.currentData())
        self.foreground(2)
        self.assertTrue(self.window.background_menu.isEnabled())
        same = self.window.background_menu.findData(2)
        self.assertFalse(self.window.background_menu.model().item(same).isEnabled())
        self.background(2)
        self.assertIsNone(self.window.background_menu.currentData())

    def test_different_calibrations_share_foreground_edges_and_preserve_counts(self):
        self.pair()
        edges, counts = self.window.background_histogram
        np.testing.assert_array_equal(edges, self.window.curves[1].xData)
        np.testing.assert_allclose(counts, np.full(8, 320.))
        self.assertEqual(counts.sum(), self.window.frames[2]['counts'].sum())
        self.assertEqual(self.window.calibrations[1], [0., 1.])
        self.assertEqual(self.window.calibrations[2], [0., 2.])
        self.assertTrue(self.window.curves[1].isVisible())
        self.assertFalse(self.window.curves[2].isVisible())
        self.assertTrue(self.window.background_curve.isVisible())
        self.assertTrue(self.window.worker.commands.empty())

    def test_background_grouping_never_changes_the_display_grid(self):
        self.pair()
        before = tuple(data.copy() for data in self.window.background_histogram)
        self.window.regroup(2, 40)
        for actual, expected in zip(self.window.background_histogram, before):
            np.testing.assert_array_equal(actual, expected)
        self.window.grouping_menus[1].setCurrentIndex(self.window.grouping_menus[1].findData(16))
        np.testing.assert_array_equal(self.window.background_histogram[0], self.window.curves[1].xData)
        np.testing.assert_allclose(self.window.background_histogram[1], np.full(4, 640.))
        self.foreground(2)
        self.assertIsNone(self.window.background_menu.currentData())
        self.background(1)
        self.assertEqual(self.window.display_stack.currentIndex(), 2)
        np.testing.assert_array_equal(self.window.background_histogram[0], self.window.curves[2].xData)
        self.assertEqual(len(self.window.background_histogram[1]), 1)
        self.assertEqual(self.window.background_histogram[1].sum(), self.window.frames[1]['counts'].sum())

    def test_background_calibration_button_only_edits_background(self):
        self.pair()
        original = self.window.frames[2]['counts'].copy()
        def edit(dialog):
            inputs = dialog.findChildren(QtWidgets.QDoubleSpinBox)
            inputs[0].setValue(8.)
            inputs[1].setValue(2.)
            return QtWidgets.QDialog.DialogCode.Accepted
        with patch('spectrum_view.QtWidgets.QDialog.exec', new=edit):
            self.window.background_calibration_button.click()
        self.assertEqual(self.window.calibrations[1], [0., 1.])
        self.assertEqual(self.window.calibrations[2], [8., 2.])
        self.assertTrue(self.window.calibrated[2])
        self.assertEqual(self.window.background_histogram[1][0], 0.)
        self.assertEqual(self.window.background_histogram[1].sum(), 2240.)
        np.testing.assert_array_equal(original, self.window.frames[2]['counts'])
        self.assertTrue(self.window.worker.commands.empty())

    def test_live_background_updates_overlay_and_subtracted_foreground(self):
        self.pair()
        before = self.window.frames[1]['counts'].copy()
        self.window.comparison_widgets[1]['subtract'].setChecked(True)
        derived, noise = self.window.analysis_data(1)
        np.testing.assert_allclose(derived, before.astype(float)-40.)
        self.assertIsNotNone(noise)
        updated = dict(self.window.frames[2], counts=np.full(32, 160, dtype=np.uint32), live=True)
        self.window.receive('frame', updated)
        np.testing.assert_allclose(self.window.background_histogram[1], np.full(8, 640.))
        np.testing.assert_allclose(self.window.analysis_data(1)[0], before.astype(float)-80.)
        np.testing.assert_array_equal(self.window.frames[1]['counts'], before)
        self.background(None)
        self.assertFalse(self.window.background_curve.isVisible())
        np.testing.assert_array_equal(self.window.analysis_data(1)[0], before)

    def test_double_click_and_cursor_follow_foreground_after_switch(self):
        x = np.arange(4096)+.5
        for ch, center in ((1, 800.), (2, 1700.)):
            counts = np.rint(20+2000*np.exp(-.5*((x-center)/20)**2)).astype(np.uint32)
            self.window.load_frame(ch, measurement(counts, (0., .5), real_time=10.))
        self.window.auto_roi(1, 400.)
        old_bounds = self.window.roi_regions[1].getRegion()
        self.foreground(2)
        self.background(1)
        self.window.show()
        QtTest.QTest.qWait(100)
        plot = self.window.spectrum_plot
        point = plot.mapFromScene(plot.getViewBox().mapViewToScene(QtCore.QPointF(850., 100)))
        QtTest.QTest.mouseDClick(plot.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=point)
        self.assertAlmostEqual(self.window.roi_results[2]['centroid_keV'], 850., delta=.1)
        self.assertEqual(self.window.roi_regions[1].getRegion(), old_bounds)
        self.assertFalse(self.window.roi_regions[1].isVisible())
        self.assertTrue(self.window.roi_regions[2].isVisible())
        scene = plot.getViewBox().mapViewToScene(QtCore.QPointF(850., 100))
        self.window.cursor_moved(2, scene)
        self.assertIn('foreground', self.window.cursor_labels[2].text())
        self.assertIn('background', self.window.cursor_labels[2].text())
        self.foreground(1)
        self.assertTrue(self.window.roi_regions[1].isVisible())
        self.assertFalse(self.window.roi_regions[2].isVisible())
        self.assertTrue(self.window.worker.commands.empty())

    def test_hidden_channel_rois_survive_calibration_and_live_updates(self):
        x = np.arange(4096)+.5
        counts = np.rint(20+1000*np.exp(-.5*((x-800)/20)**2)).astype(np.uint32)
        self.window.load_frame(1, measurement(counts, (0., .5), real_time=10.))
        self.window.auto_roi(1, 400.)
        old = self.window.roi_results[1].copy()
        self.foreground(2)
        self.window.apply_wizard_calibration(1, 10., 1.)
        self.assertAlmostEqual(self.window.roi_results[1]['centroid_keV'], 810., delta=.1)
        self.window.receive('frame', dict(self.window.frames[1], counts=counts*2, live=True))
        self.assertEqual(self.window.roi_results[1]['gross_counts'], old['gross_counts']*2)
        self.assertFalse(self.window.roi_regions[1].isVisible())
        self.foreground(1)
        self.assertTrue(self.window.roi_regions[1].isVisible())
        self.assertAlmostEqual(self.window.roi_results[1]['centroid_keV'], 810., delta=.1)

    def test_view_selection_and_channel_parameters_round_trip_in_session(self):
        self.pair()
        self.foreground(2)
        self.background(1)
        filename = self.directory/'shared.dsw'
        self.window.save_session(filename=filename)
        self.foreground(None)
        self.window.reset_spectra('Reset')
        self.window.open_session(filename=filename)
        self.assertEqual(self.window.foreground_channel, 2)
        self.assertEqual(self.window.background_menu.currentData(), 1)
        self.assertEqual(self.window.calibrations, {1: [0., 1.], 2: [0., 2.]})
        self.assertEqual(self.window.grouping, {1: 8, 2: 16})
        np.testing.assert_array_equal(self.window.background_histogram[0], self.window.curves[2].xData)

    def test_loaded_background_uses_foreground_bins_and_old_sessions_still_subtract(self):
        self.pair()
        reference = measurement(np.full(32, 40), (0., 2.), real_time=10.)
        filename = self.directory/'background.spe'
        write_spectrum(filename, reference, reference['calibration'])
        self.window.load_reference(1, 'background', filename)
        self.assertEqual(self.window.background_menu.currentData(), 'background')
        np.testing.assert_array_equal(self.window.background_histogram[0], self.window.curves[1].xData)
        np.testing.assert_allclose(self.window.background_histogram[1], np.full(8, 160.))
        self.window.comparison_widgets[1]['subtract'].setChecked(True)
        payload = self.window.session_payload()
        payload.pop('view')
        session = self.directory/'old.dsw'
        write_session(session, payload)
        self.window.open_session(filename=session)
        self.assertEqual(self.window.background_menu.currentData(), 'background')
        self.assertIsNotNone(self.window.analysis_data(1)[1])
        self.window.clear_reference(1, 'background')
        self.assertIsNone(self.window.background_menu.currentData())
        self.assertIsNone(self.window.background_histogram)
        self.assertFalse(self.window.background_calibration_button.isEnabled())

    def test_clear_hides_both_spectra_and_foreground_none_restores_empty_view(self):
        self.pair()
        self.window.receive('cleared', None)
        self.assertFalse(self.window.background_curve.isVisible())
        self.assertIsNone(self.window.background_histogram)
        for curve in self.window.curves.values():
            self.assertFalse(curve.isVisible())
        self.foreground(None)
        self.assertFalse(self.window.background_menu.isEnabled())
        self.assertEqual(self.window.analysis_stack.currentIndex(), 0)
        self.assertEqual(self.window.display_stack.currentIndex(), 0)

    def test_load_file_from_background_selector_and_cancel_preserve_selection(self):
        self.pair()
        filename = self.directory/'external.spe'
        frame = measurement(np.full(32, 40), (0., 2.), real_time=10.)
        write_spectrum(filename, frame, frame['calibration'])
        with patch('workspace.QtWidgets.QFileDialog.getOpenFileName', return_value=('', '')):
            self.background('load')
        self.assertEqual(self.window.background_menu.currentData(), 2)
        with patch('workspace.QtWidgets.QFileDialog.getOpenFileName', return_value=(str(filename), '')):
            self.background('load')
        self.assertEqual(self.window.background_menu.currentData(), 'background')
        self.assertFalse(self.window.peaks_dock.isVisible())
        np.testing.assert_allclose(self.window.background_histogram[1], np.full(8, 160.))
        self.assertTrue(self.window.worker.commands.empty())

    def test_file_without_exposure_still_overlays_and_invalid_view_preserves_data(self):
        self.pair()
        frame = measurement(np.full(32, 40), (0., 2.))
        filename = self.directory/'timeless.spe'
        write_spectrum(filename, frame, frame['calibration'])
        self.window.load_reference(1, 'background', filename, show_panel=False)
        self.assertTrue(self.window.background_curve.isVisible())
        self.assertIn('measurement time unavailable', self.window.background_message.text())
        np.testing.assert_allclose(self.window.background_histogram[1], np.full(8, 160.))
        before = self.window.frames[1]
        payload = self.window.session_payload()
        payload['view'] = dict(foreground=1, background=1)
        session = self.directory/'invalid.dsw'
        write_session(session, payload)
        with patch('workspace.QtWidgets.QMessageBox.warning') as warning:
            self.window.open_session(filename=session)
        warning.assert_called_once()
        self.assertIs(self.window.frames[1], before)

    def test_small_window_keeps_peak_analysis_accessible(self):
        self.pair()
        self.window.resize(1080, 780)
        self.window.show()
        QtTest.QTest.qWait(100)
        scroll = self.window.analysis_scroll
        message = self.window.roi_messages[1]
        scroll.ensureWidgetVisible(message)
        self.app.processEvents()
        bottom = scroll.viewport().mapFromGlobal(message.mapToGlobal(QtCore.QPoint(0, message.height()-1))).y()
        self.assertLess(bottom, scroll.viewport().height())
        self.assertGreaterEqual(bottom, 0)

    def test_open_analysis_dock_keeps_plot_controls_separate(self):
        self.pair()
        self.window.resize(1080, 780)
        self.window.show()
        self.window.show_peaks(1)
        QtTest.QTest.qWait(150)
        plot = self.window.spectrum_plot
        cursor = self.window.cursor_labels[1]
        plot_bottom = plot.mapTo(self.window, QtCore.QPoint(0, plot.height())).y()
        cursor_top = cursor.mapTo(self.window, QtCore.QPoint()).y()
        self.assertLessEqual(plot_bottom, cursor_top)
        dock_bottom = self.window.peaks_dock.mapTo(
            self.window, QtCore.QPoint(0, self.window.peaks_dock.height())).y()
        self.assertLessEqual(dock_bottom, self.window.height())
        self.assertGreaterEqual(plot.height(), 240)


if __name__ == '__main__':
    unittest.main()
