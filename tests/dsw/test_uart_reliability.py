"""Receive deadlines, read-only recovery and GUI-independent UART execution."""
import ctypes
import json
import multiprocessing
import os
import queue
import tempfile
import time
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import numpy as np

from protocol import FrameReadTimeout, Link
from spectrum_io import measurement
from test_channel_control import make_link
from uart_worker import process_main
from PySide6 import QtCore, QtTest, QtWidgets
from app import Window


class ScheduledSerial:
    def __init__(self, data, interval=.03, trickle=False):
        self.data = bytearray(data)
        self.now = 0.
        self.interval = interval
        self.trickle = trickle
        self.requests = []

    def read(self, size):
        self.requests.append(size)
        self.now += self.interval
        count = min(size, len(self.data), 1 if self.trickle else size)
        chunk = bytes(self.data[:count])
        del self.data[:count]
        return chunk


class ReceiveRecovery(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)

    def tearDown(self):
        self.temporary.cleanup()

    def test_frame_progress_gets_time_to_complete_in_bounded_chunks(self):
        data = bytes(range(256))*256+bytes(212)
        serial = ScheduledSerial(data)
        link = Link.__new__(Link)
        link.root, link.serial = self.root, serial
        with patch('protocol.time.monotonic', side_effect=lambda: serial.now):
            result = link.read(65748, timeout=.4)
        self.assertEqual(result, data)
        self.assertGreater(serial.now, .4)
        self.assertTrue(all(size <= 4096 for size in serial.requests))

    def test_exact_4288_byte_failure_is_retained_and_never_returned_as_spectrum(self):
        prefix = b'SPL5'+bytes(4284)
        serial = ScheduledSerial(prefix, interval=.1)
        link = Link.__new__(Link)
        link.root, link.serial = self.root, serial
        with patch('protocol.time.monotonic', side_effect=lambda: serial.now):
            with self.assertRaises(FrameReadTimeout) as failure:
                link.read(65748, timeout=.5)
        self.assertEqual(failure.exception.received, 4288)
        self.assertEqual(Path(failure.exception.path).read_bytes(), prefix)
        self.assertIn(str(self.root), str(failure.exception))

    def test_trickling_device_cannot_extend_frame_deadline_forever(self):
        serial = ScheduledSerial(bytes(65748), interval=.2, trickle=True)
        link = Link.__new__(Link)
        link.root, link.serial = self.root, serial
        with patch('protocol.time.monotonic', side_effect=lambda: serial.now):
            with self.assertRaises(FrameReadTimeout):
                link.read(65748, timeout=.5)
        self.assertLess(serial.now, 3.)
        self.assertLess(len(serial.requests), 20)

    def test_transport_recovery_preserves_directory_history_events_and_enable_state(self):
        link, wire = make_link(self.root, mask=2)
        link.recovery_count = 0
        wire.port = 'simulated-uart'
        wire.baudrate = 2000000
        link.snapshot(1, False)
        previous = link.last
        link.control_events = [dict(operation='start', channel=2)]
        events = link.control_events
        callbacks = []
        link.recovery_callback = lambda kind, value: callbacks.append((kind, value))
        replacement, new_wire = make_link(self.root, mask=2)
        error = FrameReadTimeout(65748, 4288, self.root/'partial.bin')
        with patch('protocol.Link', return_value=replacement) as constructor:
            link.recover_transport(error)
        constructor.assert_called_once_with('simulated-uart', session_root=self.root)
        self.assertIs(link.last, previous)
        self.assertIs(link.control_events, events)
        self.assertEqual(link.channel_acquiring, {1: False, 2: True})
        self.assertEqual(link.serial.baudrate, 921600)
        self.assertEqual(link.recovery_count, 1)
        self.assertEqual([kind for kind, _ in callbacks], ['recovering', 'recovered'])
        for command in (b'S', b'E', b'A', b'O', b'C', b'P', b'N'):
            self.assertNotIn(command, wire.commands+new_wire.commands)
        record = json.loads((self.root/'uart-recovery.jsonl').read_text())
        self.assertEqual(record['acquisition']['channel_acquiring'], {'1': False, '2': True})

    def test_snapshot_retries_read_once_and_does_not_repeat_acquisition_commands(self):
        link, wire = make_link(self.root, mask=3)
        link.recovery_count = 0
        read = link.read
        calls = [0]
        def fail_once(size, timeout=2):
            calls[0] += 1
            if calls[0] == 1:
                path = self.root/'partial.bin'
                path.write_bytes(bytes(4288))
                raise FrameReadTimeout(size, 4288, path)
            return read(size, timeout)
        link.read = fail_once
        with patch.object(link, 'recover_transport') as recovery:
            frame = link.snapshot(2, True)
        recovery.assert_called_once()
        self.assertTrue(frame['live'])
        self.assertEqual(wire.mask, 3)
        self.assertEqual(wire.commands.count(b'L'), 2)
        for command in (b'S', b'E', b'A', b'O', b'C'):
            self.assertNotIn(command, wire.commands)

    def test_persistent_failure_is_bounded_and_command_acknowledgements_are_not_replayed(self):
        link, wire = make_link(self.root, mask=3)
        link.recovery_count = 0
        error = FrameReadTimeout(65748, 4288, self.root/'partial.bin')
        link.read = lambda *args: (_ for _ in ()).throw(error)
        with patch.object(link, 'recover_transport') as recovery:
            with self.assertRaises(FrameReadTimeout):
                link.snapshot(1, True)
        self.assertEqual(recovery.call_count, 1)
        self.assertEqual(wire.commands.count(b'L'), 2)
        with patch.object(link, 'recover_transport') as recovery, patch.object(link, 'ack', side_effect=FrameReadTimeout(1, 0, self.root/'ack.bin')):
            with self.assertRaises(FrameReadTimeout):
                link.start(1)
        recovery.assert_not_called()

    def test_crc_failure_is_retained_and_successful_retry_is_validated(self):
        link, wire = make_link(self.root, mask=3)
        link.recovery_count = 0
        read = link.read
        calls = [0]
        def corrupt_once(size, timeout=2):
            raw = read(size, timeout)
            calls[0] += 1
            return raw[:-1]+bytes([raw[-1]^1]) if calls[0] == 1 else raw
        link.read = corrupt_once
        with patch.object(link, 'recover_transport') as recovery:
            frame = link.snapshot(1, True)
        recovery.assert_called_once()
        self.assertEqual(len(list(self.root.glob('invalid-frame-*.bin'))), 1)
        self.assertEqual(frame['total'], 16384)
        self.assertEqual(len(frame['raw']), 65748)


class ProcessTestLink:
    """A fake continuously acquiring board, used only in the spawned test service."""
    def __init__(self, root):
        self.root = Path(root)
        self.serial = SimpleNamespace(baudrate=115200, close=lambda: None)
        self.control_events = []
        self.running = True
        self.sequence = 0

    def acquisition_status(self):
        return dict(running=True, independent_channel_control=True, channel_acquiring={1: True, 2: True})

    def settings(self, channel):
        return dict(polarity='P', source=[0]*8)

    def baud(self, rate):
        self.serial.baudrate = rate

    def identity(self):
        return None

    def snapshot(self, channel, live):
        self.sequence += 1
        record = dict(channel=channel, sequence=self.sequence, pid=os.getpid(), live=live)
        with (self.root/'progress.jsonl').open('a') as handle:
            handle.write(json.dumps(record)+'\n')
        return measurement(np.full(16384, self.sequence, dtype=np.uint32), (0., .25), channel=channel,
                           sequence=self.sequence, live=live, raw=bytes(65748))

    def close(self):
        (self.root/'closed.json').write_text(json.dumps(dict(channel_acquiring={'1': True, '2': True})))


class PreservationProcessLink(ProcessTestLink):
    def __init__(self, root):
        super().__init__(root)
        self.running = False

    def acquisition_status(self):
        return dict(running=False, independent_channel_control=True, channel_acquiring={1: False, 2: False})

    def state(self):
        return False

    def snapshot(self, channel, live):
        return measurement([channel, channel+1], (0., 1.), channel=channel, live=live)

    def clear(self):
        (self.root/'cleared').write_text('yes')


class IndependentUARTProcess(unittest.TestCase):
    def test_uart_service_keeps_receiving_when_gui_interpreter_holds_gil(self):
        context = multiprocessing.get_context('spawn')
        commands, events = context.Queue(), context.Queue()
        with tempfile.TemporaryDirectory() as directory:
            process = context.Process(target=process_main, args=(commands, events, ProcessTestLink))
            process.start()
            commands.put(('connect', directory, {'sample_metadata': {'sample': 'IPC context'}}))
            try:
                deadline = time.monotonic()+8
                while True:
                    kind, value = events.get(timeout=max(.1, deadline-time.monotonic()))
                    self.assertNotEqual(kind, 'error', value)
                    if kind == 'frame':
                        break
                path = Path(directory)/'progress.jsonl'
                before = len(path.read_text().splitlines())
                # PyDLL retains this interpreter's GIL during the native call.
                sleep = ctypes.PyDLL(None).sleep
                sleep.argtypes, sleep.restype = [ctypes.c_uint], ctypes.c_uint
                sleep(2)
                records = [json.loads(line) for line in path.read_text().splitlines()]
                self.assertGreaterEqual(len(records), before+2)
                self.assertTrue(all(record['pid'] != os.getpid() for record in records))
            finally:
                commands.put(('quit', None, {}))
                deadline = time.monotonic()+5
                while time.monotonic() < deadline:
                    try:
                        kind, _ = events.get(timeout=.2)
                        if kind == 'worker_done':
                            break
                    except queue.Empty:
                        if not process.is_alive():
                            break
                process.join(timeout=3)
                if process.is_alive():
                    process.terminate(); process.join(timeout=2)
                commands.close(); commands.cancel_join_thread()
                events.close(); events.cancel_join_thread()
            self.assertEqual(process.exitcode, 0)
            self.assertEqual(json.loads((Path(directory)/'closed.json').read_text())['channel_acquiring'], {'1': True, '2': True})

    def test_latest_preservation_context_crosses_process_boundary_before_clear(self):
        context = multiprocessing.get_context('spawn')
        commands, events = context.Queue(), context.Queue()
        with tempfile.TemporaryDirectory() as directory:
            process = context.Process(target=process_main, args=(commands, events, PreservationProcessLink))
            process.start()
            commands.put(('connect', directory, {'sample_metadata': {'sample': 'old context'}}))
            commands.put(('clear', None, {'channels': {}, 'sample_metadata': {'sample': 'updated context'}}))
            saved = None
            try:
                deadline = time.monotonic()+8
                while time.monotonic() < deadline:
                    kind, value = events.get(timeout=.5)
                    self.assertNotEqual(kind, 'error', value)
                    if kind == 'preserved':
                        saved = value
                    if kind == 'complete' and value == 'clear':
                        break
                self.assertIsNotNone(saved)
                self.assertEqual(json.loads(Path(saved).read_text())['sample_metadata']['sample'], 'updated context')
                self.assertTrue((Path(directory)/'cleared').exists())
            finally:
                commands.put(('quit', None, {}))
                deadline = time.monotonic()+5
                while time.monotonic() < deadline:
                    try:
                        kind, _ = events.get(timeout=.2)
                        if kind == 'worker_done':
                            break
                    except queue.Empty:
                        if not process.is_alive():
                            break
                process.join(timeout=3)
                if process.is_alive():
                    process.terminate(); process.join(timeout=2)
                commands.close(); commands.cancel_join_thread()
                events.close(); events.cancel_join_thread()
            self.assertEqual(process.exitcode, 0)


class GUIRecovery(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.app = QtWidgets.QApplication.instance() or QtWidgets.QApplication([])

    @classmethod
    def tearDownClass(cls):
        QtTest.QTest.qWait(120)
        cls.app.sendPostedEvents(None, QtCore.QEvent.Type.DeferredDelete)

    def test_recovery_preserves_spectra_rois_calibration_and_shared_target(self):
        window = Window()
        try:
            window.receive('connected', dict(running=True, independent_channel_control=True, channel_acquiring={1: True, 2: True}, settings={}))
            frame = measurement(np.full(4096, 10), (0., .5), real_time=10.)
            window.load_frame(1, frame)
            window.set_roi(1, 100., 120.)
            window.arm_preset()
            original = window.frames[1]
            rois, calibration, started = window.roi_records(1), window.calibrations[1].copy(), window.preset.started
            window.receive('recovering', {'error': 'short frame'})
            self.assertTrue(window.busy)
            self.assertTrue(all(not button.isEnabled() for button in window.channel_buttons.values()))
            window.receive('recovered', dict(running=True, independent_channel_control=True, channel_acquiring={1: True, 2: True}, baudrate=921600, control_events=[]))
            self.assertFalse(window.busy)
            self.assertIs(window.frames[1], original)
            self.assertEqual(window.roi_records(1), rois)
            self.assertEqual(window.calibrations[1], calibration)
            self.assertEqual(window.preset.started, started)
            self.assertIn('921,600 baud', window.status.text())
            self.assertTrue(window.channel_buttons[1, 'stop'].isEnabled())
        finally:
            window.close(); window.worker.thread.join(timeout=3); window.close()

    def test_pending_command_stays_busy_until_complete_after_recovery(self):
        window = Window()
        try:
            window.receive('connected', dict(running=True, independent_channel_control=True, channel_acquiring={1: True, 2: True}, settings={}))
            window.busy = True
            window.receive('recovering', {'error': 'short frame'})
            window.receive('recovered', dict(running=True, independent_channel_control=True, channel_acquiring={1: True, 2: True}, baudrate=921600))
            self.assertTrue(window.busy)
            window.receive('complete', 'read')
            self.assertFalse(window.busy)
        finally:
            window.close(); window.worker.thread.join(timeout=3); window.close()


if __name__ == '__main__':
    unittest.main()
