import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import tempfile
import unittest
import queue
from types import SimpleNamespace
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PySide6 import QtCore, QtTest, QtWidgets

from app import Window, Worker
from spectrum_io import measurement, write_spectrum, read_spectrum, write_session, read_session


class WorkspaceAcceptance(unittest.TestCase):
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
        x = np.arange(4096)+.5
        counts = np.rint(40+2000*np.exp(-.5*((x-1000)/20)**2)+1200*np.exp(-.5*((x-1700)/28)**2)).astype(np.uint32)
        self.frame = measurement(counts, (0., .5), title='Reference sample', live_time=15., real_time=20.)
        self.window.load_frame(1, self.frame)

    def tearDown(self):
        self.window.close()
        self.window.worker.thread.join(timeout=3)
        self.window.close()
        self.temporary.cleanup()

    def test_multiple_rois_update_independently_and_follow_calibration(self):
        self.window.add_roi(1, (430., 570.), 'First line', 'gaussian')
        self.window.add_roi(1, (760., 940.), 'Second line', 'gaussian')
        self.assertEqual(len(self.window.rois[1]), 2)
        results = [dict(entry['result']) for entry in self.window.rois[1]]
        self.assertAlmostEqual(results[0]['centroid_keV'], 500., delta=.1)
        self.assertAlmostEqual(results[1]['centroid_keV'], 850., delta=.1)
        self.assertTrue(all(entry['result']['converged'] for entry in self.window.rois[1]))
        self.assertTrue(all(entry['result']['fwhm_uncertainty_keV'] > 0 for entry in self.window.rois[1]))
        self.window.inputs[1][0].setValue(10.)
        self.window.inputs[1][1].setValue(1.)
        self.window.calibrate(1)
        for before, after in zip(results, [entry['result'] for entry in self.window.rois[1]]):
            self.assertEqual(before['gross_counts'], after['gross_counts'])
            self.assertAlmostEqual(after['centroid_keV'], 10+2*before['centroid_keV'], places=5)
            self.assertAlmostEqual(after['fwhm_keV'], 2*before['fwhm_keV'], places=5)
        self.window.activate_roi(1, self.window.rois[1][0]['id'])
        self.window.remove_roi(1)
        self.assertEqual(len(self.window.rois[1]), 1)
        self.assertEqual(self.window.rois[1][0]['name'], 'Second line')
        self.assertTrue(self.window.worker.commands.empty())

    def test_session_round_trip_restores_rois_references_notes_and_presets(self):
        self.window.add_roi(1, (430., 570.), 'First line', 'gaussian')
        self.window.add_roi(1, (760., 940.), 'Second line', 'endpoint', 3)
        reference = measurement(np.full(4096, 80), (0., .5), live_time=30, real_time=40)
        filename = self.directory/'background.spe'
        write_spectrum(filename, reference, reference['calibration'])
        self.window.load_reference(1, 'background', filename)
        self.window.comparison_widgets[1]['subtract'].setChecked(True)
        self.window.sample_metadata = {'sample': 'test source', 'detector': 'CH1 scintillator', 'notes': 'geometry A'}
        self.window.preset_mode.setCurrentIndex(1)
        self.window.preset_target.setValue(120.)
        colors = [entry['color'] for entry in self.window.rois[1]]
        session = self.directory/'measurement.dsw'
        self.window.save_session(filename=session)
        self.window.reset_spectra('reset')
        self.window.open_session(filename=session)
        np.testing.assert_array_equal(self.window.frames[1]['counts'], self.frame['counts'])
        self.assertEqual([entry['name'] for entry in self.window.rois[1]], ['First line', 'Second line'])
        self.assertEqual([entry['color'] for entry in self.window.rois[1]], colors)
        self.assertEqual(self.window.sample_metadata['notes'], 'geometry A')
        self.assertTrue(self.window.comparison_widgets[1]['subtract'].isChecked())
        self.assertEqual(self.window.preset_target.value(), 120.)
        self.assertTrue(self.window.worker.commands.empty())

    def test_reference_subtraction_propagates_errors_without_changing_raw_counts(self):
        original = self.window.frames[1]['counts'].copy()
        self.window.add_roi(1, (430., 570.), 'First line', 'gaussian')
        reference = measurement(np.full(2048, 160), (0., 1.), live_time=30., real_time=40.)
        filename = self.directory/'background.n42'
        write_spectrum(filename, reference, reference['calibration'])
        self.window.load_reference(1, 'reference', filename)
        self.assertTrue(self.window.reference_curves[1].isVisible())
        self.window.load_reference(1, 'background', filename)
        self.window.comparison_widgets[1]['subtract'].setChecked(True)
        derived, noise = self.window.analysis_data(1)
        np.testing.assert_allclose(derived, original.astype(float)-40.)
        self.assertIsNotNone(noise)
        self.assertGreater(self.window.roi_results[1]['net_counts_uncertainty'], 0.)
        np.testing.assert_array_equal(self.window.frames[1]['counts'], original)
        exported = self.directory/'foreground.spe'
        self.window.export_spectrum(1, exported)
        np.testing.assert_array_equal(read_spectrum(exported)['counts'], original)

    def test_foreground_open_is_offline_and_malformed_files_preserve_current_data(self):
        filename = self.directory/'external.spe'
        write_spectrum(filename, self.frame, self.frame['calibration'])
        self.window.connected = True
        before = self.window.frames[1]
        self.window.open_spectrum(1, filename)
        self.assertIs(self.window.frames[1], before)
        self.window.connected = False
        filename.write_text('$DATA:\n0 4\n1 2\n')
        with patch('workspace.QtWidgets.QMessageBox.warning') as warning:
            self.window.open_spectrum(1, filename)
            warning.assert_called_once()
        self.assertIs(self.window.frames[1], before)
        self.assertTrue(self.window.worker.commands.empty())

    def test_recorded_channel_two_can_be_opened_in_channel_one_and_saved(self):
        files = list((Path(__file__).parent / 'fixtures' / 'recorded').glob('*-ch2-final.bin'))
        self.assertTrue(files)
        frame = read_spectrum(files[0])
        self.window.load_frame(1, frame)
        self.assertEqual(self.window.frames[1]['original_channel'], 2)
        filename = self.directory/'channel-map.dsw'
        self.window.save_session(filename=filename)
        self.window.open_session(filename=filename)
        np.testing.assert_array_equal(self.window.frames[1]['counts'], frame['counts'])
        self.assertEqual(self.window.frames[1]['original_channel'], 2)

    def test_peak_search_and_csv_report(self):
        self.window.find_all_peaks(1)
        self.assertEqual(len(self.window.rois[1]), 2)
        filename = self.directory/'peaks.csv'
        self.window.export_peak_table(1, filename)
        self.assertEqual(len(filename.read_text().splitlines()), 3)
        self.assertIn('net_counts_uncertainty', filename.read_text().splitlines()[0])

    def test_preset_queues_one_stop_and_autosaves_final(self):
        self.window.frames[1]['loaded'] = False
        self.window.frames[1]['live'] = True
        self.window.connected = True
        self.window.preset_mode.setCurrentIndex(2)
        self.window.preset_target.setValue(100.)
        self.window.receive('running', True)
        self.window.frames[1]['total'] += 100
        with patch.object(self.window.worker, 'send') as send:
            self.window.check_preset(now=self.window.preset.started+1)
            self.window.check_preset(now=self.window.preset.started+2)
            send.assert_called_once_with('stop')
        self.window.receive('running', False)
        final = dict(self.frame, live=False, loaded=False, total=int(self.frame['counts'].sum()))
        self.window.receive('frame', final)
        filename = self.directory/'final.dsw'
        def save(reason):
            write_session(filename, self.window.session_payload())
            return filename
        with patch.object(self.window, 'autosave_session', side_effect=save):
            self.window.receive('complete', 'stop')
        self.assertTrue(filename.exists())
        self.assertIsNone(self.window.preset.started)

    def test_gaussian_diagnostics_builds_residual_plot(self):
        self.window.add_roi(1, (430., 570.), 'First line', 'gaussian')
        self.assertIsNotNone(self.window.roi_residuals[1])
        with patch('workspace.QtWidgets.QDialog.exec', return_value=QtWidgets.QDialog.DialogCode.Accepted) as execute:
            self.window.show_diagnostics(1)
            execute.assert_called_once()
        self.window.remove_roi(1)
        with patch('workspace.QtWidgets.QMessageBox.information') as message:
            self.window.show_diagnostics(1)
            message.assert_called_once()

    def test_shift_double_click_adds_a_second_peak(self):
        self.window.add_roi(1, (430., 570.), 'First line', 'gaussian')
        self.window.show()
        QtTest.QTest.qWait(100)
        self.window.peaks_dock.hide()
        QtTest.QTest.qWait(100)
        plot = self.window.plots[1]
        point = plot.mapFromScene(plot.getViewBox().mapViewToScene(QtCore.QPointF(850., 100)))
        QtTest.QTest.mouseDClick(plot.viewport(), QtCore.Qt.MouseButton.LeftButton, QtCore.Qt.KeyboardModifier.ShiftModifier, point)
        self.assertEqual(len(self.window.rois[1]), 2)
        self.assertEqual(self.window.rois[1][0]['name'], 'First line')
        self.assertAlmostEqual(self.window.rois[1][1]['result']['centroid_keV'], 850., delta=.5)


class WorkerPreservation(unittest.TestCase):
    def test_save_failure_prevents_clear_command(self):
        worker = Worker.__new__(Worker)
        QtCore.QObject.__init__(worker)
        cleared, closed = [], []
        frame = measurement([1, 2, 3], (0, 1), real_time=1.)
        worker.link = SimpleNamespace(state=lambda: False, snapshot=lambda ch, live: dict(frame, channel=ch),
                                      settings=lambda ch: {'polarity': 'P'}, root=Path('.'),
                                      clear=lambda: cleared.append(True), serial=SimpleNamespace(close=lambda: closed.append(True)))
        worker.preservation_context = {}
        worker.commands = queue.Queue()
        worker.commands.put(('clear', None)); worker.commands.put(('quit', None))
        worker.done = worker.live = False
        with patch('uart_worker.write_session', side_effect=OSError('simulated disk failure')):
            worker.run()
        self.assertEqual(cleared, [])
        self.assertEqual(closed, [True])
        self.assertTrue(worker.done)

    def test_preserves_both_final_frames_before_destructive_change(self):
        class LinkStub:
            def state(self):
                return False
            def snapshot(self, channel, live):
                self.calls.append(('snapshot', channel, live))
                return measurement([1, 2, 3], (0, 1), real_time=1, channel=channel)
            def settings(self, channel):
                return {'polarity': 'N' if channel == 2 else 'P'}
        with tempfile.TemporaryDirectory() as directory:
            worker = Worker.__new__(Worker)
            QtCore.QObject.__init__(worker)
            worker.link = LinkStub()
            worker.link.root = Path(directory)
            worker.link.calls = []
            worker.preservation_context = {'channels': {}}
            worker.preserve_before_change('polarity')
            self.assertEqual(worker.link.calls, [('snapshot', 1, False), ('snapshot', 2, False)])
            filename = next(Path(directory).rglob('*.dsw'))
            payload = read_session(filename)
            self.assertEqual(set(payload['channels']), {'1', '2'})
            self.assertEqual(payload['channels']['2']['device_settings']['polarity'], 'N')


if __name__ == '__main__':
    unittest.main()
