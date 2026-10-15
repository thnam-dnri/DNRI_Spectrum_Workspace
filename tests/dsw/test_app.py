import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import unittest
import tempfile
import json
from unittest.mock import patch
from pathlib import Path
import numpy as np
from PySide6 import QtCore, QtGui, QtTest, QtWidgets
from PySide6.QtWidgets import QApplication
from app import Window
from protocol import decode, Link
from histogram import energy_histogram, DEFAULT_GAIN, GROUPS


class Acceptance(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QApplication.instance() or QApplication([])
        cls.files = list((Path(__file__).parent / 'fixtures' / 'recorded').glob('*-final.bin'))

    @classmethod
    def tearDownClass(cls):
        # Finish pending close timers and queued worker events before Qt teardown.
        QtTest.QTest.qWait(120)
        cls.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_hardware_frames_and_corruption(self):
        self.assertTrue(self.files)
        for path in self.files:
            raw = path.read_bytes()
            frame = decode(raw, raw[5], False)
            self.assertEqual(frame['total'], frame['stats']['histogrammed'])
            damaged = bytearray(raw)
            damaged[200] ^= 1
            with self.assertRaisesRegex(ValueError, 'CRC'):
                decode(damaged, raw[5], False)
            with self.assertRaisesRegex(ValueError, 'identity'):
                decode(raw, 3 - raw[5], False)

    def test_mid_acquisition_calibration_has_no_uart_writes(self):
        window = Window()
        try:
            path = max(self.files, key=lambda p: p.stat().st_size)
            raw = path.read_bytes()
            frame = decode(raw, raw[5], False)
            ch = frame['channel']
            window.receive('frame', frame)
            window.running = True
            before = frame['counts'].copy()
            window.inputs[ch][0].setValue(12)
            window.inputs[ch][1].setValue(.5)
            window.calibrate(ch)
            self.assertTrue(np.array_equal(before, frame['counts']))
            self.assertTrue(window.worker.commands.empty())
            edges, counts = window.host_histograms[ch]
            self.assertEqual(edges[0], 12)
            self.assertEqual(len(counts), 997)
            self.assertEqual(int(counts.sum()) + sum(window.range_counts[ch]), frame['total'])
            self.assertTrue(window.running)
            window.show()
            self.app.processEvents()
            preview = Path.cwd() / 'outputs' / 'dsw' / 'validation' / 'test_preview.png'
            preview.parent.mkdir(parents=True, exist_ok=True)
            window.grab().save(str(preview))
        finally:
            window.close()
            window.worker.thread.join(timeout=3)
            window.close()

    def test_energy_rebin_conserves_counts(self):
        raw = np.arange(16384, dtype=np.uint32)
        original = raw.copy()
        for group in GROUPS:
            edges, host, under, over = energy_histogram(raw, group=group)
            self.assertEqual(len(host), (16384 + group - 1)//group)
            self.assertEqual(edges[0], 0)
            self.assertEqual(edges[-1], 4000)
            self.assertEqual(edges[1], DEFAULT_GAIN * group)
            self.assertEqual(int(host[0]), sum(range(group)))
            self.assertEqual(int(host.sum()) + under + over, int(raw.sum()))
            self.assertEqual((under, over), (0, 0))
        edges, host, under, over = energy_histogram(raw, (-100, .5), 24)
        self.assertTrue(np.all(edges >= 0) and np.all(edges <= 4000))
        self.assertGreater(under, 0)
        self.assertGreater(over, 0)
        self.assertEqual(int(host.sum()) + under + over, int(raw.sum()))
        edges, host, under, over = energy_histogram(raw, (5000, 1))
        self.assertEqual(int(host.sum()), 0)
        self.assertEqual(over, int(raw.sum()))
        np.testing.assert_array_equal(raw, original)

    def test_clear_both_hardware_request_and_host_reset(self):
        window = Window()
        try:
            raw = self.files[0].read_bytes()
            frame = decode(raw, raw[5], False)
            frame2 = dict(frame, channel=3-frame['channel'])
            window.receive('frame', frame)
            window.receive('frame', frame2)
            window.connected = True
            window.controls()
            with patch.object(window.worker, 'send') as send:
                with patch('app.QtWidgets.QMessageBox.question', return_value=QtWidgets.QMessageBox.Yes):
                    window.clear_buttons[1].click()
                send.assert_called_once_with('clear')
            window.receive('cleared', None)
            window.receive('complete', 'clear')
            self.assertEqual(window.frames, {})
            self.assertEqual(window.host_histograms, {})
            for ch in (1, 2):
                data = window.curves[ch].yData
                self.assertTrue(data is None or len(data) == 0)
            window.running = True
            window.controls()
            self.assertFalse(window.clear_buttons[1].isEnabled())
            self.assertFalse(window.clear_buttons[2].isEnabled())
        finally:
            window.close()
            window.worker.thread.join(timeout=3)
            window.close()

    def test_export_only_selected_channel(self):
        window = Window()
        try:
            raw = self.files[0].read_bytes()
            frame = decode(raw, raw[5], False)
            ch = frame['channel']
            window.receive('frame', frame)
            window.receive('frame', dict(frame, channel=3-ch))
            window.auto_roi(ch)
            self.assertIn(ch, window.roi_results)
            with tempfile.TemporaryDirectory() as directory:
                with patch('app.QtWidgets.QFileDialog.getExistingDirectory', return_value=directory):
                    window.save(ch)
                files = list(Path(directory).rglob('*'))
                self.assertFalse(any(p.name.startswith(f'ch{3-ch}') for p in files))
                csv = next(p for p in files if p.suffix == '.csv')
                self.assertEqual(csv.read_text().splitlines()[0], 'energy_low_keV,energy_high_keV,counts')
                total = sum(int(line.split(',')[-1]) for line in csv.read_text().splitlines()[1:])
                self.assertEqual(total, frame['total'])
                self.assertEqual(next(p for p in files if p.suffix == '.bin').read_bytes(), raw)
                metadata = json.loads(next(p for p in files if p.suffix == '.json').read_text())
                self.assertEqual(metadata['roi_analysis'], window.roi_results[ch])
        finally:
            window.close()
            window.worker.thread.join(timeout=3)
            window.close()

    def gaussian_frame(self, ch=1, live=False):
        raw = self.files[0].read_bytes()
        template = decode(raw, raw[5], False)
        coordinates = np.arange(16384) + .5
        counts = np.rint(30 + 4000*np.exp(-.5*((coordinates-3100)/28)**2) +
                         1500*np.exp(-.5*((coordinates-7200)/40)**2)).astype(np.uint32)
        return dict(template, channel=ch, live=live, counts=counts, total=int(counts.sum()))

    def test_double_click_selects_peak_and_roi_controls_stay_host_only(self):
        window = Window()
        try:
            frame = self.gaussian_frame()
            before = frame['counts'].copy()
            window.receive('frame', frame)
            window.running = True
            window.show()
            self.app.processEvents()
            plot = window.plots[1]
            with patch.object(window.worker, 'send') as send:
                scene = plot.getViewBox().mapViewToScene(QtCore.QPointF(7200*DEFAULT_GAIN, 1500))
                position = plot.mapFromScene(scene)
                QtTest.QTest.mouseDClick(plot.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=position)
                self.app.processEvents()
                result = window.roi_results[1]
                self.assertAlmostEqual(result['centroid_keV'], 7200*DEFAULT_GAIN, delta=.1)
                self.assertAlmostEqual(result['fwhm_keV'], 2*np.sqrt(2*np.log(2))*40*DEFAULT_GAIN, delta=.3)
                window.zoom_roi(1)
                low, high = plot.getViewBox().viewRange()[0]
                self.assertTrue(low < result['centroid_keV'] < high)
                self.assertLess(high-low, 250.)
                window.regroup(1, 40)
                self.assertEqual(window.roi_results[1], result)
                window.roi_regions[1].setRegion((1700., 1810.))
                self.assertEqual(window.roi_inputs[1][0].value(), 1700.)
                window.smoothing[1].setCurrentIndex(2)
                self.assertEqual(window.roi_results[1]['smoothing_native_bins'], 5)
                self.assertTrue(window.running)
                send.assert_not_called()
            np.testing.assert_array_equal(frame['counts'], before)
            self.assertFalse(window.roi_regions[2].isVisible())
        finally:
            window.close()
            window.worker.thread.join(timeout=3)
            window.close()

    def test_roi_calibration_tracks_native_bins_and_live_updates(self):
        window = Window()
        try:
            frame = self.gaussian_frame(live=True)
            window.receive('frame', frame)
            window.auto_roi(1, 3100*DEFAULT_GAIN)
            old = window.roi_results[1].copy()
            window.inputs[1][0].setValue(10.)
            window.inputs[1][1].setValue(.5)
            with patch.object(window.worker, 'send') as send:
                window.calibrate(1)
                new = window.roi_results[1].copy()
                self.assertEqual(new['gross_counts'], old['gross_counts'])
                self.assertAlmostEqual(new['centroid_keV'], 10+old['centroid_keV']*.5/DEFAULT_GAIN, places=6)
                self.assertAlmostEqual(new['fwhm_keV'], old['fwhm_keV']*.5/DEFAULT_GAIN, places=6)
                window.receive('frame', dict(frame, counts=frame['counts']*2))
                self.assertEqual(window.roi_results[1]['gross_counts'], 2*new['gross_counts'])
                self.assertEqual(window.roi_results[1]['spectrum_mode'], 'LIVE')
                self.assertTrue(window.roi_results[1]['calibrated'])
                send.assert_not_called()
            window.receive('cleared', None)
            self.assertEqual(window.roi_results, {})
            self.assertFalse(window.roi_regions[1].isVisible())
            self.assertFalse(window.width_markers[1][0].isVisible())
            self.assertFalse(window.roi_buttons[1]['zoom'].isEnabled())
        finally:
            window.close()
            window.worker.thread.join(timeout=3)
            window.close()

    def test_mouse_drag_moves_roi_edge_and_updates_measurements(self):
        window = Window()
        try:
            window.receive('frame', self.gaussian_frame())
            window.auto_roi(1, 3100*DEFAULT_GAIN)
            window.zoom_roi(1)
            window.show()
            # Let deferred axis/layout changes settle before mapping viewport coordinates.
            QtTest.QTest.qWait(100)
            plot = window.plots[1]
            original = window.roi_regions[1].getRegion()
            height = plot.getViewBox().viewRange()[1][1] * .75
            start = plot.mapFromScene(plot.getViewBox().mapViewToScene(QtCore.QPointF(original[0], height)))
            end = plot.mapFromScene(plot.getViewBox().mapViewToScene(QtCore.QPointF(original[0]+5., height)))
            QtTest.QTest.mouseMove(plot.viewport(), start, delay=30)
            QtTest.QTest.qWait(50)
            QtTest.QTest.mousePress(plot.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=start)
            QtTest.QTest.qWait(50)
            move = QtGui.QMouseEvent(QtCore.QEvent.Type.MouseMove, QtCore.QPointF(end),
                                    QtCore.QPointF(plot.viewport().mapToGlobal(end)),
                                    QtCore.Qt.MouseButton.NoButton, QtCore.Qt.MouseButton.LeftButton,
                                    QtCore.Qt.KeyboardModifier.NoModifier)
            self.app.sendEvent(plot.viewport(), move)
            QtTest.QTest.qWait(50)
            QtTest.QTest.mouseRelease(plot.viewport(), QtCore.Qt.MouseButton.LeftButton, pos=end)
            QtTest.QTest.qWait(50)
            moved = window.roi_regions[1].getRegion()
            self.assertAlmostEqual(moved[0], original[0]+5., delta=.2)
            self.assertEqual(moved[1], original[1])
            self.assertAlmostEqual(window.roi_inputs[1][0].value(), moved[0], delta=.0001)
            self.assertEqual(window.roi_results[1]['bounds_keV'], list(moved))
            self.assertTrue(window.worker.commands.empty())
        finally:
            window.close()
            window.worker.thread.join(timeout=3)
            window.close()

    def test_roi_cursor_and_empty_peak_feedback(self):
        window = Window()
        try:
            frame = self.gaussian_frame()
            window.receive('frame', frame)
            window.show()
            self.app.processEvents()
            point = window.plots[1].getViewBox().mapViewToScene(QtCore.QPointF(3100*DEFAULT_GAIN, 100))
            window.cursor_moved(1, point)
            self.assertTrue(window.cursors[1].isVisible())
            self.assertIn('counts', window.cursor_labels[1].text())
            self.assertIn('756.', window.cursor_labels[1].text())
            window.auto_roi(1, 3000.)
            self.assertNotIn(1, window.roi_results)
            self.assertIn('peak', window.roi_messages[1].text())
            window.set_roi(1, 100., 100.)
            self.assertFalse(window.roi_regions[1].isVisible())
            window.set_roi(1, 700., 820.)
            self.assertIn(1, window.roi_results)
            window.remove_roi(1)
            self.assertNotIn(1, window.roi_results)
            for label in window.roi_metrics[1].values():
                self.assertEqual(label.text(), '—')
        finally:
            window.close()
            window.worker.thread.join(timeout=3)
            window.close()

    def test_gaussian_fit_mode_and_overlay_curve(self):
        window = Window()
        try:
            frame = self.gaussian_frame()
            window.receive('frame', frame)
            window.show()
            self.app.processEvents()
            # Select peak near 3100
            window.auto_roi(1, 3100 * DEFAULT_GAIN)
            self.assertIn(1, window.roi_results)
            self.assertFalse(window.fit_curves[1].isVisible())

            # Switch to Gaussian fit
            window.roi_methods[1].setCurrentIndex(1)
            result = window.roi_results[1]
            self.assertEqual(result['method'], 'Gaussian fit (linear background)')
            self.assertAlmostEqual(result['centroid_keV'], 3100 * DEFAULT_GAIN, delta=0.5)
            self.assertTrue(window.fit_curves[1].isVisible())
            x_fit, y_fit = window.fit_curves[1].getData()
            self.assertGreater(len(x_fit), 10)
            self.assertIn('χ²', window.roi_messages[1].text())

            # Remove ROI hides fit curve
            window.remove_roi(1)
            self.assertFalse(window.fit_curves[1].isVisible())
        finally:
            window.close()
            window.worker.thread.join(timeout=3)
            window.close()

    def test_two_point_calibration_wizard_application(self):
        window = Window()
        try:
            frame = self.gaussian_frame()
            window.receive('frame', frame)
            window.show()
            self.app.processEvents()

            # Apply calibration via wizard callback
            window.apply_wizard_calibration(1, 15.0, 0.45)
            self.assertEqual(window.inputs[1][0].value(), 15.0)
            self.assertEqual(window.inputs[1][1].value(), 0.45)
            self.assertTrue(window.calibrated[1])
            self.assertEqual(window.scale_labels[1].text(), 'USER CALIBRATION')
            self.assertTrue(window.worker.commands.empty())
        finally:
            window.close()
            window.worker.thread.join(timeout=3)
            window.close()

    def test_source_selection_and_auto_clear(self):
        window = Window()
        try:
            frame = self.gaussian_frame()
            window.receive('frame', frame)
            window.connected = True
            window.controls()

            # Selecting ADC changes only source, preserving individual polarities.
            with patch.object(window.worker, 'send') as send:
                window.source_menu.setCurrentIndex(2)
                window.source_menu.activated.emit(2)
                send.assert_called_once_with('adc')

            # Receiving adc settings resets spectra
            settings = {1: {'source': [0]*8, 'polarity': 'P'}, 2: {'source': [0]*8, 'polarity': 'N'}}
            window.receive('adc', settings)
            window.receive('complete', 'adc')
            self.assertEqual(len(window.frames), 0)
            self.assertEqual(window.source_menu.currentText(), 'ADC')
            self.assertEqual(window.polarity_menus[1].currentIndex(), 0)
            self.assertEqual(window.polarity_menus[2].currentIndex(), 1)
            self.assertIn('ADC source ready · automatically cleared', window.status.text())
            self.assertTrue(all(menu.isEnabled() for menu in window.polarity_menus.values()))

            physical_frame = dict(frame, source=(0, *frame['source'][1:]), negative=False)
            window.receive('frame', physical_frame)
            with patch.object(window.worker, 'send') as send:
                window.source_menu.activated.emit(2)
                window.polarity_menus[1].activated.emit(0)
                send.assert_not_called()
            self.assertIs(window.frames[1], physical_frame)
            self.assertEqual(window.polarity_menus[2].currentData(), 'N')
        finally:
            window.close()
            window.worker.thread.join(timeout=3)
            window.close()

    def test_polarity_selection_per_channel(self):
        window = Window()
        try:
            settings = {ch: {'source': [0]*8, 'polarity': 'P'} for ch in (1, 2)}
            window.receive('connected', dict(running=False, settings=settings))
            frame = self.gaussian_frame()
            frame = dict(frame, source=(0, *frame['source'][1:]), negative=False)
            window.receive('frame', frame)
            for ch, polarity in ((1, 'N'), (2, 'N'), (1, 'P'), (2, 'P')):
                other = 3-ch
                preserved = window.polarity_menus[other].currentData()
                with patch.object(window.worker, 'send') as send:
                    index = 1 if polarity == 'N' else 0
                    window.polarity_menus[ch].setCurrentIndex(index)
                    window.polarity_menus[ch].activated.emit(index)
                    send.assert_called_once_with('polarity', (ch, polarity))
                settings[ch] = dict(settings[ch], polarity=polarity)
                window.receive('polarity', dict(channel=ch, polarity=polarity, settings=settings))
                window.receive('complete', 'polarity')
                self.assertEqual(window.polarity_menus[ch].currentData(), polarity)
                self.assertEqual(window.polarity_menus[other].currentData(), preserved)
                self.assertEqual(window.source_menu.currentText(), 'ADC')
                self.assertEqual(window.frames, {})
                self.assertIn(window.adc_polarity_summary(), window.status.text())
        finally:
            window.close()
            window.worker.thread.join(timeout=3)
            window.close()

    def test_adc_polarity_editing_requires_idle_adc_channel(self):
        window = Window()
        try:
            mixed = {1: {'source': [0]*8, 'polarity': 'N'},
                     2: {'source': [1]*8, 'polarity': 'P'}}
            window.receive('connected', dict(running=False, settings=mixed))
            self.assertTrue(window.polarity_menus[1].isEnabled())
            self.assertFalse(window.polarity_menus[2].isEnabled())
            with patch.object(window.worker, 'send') as send:
                window.polarity_menus[2].setCurrentIndex(1)
                window.polarity_selected(2, 'N')
                send.assert_not_called()
            self.assertEqual(window.polarity_menus[2].currentData(), 'P')
            for connected, busy, running in ((False, False, False), (True, True, False), (True, False, True)):
                window.connected, window.busy, window.running = connected, busy, running
                window.controls()
                self.assertFalse(window.polarity_menus[1].isEnabled())
                with patch.object(window.worker, 'send') as send:
                    window.polarity_menus[1].setCurrentIndex(0)
                    window.polarity_selected(1, 'P')
                    send.assert_not_called()
                self.assertEqual(window.polarity_menus[1].currentData(), 'N')
        finally:
            window.close()
            window.worker.thread.join(timeout=3)
            window.close()

    def test_adc_polarity_readback_updates_both_channels_without_writes(self):
        window = Window()
        try:
            settings = {1: {'source': [0]*8, 'polarity': 'P'}, 2: {'source': [0]*8, 'polarity': 'N'}}
            with patch.object(window.worker, 'send') as send:
                window.receive('connected', dict(running=False, settings=settings))
                self.assertEqual(window.polarity_menus[1].currentData(), 'P')
                self.assertEqual(window.polarity_menus[2].currentData(), 'N')
                changed = {1: dict(settings[1], polarity='N'), 2: dict(settings[2], polarity='P')}
                window.receive('polarity', dict(channel=1, polarity='N', settings=changed))
                self.assertEqual(window.polarity_menus[1].currentData(), 'N')
                self.assertEqual(window.polarity_menus[2].currentData(), 'P')
                self.assertEqual(window.source_menu.currentText(), 'ADC')
                send.assert_not_called()
        finally:
            window.close()
            window.worker.thread.join(timeout=3)
            window.close()

    def test_link_synthetic_and_adc_clear_and_polarity(self):
        link = Link.__new__(Link)
        link.last = {1: np.zeros(16384)}
        link.running = True
        calls = []
        selected = [1]
        polarities = {1: 'P', 2: 'P'}

        def fake_query(cmd, size=1):
            calls.append(cmd)
            if cmd == b'?':
                return b'0'
            if cmd == b'R':
                return b'R'
            if cmd == b'V':
                return b'\x00'*32
            if cmd == b'F':
                return polarities[selected[0]].encode()
            return b'K'

        link.query = fake_query
        def fake_ack(cmd, exp=b'K'):
            calls.append(cmd)
            if cmd in (b'P', b'N'):
                polarities[selected[0]] = cmd.decode()
        def fake_select(ch):
            calls.append(f'select_{ch}')
            selected[0] = ch
        link.ack = fake_ack
        link.state = lambda: False
        link.ready = lambda: None
        link.select = fake_select
        link.source = lambda ch, p: calls.append(f'source_{ch}')
        link.settings = lambda ch: dict(source=[1, 1, 100, 25000, 16770683, 0, 0, 0])

        # Test synthetic clears and sets positive
        link.synthetic()
        self.assertIn(b'C', calls)
        self.assertEqual(link.last, {})

        # Test adc negative clears and sets negative
        calls.clear()
        link.adc('N')
        self.assertIn(b'C', calls)
        self.assertIn(b'N', calls)

        # Test set_polarity
        calls.clear()
        link.set_polarity(1, 'N')
        self.assertIn(b'C', calls)
        self.assertIn('select_1', calls)
        self.assertIn(b'N', calls)


if __name__ == '__main__':
    unittest.main()
