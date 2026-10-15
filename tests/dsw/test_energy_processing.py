"""v6 GUI transport, retained-frame settings and calibration invalidation."""
import os
import json
from pathlib import Path
import sys
import tempfile
import unittest
from types import SimpleNamespace
from unittest.mock import patch

os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(ROOT/'src'))
sys.path.insert(0, str(ROOT/'src/dsw'))
from PySide6 import QtWidgets
from app import Window
from uart_worker import WorkerCore
from protocol import Link, ENERGY_PRESETS, decode
from spectrum_io import frame_record, restore_frame
from mock_dual_mca import MockPort
from dual_mca_protocol import decode_snapshot, decode_live_frame


class EnergyProcessingTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.TemporaryDirectory()
        self.addCleanup(self.directory.cleanup)
        self.link = Link.__new__(Link)
        self.link.root = Path(self.directory.name)
        self.link.serial = MockPort(version=6)
        self.link.protocol_version = 6
        self.link.independent_channel_control = True
        self.link.channel_acquiring = {1: False, 2: False}
        self.link.running = False
        self.link.last = {}

    def test_dialog_custom_pre_and_post_map_to_total_gate(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = Window()
        window.connected = True
        window.protocol_version = 6
        window.device_settings[1] = {'energy': self.link.settings(1)['energy']}
        def customize(dialog):
            pre = dialog.findChild(QtWidgets.QDoubleSpinBox, 'pre_trigger_us')
            post = dialog.findChild(QtWidgets.QDoubleSpinBox, 'post_trigger_us')
            total = dialog.findChild(QtWidgets.QLabel, 'total_gate')
            apply = dialog.findChild(QtWidgets.QDialogButtonBox).button(QtWidgets.QDialogButtonBox.Ok)
            self.assertEqual((pre.value(), post.value()), (.5, 4.))
            self.assertEqual(total.text(), '4.50 µs (225 samples)')
            pre.setValue(.51)
            self.assertFalse(apply.isEnabled())
            pre.setValue(.5); post.setValue(81.92)
            self.assertFalse(apply.isEnabled())
            pre.setValue(0); post.setValue(.02)
            self.assertTrue(apply.isEnabled())
            pre.setValue(.4); post.setValue(12.)
            self.assertTrue(apply.isEnabled())
            self.assertEqual(total.text(), '12.40 µs (620 samples)')
            return QtWidgets.QDialog.Accepted
        try:
            with patch('app.QtWidgets.QDialog.exec', new=customize), patch.object(window, 'action') as action:
                window.open_energy_settings(1)
            action.assert_called_once_with('energy', (1, dict(mode='charge', gate_samples=620,
                                                            pretrigger_samples=20, area_shift=6)))
        finally:
            window.close(); app.processEvents()

    def test_dialog_presets_recalculate_post_trigger_and_cancel_is_read_only(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = Window()
        window.connected = True
        window.protocol_version = 6
        window.device_settings[2] = {'energy': self.link.settings(2)['energy']}
        def customize(dialog):
            preset = next(combo for combo in dialog.findChildren(QtWidgets.QComboBox)
                          if combo.findData('CsI(Tl)') >= 0)
            preset.setCurrentIndex(preset.findData('CsI(Tl)'))
            pre = dialog.findChild(QtWidgets.QDoubleSpinBox, 'pre_trigger_us')
            post = dialog.findChild(QtWidgets.QDoubleSpinBox, 'post_trigger_us')
            self.assertEqual((pre.value(), post.value()), (.1, 7.9))
            self.assertEqual(preset.currentData(), 'CsI(Tl)')
            post.setValue(10.)
            self.assertIsNone(preset.currentData())
            return QtWidgets.QDialog.Rejected
        try:
            with patch('app.QtWidgets.QDialog.exec', new=customize), patch.object(window, 'action') as action:
                window.open_energy_settings(2)
            action.assert_not_called()
        finally:
            window.close(); app.processEvents()

    def test_v6_shared_and_gui_decoders_agree(self):
        for live in (False, True):
            raw = self.link.serial._live_frame(1) if live else self.link.serial._frame(1)
            gui = decode(raw, 1, live)
            shared, counts = decode_live_frame(raw, 1) if live else decode_snapshot(raw, 1)
            self.assertEqual(gui['energy'], shared['energy_configuration'])
            self.assertEqual(gui['total'], int(counts.sum()))
            self.assertEqual(gui['protocol_version'], 6)

    def test_link_identity_settings_and_channel_configuration(self):
        self.assertEqual(self.link.identity()[1], 6)
        first = self.link.settings(1)
        self.link.set_energy(2, ENERGY_PRESETS['CsI(Tl)'])
        self.assertEqual(self.link.settings(2)['energy']['gate_samples'], 400)
        self.assertEqual(self.link.settings(1), first)
        frame = self.link.snapshot(2, False)
        self.assertEqual(len(frame['raw']), 65692)
        self.assertEqual(frame['energy']['gate_samples'], 400)

    def test_retained_raw_frame_controls_processing_metadata(self):
        frame = self.link.snapshot(1, False)
        restored = restore_frame(frame_record(frame))
        self.assertEqual(restored['energy'], frame['energy'])
        record = frame_record(frame)
        record['energy'] = dict(record['energy'], gate_samples=400)
        with self.assertRaisesRegex(ValueError, 'energy settings'):
            restore_frame(record)

    def test_gui_calibration_bound_to_mode_and_gate(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = Window()
        try:
            frame = self.link.snapshot(1, False)
            window.receive('frame', frame)
            window.calibrate(1)
            self.assertTrue(window.calibrated[1])
            window.receive('frame', self.link.snapshot(1, False))
            self.assertTrue(window.calibrated[1])
            self.link.set_energy(1, ENERGY_PRESETS['CsI(Tl)'])
            window.receive('frame', self.link.snapshot(1, False))
            self.assertFalse(window.calibrated[1])
            self.assertIn('RECALIBRATE', window.scale_labels[1].text())
            window.connected = True
            window.protocol_version = 6
            window.device_settings[1]['energy'] = None
            window.controls()
            self.assertFalse(window.energy_actions[1].isEnabled())
            window.device_settings[1]['energy'] = self.link.settings(1)['energy']
            window.controls()
            self.assertTrue(window.energy_actions[1].isEnabled())
        finally:
            window.close()
            for _ in range(20):
                app.processEvents()

    def test_queued_preservation_does_not_attach_stale_calibration(self):
        self.link.serial._add_event(1, 200)
        events = []
        worker = WorkerCore()
        worker.link = self.link
        worker.event = SimpleNamespace(emit=lambda kind, value: events.append((kind, value)))
        worker.preservation_context = {'channels': {'1': dict(calibration=[0., .1], calibrated=True,
                                                           calibration_energy=None, grouping=8, rois=[])}}
        worker.preserve_before_change('energy')
        path = next(value for kind, value in events if kind == 'preserved')
        record = json.loads(Path(path).read_text())
        self.assertFalse(record['channels']['1']['calibrated'])
        self.assertEqual(record['channels']['1']['frame']['energy']['mode'], 'charge')
        self.assertTrue(worker.preservation_context['channels']['1']['calibrated'])

    def test_readback_invalidates_calibration_before_first_frame(self):
        app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])
        window = Window()
        try:
            window.calibrated[1] = True
            window.update_device_settings({1: {'energy': self.link.settings(1)['energy']}})
            self.assertFalse(window.calibrated[1])
        finally:
            window.close()
            for _ in range(20):
                app.processEvents()


if __name__ == '__main__':
    unittest.main()
