"""UART v5/v6 frames, energy settings, and detected H/A/O/Q channel controls."""
import json
import struct
import time
import zlib
from pathlib import Path

import numpy as np
import serial

STATS = ('triggered', 'pileup_rejected', 'clip_rejected', 'energy_overflow',
         'dpp_valid', 'histogrammed', 'dropped', 'bin_saturated', 'acquisition_ticks')


class FrameReadTimeout(TimeoutError):
    def __init__(self, expected, received, path):
        self.expected, self.received, self.path = expected, received, str(path)
        super().__init__(f'UART expected {expected} bytes, received {received}; partial bytes saved to {path}')


def polarity_code(polarity):
    if polarity in ('P', 'positive', '+'):
        return 'P'
    if polarity in ('N', 'negative', '-'):
        return 'N'
    raise ValueError('Pulse polarity must be Positive (P) or Negative (N)')


def crc8(data):
    value = 0
    for byte in data:
        value ^= byte
        for _ in range(8):
            value = ((value << 1) ^ (7 if value & 128 else 0)) & 255
    return value


ENERGY_PRESETS = {
    'NaI(Tl)': dict(mode='charge', gate_samples=100, pretrigger_samples=5, area_shift=6),
    'CsI(Tl)': dict(mode='charge', gate_samples=400, pretrigger_samples=5, area_shift=6),
    'CsI(Na)': dict(mode='charge', gate_samples=200, pretrigger_samples=5, area_shift=6),
    'Trapezoidal (existing profile)': dict(mode='trapezoidal', gate_samples=100, pretrigger_samples=5, area_shift=6),
}


def encode_energy(configuration):
    if not isinstance(configuration, dict) or set(configuration) != {'mode', 'gate_samples', 'pretrigger_samples', 'area_shift'}:
        raise ValueError('Energy settings require mode, gate_samples, pretrigger_samples and area_shift')
    mode, gate, pre, shift = (configuration[key] for key in ('mode', 'gate_samples', 'pretrigger_samples', 'area_shift'))
    if mode not in ('charge', 'trapezoidal') or any(type(value) is not int for value in (gate, pre, shift)):
        raise ValueError('Invalid energy mode or gate values')
    if not 1 <= gate <= 4096 or not 0 <= pre <= 63 or pre >= gate or not 0 <= shift <= 15:
        raise ValueError('Gate 1–4096 samples, pre-trigger 0–63 and < gate, normalization shift 0–15')
    return struct.pack('<BBHHH', int(mode == 'trapezoidal'), shift, gate, pre, 0)


def decode_energy(raw):
    if len(raw) != 16:
        raise ValueError('Incomplete energy settings')
    mode, shift, gate, pre, reserved, pole_zero, trap_norm = struct.unpack('<BBHHHII', raw)
    if mode not in (0, 1) or reserved or not pole_zero or not trap_norm:
        raise ValueError('Invalid energy settings')
    result = dict(mode='charge' if mode == 0 else 'trapezoidal', gate_samples=gate, pretrigger_samples=pre, area_shift=shift)
    encode_energy(result)
    return dict(result, gate_us=gate*.02, pretrigger_us=pre*.02, area_divisor=1 << shift,
                pole_zero_m_q12=pole_zero, trapezoidal_normalizer=trap_norm,
                pole_zero_applied=mode == 1, energy_quantity='pulse_area' if mode == 0 else 'shaped_height')


def decode(raw, channel, live):
    if len(raw) < 16:
        raise ValueError('Incomplete spectrum')
    magic, ver, ch, bits, flags, bins, seq = struct.unpack('<4sBBBBII', raw[:16])
    if ver not in (5, 6):
        raise ValueError('Unexpected spectrum identity')
    size = (65748 if live else 65676) + (16 if ver == 6 else 0)
    if len(raw) != size:
        raise ValueError('Incomplete spectrum')
    if zlib.crc32(raw[:-4]) != struct.unpack('<I', raw[-4:])[0]:
        raise ValueError('Spectrum CRC mismatch')
    if (magic, ch, bits, bins, flags & 127) != (
            f'SPL{ver}'.encode() if live else b'SPC2', channel, 14, 16384, 3):
        raise ValueError('Unexpected spectrum identity')
    start = dict(zip(STATS, struct.unpack('<9Q', raw[16:88])))
    offset = 152 if ver == 6 else 136
    end = dict(zip(STATS, struct.unpack('<9Q', raw[offset+65536:offset+65536+72]))) if live else start
    counts = np.frombuffer(raw[offset:offset+65536], dtype='<u4').copy()
    total = int(counts.sum(dtype=np.uint64))
    for stats in (start, end):
        if stats['dpp_valid'] != stats['histogrammed'] + stats['dropped'] + stats['bin_saturated']:
            raise ValueError('Committed accounting mismatch')
    if not start['histogrammed'] <= total <= end['histogrammed']:
        raise ValueError('Histogram sum outside statistics bracket')
    return dict(channel=ch, live=live, sequence=seq, counts=counts, stats=end,
                protocol_version=ver, energy=decode_energy(raw[136:152]) if ver == 6 else None,
                start_stats=start, total=total, raw=raw,
                source=struct.unpack('<BBHIIIQQ', raw[104:136]),
                trigger=struct.unpack('<HHIII', raw[88:104]), negative=bool(flags & 128))


class Link:
    def __init__(self, port, root='outputs', session_root=None):
        self.root = Path(session_root) if session_root is not None else Path(root) / time.strftime('%Y%m%d-%H%M%S')
        self.root.mkdir(parents=True, exist_ok=True)
        self.serial = serial.Serial(port=None, baudrate=115200, timeout=.1, write_timeout=1)
        self.serial.dtr = False
        self.serial.rts = False
        self.serial.port = port
        self.serial.exclusive = True
        self.serial.open()
        self.last = {}
        self.control_events = []
        self.independent_channel_control = False
        self.channel_acquiring = None
        self.recovery_callback = None
        self.recovery_count = 0
        try:
            # Drain an outstanding frame and let the documented high-baud lease expire.
            until = time.monotonic() + 10
            while time.monotonic() < until:
                self.serial.read(65536)
            self.serial.reset_input_buffer()
            self.identity()
            self.detect_channel_control()
            self.running = self.state()
        except Exception:
            self.serial.close()
            raise

    def read(self, size, timeout=2):
        data = bytearray()
        started = time.monotonic()
        frame = size > 4096
        # Drain modest chunks promptly. A frame gets a progress-based deadline,
        # with a hard cap so a stalled/trickling device cannot hold the worker.
        end = started + timeout
        hard_end = started + (timeout + 2 if frame else timeout)
        while len(data) < size and time.monotonic() < min(end, hard_end):
            chunk = self.serial.read(min(4096, size-len(data)))
            if chunk:
                data.extend(chunk)
                if frame:
                    end = time.monotonic() + timeout
        if len(data) != size:
            path = self.root / f'partial-{time.time_ns()}.bin'
            path.write_bytes(data)
            raise FrameReadTimeout(size, len(data), path)
        return bytes(data)

    def query(self, command, size=1):
        self.serial.write(command)
        return self.read(size)

    def ack(self, command, expected=b'K'):
        reply = self.query(command)
        if reply != expected:
            raise RuntimeError(f'{command[:1]!r} rejected: {reply!r}')

    def identity(self):
        identity = struct.unpack('<4sBBBBII', self.query(b'I', 16))
        if identity[1] not in (5, 6) or (identity[0], *identity[2:]) != (b'DMC2', 2, 14, 3, 16384, 50000000):
            raise RuntimeError(f'Unsupported firmware identity: {identity}')
        self.protocol_version = identity[1]
        return identity

    def state(self):
        if getattr(self, 'independent_channel_control', False):
            self.channel_state()
            return self.running
        reply = self.query(b'?')
        if reply not in (b'0', b'1'):
            raise ValueError('Invalid acquisition state')
        self.running = reply == b'1'
        self.channel_acquiring = None
        return self.running

    def detect_channel_control(self):
        reply = self.query(b'H')
        if reply not in (b'1', b'?'):
            raise ValueError(f'Invalid independent-control capability: {reply!r}')
        self.independent_channel_control = reply == b'1'
        self.channel_acquiring = None
        return self.independent_channel_control

    def channel_state(self):
        if not getattr(self, 'independent_channel_control', False):
            raise RuntimeError('Independent channel control is unsupported by this firmware')
        reply = self.query(b'Q')
        if reply not in (b'0', b'1', b'2', b'3'):
            raise ValueError(f'Invalid channel acquisition state: {reply!r}')
        mask = int(reply)
        self.channel_acquiring = {ch: bool(mask & (1 << (ch-1))) for ch in (1, 2)}
        self.running = bool(mask)
        return dict(self.channel_acquiring)

    def acquisition_status(self, refresh=True):
        if refresh:
            self.state()
        return dict(running=self.running,
                    protocol_version=getattr(self, 'protocol_version', 5),
                    independent_channel_control=self.independent_channel_control,
                    channel_acquiring=dict(self.channel_acquiring) if self.channel_acquiring is not None else None)

    def baud(self, rate):
        packet = b'U' + bytes([{115200: 0, 921600: 1, 2000000: 2}[rate]])
        self.ack(packet + bytes([crc8(packet)]))
        self.serial.baudrate = rate
        self.identity()

    def select(self, channel):
        if channel not in (1, 2):
            raise ValueError('Channel must be 1 or 2')
        self.ack(b'J' if channel == 1 else b'K')

    def settings(self, channel):
        self.select(channel)
        result = dict(source=struct.unpack('<BBHIIIQQ', self.query(b'V', 32)),
                    trigger=struct.unpack('<HHIII', self.query(b'G', 16)),
                    polarity=self.query(b'F').decode(),
                    stats=dict(zip(STATS, struct.unpack('<9Q', self.query(b'T', 72)))))
        result['energy'] = decode_energy(self.query(b'M', 16)) if getattr(self, 'protocol_version', 5) >= 6 else None
        return result

    def count_baselines(self):
        totals = {}
        for ch in (1, 2):
            self.select(ch)
            stats = dict(zip(STATS, struct.unpack('<9Q', self.query(b'T', 72))))
            totals[ch] = stats['histogrammed']
        return totals

    def ready(self):
        deadline = time.monotonic() + 3
        while time.monotonic() < deadline:
            if self.query(b'R') == b'R':
                return
            time.sleep(.02)
        raise TimeoutError('FPGA not ready')

    def clear(self):
        if self.state():
            raise RuntimeError('Stop before Clear')
        self.ack(b'C', b'C')
        self.ready()
        self.last.clear()

    def _control(self, operation, channel):
        if channel is not None and channel not in (1, 2):
            raise ValueError('Channel must be 1 or 2')
        independent = getattr(self, 'independent_channel_control', False)
        if channel is not None and not independent:
            raise RuntimeError('Independent channel control is unsupported by this firmware')
        before = self.acquisition_status()
        start = operation == 'start'
        if not independent and start and before['running']:
            raise RuntimeError('Already acquiring')
        if channel is None:
            self.ack(b'S' if start else b'E', b'A' if start else b'O')
        else:
            self.select(channel)
            self.ack(b'A' if start else b'O', b'A' if start else b'O')
        after = self.acquisition_status()
        if independent:
            expected = dict(before['channel_acquiring'])
            for ch in (1, 2) if channel is None else (channel,):
                expected[ch] = start
            if after['channel_acquiring'] != expected:
                raise RuntimeError('Acquisition readback mismatch; selected channel or untouched peer changed unexpectedly')
        elif after['running'] != start:
            raise RuntimeError('Acquisition readback mismatch')
        event = dict(time=time.strftime('%Y-%m-%dT%H:%M:%S%z'), operation=operation,
                     channel=channel if channel is not None else 'all', before=before, after=after)
        if not hasattr(self, 'control_events'):
            self.control_events = []
        self.control_events.append(event)
        with (self.root/'channel-control.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(event)+'\n')

    def start(self, channel=None):
        self._control('start', channel)

    def stop(self, channel=None):
        self._control('stop', channel)

    def snapshot(self, channel, live=True):
        if channel not in (1, 2):
            raise ValueError('Channel must be 1 or 2')
        for attempt in range(2):
            try:
                return self._snapshot(channel, live)
            except FrameReadTimeout as exc:
                if attempt or self.recovery_count >= 2:
                    raise
                self.recover_transport(exc)
            except ValueError as exc:
                if 'CRC mismatch' not in str(exc) or attempt or self.recovery_count >= 2:
                    raise
                self.recover_transport(exc)

    def recover_transport(self, error):
        old_rate = self.serial.baudrate
        # The documented rate fallback changes transport only, never enables,
        # histograms, source, polarity or trigger settings.
        rate = 921600
        callback = self.recovery_callback
        if callback:
            callback('recovering', dict(error=str(error), baudrate=old_rate))
        port, root = self.serial.port, self.root
        previous, events = self.last, self.control_events
        recoveries = self.recovery_count + 1
        self.serial.close()
        replacement = Link(port, session_root=root)
        try:
            replacement.baud(rate)
        except Exception:
            replacement.serial.close()
            raise
        replacement.last = previous
        replacement.control_events = events
        replacement.recovery_callback = callback
        replacement.recovery_count = recoveries
        self.__dict__.update(replacement.__dict__)
        record = dict(time=time.strftime('%Y-%m-%dT%H:%M:%S%z'), error=str(error),
                      old_baudrate=old_rate, baudrate=rate, attempt=recoveries,
                      acquisition=self.acquisition_status())
        with (root/'uart-recovery.jsonl').open('a', encoding='utf-8') as handle:
            handle.write(json.dumps(record)+'\n')
        if callback:
            callback('recovered', record)

    def _snapshot(self, channel, live=True):
        if not live:
            running = self.state()
            selected_running = self.channel_acquiring[channel] if self.independent_channel_control else running
            if selected_running:
                raise RuntimeError(f'Stop channel {channel} before reading a coherent FINAL spectrum')
        self.select(channel)
        self.serial.write(b'L' if live else b'D')
        size = (65748 if live else 65676) + (16 if getattr(self, 'protocol_version', 5) == 6 else 0)
        raw = self.read(size, size * 10 / self.serial.baudrate + 2)
        path = self.root / f'{time.time_ns()}-ch{channel}-{"live" if live else "final"}.bin'
        path.write_bytes(raw)
        try:
            frame = decode(raw, channel, live)
        except ValueError:
            path.replace(self.root/f'invalid-frame-{time.time_ns()}.bin')
            raise
        previous = self.last.get(channel)
        if previous is not None and np.any(frame['counts'] < previous):
            raise ValueError('Counts decreased: reset or continuity break; reconnect before proceeding')
        self.last[channel] = frame['counts'].copy()
        frame['path'] = str(path)
        # Bound automatic live retention; FINAL files are always retained.
        files = sorted(self.root.glob(f'*-ch{channel}-live.bin'))
        for old in files[:-20]:
            old.unlink()
        return frame

    def source(self, channel, payload):
        if self.state():
            raise RuntimeError('Stop before changing source')
        self.select(channel)
        checksum = zlib.crc32(bytes([channel]) + payload)
        self.ack(b'X' + payload + struct.pack('<I', checksum))
        actual = self.query(b'V', 32)
        if actual[:16] != payload:
            raise RuntimeError('Source readback mismatch')

    def synthetic(self):
        """Switch to synthetic pulse generator, automatically clearing histograms."""
        if self.state():
            self.stop()
        self.clear()
        for ch, amplitude, period in ((1, 101, 25000), (2, 401, 50000)):
            self.select(ch)
            self.ack(b'P')
            self.source(ch, struct.pack('<BBHIII', 1, 1, amplitude, period, 16770683, 0))
        self.ready()

    def adc(self, polarities=None):
        """Switch both channels to physical ADC sources, automatically clearing histograms.

        polarities: optional mapping of {channel: 'P'/'N'} or a single 'P' / 'N'.
        If None, preserves each channel's configured polarity.
        """
        requested = {}
        if isinstance(polarities, dict):
            if any(ch not in (1, 2) for ch in polarities):
                raise ValueError('ADC channel must be 1 or 2')
            requested = {ch: polarity_code(pol) for ch, pol in polarities.items()}
        elif polarities is not None:
            requested = {ch: polarity_code(polarities) for ch in (1, 2)}
        if self.state():
            self.stop()
        self.clear()
        for ch in (1, 2):
            if ch in requested:
                self._apply_polarity(ch, requested[ch])
            values = list(self.settings(ch)['source'][:6])
            values[0] = 0
            self.source(ch, struct.pack('<BBHIII', *values))
        self.ready()

    def set_polarity(self, channel, polarity):
        """Set pulse polarity ('P'/'positive' or 'N'/'negative') on a channel, automatically clearing histograms."""
        if channel not in (1, 2):
            raise ValueError('ADC channel must be 1 or 2')
        code = polarity_code(polarity)
        if self.state():
            self.stop()
        self.clear()
        self._apply_polarity(channel, code)
        self.ready()

    def _apply_polarity(self, channel, code):
        self.select(channel)
        self.ack(code.encode())
        if self.query(b'F').decode() != code:
            raise RuntimeError(f'Channel {channel} pulse polarity readback mismatch')

    def set_trigger(self, channel, floor_codes, sigma_multiplier):
        """Set existing v5 W trigger fields after an explicit preserved, stopped clear."""
        if channel not in (1, 2) or not isinstance(floor_codes, int) or not 1 <= floor_codes <= 4095:
            raise ValueError('Use channel 1/2 and integer trigger floor 1–4095 ADC codes')
        if not np.isfinite(sigma_multiplier) or not 0 <= sigma_multiplier <= 16:
            raise ValueError('Noise multiplier must be between 0 and 16')
        multiplier = round(sigma_multiplier*256)
        if self.state():
            raise RuntimeError('Stop before changing ADC trigger settings')
        self.clear()
        self.select(channel)
        fields = struct.pack('<HH', floor_codes, multiplier)
        checksum = zlib.crc32(bytes([channel])+fields)
        self.ack(b'W'+fields+struct.pack('<I', checksum))
        actual = struct.unpack('<HHIII', self.query(b'G', 16))
        if actual[:2] != (floor_codes, multiplier):
            raise RuntimeError(f'Channel {channel} trigger readback mismatch')
        self.ready()

    def set_energy(self, channel, configuration):
        if getattr(self, 'protocol_version', 5) < 6:
            raise RuntimeError('Energy processing controls require firmware v6')
        if channel not in (1, 2):
            raise ValueError('Channel must be 1 or 2')
        payload = encode_energy(configuration)
        if self.state():
            raise RuntimeError('Stop before changing energy processing')
        self.clear()
        self.select(channel)
        self.ack(b'Z'+payload+struct.pack('<I', zlib.crc32(bytes([channel])+payload)))
        actual = decode_energy(self.query(b'M', 16))
        if any(actual[key] != value for key, value in configuration.items()):
            raise RuntimeError('Energy processing readback mismatch')
        self.ready()
        return actual

    def close(self):
        # Never stop or clear acquisition on disconnect.
        try:
            self.baud(115200)
        finally:
            self.serial.close()
