"""Byte-level mock backend for v1-v6 dual-MCA worker tests and UI review."""
import errno
from pathlib import Path
import struct
import threading
import time
import zlib

import numpy as np

from dual_mca_protocol import (FRAME_LAYOUTS, STAT_FIELDS, LIVE_FRAME_SIZE,
    crc8_atm, decode_snapshot, decode_energy_configuration)


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_FIXTURES = ROOT / 'tests' / 'fixtures' / 'frames'


class MockPort:
    def __init__(self, fixtures=None, version=4, running=False, throttle=False,
                 fault=None, chunk_size=4096, baud_1s_timeout=1.0, baud_idle_timeout=3.0):
        self.fixtures = Path(fixtures or DEFAULT_FIXTURES)
        self.version = version
        if version not in FRAME_LAYOUTS:
            raise ValueError(f'Mock supports only v1-v6, not {version}')
        # v5 reuses v4 golden fixtures (identical D layout, new version byte).
        fixture_version = 4 if version >= 5 else version
        self.running_mask = 3 if running else 0
        self.throttle = throttle
        self.fault = fault
        self.chunk_size = min(max(1, int(chunk_size)), 1152) if throttle else max(1, int(chunk_size))
        self.pending = bytearray()
        self.channel = 1
        self.commands = []
        self.closed = False
        self.mutations = 0
        self._delivered_for_frame = 0
        self._frame_active = False
        self._run_started = None
        # v5 baud state (logical; no real serial timing in the mock).
        self.baudrate = 115200
        self._baud_need_i = False
        self._baud_deadline = None
        self.baud_1s_timeout = float(baud_1s_timeout)
        self.baud_idle_timeout = float(baud_idle_timeout)
        self.ready_busy_replies = 1 if fault == 'busy_ready' else 0
        self.polarities = {1: False, 2: False}
        self.triggers = {1: (25, 1280, 0, 25, 1), 2: (25, 1280, 0, 25, 1)}
        self.energies = {ch: (0, 6, 225, 25, 0, 10514432, 41088) for ch in (1, 2)}
        self.sources = {1: (0, 1, 101, 25000, 16770683, 0, 0, 0),
                        2: (0, 1, 101, 25000, 16770683, 0, 0, 0)}
        self.frames = {}
        self.spectra = {}
        self.stats = {}
        self.sequences = {}
        for channel in (1, 2):
            raw = (self.fixtures / f'v{fixture_version}_ch{channel}.bin').read_bytes()
            info, counts = decode_snapshot(raw, channel)
            self.frames[channel] = raw
            self.spectra[channel] = counts.copy()
            self.stats[channel] = dict(info['counters'])
            self.sequences[channel] = info['sequence']
            self.polarities[channel] = info['polarity'] == 'negative'
            trig = info['trigger_configuration']
            if trig:
                self.triggers[channel] = (trig['floor_codes'], trig['sigma_multiplier_q8'],
                                          trig['sigma_fast_q8'], trig['effective_threshold'],
                                          int(trig['noise_valid']) | (trig['noise_updates'] << 1))
            source = info['source_configuration']
            if source:
                self.sources[channel] = (
                    int(source['source'] == 'synthetic'),
                    int(source['mode'] == 'continuous') | (int(source['negative']) << 1),
                    source['amplitude_codes'], source['period_ticks'],
                    source['decay_alpha_q24'], source['noise_amplitude_codes'],
                    source['injected'], source['clipped_samples'])

        if version == 6:
            # v4 fixture headers are transport seeds, not charge-energy fixtures.
            # A new v6 mock starts with empty spectra and actual v6 reset settings.
            for channel in (1, 2):
                self.spectra[channel].fill(0)
                self.stats[channel] = dict.fromkeys(STAT_FIELDS, 0)
                self.polarities[channel] = False
                self.triggers[channel] = (25, 1280, 0, 25, 1)
                self.sources[channel] = (0, 1, 101, 25000, 16770683, 0, 0, 0)

    @property
    def running(self):
        return bool(self.running_mask)

    @running.setter
    def running(self, value):
        self.running_mask = 3 if value else 0

    def _update_clock(self):
        if self.running and self._run_started is not None:
            elapsed = max(0.0, time.monotonic() - self._run_started)
            ticks = int(elapsed * 50_000_000)
            for channel in (1, 2):
                if not self.running_mask & (1 << (channel - 1)):
                    continue
                self.stats[channel]['acquisition_ticks'] = min(
                    0xffffffffffffffff,
                    self.stats[channel]['acquisition_ticks'] + ticks)
            self._run_started = time.monotonic()

    def _has_counts(self, channel):
        return any(int(value) for value in self.spectra[channel])

    def _add_event(self, channel, bin_index):
        stats = self.stats[channel]
        stats['triggered'] += 1
        stats['dpp_valid'] += 1
        bin_index = max(0, min(16383, int(bin_index)))
        if int(self.spectra[channel][bin_index]) == 0xffffffff:
            stats['bin_saturated'] += 1
        else:
            self.spectra[channel][bin_index] += 1
            stats['histogrammed'] += 1

    def _frame(self, channel):
        self._update_clock()
        raw_header = bytearray(self.frames[channel][:16])
        raw_header[4] = self.version
        raw_header[7] = (raw_header[7] & 0x7f) | (0x80 if self.polarities[channel] else 0)
        body = bytearray(raw_header)
        body.extend(struct.pack('<9Q', *(self.stats[channel][name] for name in STAT_FIELDS)))
        if self.version >= 3:
            body.extend(struct.pack('<HHIII', *self.triggers[channel]))
        if self.version >= 4:
            body.extend(struct.pack('<BBHIIIQQ', *self.sources[channel]))
        if self.version >= 6:
            body.extend(struct.pack('<BBHHHII', *self.energies[channel]))
        body.extend(self.spectra[channel].astype('<u4', copy=False).tobytes())
        return bytes(body) + struct.pack('<I', zlib.crc32(body))

    def _check_baud_revert(self):
        if self.baudrate != 115200 and self._baud_deadline is not None:
            if time.monotonic() >= self._baud_deadline:
                self.baudrate = 115200
                self._baud_need_i = False
                self._baud_deadline = None

    def _touch_baud(self, cmd):
        valid = cmd in (b'I', b'B', b'J', b'K', b'V', b'X', b'Y', b'G', b'W',
                        b'F', b'P', b'N', b'S', b'E', b'C', b'R', b'?', b'T',
                        b'D', b'L', b'U', b'A', b'O', b'Q', b'H') or (self.version >= 6 and cmd in (b'M', b'Z'))
        if self.baudrate != 115200 and valid:
            # Mirror the firmware: while waiting for the first I after a switch
            # the link stays in the 1 s window (any valid reloads it); only I
            # promotes to the 3 s idle lease.
            if cmd == b'I':
                self._baud_need_i = False
                self._baud_deadline = time.monotonic() + self.baud_idle_timeout
            elif self._baud_need_i:
                self._baud_deadline = time.monotonic() + self.baud_1s_timeout
            else:
                self._baud_deadline = time.monotonic() + self.baud_idle_timeout

    def _live_frame(self, channel):
        self._update_clock()
        self.sequences[channel] += 1
        start = dict(self.stats[channel])
        # Emulate acquisition during the transfer: advance 1-2 committed events
        # so start <= sum <= end and consecutive frames are monotonic.
        if self.running_mask & (1 << (channel - 1)):
            for _ in range(2):
                self._add_event(channel, (len(self.spectra[channel]) // 2 + self.sequences[channel]) % 16384)
        end = dict(self.stats[channel])
        header = struct.pack('<4sBBBBII', f'SPL{self.version}'.encode(), self.version, channel, 14,
                             0x80 | 3 if self.polarities[channel] else 3,
                             16384, self.sequences[channel])
        body = bytearray(header)
        body.extend(struct.pack('<9Q', *(start[name] for name in STAT_FIELDS)))
        body.extend(struct.pack('<HHIII', *self.triggers[channel]))
        body.extend(struct.pack('<BBHIIIQQ', *self.sources[channel]))
        if self.version >= 6:
            body.extend(struct.pack('<BBHHHII', *self.energies[channel]))
        body.extend(self.spectra[channel].astype('<u4', copy=False).tobytes())
        body.extend(struct.pack('<9Q', *(end[name] for name in STAT_FIELDS)))
        return bytes(body) + struct.pack('<I', zlib.crc32(body))

    def _mutation_allowed(self, channel, minimum_version):
        return (self.version >= minimum_version and not self.running and
                not self._has_counts(channel))

    def write(self, value):
        if self.closed:
            raise OSError(errno.EIO, 'mock device is closed')
        self.commands.append(bytes(value))
        self._check_baud_revert()
        if value == b'I':
            reported = self.version
            if self.fault == 'reset_version':
                reported = 7
            elif self.fault == 'reset_v3':
                self.version = 3
                reported = 3
            self._touch_baud(b'I')
            self._reply(struct.pack('<4sBBBBII', b'DMC2', reported, 2, 14, 3, 16384, 50000000))
        elif value == b'H':
            self._touch_baud(value)
            self._reply(b'1' if self.version >= 5 else b'?')
        elif value == b'Q':
            self._touch_baud(value)
            self._reply(str(self.running_mask).encode() if self.version >= 5 else b'?')
        elif value in (b'A', b'O'):
            if self.version < 5:
                self._reply(b'?')
            else:
                self._touch_baud(value)
                self._update_clock()
                bit = 1 << (self.channel - 1)
                self.running_mask = self.running_mask | bit if value == b'A' else self.running_mask & ~bit
                self._run_started = time.monotonic() if self.running else None
                self._reply(value)
        elif value == b'?':
            self._touch_baud(b'?')
            self._reply(b'1' if self.running else b'0')
        elif value in (b'J', b'K'):
            self.channel = 1 if value == b'J' else 2
            self._touch_baud(value)
            self._reply(b'K')
        elif value == b'T':
            self._update_clock()
            self._touch_baud(b'T')
            self._reply(struct.pack('<9Q', *(self.stats[self.channel][name] for name in STAT_FIELDS)))
        elif value == b'F':
            self._touch_baud(b'F')
            self._reply(b'N' if self.polarities[self.channel] else b'P')
        elif value == b'G':
            self._touch_baud(b'G')
            self._reply(struct.pack('<HHIII', *self.triggers[self.channel]))
        elif value == b'V':
            self._touch_baud(b'V')
            self._reply(struct.pack('<BBHIIIQQ', *self.sources[self.channel]))
        elif value == b'R':
            self._touch_baud(b'R')
            if self.ready_busy_replies:
                self.ready_busy_replies -= 1
                self._reply(b'B')
            else:
                self._reply(b'R')
        elif value == b'D':
            self._touch_baud(b'D')
            if self.running_mask & (1 << (self.channel - 1)):
                self._reply(b'B')
                return len(value)
            raw = self._frame(self.channel)
            if self.fault == 'crc':
                raw = raw[:-1] + bytes([raw[-1] ^ 1])
            if self.fault in ('truncate', 'unplug'):
                self._delivered_for_frame = 0
                self._frame_active = True
            self._reply(raw)
        elif value == b'L':
            # v5 live read: allowed acquiring or stopped, rejected while clearing
            # (mock clear is instant, so always allowed on v5).
            if self.version < 5:
                self._reply(b'B')
                return len(value)
            self._touch_baud(b'L')
            raw = self._live_frame(self.channel)
            if self.fault == 'crc':
                raw = raw[:-1] + bytes([raw[-1] ^ 1])
            if self.fault in ('truncate', 'unplug'):
                self._delivered_for_frame = 0
                self._frame_active = True
            self._reply(raw)
        elif value.startswith(b'U'):
            # v5 baud switch packet: U, rate, CRC8/ATM. Malformed -> B, no switch.
            if self.version < 5:
                self._reply(b'B')
                return len(value)
            valid = len(value) == 3
            if valid:
                rate, crc = value[1], value[2]
                valid = rate in (0, 1, 2) and crc == crc8_atm(value[:2])
            if not valid:
                self._reply(b'B')
                return len(value)
            self._touch_baud(b'U')
            self.baudrate = {0: 115200, 1: 921600, 2: 2000000}[value[1]]
            if self.baudrate == 115200:
                self._baud_need_i = False
                self._baud_deadline = None
            else:
                self._baud_need_i = True
                self._baud_deadline = time.monotonic() + self.baud_1s_timeout
            self._reply(b'K')
        elif value == b'C':
            self._touch_baud(b'C')
            if self.running:
                self._reply(b'B')
            else:
                for channel in (1, 2):
                    self.spectra[channel].fill(0)
                    self.stats[channel] = dict.fromkeys(STAT_FIELDS, 0)
                self._reply(b'C')
        elif value == b'S':
            self._touch_baud(b'S')
            self._update_clock()
            self.running = True
            self._run_started = time.monotonic()
            self._reply(b'A')
        elif value == b'E':
            self._touch_baud(b'E')
            self._update_clock()
            self.running = False
            self._run_started = None
            self._reply(b'O')
        elif value in (b'P', b'N'):
            self._touch_baud(value)
            self.mutations += 1
            if (not self._mutation_allowed(self.channel, 2) or
                    (self.fault == 'fail_second_write' and self.mutations == 2)):
                self._reply(b'B')
            else:
                self.polarities[self.channel] = value == b'N'
                self._reply(b'K')
        elif value.startswith(b'W'):
            self._touch_baud(b'W')
            self.mutations += 1
            valid = len(value) == 9
            if valid:
                payload = value[1:5]
                floor, multiplier = struct.unpack('<HH', payload)
                crc = struct.unpack('<I', value[5:])[0]
                valid = (1 <= floor <= 4095 and multiplier <= 4096 and
                         crc == zlib.crc32(bytes([self.channel]) + payload))
            if (not valid or not self._mutation_allowed(self.channel, 3) or
                    (self.fault == 'fail_second_write' and self.mutations == 2)):
                self._reply(b'B')
            else:
                self.triggers[self.channel] = (floor, multiplier, 0, floor, 1)
                self._reply(b'K')
        elif value.startswith(b'X'):
            self._touch_baud(b'X')
            self.mutations += 1
            valid = len(value) == 21
            if valid:
                payload = value[1:17]
                source, flags, amp, period, alpha, noise = struct.unpack('<BBHIII', payload)
                crc = struct.unpack('<I', value[17:])[0]
                valid = (source <= 1 and flags <= 3 and 1 <= amp <= 2047 and
                         100 <= period <= 0xffffffff and alpha < (1 << 24) and noise <= 7 and
                         crc == zlib.crc32(bytes([self.channel]) + payload))
            if (not valid or not self._mutation_allowed(self.channel, 4) or
                    (self.fault == 'fail_second_write' and self.mutations == 2)):
                self._reply(b'B')
            else:
                self.sources[self.channel] = (source, flags, amp, period, alpha, noise, 0, 0)
                self._reply(b'K')
        elif value == b'M':
            self._touch_baud(b'M')
            self._reply(struct.pack('<BBHHHII', *self.energies[self.channel]) if self.version >= 6 else b'?')
        elif value.startswith(b'Z'):
            if self.version < 6:
                self._reply(b'?')
                return len(value)
            self._touch_baud(b'Z')
            valid = self.version >= 6 and len(value) == 13
            if valid:
                payload = value[1:9]
                try:
                    decoded = decode_energy_configuration(payload + struct.pack('<II', 10514432, 41088))
                    valid = struct.unpack('<I', value[9:])[0] == zlib.crc32(bytes([self.channel])+payload)
                except ValueError:
                    valid = False
            if not valid:
                self._reply(b'X')
            elif self.running or (self.stats[self.channel]['acquisition_ticks'] and
                                  payload != struct.pack('<BBHHH', *self.energies[self.channel][:5])):
                self._reply(b'B')
            else:
                self.energies[self.channel] = struct.unpack('<BBHHH', payload) + self.energies[self.channel][5:]
                self._reply(b'K')
        elif value == b'Y':
            self._touch_baud(b'Y')
            source = self.sources[self.channel]
            if (self.version < 4 or not self.running_mask & (1 << (self.channel - 1)) or source[0] != 1 or source[1] & 1):
                self._reply(b'B')
            else:
                self._add_event(self.channel, source[2] * 8)
                source = list(source)
                source[6] = min(0xffffffffffffffff, source[6] + 1)
                self.sources[self.channel] = tuple(source)
                self._reply(b'K')
        else:
            raise OSError(errno.EPROTO, f'unsupported mock command {value!r}')
        return len(value)

    def _reply(self, data):
        self.pending = bytearray(data)

    def read(self, count):
        if self.closed:
            raise OSError(errno.EIO, 'mock device unplugged')
        if self._frame_active and self.fault == 'unplug' and self._delivered_for_frame >= 128:
            raise OSError(errno.ENODEV, 'mock device unplugged mid-frame')
        if not self.pending:
            return b''
        limit = min(count, self.chunk_size, len(self.pending))
        if self._frame_active and self.fault == 'truncate' and self._delivered_for_frame + limit > 128:
            limit = max(0, 128 - self._delivered_for_frame)
        if limit <= 0:
            raise OSError(errno.ENODEV, 'mock device ended mid-frame')
        if self.throttle:
            time.sleep(min(limit / 11520.0, 0.1))
        data = bytes(self.pending[:limit])
        del self.pending[:limit]
        if self._frame_active:
            self._delivered_for_frame += len(data)
            if not self.pending:
                self._frame_active = False
        return data

    def reset_input_buffer(self):
        self.pending.clear()

    def flush(self):
        return None

    def cancel_read(self):
        return None

    def close(self):
        self.closed = True


class MockBackend:
    def __init__(self, fixtures=None, version=4, running=False, throttle=False,
                 fault=None, chunk_size=4096, baud_1s_timeout=1.0, baud_idle_timeout=3.0):
        self.options = dict(fixtures=fixtures, version=version, running=running,
                            throttle=throttle, fault=fault, chunk_size=chunk_size,
                            baud_1s_timeout=baud_1s_timeout, baud_idle_timeout=baud_idle_timeout)
        self.port = None
        self.baudrate = 115200
        self.cancel_event = threading.Event()

    def open(self, device='mock://dual-mca'):
        if self.port is not None:
            raise RuntimeError('Mock backend is already connected')
        self.cancel_event.clear()
        self.port = MockPort(**self.options)
        self.baudrate = self.port.baudrate
        return self.port

    def sync_baudrate(self):
        if self.port is not None:
            self.port._check_baud_revert()
            self.baudrate = self.port.baudrate
        return self.baudrate

    def close(self):
        port, self.port = self.port, None
        if port is not None:
            port.close()

    def cancel(self):
        self.cancel_event.set()
        if self.port is not None:
            self.port.cancel_read()


def list_mock_devices():
    return [{'device': 'mock://dual-mca', 'description': 'Mock dual MCA (no hardware)',
             'serial_number': 'MOCK-0001'}]
