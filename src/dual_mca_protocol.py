"""Shared byte-level protocol implementation for the dual-channel MCA."""
import hashlib
import json
import math
import struct
import time
import zlib
from pathlib import Path

import numpy as np
from energy_calibration import rebin_kev


def read_exact(ser, count, deadline_seconds=5.0, sink=None):
    """Read exactly up to count bytes using the legacy MCA timeout semantics."""
    data = bytearray()
    deadline = time.monotonic() + deadline_seconds
    while len(data) < count and time.monotonic() < deadline:
        chunk = ser.read(count - len(data))
        if not chunk:
            break
        data.extend(chunk)
        if sink is not None:
            sink.write(chunk)
            sink.flush()
    return bytes(data)


def command(ser, code, expected):
    ser.write(code)
    ser.flush()
    reply = read_exact(ser, 1)
    if reply != expected:
        raise RuntimeError(f'Command {code!r}: expected {expected!r}, received {reply!r}')


class ReadCancelled(RuntimeError):
    """A bounded snapshot read was cancelled while preserving received bytes."""


def read_exact_transfer(ser, count, deadline_seconds, sink=None, cancel_event=None,
                       progress_callback=None, progress_offset=0):
    """Read a long frame through short serial polls until its transfer deadline.

    Unlike the legacy helper above, an empty poll is not treated as EOF. This
    is required when pySerial uses a 100 ms timeout to make shutdown responsive.
    """
    data = bytearray()
    deadline = time.monotonic() + deadline_seconds
    last_progress = 0.0
    while len(data) < count:
        if cancel_event is not None and cancel_event.is_set():
            raise ReadCancelled(f'Snapshot read cancelled after {len(data)}/{count} bytes')
        remaining = deadline - time.monotonic()
        if remaining <= 0:
            break
        chunk = ser.read(min(count - len(data), 65536))
        if not chunk:
            continue
        data.extend(chunk)
        if sink is not None:
            sink.write(chunk)
            sink.flush()
        now = time.monotonic()
        if progress_callback is not None and now - last_progress >= 0.2:
            progress_callback(progress_offset + len(data), progress_offset + count)
            last_progress = now
    if progress_callback is not None:
        progress_callback(progress_offset + len(data), progress_offset + count)
    return bytes(data)


STAT_FIELDS = ('triggered','pileup_rejected','clip_rejected','energy_overflow',
               'dpp_valid','histogrammed','dropped','bin_saturated','acquisition_ticks')
INFO_SIZE = 16
SNAPSHOT_SIZE = 16+72+16384*4+4

# Bytes following the common header/statistics fields. v5 coherent D-frames share
# the v4 layout; live L-frames carry two stats blocks (see LIVE_FRAME_SIZE).
FRAME_LAYOUTS = {
    1: {'trigger_bytes': 0, 'source_bytes': 0},
    2: {'trigger_bytes': 0, 'source_bytes': 0},
    3: {'trigger_bytes': 16, 'source_bytes': 0},
    4: {'trigger_bytes': 16, 'source_bytes': 32},
    5: {'trigger_bytes': 16, 'source_bytes': 32},
    6: {'trigger_bytes': 16, 'source_bytes': 32, 'energy_bytes': 16},
}

# v5 baud switch (S7): rate byte -> baud -> clks_per_bit @50 MHz.
BAUD_RATES = {115200: 0, 921600: 1, 2000000: 2}
BAUD_RATE_BYTES = {0: 115200, 1: 921600, 2: 2000000}
BAUD_CLKS = {115200: 434, 921600: 54, 2000000: 25}
LIVE_FRAME_SIZE = 16 + 72 + 16 + 32 + 16384 * 4 + 72 + 4  # 65748


def crc8_atm(data):
    """CRC8/ATM (poly 0x07, init 0) over bytes, matching RTL crc8_byte."""
    crc = 0
    for byte in data:
        crc ^= byte
        for _ in range(8):
            crc = ((crc << 1) ^ 0x07) & 0xff if crc & 0x80 else (crc << 1) & 0xff
    return crc


def encode_baud_packet(rate):
    """Build a 3-byte U packet for the requested baud rate."""
    if rate not in BAUD_RATES:
        raise ValueError(f'Unsupported baud rate: {rate!r}; choose 115200, 921600 or 2000000')
    rate_byte = BAUD_RATES[rate]
    packet = bytes([ord('U'), rate_byte])
    return packet + bytes([crc8_atm(packet)])


def live_frame_size(version=5):
    if version not in (5, 6):
        raise ValueError(f'Unsupported live protocol version: {version!r}')
    return LIVE_FRAME_SIZE + (16 if version == 6 else 0)


def snapshot_size(version):
    try:
        layout = FRAME_LAYOUTS[version]
    except (KeyError, TypeError):
        raise ValueError(f'Unsupported dual MCA protocol version: {version!r}') from None
    return SNAPSHOT_SIZE + layout['trigger_bytes'] + layout['source_bytes'] + layout.get('energy_bytes', 0)


def identify(port, allow_unknown=False):
    port.write(b'I');port.flush()
    raw = read_exact(port, INFO_SIZE)
    if len(raw) != INFO_SIZE:
        raise RuntimeError('Incomplete dual MCA identity; firmware may be legacy')
    magic,version,channels,bits,fraction,bins,clock = struct.unpack('<4sBBBBII',raw)
    if (not allow_unknown and version not in FRAME_LAYOUTS) or (magic,channels,bits,fraction,bins,clock) != (b'DMC2',2,14,3,16384,50000000):
        raise RuntimeError('Unexpected dual MCA identity; no acquisition settings changed')
    return dict(protocol_version=version,channels=channels,address_bits=bits,
                fractional_bits=fraction,bins=bins,clock_hz_nominal=clock)


def statistics(port, channel):
    command(port, b'J' if channel == 1 else b'K', b'K')
    port.write(b'T');port.flush()
    raw = read_exact(port,72)
    if len(raw) != 72:
        raise RuntimeError('Incomplete dual MCA statistics')
    return dict(zip(STAT_FIELDS,struct.unpack('<9Q',raw)))


def polarity(port, channel, firmware):
    if firmware['protocol_version']==1:return 'positive'
    command(port,b'J' if channel==1 else b'K',b'K')
    port.write(b'F');port.flush();reply=read_exact(port,1)
    if reply not in (b'P',b'N'):raise RuntimeError('Invalid polarity readback')
    return 'negative' if reply==b'N' else 'positive'


def set_polarity(port, channel, value, firmware):
    if value not in ('positive','negative'):raise ValueError('Unknown polarity')
    if firmware['protocol_version']==1:
        if value=='negative':raise RuntimeError('Legacy dual firmware has no runtime polarity control')
        return
    command(port,b'J' if channel==1 else b'K',b'K')
    command(port,b'N' if value=='negative' else b'P',b'K')
    if polarity(port,channel,firmware)!=value:raise RuntimeError('Polarity write/readback mismatch')


def validate_trigger(configuration):
    if configuration is None:return
    floor=configuration['floor_codes'];multiplier=configuration['sigma_multiplier']
    if type(floor) is not int or isinstance(multiplier, bool) or not isinstance(multiplier, (int, float)) or not np.isfinite(multiplier) or not 1<=floor<=4095 or not 0<=multiplier<=16:
        raise ValueError('Trigger floor must be 1..4095 codes; N must be 0..16')


def decode_trigger_configuration(raw):
    if len(raw)!=16:raise ValueError('Incomplete trigger configuration')
    floor,multiplier,sigma,threshold,flags=struct.unpack('<HHIII',raw)
    if not 1<=floor<=4095 or multiplier>4096 or threshold>8191:raise ValueError('Invalid trigger configuration')
    return dict(floor_codes=floor,sigma_multiplier_q8=multiplier,sigma_multiplier=multiplier/256,
                sigma_fast_q8=sigma,sigma_fast_codes=sigma/256,effective_threshold=threshold,
                noise_valid=bool(flags&1),noise_updates=flags>>1,method='max(floor, ceil(N * sigma_fast))')


def trigger_configuration(port,channel,firmware):
    if firmware['protocol_version']<3:return None
    command(port,b'J' if channel==1 else b'K',b'K')
    port.write(b'G');port.flush()
    return decode_trigger_configuration(read_exact(port,16))


def set_trigger_configuration(port,channel,floor,multiplier,firmware):
    if firmware['protocol_version']<3:raise RuntimeError('Runtime trigger configuration requires protocol v3')
    if not isinstance(floor,int) or not 1<=floor<=4095 or not np.isfinite(multiplier) or not 0<=multiplier<=16:
        raise ValueError('Trigger floor must be 1..4095 codes; N must be 0..16')
    q8=int(round(multiplier*256));payload=struct.pack('<HH',floor,q8)
    crc=zlib.crc32(bytes([channel])+payload)
    command(port,b'J' if channel==1 else b'K',b'K')
    port.write(b'W'+payload+struct.pack('<I',crc));port.flush()
    reply=read_exact(port,1)
    if reply!=b'K':raise RuntimeError(f'Trigger write rejected: {reply!r}; counts were not cleared automatically')
    result=trigger_configuration(port,channel,firmware)
    if (result['floor_codes'],result['sigma_multiplier_q8'])!=(floor,q8):raise RuntimeError('Trigger write/readback mismatch')
    return result


def encode_source_settings(configuration):
    source=configuration['source'];mode=configuration.get('mode','continuous')
    amplitude=configuration.get('amplitude_codes',101);period=configuration.get('period_ticks',25000)
    noise=configuration.get('noise_amplitude_codes',0);negative=configuration.get('negative',False)
    tau=configuration.get('decay_us',51.35)
    if source not in ('adc','synthetic') or mode not in ('single','continuous') or not isinstance(negative,bool):raise ValueError('Invalid source/mode/polarity')
    if type(amplitude) is not int or not 1<=amplitude<=2047 or type(period) is not int or not 100<=period<=0xffffffff:
        raise ValueError('Amplitude 1..2047 codes and period 100..2^32-1 ticks required')
    if type(noise) is not int or not 0<=noise<=7 or isinstance(tau, bool) or not isinstance(tau, (int, float)) or not np.isfinite(tau) or tau<0:raise ValueError('Invalid noise amplitude or decay time')
    alpha=0 if tau==0 else int(round(math.exp(-.02/tau)*(1<<24)))
    if not 0<=alpha<1<<24:raise ValueError('Decay time exceeds Q24 representation')
    return struct.pack('<BBHIII',int(source=='synthetic'),int(mode=='continuous')|(int(negative)<<1),amplitude,period,alpha,noise)


def decode_source_configuration(raw):
    if len(raw)!=32:raise ValueError('Incomplete source configuration')
    source,flags,amp,period,alpha,noise,injected,clipped=struct.unpack('<BBHIIIQQ',raw)
    if source>1 or flags>3 or not 1<=amp<=2047 or period<100 or alpha>=1<<24 or noise>7:raise ValueError('Invalid source configuration')
    return dict(source='synthetic' if source else 'adc',mode='continuous' if flags&1 else 'single',negative=bool(flags&2),
                amplitude_codes=amp,period_ticks=period,rate_hz_nominal=50000000/period,decay_alpha_q24=alpha,
                decay_us_actual=0 if not alpha else -.02/math.log(alpha/(1<<24)),noise_amplitude_codes=noise,
                injected=injected,clipped_samples=clipped)


def source_configuration(port,channel,firmware):
    if firmware['protocol_version']<4:return None
    command(port,b'J' if channel==1 else b'K',b'K');port.write(b'V');port.flush()
    return decode_source_configuration(read_exact(port,32))


def set_source_configuration(port,channel,configuration,firmware):
    if firmware['protocol_version']<4:raise RuntimeError('Runtime source control requires protocol v4')
    payload=encode_source_settings(configuration)
    command(port,b'J' if channel==1 else b'K',b'K')
    port.write(b'X'+payload+struct.pack('<I',zlib.crc32(bytes([channel])+payload)));port.flush()
    reply=read_exact(port,1)
    if reply!=b'K':raise RuntimeError(f'Source write rejected: {reply!r}; no implicit clear')
    result=source_configuration(port,channel,firmware)
    expected=struct.unpack('<BBHIII',payload)
    actual=(int(result['source']=='synthetic'),int(result['mode']=='continuous')|(int(result['negative'])<<1),
            result['amplitude_codes'],result['period_ticks'],result['decay_alpha_q24'],result['noise_amplitude_codes'])
    if actual!=expected:raise RuntimeError('Source write/readback mismatch')
    return result


ENERGY_MODES = {'charge': 0, 'trapezoidal': 1}
ENERGY_PRESETS = {
    'nai_tl': dict(mode='charge', gate_samples=100, pretrigger_samples=5, area_shift=6),
    'csi_tl': dict(mode='charge', gate_samples=400, pretrigger_samples=5, area_shift=6),
    'csi_na': dict(mode='charge', gate_samples=200, pretrigger_samples=5, area_shift=6),
    'trapezoidal': dict(mode='trapezoidal', gate_samples=100, pretrigger_samples=5, area_shift=6),
}


def encode_energy_settings(configuration):
    """v6 Z payload; gate width includes pre-trigger samples, at 20 ns/sample."""
    if not isinstance(configuration, dict) or set(configuration) != {'mode', 'gate_samples', 'pretrigger_samples', 'area_shift'}:
        raise ValueError('Energy settings require mode, gate_samples, pretrigger_samples and area_shift')
    mode, gate, pre, shift = (configuration[key] for key in ('mode', 'gate_samples', 'pretrigger_samples', 'area_shift'))
    if mode not in ENERGY_MODES or any(type(value) is not int for value in (gate, pre, shift)):
        raise ValueError('Invalid energy mode or noninteger gate/normalization')
    if not 1 <= gate <= 4096 or not 0 <= pre <= 63 or pre >= gate or not 0 <= shift <= 15:
        raise ValueError('Gate 1..4096, pre-trigger 0..63 and < gate, area shift 0..15 required')
    return struct.pack('<BBHHH', ENERGY_MODES[mode], shift, gate, pre, 0)


def decode_energy_configuration(raw):
    if len(raw) != 16:
        raise ValueError('Incomplete energy configuration')
    mode, shift, gate, pre, reserved, pole_zero, trap_norm = struct.unpack('<BBHHHII', raw)
    if mode not in (0, 1) or reserved or not pole_zero or not trap_norm:
        raise ValueError('Invalid energy configuration')
    result = dict(mode='charge' if mode == 0 else 'trapezoidal', gate_samples=gate,
                  pretrigger_samples=pre, area_shift=shift)
    encode_energy_settings(result)
    return dict(result, gate_us=gate*.02, pretrigger_us=pre*.02,
                area_divisor=1 << shift, pole_zero_m_q12=pole_zero,
                trapezoidal_normalizer=trap_norm,
                pole_zero_applied=mode == 1,
                energy_quantity='pulse_area' if mode == 0 else 'shaped_height')


def energy_identity(configuration):
    if configuration is None:
        return None
    return {key: configuration.get(key) for key in
            ('mode', 'gate_samples', 'pretrigger_samples', 'area_shift',
             'pole_zero_m_q12', 'trapezoidal_normalizer')}


def energy_configuration(port, channel, firmware):
    if firmware['protocol_version'] < 6:
        return None
    command(port, b'J' if channel == 1 else b'K', b'K')
    port.write(b'M'); port.flush()
    return decode_energy_configuration(read_exact(port, 16))


def set_energy_configuration(port, channel, configuration, firmware):
    if firmware['protocol_version'] < 6:
        raise RuntimeError('Charge integration settings require protocol v6')
    payload = encode_energy_settings(configuration)
    command(port, b'J' if channel == 1 else b'K', b'K')
    port.write(b'Z'+payload+struct.pack('<I', zlib.crc32(bytes([channel])+payload))); port.flush()
    reply = read_exact(port, 1)
    if reply != b'K':
        raise RuntimeError(f'Energy write rejected: {reply!r}; no implicit clear')
    actual = energy_configuration(port, channel, firmware)
    if any(actual[key] != value for key, value in configuration.items()):
        raise RuntimeError('Energy write/readback mismatch')
    return actual


def manual_pulse(port,channel,firmware):
    if firmware['protocol_version']<4:raise RuntimeError('Manual synthetic pulse requires protocol v4')
    command(port,b'J' if channel==1 else b'K',b'K');command(port,b'Y',b'K')


def diagnostics(port, channel):
    command(port,b'J' if channel==1 else b'K',b'K')
    port.write(b'B');port.flush()
    raw=read_exact(port,16)
    if len(raw)!=16:raise RuntimeError('Incomplete ADC/DSP diagnostics')
    word,baseline,fast,threshold=struct.unpack('<IIii',raw)
    return dict(adc_code=word&4095,adc_otr=bool(word&4096),baseline_code=baseline,
                fast_filter=fast,effective_threshold=threshold)


def decode_snapshot(raw, channel):
    if len(raw) < 16:
        raise ValueError(f'Incomplete snapshot header: {len(raw)}/16')
    magic,version,actual,bits,flags,bins,sequence = struct.unpack('<4sBBBBII',raw[:16])
    expected_size = snapshot_size(version)
    if len(raw) != expected_size:
        raise ValueError(f'Incomplete snapshot: {len(raw)}/{expected_size}')
    fraction=flags&0x7f
    negative=bool(flags&0x80)
    if channel not in (1, 2) or (version==1 and negative) or (magic,actual,bits,fraction,bins) != (b'SPC2',channel,14,3,16384):
        raise ValueError('Snapshot format or channel mismatch')
    if zlib.crc32(raw[:-4]) != int.from_bytes(raw[-4:],'little'):
        raise ValueError('Snapshot CRC32 mismatch')
    stats = dict(zip(STAT_FIELDS,struct.unpack('<9Q',raw[16:88])))
    layout = FRAME_LAYOUTS[version]
    trigger_end = 88 + layout['trigger_bytes']
    source_end = trigger_end + layout['source_bytes']
    trigger_configuration=decode_trigger_configuration(raw[88:trigger_end]) if layout['trigger_bytes'] else None
    source_configuration_value=decode_source_configuration(raw[trigger_end:source_end]) if layout['source_bytes'] else None
    energy_end = source_end + layout.get('energy_bytes', 0)
    energy_configuration_value = decode_energy_configuration(raw[source_end:energy_end]) if layout.get('energy_bytes') else None
    spectrum = np.frombuffer(raw[energy_end:-4],dtype='<u4').copy()
    if int(spectrum.sum(dtype=np.uint64)) != stats['histogrammed']:
        raise ValueError('Histogram sum differs from accepted histogram count')
    if stats['dpp_valid'] != stats['histogrammed']+stats['dropped']+stats['bin_saturated']:
        raise ValueError('DPP/histogram accounting mismatch')
    return dict(channel=channel,sequence=sequence,bins=bins,fractional_bits=fraction,
                protocol_version=version,polarity='negative' if negative else 'positive',trigger_configuration=trigger_configuration,source_configuration=source_configuration_value,energy_configuration=energy_configuration_value,counters=stats), spectrum


def decode_live_frame(raw, channel):
    """Decode a v5 non-coherent live frame (SPL5) with bracket checks (S7)."""
    if len(raw) < 16:
        raise ValueError(f'Incomplete live header: {len(raw)}/16')
    magic,version,actual,bits,flags,bins,sequence = struct.unpack('<4sBBBBII',raw[:16])
    size = live_frame_size(version)
    if len(raw) != size:
        raise ValueError(f'Incomplete live frame: {len(raw)}/{size}')
    fraction = flags & 0x7f
    negative = bool(flags & 0x80)
    if channel not in (1, 2) or (magic,actual,bits,fraction,bins) != (f'SPL{version}'.encode(),channel,14,3,16384):
        raise ValueError('Live frame format or channel mismatch')
    if zlib.crc32(raw[:-4]) != int.from_bytes(raw[-4:],'little'):
        raise ValueError('Live frame CRC32 mismatch')
    start = dict(zip(STAT_FIELDS, struct.unpack('<9Q', raw[16:88])))
    trigger_configuration = decode_trigger_configuration(raw[88:104])
    source_configuration_value = decode_source_configuration(raw[104:136])
    energy_end = 152 if version == 6 else 136
    energy_configuration_value = decode_energy_configuration(raw[136:energy_end]) if version == 6 else None
    spectrum = np.frombuffer(raw[energy_end:energy_end+65536], dtype='<u4').copy()
    end = dict(zip(STAT_FIELDS, struct.unpack('<9Q', raw[energy_end+65536:energy_end+65536+72])))
    for label, stats in (('start', start), ('end', end)):
        if stats['dpp_valid'] != stats['histogrammed']+stats['dropped']+stats['bin_saturated']:
            raise ValueError(f'Live {label} DPP/histogram accounting mismatch')
    total = int(spectrum.sum(dtype=np.uint64))
    if not start['histogrammed'] <= total <= end['histogrammed']:
        raise ValueError(f'Live bracket violated: start={start["histogrammed"]} sum={total} end={end["histogrammed"]}')
    return dict(channel=channel,sequence=sequence,bins=bins,fractional_bits=fraction,
                protocol_version=version,polarity='negative' if negative else 'positive',
                trigger_configuration=trigger_configuration,source_configuration=source_configuration_value,
                energy_configuration=energy_configuration_value,start_counters=start,end_counters=end), spectrum


def negotiate_baud(port, rate):
    """Send a v5 U packet and expect K at the old rate (host switches baud after).

    Full negotiation (baud switch, I validation within 1 s, revert/lease) lives in
    DaqSession which owns the serial port; this helper shares packet framing.
    """
    port.write(encode_baud_packet(rate));port.flush()
    reply = read_exact(port, 1, deadline_seconds=2.0)
    if reply != b'K':
        raise RuntimeError(f'Baud switch rejected: {reply!r}; rate unchanged')
    return reply


def dump_live_channel(port, channel, baudrate=2000000, cancel_event=None, on_progress=None):
    """Read one v5 live frame (allowed while acquiring). Returns (info, spectrum)."""
    info, spectrum, _raw = dump_live_raw(port, channel, baudrate, cancel_event, on_progress)
    return info, spectrum


def dump_live_raw(port, channel, baudrate=2000000, cancel_event=None, on_progress=None, sink=None):
    """Read one v5 live frame, returning (info, spectrum, raw validated bytes)."""
    command(port, b'J' if channel == 1 else b'K', b'K')
    port.write(b'L');port.flush()
    first = read_exact_transfer(port, 1, 3.0, cancel_event=cancel_event, sink=sink)
    if first == b'B':
        raise RuntimeError('Live read rejected while clearing')
    if len(first) != 1:
        raise ValueError(f'Incomplete live reply: {first!r}')
    header = first + read_exact_transfer(port, 15, 3.0, cancel_event=cancel_event, sink=sink)
    if len(header) != 16:
        raise ValueError(f'Incomplete live header: {len(header)}/16')
    size = live_frame_size(header[4])
    if header[:4] != f'SPL{header[4]}'.encode():
        raise ValueError('Live frame requires matching v5/v6 SPL magic')
    wire_bytes_per_second = max(1.0, float(baudrate) / 10.0)
    transfer_deadline = max(3.0, (size - 16) / wire_bytes_per_second * 1.5 + 1.0)
    if on_progress is not None:
        on_progress(16, size)
    remainder = read_exact_transfer(port, size - 16, transfer_deadline,
                                    cancel_event=cancel_event, sink=sink,
                                    progress_callback=on_progress, progress_offset=16)
    raw = header + remainder
    info, spectrum = decode_live_frame(raw, channel)
    return info, spectrum, raw


def dump_channel(port, channel, directory, calibration=None, width=1, maximum=3000,
                 baudrate=115200, cancel_event=None, on_progress=None):
    directory.mkdir(parents=True,exist_ok=False)
    metadata = dict(status='incomplete',channel=channel)
    try:
        command(port,b'J' if channel==1 else b'K',b'K')
        port.write(b'D');port.flush()
        with (directory/'snapshot.bin').open('wb') as stream:
            header=read_exact_transfer(port,16,3.0,sink=stream,cancel_event=cancel_event)
            if len(header) != 16:
                raise ValueError(f'Incomplete snapshot header: {len(header)}/16')
            size=snapshot_size(header[4])
            wire_bytes_per_second=max(1.0,float(baudrate)/10.0)
            transfer_deadline=max(3.0,(size-16)/wire_bytes_per_second*1.5+1.0)
            if on_progress is not None:
                on_progress(16, size)
            remainder=read_exact_transfer(port,size-16,transfer_deadline,
                                          sink=stream,cancel_event=cancel_event,
                                          progress_callback=on_progress,progress_offset=16)
            raw=header+remainder
        info,spectrum = decode_snapshot(raw,channel)
        metadata.update(info)
        metadata['snapshot_sha256'] = hashlib.sha256(raw).hexdigest()
        metadata['native_coordinate'] = ('bin index + 0.5; one fine bin = 2^(area_shift-3) ADC-code samples'
                                         if (info.get('energy_configuration') or {}).get('mode') == 'charge'
                                         else 'bin index + 0.5; one bin is 1/8 legacy energy code')
        counts = int(spectrum.sum(dtype=np.uint64))
        metadata['centroid_channel'] = float(np.dot(np.arange(len(spectrum))+.5,spectrum.astype(float))/counts) if counts else None
        ticks = info['counters']['acquisition_ticks']
        metadata['enabled_seconds_nominal'] = ticks/50000000
        metadata['cumulative_counts_per_enabled_second'] = counts/(ticks/50000000) if ticks else 0
        np.save(directory/'spectrum.npy',spectrum)
        if calibration is not None:
            if info.get('energy_configuration') is not None and energy_identity(calibration.get('energy_configuration')) != energy_identity(info['energy_configuration']):
                raise ValueError('Calibration energy settings do not match the stored spectrum')
            actual_source=(info['source_configuration'] or {}).get('source','adc')
            if calibration.get('source','adc')!=actual_source:raise ValueError('Calibration source does not match the stored spectrum')
            if calibration.get('polarity',info['polarity'])!=info['polarity']:
                raise ValueError('Calibration polarity does not match the stored spectrum')
            rebinned = rebin_kev(spectrum,calibration,width,0,maximum)
            np.savez(directory/'calibrated_spectrum.npz',edges_kev=rebinned['edges_kev'],counts=rebinned['counts'])
            metadata['calibration'] = calibration
            metadata['energy_display'] = dict(bin_width_kev=width,maximum_kev=maximum,
                                              underflow=rebinned['underflow'],overflow=rebinned['overflow'],
                                              method='count-conserving overlap; uniform within native bins')
        metadata['status'] = 'complete'
        return metadata,spectrum
    except Exception as exc:
        metadata['error'] = str(exc)
        raise
    finally:
        (directory/'metadata.json').write_text(json.dumps(metadata,indent=2)+'\n')


def wait_ready(port):
    deadline = time.monotonic()+2
    while True:
        port.write(b'R');port.flush();response=read_exact(port,1)
        if response == b'R':return
        if response != b'B' or time.monotonic() >= deadline:
            raise RuntimeError(f'Dual DSP readiness failed: {response!r}')
        time.sleep(.001)



def channel_control_capability(port, firmware):
    """Read-only discovery of additive v5 selected-channel control."""
    if firmware.get('protocol_version', 0) < 5:
        return False
    port.write(b'H'); port.flush()
    reply = read_exact(port, 1)
    if reply == b'1':
        return True
    if reply == b'?':
        return False  # Original v5 firmware's unknown-command reply.
    raise RuntimeError(f'Invalid channel-control capability reply: {reply!r}')


def channel_acquisition_mask(port):
    port.write(b'Q'); port.flush()
    reply = read_exact(port, 1)
    if reply not in (b'0', b'1', b'2', b'3'):
        raise RuntimeError(f'Invalid channel acquisition mask: {reply!r}')
    return int(reply)


def control_channel(port, channel, start, firmware):
    """Set only one enable, and verify both enable bits. Caller owns I/O lock."""
    if type(channel) is not int or channel not in (1, 2) or type(start) is not bool:
        raise ValueError('channel must be 1/2 and start must be bool')
    if not channel_control_capability(port, firmware):
        raise NotImplementedError('Firmware lacks independent channel control')
    before = channel_acquisition_mask(port)
    command(port, b'J' if channel == 1 else b'K', b'K')
    command(port, b'A' if start else b'O', b'A' if start else b'O')
    after = channel_acquisition_mask(port)
    bit = 1 << (channel - 1)
    expected = before | bit if start else before & ~bit
    if after != expected:
        raise RuntimeError(f'Channel control readback mismatch: {after} != {expected}')
    return after
