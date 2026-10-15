"""Serialized UART service, run independently of the GUI interpreter."""
import json
import queue
import time

from protocol import Link
from histogram import DEFAULT_GAIN
from spectrum_io import frame_record, write_session


class WorkerCore:
    def publish_state(self, operation=None, channel=None, baseline_counts=None):
        status = self.link.acquisition_status()
        self.live = status['running']
        self.event.emit('acquisition_state', dict(status, operation=operation, channel=channel,
                                                baseline_counts=baseline_counts,
                                                baudrate=self.link.serial.baudrate,
                                                control_events=list(self.link.control_events)))
        return status

    def transport_event(self, kind, value):
        if kind == 'recovered':
            value = dict(value, **self.link.acquisition_status(), control_events=list(self.link.control_events))
        self.event.emit(kind, value)

    def pair(self, force=True):
        status = self.publish_state()
        for ch in (1, 2):
            live = status['channel_acquiring'][ch] if status['independent_channel_control'] else status['running']
            if force or live or self.last_modes.get(ch) is not False:
                self.event.emit('frame', self.link.snapshot(ch, live))
                self.last_modes[ch] = live

    def preserve_before_change(self, op):
        if self.link.state():
            raise RuntimeError('Stop acquisition before changing settings or clearing')
        frames = {ch: self.link.snapshot(ch, False) for ch in (1, 2)}
        for frame in frames.values():
            self.event.emit('frame', frame)
        context = json.loads(json.dumps(self.preservation_context))
        channels = context.setdefault('channels', {})
        for ch, frame in frames.items():
            entry = channels.setdefault(str(ch), dict(calibration=[0., DEFAULT_GAIN], calibrated=False, grouping=8, rois=[]))
            if entry.get('calibrated') and entry.get('calibration_energy') != frame.get('energy'):
                entry['calibrated'] = False
            entry['frame'] = frame_record(frame)
            entry['device_settings'] = self.link.settings(ch)
        if any(frame['total'] or frame['stats']['acquisition_ticks'] for frame in frames.values()):
            path = self.link.root/'autosave'/f'{time.time_ns()}-before-{op}.dsw'
            write_session(path, context)
            self.event.emit('preserved', str(path))

    def run(self):
        next_pair = 0
        keepalive = 0
        while not self.done:
            try:
                try:
                    command = self.commands.get(timeout=.03)
                    op, arg = command[:2]
                    if len(command) == 3:
                        self.preservation_context = command[2]
                except queue.Empty:
                    op = None
                if op:
                    if op in ('clear', 'synthetic', 'adc', 'polarity', 'trigger', 'energy'):
                        self.preserve_before_change(op)
                    if op == 'connect':
                        self.link = getattr(self, 'link_factory', Link)(arg)
                        self.link.recovery_callback = self.transport_event
                        settings = {ch: self.link.settings(ch) for ch in (1, 2)}
                        self.link.baud(2000000)
                        self.live = self.link.running
                        self.last_modes.clear()
                        self.event.emit('connected', dict(self.link.acquisition_status(), settings=settings, baudrate=self.link.serial.baudrate))
                        self.pair()
                    elif op == 'disconnect':
                        self.live = False
                        if self.link:
                            self.link.close()
                        self.link = None
                        self.event.emit('disconnected', None)
                    elif op == 'start':
                        baseline = self.link.count_baselines() if arg is None else None
                        self.link.start(arg)
                        self.publish_state('start', arg, baseline)
                        next_pair = 0
                    elif op == 'stop':
                        self.link.stop(arg)
                        self.publish_state('stop', arg)
                        self.pair()
                        next_pair = time.monotonic()+1
                    elif op == 'clear':
                        self.link.clear()
                        self.last_modes.clear()
                        self.event.emit('cleared', None)
                    elif op == 'synthetic':
                        self.link.synthetic()
                        settings = {ch: self.link.settings(ch) for ch in (1, 2)}
                        self.event.emit('cleared', 'Cleared for synthetic source · zero counts')
                        self.event.emit('settings', settings)
                    elif op == 'adc':
                        self.link.adc(arg)
                        settings = {ch: self.link.settings(ch) for ch in (1, 2)}
                        self.event.emit('cleared', 'Cleared for ADC source · zero counts')
                        self.event.emit('adc', settings)
                    elif op == 'polarity':
                        ch, pol = arg
                        self.link.set_polarity(ch, pol)
                        settings = {c: self.link.settings(c) for c in (1, 2)}
                        self.event.emit('cleared', f'Cleared on Channel {ch} polarity change · zero counts')
                        self.event.emit('polarity', dict(channel=ch, polarity=pol, settings=settings))
                    elif op == 'trigger':
                        ch, floor, multiplier = arg
                        self.link.set_trigger(ch, floor, multiplier)
                        self.event.emit('cleared', f'Cleared on CH{ch} trigger change · zero counts')
                        self.event.emit('readback', {c: self.link.settings(c) for c in (1, 2)})
                    elif op == 'energy':
                        ch, configuration = arg
                        self.link.set_energy(ch, configuration)
                        self.last_modes.clear()
                        self.event.emit('energy_changed', dict(channel=ch, settings={c: self.link.settings(c) for c in (1, 2)}))
                    elif op == 'read_settings':
                        self.event.emit('readback', {c: self.link.settings(c) for c in (1, 2)})
                    elif op == 'read':
                        self.pair()
                    elif op == 'quit':
                        self.live = False
                        if self.link:
                            self.link.close()
                        self.link = None
                        self.done = True
                    self.event.emit('complete', op)
                now = time.monotonic()
                if self.link and self.live and now >= next_pair:
                    started = now
                    self.pair(force=False)
                    next_pair = max(started + 1, time.monotonic())
                    keepalive = time.monotonic()
                elif self.link and now - keepalive >= .8:
                    self.link.identity()
                    self.publish_state()
                    keepalive = time.monotonic()
            except Exception as exc:
                self.live = False
                if self.link:
                    self.link.serial.close()
                    self.link = None
                self.event.emit('error', str(exc))
                if op == 'quit':
                    self.done = True


class ProcessEvents:
    def __init__(self, events):
        self.events = events

    def emit(self, kind, value):
        self.events.put((kind, value))


def process_main(commands, events, link_factory=Link):
    service = WorkerCore()
    service.commands = commands
    service.event = ProcessEvents(events)
    service.link = None
    service.live = service.done = False
    service.last_modes = {}
    service.preservation_context = {}
    service.link_factory = link_factory
    try:
        service.run()
    finally:
        if service.link:
            service.link.serial.close()
        events.put(('worker_done', None))
