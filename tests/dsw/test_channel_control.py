"""Independent acquisition protocol and worker tests; no physical hardware writes."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
import json
import queue
import struct
import tempfile
import threading
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

import numpy as np
from PySide6 import QtCore, QtTest, QtWidgets

from app import Window, Worker
from protocol import Link, STATS
from spectrum_io import read_session


class FirmwareWire:
    def __init__(self, supported=True, mask=0):
        self.supported, self.mask, self.selected = supported, mask, 1
        self.commands = []
        self.counts = {ch: np.full(16384, ch, dtype=np.uint32) for ch in (1, 2)}
        self.ticks = {1: 50000000, 2: 100000000}
        self.corrupt_peer = False
        self.bad_q = None
        self.last_dump = None
        self.baudrate = 115200
        self.closed = False

    def query(self, command, size=1):
        self.commands.append(command)
        if command == b'I':
            return struct.pack('<4sBBBBII', b'DMC2', 5, 2, 14, 3, 16384, 50000000)
        if command == b'H':
            return b'1' if self.supported else b'?'
        if command == b'Q':
            return self.bad_q or str(self.mask).encode() if self.supported else b'?'
        if command == b'?':
            return b'1' if self.mask else b'0'
        if command in (b'J', b'K'):
            self.selected = 1 if command == b'J' else 2
            return b'K'
        if command in (b'A', b'O'):
            if not self.supported:
                return b'?'
            bit = 1 << (self.selected-1)
            self.mask = self.mask | bit if command == b'A' else self.mask & ~bit
            if self.corrupt_peer:
                self.mask ^= 1 << (2-self.selected)
            return command
        if command == b'S':
            self.mask = 3
            return b'A'
        if command == b'E':
            self.mask = 0
            return b'O'
        if command == b'C':
            if self.mask:
                return b'B'
            for ch in (1, 2):
                self.counts[ch].fill(0)
                self.ticks[ch] = 0
            return b'C'
        if command == b'R':
            return b'R'
        if command == b'T':
            total = int(self.counts[self.selected].sum(dtype=np.uint64))
            stats = dict.fromkeys(STATS, 0)
            stats.update(histogrammed=total, dpp_valid=total, acquisition_ticks=self.ticks[self.selected])
            return struct.pack('<9Q', *(stats[key] for key in STATS))
        if command.startswith(b'U'):
            return b'K'
        raise AssertionError(f'Unexpected command {command!r}')

    def advance(self):
        for ch in (1, 2):
            if self.mask & (1 << (ch-1)):
                self.counts[ch] += ch
                self.ticks[ch] += 50000000

    def write(self, command):
        self.commands.append(command)
        assert command in (b'D', b'L')
        if command == b'D':
            assert not self.mask & (1 << (self.selected-1)) if self.supported else not self.mask
        self.last_dump = (self.selected, command == b'L')

    def read(self, size, timeout=2):
        ch, live = self.last_dump
        total = int(self.counts[ch].sum(dtype=np.uint64))
        values = dict.fromkeys(STATS, 0)
        values.update(histogrammed=total, dpp_valid=total, acquisition_ticks=self.ticks[ch])
        stats = struct.pack('<9Q', *(values[key] for key in STATS))
        raw = (struct.pack('<4sBBBBII', b'SPL5' if live else b'SPC2', 5, ch, 14, 3, 16384, 1)
               + stats + bytes(16) + bytes(32) + self.counts[ch].astype('<u4').tobytes()
               + (stats if live else b''))
        raw += struct.pack('<I', zlib.crc32(raw))
        assert len(raw) == size
        self.commands.append(('frame_complete', ch, live))
        return raw

    def close(self):
        self.closed = True


def make_link(directory, supported=True, mask=0):
    wire = FirmwareWire(supported, mask)
    link = Link.__new__(Link)
    link.root = Path(directory)
    link.serial = wire
    link.query, link.read = wire.query, wire.read
    link.last, link.control_events = {}, []
    link.independent_channel_control = False
    link.channel_acquiring = None
    link.detect_channel_control()
    link.state()
    return link, wire


class ChannelProtocol(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_connect_probes_capability_and_states_without_start_stop_or_clear(self):
        wire = FirmwareWire(mask=2)
        for name in ('open', 'reset_input_buffer'):
            setattr(wire, name, lambda: None)
        with patch('protocol.serial.Serial', return_value=wire), patch.object(Link, 'query', side_effect=wire.query), patch('protocol.time.monotonic', side_effect=[0., 11.]):
            link = Link('simulated-uart', self.root)
        self.assertEqual(wire.commands, [b'I', b'H', b'Q'])
        self.assertTrue(link.independent_channel_control)
        self.assertEqual(link.channel_acquiring, {1: False, 2: True})
        self.assertTrue(link.running)

    def test_all_masks_and_unsupported_firmware_remain_distinct(self):
        for mask in range(4):
            link, _ = make_link(self.root, mask=mask)
            self.assertEqual(link.channel_acquiring, {1: bool(mask & 1), 2: bool(mask & 2)})
            self.assertEqual(link.running, bool(mask))
        link, wire = make_link(self.root, supported=False, mask=3)
        self.assertIsNone(link.channel_acquiring)
        self.assertTrue(link.running)
        before = wire.commands.copy()
        for operation in (link.start, link.stop):
            with self.assertRaisesRegex(RuntimeError, 'unsupported'):
                operation(1)
        self.assertEqual(wire.commands, before)
        self.assertFalse(link.independent_channel_control)

    def test_invalid_capability_is_rejected_without_guessing_from_version(self):
        link, wire = make_link(self.root)
        query = wire.query
        link.query = lambda command, size=1: b'0' if command == b'H' else query(command, size)
        with self.assertRaisesRegex(ValueError, 'capability'):
            link.detect_channel_control()
        self.assertNotIn(b'A', wire.commands)
        self.assertNotIn(b'O', wire.commands)

    def test_both_start_stop_directions_verify_and_preserve_peer_counts_and_ticks(self):
        for ch in (1, 2):
            with self.subTest(channel=ch):
                peer = 3-ch
                link, wire = make_link(self.root, mask=1 << (peer-1))
                counts, ticks = wire.counts[peer].copy(), wire.ticks[peer]
                link.start(ch)
                self.assertEqual(wire.mask, 3)
                link.stop(ch)
                self.assertEqual(wire.mask, 1 << (peer-1))
                np.testing.assert_array_equal(wire.counts[peer], counts)
                self.assertEqual(wire.ticks[peer], ticks)
                self.assertNotIn(b'E', wire.commands)
                self.assertNotIn(b'S', wire.commands)
                self.assertNotIn(b'C', wire.commands)
                selected = b'J' if ch == 1 else b'K'
                self.assertEqual(wire.commands[wire.commands.index(b'A')-1], selected)
                self.assertEqual(wire.commands[wire.commands.index(b'O')-1], selected)
                self.assertEqual(link.control_events[-1]['after']['channel_acquiring'], {ch: False, peer: True})
        records = [json.loads(line) for line in (self.root/'channel-control.jsonl').read_text().splitlines()]
        self.assertEqual(len(records), 4)

    def test_stopped_ticks_and_counts_stay_frozen_and_resume_without_clear(self):
        link, wire = make_link(self.root)
        link.start(1)
        wire.advance()
        link.stop(1)
        first = link.snapshot(1, False)
        link.start(2)
        wire.advance()
        second = link.snapshot(1, False)
        np.testing.assert_array_equal(first['counts'], second['counts'])
        self.assertEqual(first['stats']['acquisition_ticks'], second['stats']['acquisition_ticks'])
        link.start(1)
        wire.advance()
        self.assertTrue(np.all(wire.counts[1] > first['counts']))
        self.assertNotIn(b'C', wire.commands)

    def test_global_commands_and_channel_noops_preserve_existing_measurements(self):
        link, wire = make_link(self.root, mask=2)
        before = wire.counts[2].copy()
        link.start(2)
        link.start()
        self.assertEqual(wire.mask, 3)
        np.testing.assert_array_equal(wire.counts[2], before)
        link.stop()
        link.stop(1)
        self.assertEqual(wire.mask, 0)
        self.assertIn(b'S', wire.commands)
        self.assertIn(b'E', wire.commands)

    def test_global_clear_guard_and_running_dump_guard(self):
        link, wire = make_link(self.root, mask=2)
        with self.assertRaisesRegex(RuntimeError, 'Stop'):
            link.clear()
        self.assertNotIn(b'C', wire.commands)
        with self.assertRaisesRegex(RuntimeError, 'Stop channel 2'):
            link.snapshot(2, False)
        self.assertNotIn(b'D', wire.commands)
        frame = link.snapshot(1, False)
        self.assertFalse(frame['live'])
        self.assertEqual(frame['total'], frame['stats']['histogrammed'])
        self.assertEqual(wire.mask, 2)

    def test_invalid_state_or_changed_peer_aborts_verification(self):
        link, wire = make_link(self.root, mask=2)
        wire.bad_q = b'4'
        with self.assertRaisesRegex(ValueError, 'channel acquisition state'):
            link.start(1)
        self.assertNotIn(b'A', wire.commands)
        wire.bad_q, wire.corrupt_peer = None, True
        with self.assertRaisesRegex(RuntimeError, 'untouched peer'):
            link.start(1)
        self.assertEqual(link.control_events, [])
        self.assertNotIn(b'E', wire.commands)

    def test_invalid_channel_sends_no_uart_commands_and_legacy_global_still_works(self):
        link, wire = make_link(self.root)
        before = wire.commands.copy()
        for channel in (0, 3, '1'):
            with self.assertRaises(ValueError):
                link.start(channel)
            with self.assertRaises(ValueError):
                link.stop(channel)
        self.assertEqual(wire.commands, before)
        legacy, wire = make_link(self.root, supported=False)
        legacy.start()
        self.assertTrue(legacy.running)
        legacy.stop()
        self.assertFalse(legacy.running)
        self.assertIsNone(legacy.channel_acquiring)


class ChannelGUI(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    @classmethod
    def tearDownClass(cls):
        QtTest.QTest.qWait(120)
        cls.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.link, self.wire = make_link(self.root, mask=2)
        self.window = Window()
        self.window.receive('connected', dict(self.link.acquisition_status(), settings={}))

    def tearDown(self):
        self.window.close()
        self.window.worker.thread.join(timeout=3)
        self.window.close()
        self.temporary.cleanup()

    def test_buttons_follow_verified_channel_states_and_keep_configuration_guard(self):
        self.assertTrue(self.window.channel_buttons[1, 'start'].isEnabled())
        self.assertFalse(self.window.channel_buttons[1, 'stop'].isEnabled())
        self.assertFalse(self.window.channel_buttons[2, 'start'].isEnabled())
        self.assertTrue(self.window.channel_buttons[2, 'stop'].isEnabled())
        self.assertTrue(self.window.buttons['start'].isEnabled())
        self.assertFalse(self.window.hardware_clear.isEnabled())
        self.assertFalse(self.window.source_menu.isEnabled())
        self.window.foreground_menu.setCurrentIndex(self.window.foreground_menu.findData(2))
        with patch.object(self.window.worker, 'send') as send:
            self.window.channel_buttons[1, 'start'].click()
        send.assert_called_once_with('start', 1)
        self.assertTrue(self.window.busy)
        self.assertTrue(all(not button.isEnabled() for button in self.window.channel_buttons.values()))

    def test_old_firmware_and_disconnect_never_guess_individual_states(self):
        self.window.receive('connected', dict(running=True, independent_channel_control=False, channel_acquiring=None, settings={}))
        self.assertIsNone(self.window.channel_acquiring)
        self.assertTrue(all(not button.isEnabled() for button in self.window.channel_buttons.values()))
        self.assertIn('UNKNOWN', self.window.channel_status_labels[1].text())
        with patch.object(self.window.worker, 'send') as send:
            self.window.channel_action('stop', 1)
        send.assert_not_called()
        self.window.receive('disconnected', None)
        self.assertIn('DISCONNECTED', self.window.channel_status_labels[2].text())
        self.assertIsNone(self.window.channel_acquiring)

    def test_manual_channel_command_cancels_shared_preset_and_does_not_arm_another(self):
        self.window.preset_mode.setCurrentIndex(self.window.preset_mode.findData('duration'))
        self.window.preset_target.setValue(2.)
        self.window.arm_preset()
        self.link.start(1)
        self.window.receive('acquisition_state', dict(self.link.acquisition_status(), operation='start', channel=1))
        self.assertIsNone(self.window.preset.started)
        self.assertIn('cancelled', self.window.preset_progress.format())
        with patch.object(self.window.worker, 'send') as send:
            self.window.check_preset(now=1e12)
        send.assert_not_called()

    def test_shared_preset_uses_fresh_baseline_when_peer_was_already_running(self):
        self.window.receive('frame', self.link.snapshot(1, False))
        self.window.receive('frame', self.link.snapshot(2, True))
        self.wire.advance()
        fresh = self.link.count_baselines()
        self.link.start()
        self.window.receive('acquisition_state', dict(self.link.acquisition_status(), operation='start', channel=None, baseline_counts=fresh))
        self.assertEqual(self.window.preset.baseline_counts, fresh)
        self.assertGreater(fresh[2], self.window.frames[2]['total'])

    def test_stop_one_channel_saves_final_and_live_peer_with_control_metadata(self):
        self.link.start(1)
        self.window.receive('acquisition_state', dict(self.link.acquisition_status(), operation='start', channel=1))
        self.link.stop(1)
        state = dict(self.link.acquisition_status(), operation='stop', channel=1, control_events=self.link.control_events)
        self.window.receive('acquisition_state', state)
        self.window.receive('frame', self.link.snapshot(1, False))
        self.window.receive('frame', self.link.snapshot(2, True))
        session = self.root/'partial-final.dsw'
        with patch.object(self.window, 'autosave_session', side_effect=lambda reason: self.window.save_session(filename=session) or session):
            self.window.receive('complete', 'stop')
        payload = read_session(session)
        self.assertFalse(payload['channels']['1']['frame']['live'])
        self.assertTrue(payload['channels']['2']['frame']['live'])
        self.assertEqual(payload['acquisition']['channel_acquiring'], {'1': False, '2': True})
        self.assertEqual(payload['acquisition']['control_events'][-1]['channel'], 1)
        self.assertTrue(self.window.running)
        self.assertTrue(self.window.channel_buttons[2, 'stop'].isEnabled())
        self.assertFalse(self.window._final_autosave_pending)

    def test_save_failure_keeps_peer_controls_and_retains_stopped_spectrum(self):
        self.link.stop(1)
        self.window.receive('acquisition_state', dict(self.link.acquisition_status(), operation='stop', channel=1))
        self.window.receive('frame', self.link.snapshot(1, False))
        self.window.receive('frame', self.link.snapshot(2, True))
        with patch.object(self.window, 'autosave_session', side_effect=OSError('disk full')):
            self.window.receive('complete', 'stop')
        self.assertTrue(self.window._final_autosave_pending)
        self.assertTrue(self.window.running)
        self.assertTrue(self.window.channel_buttons[2, 'stop'].isEnabled())
        self.assertFalse(self.window.frames[1]['live'])
        self.assertIn('disk full', self.window.status.text())

    def test_invalid_saved_acquisition_metadata_preserves_current_spectra(self):
        self.window.receive('frame', self.link.snapshot(1, False))
        before = self.window.frames[1]
        payload = self.window.session_payload()
        payload['acquisition'] = 'invalid'
        session = self.root/'invalid.dsw'
        session.write_text(json.dumps(dict(format='DSW session', version=1, **payload)))
        self.window.receive('disconnected', None)
        with patch('workspace.QtWidgets.QMessageBox.warning') as warning:
            self.window.open_session(filename=session)
        warning.assert_called_once()
        self.assertIs(self.window.frames[1], before)


class ChannelWorker(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.link, self.wire = make_link(self.temporary.name, mask=2)
        self.worker = Worker.__new__(Worker)
        QtCore.QObject.__init__(self.worker)
        self.worker.link = self.link
        self.worker.live, self.worker.done = True, False
        self.worker.last_modes = {}
        self.worker.commands = queue.Queue()
        self.worker.preservation_context = {}
        self.events = []
        self.worker.event.connect(lambda kind, value: self.events.append((kind, value)), QtCore.Qt.ConnectionType.DirectConnection)

    def tearDown(self):
        self.temporary.cleanup()

    def test_mixed_pair_uses_final_for_stopped_channel_and_bounds_idle_retention(self):
        self.worker.pair()
        first = [value for kind, value in self.events if kind == 'frame']
        self.assertEqual([(frame['channel'], frame['live']) for frame in first], [(1, False), (2, True)])
        self.worker.pair(force=False)
        self.worker.pair(force=False)
        self.assertEqual(self.wire.commands.count(b'D'), 1)
        self.assertEqual(self.wire.commands.count(b'L'), 3)
        self.assertEqual(len(list(Path(self.temporary.name).glob('*-ch1-final.bin'))), 1)

    def test_worker_stops_selected_channel_and_keeps_peer_refreshing(self):
        self.worker.commands.put(('start', 1))
        self.worker.commands.put(('stop', 1))
        self.worker.commands.put(('read', None))
        self.worker.commands.put(('quit', None))
        self.worker.run()
        self.assertEqual(self.wire.mask, 2)
        self.assertNotIn(b'E', self.wire.commands)
        self.assertNotIn(b'C', self.wire.commands)
        self.assertFalse(any(kind == 'error' for kind, _ in self.events))
        states = [value for kind, value in self.events if kind == 'acquisition_state' and value.get('operation') == 'stop']
        self.assertEqual(states[-1]['channel_acquiring'], {1: False, 2: True})
        frames = [value for kind, value in self.events if kind == 'frame']
        self.assertTrue(any(frame['channel'] == 1 and not frame['live'] for frame in frames))
        self.assertTrue(any(frame['channel'] == 2 and frame['live'] for frame in frames))

    def test_global_stop_reads_both_final_channels(self):
        self.worker.commands.put(('stop', None))
        self.worker.commands.put(('quit', None))
        self.worker.run()
        frames = [value for kind, value in self.events if kind == 'frame']
        self.assertEqual([(frame['channel'], frame['live']) for frame in frames], [(1, False), (2, False)])
        self.assertEqual(self.wire.commands.count(b'E'), 1)
        self.assertNotIn(b'O', self.wire.commands)
        self.assertNotIn(b'L', self.wire.commands)
        self.assertEqual(self.wire.mask, 0)

    def test_global_start_publishes_current_counts_for_shared_target(self):
        self.worker.commands.put(('start', None))
        self.worker.commands.put(('quit', None))
        self.worker.run()
        starts = [value for kind, value in self.events if kind == 'acquisition_state' and value.get('operation') == 'start']
        self.assertEqual(starts[0]['baseline_counts'], {1: 16384, 2: 32768})
        self.assertEqual(self.wire.mask, 3)

    def test_stop_waits_for_live_frame_completion(self):
        entered, release = threading.Event(), threading.Event()
        original_read = self.link.read
        blocked = [False]
        def read(size, timeout=2):
            if not blocked[0] and self.wire.last_dump[1]:
                blocked[0] = True
                entered.set()
                if not release.wait(3):
                    raise TimeoutError('Simulated frame was not released')
            return original_read(size, timeout)
        self.link.read = read
        self.worker.commands.put(('read', None))
        thread = threading.Thread(target=self.worker.run)
        thread.start()
        try:
            self.assertTrue(entered.wait(2))
            self.worker.commands.put(('stop', 2))
            self.worker.commands.put(('quit', None))
            self.assertNotIn(b'O', self.wire.commands)
        finally:
            release.set()
            thread.join(timeout=4)
        self.assertFalse(thread.is_alive())
        frame_end = self.wire.commands.index(('frame_complete', 2, True))
        self.assertGreater(self.wire.commands.index(b'O'), frame_end)
        self.assertEqual(self.wire.mask, 0)
        self.assertFalse(any(kind == 'error' for kind, _ in self.events))


if __name__ == '__main__':
    unittest.main()
