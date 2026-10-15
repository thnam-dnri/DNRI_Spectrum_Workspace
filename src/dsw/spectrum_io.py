"""Lossless DSW sessions and common linear-calibration spectrum formats.

SPE/CHN conventions follow Sandia SpecUtils' public format readers/writers.
Calibration coefficients map native channel edges to energy, as in DSW.
"""
import base64
import csv
import io
import json
import math
import os
import re
import struct
import tempfile
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
import xml.etree.ElementTree as ET

import numpy as np

from protocol import STATS, decode

OPEN_FILTER = 'Spectrum / session (*.dsw *.spe *.chn *.n42 *.xml *.csv *.json *.bin *.npy);;All files (*)'
SAVE_FILTER = 'IAEA ASCII spectrum (*.spe);;ORTEC spectrum (*.chn);;N42 spectrum (*.n42);;Native spectrum CSV (*.csv);;DSW spectrum JSON (*.json)'
MAX_BINS = 262144


def validate_counts(values):
    array = np.asarray(values)
    if array.ndim != 1 or not 0 < len(array) <= MAX_BINS:
        raise ValueError(f'Spectrum must contain 1–{MAX_BINS:,} bins')
    converted = []
    for value in array:
        try:
            number = Decimal(str(value))
        except InvalidOperation as exc:
            raise ValueError('Invalid spectrum count') from exc
        if not number.is_finite() or number < 0 or number != number.to_integral_value() or number > 2**64-1:
            raise ValueError('Raw spectrum counts must be nonnegative uint64 integers')
        converted.append(int(number))
    if sum(converted) > 2**64-1:
        raise ValueError('Spectrum total exceeds uint64 capacity')
    return np.asarray(converted, dtype=np.uint64)


def linear_calibration(coefficients, count, first=0):
    if not 0 < count <= MAX_BINS:
        raise ValueError('Invalid spectrum bin count')
    if coefficients is None:
        return [0., 4000/count], False
    coefficients = [float(value) for value in coefficients]
    if len(coefficients) < 2 or not np.isfinite(coefficients).all():
        raise ValueError('Invalid energy calibration')
    if any(abs(value) > 1e-12 for value in coefficients[2:]):
        raise ValueError('This spectrum has nonlinear calibration; DSW currently supports linear calibration')
    offset, gain = coefficients[:2]
    if gain <= 0:
        raise ValueError('Energy calibration gain must be positive')
    return [offset + first*gain, gain], True


def finite_time(value):
    value = float(value)
    if not math.isfinite(value) or value < 0:
        raise ValueError('Measurement time must be nonnegative and finite')
    return value


def measurement(counts, calibration=None, calibrated=None, title='', live_time=0., real_time=0., **metadata):
    counts = validate_counts(counts)
    calibration, inferred = linear_calibration(calibration, len(counts))
    live_time, real_time = finite_time(live_time), finite_time(real_time)
    total = int(counts.sum(dtype=np.uint64))
    stats = dict.fromkeys(STATS, 0)
    stats.update(histogrammed=total, dpp_valid=total, acquisition_ticks=round(real_time*50000000))
    result = dict(channel=1, live=False, sequence=0, counts=counts, total=total, stats=stats,
                  start_stats=stats.copy(), loaded=True, title=title, live_time=live_time,
                  real_time=real_time, calibration=calibration,
                  calibrated=inferred if calibrated is None else bool(calibrated))
    result.update(metadata)
    return result


def duration(frame):
    """Available exposure; this does not invent detector-corrected live time."""
    live = float(frame.get('live_time', 0.))
    real = float(frame.get('real_time', frame.get('stats', {}).get('acquisition_ticks', 0)/50000000))
    return live if live > 0 else real


def _atomic_write(filename, data):
    filename = Path(filename)
    filename.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(dir=filename.parent, prefix=f'.{filename.name}-', delete=False) as handle:
            temporary = Path(handle.name)
            handle.write(data.encode('utf-8') if isinstance(data, str) else data)
        os.replace(temporary, filename)
    finally:
        if temporary is not None and temporary.exists():
            temporary.unlink()


def frame_record(frame):
    record = {key: value for key, value in frame.items() if key not in ('raw', 'counts')}
    record['counts'] = validate_counts(frame['counts']).tolist()
    record['total'] = sum(record['counts'])
    if frame.get('raw'):
        record['raw_base64'] = base64.b64encode(frame['raw']).decode('ascii')
    return record


def restore_frame(record):
    record = dict(record)
    counts = validate_counts(record.pop('counts'))
    raw = record.pop('raw_base64', None)
    if raw:
        raw = base64.b64decode(raw, validate=True)
        decoded = decode(raw, int(record.get('original_channel', record.get('channel', 1))), bool(record.get('live', False)))
        if not np.array_equal(counts, decoded['counts']):
            raise ValueError('Session counts do not match retained FPGA frame')
        if record.get('energy') is not None and record['energy'] != decoded.get('energy'):
            raise ValueError('Stored energy settings do not match retained FPGA frame')
        record['energy'] = decoded.get('energy')
        record['protocol_version'] = decoded['protocol_version']
        record['raw'] = raw
    record['counts'] = counts
    record['loaded'] = True
    record.setdefault('live', False)
    record.setdefault('sequence', 0)
    record.setdefault('channel', 1)
    total = int(counts.sum(dtype=np.uint64))
    if record.get('total', total) != total:
        raise ValueError('Stored spectrum total does not match counts')
    record['total'] = total
    record['calibration'], inferred = linear_calibration(record.get('calibration'), len(counts))
    record.setdefault('calibrated', inferred)
    defaults = dict.fromkeys(STATS, 0)
    defaults.update(histogrammed=total, dpp_valid=total)
    record['stats'] = {**defaults, **record.get('stats', {})}
    if any(not isinstance(value, (int, float)) or not math.isfinite(value) or value < 0 for value in record['stats'].values()):
        raise ValueError('Invalid saved acquisition statistics')
    for key in ('live_time', 'real_time'):
        if key in record:
            record[key] = finite_time(record[key])
    return record


def validate_rois(rois):
    if not isinstance(rois, list) or len(rois) > 64:
        raise ValueError('Use at most 64 ROIs per channel')
    for roi in rois:
        low, high = roi.get('bounds', (None, None))
        if low is None or high is None or not np.isfinite([low, high]).all() or low >= high:
            raise ValueError('Invalid saved ROI bounds')
        if roi.get('method', 'endpoint') not in ('endpoint', 'gaussian') or roi.get('smoothing', 1) not in (1, 3, 5, 9):
            raise ValueError('Invalid saved ROI analysis settings')
        if roi.get('color') and not re.fullmatch(r'#[0-9a-fA-F]{6}', roi['color']):
            raise ValueError('Invalid saved ROI color')


def write_session(filename, payload):
    _atomic_write(filename, json.dumps(dict(format='DSW session', version=1, **payload), indent=2, allow_nan=False))


def read_session(filename):
    payload = json.loads(Path(filename).read_text(encoding='utf-8'))
    if payload.get('format') != 'DSW session' or payload.get('version') != 1:
        raise ValueError('Unsupported DSW session version')
    if not isinstance(payload.get('channels'), dict) or not payload['channels']:
        raise ValueError('Session has no spectra')
    acquisition = payload.get('acquisition', {})
    if not isinstance(acquisition, dict) or not isinstance(acquisition.get('control_events', []), list) or any(not isinstance(event, dict) for event in acquisition.get('control_events', [])):
        raise ValueError('Invalid saved acquisition metadata')
    states = acquisition.get('channel_acquiring')
    if states is not None and (not isinstance(states, dict) or set(states) != {'1', '2'} or any(type(value) is not bool for value in states.values())):
        raise ValueError('Invalid saved channel acquisition states')
    if 'view' in payload:
        view = payload['view']
        if not isinstance(view, dict) or view.get('foreground') not in (None, 1, 2) or view.get('background') not in (None, 1, 2, 'background', 'reference'):
            raise ValueError('Invalid saved foreground/background selection')
        foreground, background = view.get('foreground'), view.get('background')
        if background is not None and (foreground is None or foreground == background):
            raise ValueError('Background needs a different selected foreground channel')
    for channel, entry in payload['channels'].items():
        if channel not in ('1', '2'):
            raise ValueError('Session channel must be 1 or 2')
        restore_frame(entry['frame'])
        linear_calibration(entry['calibration'], len(entry['frame']['counts']))
        if entry.get('grouping', 8) not in (1, 8, 16, 24, 32, 40):
            raise ValueError('Invalid saved display grouping')
        validate_rois(entry.get('rois', []))
        if entry.get('baseline') is not None:
            baseline = validate_counts(entry['baseline'])
            foreground = validate_counts(entry['frame']['counts'])
            if len(baseline) != len(foreground) or np.any(baseline > foreground):
                raise ValueError('Saved baseline is incompatible with spectrum')
        for roi in entry.get('rois', []):
            low, high = roi['bounds']
            if not np.isfinite([low, high]).all() or not 0 <= low < high <= 4000:
                raise ValueError('Invalid saved ROI bounds')
        for key in ('reference', 'background'):
            if entry.get(key):
                reference = restore_frame(entry[key])
                linear_calibration(reference.get('calibration'), len(reference['counts']))
    return payload


def _spe(filename):
    try:
        text = Path(filename).read_bytes().decode('utf-8-sig')
    except UnicodeDecodeError as exc:
        raise ValueError('Only IAEA/ORTEC ASCII .spe is supported; this appears to be a binary/vendor SPE file') from exc
    sections, current = {}, None
    for line in text.splitlines():
        line = line.strip()
        if line.startswith('$'):
            current = line.split(':', 1)[0].upper()
            if current in sections and current == '$DATA':
                raise ValueError('Multi-record SPE files must be split into individual spectra')
            sections[current] = []
            remainder = line.partition(':')[2].strip()
            if remainder:
                sections[current].append(remainder)
        elif current and line:
            sections[current].append(line)
    data = sections.get('$DATA', [])
    if len(data) < 2:
        raise ValueError('SPE is missing a complete $DATA section')
    limits = data[0].split()
    if len(limits) != 2:
        raise ValueError('SPE $DATA must start with first and last channel')
    first, last = map(int, limits)
    tokens = ' '.join(data[1:]).split()
    if first < 0 or last < first or len(tokens) != last-first+1:
        raise ValueError('SPE channel range does not match the number of counts')
    calibration = None
    if '$MCA_CAL' in sections:
        rows = sections['$MCA_CAL']
        count = int(rows[0])
        calibration = re.sub(r'\bkev\b', '', ' '.join(rows[1:]), flags=re.IGNORECASE).split()
        if len(calibration) != count:
            raise ValueError('SPE calibration coefficient count mismatch')
    elif '$ENER_FIT' in sections:
        calibration = ' '.join(sections['$ENER_FIT']).split()
    calibration, calibrated = linear_calibration(calibration, len(tokens), first)
    times = ' '.join(sections.get('$MEAS_TIM', ['0 0'])).split()
    if len(times) != 2:
        raise ValueError('SPE $MEAS_TIM requires live and real time')
    extras = json.loads('\n'.join(sections.get('$DSW_METADATA', ['{}'])))
    rois = []
    if '$ROI' in sections:
        for row in sections['$ROI'][1:]:
            low, high = map(int, row.split()[:2])
            rois.append(dict(name=f'ROI {len(rois)+1}', bounds=[calibration[0]+calibration[1]*(low-first),
                                                              calibration[0]+calibration[1]*(high-first+1)],
                             method='endpoint', smoothing=1))
    return measurement(tokens, calibration, extras.get('calibrated', calibrated),
                       title='\n'.join(sections.get('$SPEC_ID', [Path(filename).stem])),
                       live_time=times[0], real_time=times[1],
                       acquisition_date=' '.join(sections.get('$DATE_MEA', [])),
                       rois=extras.get('rois', rois), sample_metadata=extras.get('sample_metadata', {}),
                       time_basis=extras.get('time_basis', 'file live/real time'))


def _chn(filename):
    data = Path(filename).read_bytes()
    if len(data) < 32:
        raise ValueError('CHN header is truncated')
    identity, detector, segment, seconds, real, live, date, clock, first, count = struct.unpack('<hhh2sII8s4sHH', data[:32])
    if identity != -1:
        raise ValueError('Not an ORTEC CHN spectrum')
    if count == 0:
        count = (len(data)-32-512)//4
    if not 0 < count <= 65535 or len(data) < 32+4*count:
        raise ValueError('CHN channel data is truncated or invalid')
    counts = np.frombuffer(data[32:32+4*count], dtype='<u4').copy()
    trailer = data[32+4*count:]
    coefficients = None
    if len(trailer) >= 16:
        kind = struct.unpack_from('<h', trailer)[0]
        coefficients = struct.unpack_from('<3f' if kind == -102 else '<2f', trailer, 4)
        if not any(coefficients):
            coefficients = None
    calibration, calibrated = linear_calibration(coefficients, len(counts), first)
    title = Path(filename).stem
    if len(trailer) >= 384:
        length = min(trailer[320], 63)
        title = trailer[321:321+length].decode('latin-1').strip() or title
    acquired = ''
    try:
        century = 2000 if date[7:8] != b'0' else 1900
        timestamp = date[:5].decode() + str(century+int(date[5:7])) + clock.decode() + seconds.decode()
        acquired = datetime.strptime(timestamp, '%d%b%Y%H%M%S').isoformat()
    except (ValueError, UnicodeDecodeError):
        pass
    return measurement(counts, calibration, calibrated, title=title, live_time=live/50., real_time=real/50.,
                       acquisition_date=acquired, detector_number=detector)


def _local(node):
    return node.tag.rsplit('}', 1)[-1]


def _child_text(node, name, default=''):
    return next((child.text or default for child in node.iter() if _local(child) == name), default)


def _seconds(value):
    if not value:
        return 0.
    match = re.fullmatch(r'P(?:(\d+(?:\.\d+)?)D)?T(?:(\d+(?:\.\d+)?)H)?(?:(\d+(?:\.\d+)?)M)?(?:(\d+(?:\.\d+)?)S)?', value.strip())
    if not match:
        raise ValueError('Unsupported N42 duration (expected PT…H…M…S)')
    return sum(float(value or 0)*scale for value, scale in zip(match.groups(), (86400, 3600, 60, 1)))


def _n42(filename):
    data = Path(filename).read_bytes()
    if b'<!DOCTYPE' in data.upper():
        raise ValueError('N42 documents with DTD declarations are unsupported')
    try:
        root = ET.fromstring(data)
    except ET.ParseError as exc:
        raise ValueError(f'Malformed N42 XML: {exc}') from exc
    calibrations, measurements = {}, []
    detectors = {node.get('id'): _child_text(node, 'RadDetectorCategoryCode') for node in root.iter() if _local(node) == 'RadDetectorInformation'}
    for node in root.iter():
        if _local(node) == 'EnergyCalibration':
            coefficients = _child_text(node, 'CoefficientValues')
            if coefficients:
                calibrations[node.get('id', '')] = coefficients.split()
    for container in root.iter():
        if _local(container) not in ('RadMeasurement', 'Measurement'):
            continue
        extras = {}
        for remark in container.iter():
            if _local(remark) == 'Remark' and (remark.text or '').startswith('DSW_METADATA:'):
                extras = json.loads(remark.text.partition(':')[2])
                validate_rois(extras.get('rois', []))
        for spectrum in container.iter():
            if _local(spectrum) != 'Spectrum':
                continue
            if detectors.get(spectrum.get('radDetectorInformationReference'), '').lower() == 'neutron':
                continue
            text = _child_text(spectrum, 'ChannelData')
            if not text:
                continue
            node = next(child for child in spectrum.iter() if _local(child) == 'ChannelData')
            compression = node.get('compressionCode', node.get('Compression', 'None'))
            tokens = text.split()
            if compression.lower() == 'countedzeroes':
                expanded, index = [], 0
                while index < len(tokens):
                    value = int(tokens[index])
                    index += 1
                    if value == 0:
                        if index >= len(tokens):
                            raise ValueError('Truncated N42 zero run')
                        run = int(tokens[index]); index += 1
                        if run < 0 or len(expanded)+run > MAX_BINS:
                            raise ValueError('Invalid N42 zero run')
                        expanded.extend([0]*run)
                    else:
                        expanded.append(value)
                tokens = expanded
            elif compression.lower() not in ('none', ''):
                raise ValueError(f'Unsupported N42 channel compression: {compression}')
            reference = spectrum.get('energyCalibrationReference', '')
            coefficients = calibrations.get(reference)
            if reference and coefficients is None:
                raise ValueError(f'N42 energy calibration reference is missing: {reference}')
            if coefficients is None:
                coefficients = _child_text(spectrum, 'CoefficientValues', _child_text(spectrum, 'Coefficients')).split() or None
            if coefficients is None and len(calibrations) == 1:
                coefficients = next(iter(calibrations.values()))
            calibration, calibrated = linear_calibration(coefficients, len(tokens))
            measurements.append(measurement(tokens, calibration, extras.get('calibrated', calibrated),
                               title=_child_text(container, 'MeasurementDescription', Path(filename).stem),
                               live_time=_seconds(_child_text(spectrum, 'LiveTimeDuration', _child_text(spectrum, 'LiveTime'))),
                               real_time=_seconds(_child_text(container, 'RealTimeDuration', _child_text(container, 'RealTime'))),
                               acquisition_date=_child_text(container, 'StartDateTime'),
                               measurement_class=_child_text(container, 'MeasurementClassCode'), detector_id=spectrum.get('radDetectorInformationReference', ''),
                               rois=extras.get('rois', []), sample_metadata=extras.get('sample_metadata', {}),
                               time_basis=extras.get('time_basis', 'file live/real time')))
    if not measurements:
        raise ValueError('N42 contains no readable gamma spectrum')
    return measurements


def read_spectra(filename):
    """Return all N42 gamma spectra so the GUI can explicitly select a measurement."""
    if Path(filename).suffix.lower() not in ('.n42', '.xml'):
        return [read_spectrum(filename)]
    frames = _n42(filename)
    for index, frame in enumerate(frames):
        frame.update(path=str(filename), loaded=True, measurement_index=index, measurement_count=len(frames))
    return frames


def read_spectrum(filename):
    filename = Path(filename)
    suffix = filename.suffix.lower()
    if suffix == '.spe':
        frame = _spe(filename)
    elif suffix == '.chn':
        frame = _chn(filename)
    elif suffix in ('.n42', '.xml'):
        frame = read_spectra(filename)[0]
    elif suffix == '.bin':
        raw = filename.read_bytes()
        if len(raw) < 6:
            raise ValueError('Truncated FPGA frame')
        frame = decode(raw, raw[5], raw[:4] in (b'SPL5', b'SPL6'))
        frame.update(calibration=[0., 4000/len(frame['counts'])], calibrated=False, loaded=True)
    elif suffix == '.npy':
        frame = measurement(np.load(filename, allow_pickle=False))
    elif suffix == '.csv':
        with filename.open(newline='', encoding='utf-8-sig') as handle:
            rows = list(csv.DictReader(handle))
        if not rows or 'counts' not in rows[0]:
            raise ValueError('CSV requires a counts column')
        counts = [row['counts'] for row in rows]
        calibration = None
        if 'energy_low_keV' in rows[0] and 'energy_high_keV' in rows[0]:
            lows = np.array([float(row['energy_low_keV']) for row in rows])
            highs = np.array([float(row['energy_high_keV']) for row in rows])
            gain = highs[0]-lows[0]
            if gain <= 0 or not np.allclose(lows[1:], highs[:-1]) or not np.allclose(highs-lows, gain):
                raise ValueError('CSV has nonuniform energy bins; open its companion JSON/.bin for native data')
            calibration = [lows[0], gain]
        frame = measurement(counts, calibration)
    elif suffix == '.json':
        record = json.loads(filename.read_text(encoding='utf-8'))
        if record.get('format') == 'DSW spectrum':
            frame = restore_frame(record['frame'])
        elif 'counts' in record:
            frame = measurement(record['counts'], record.get('calibration'), record.get('calibrated'),
                                live_time=record.get('live_time', 0), real_time=record.get('real_time', 0))
        elif filename.with_suffix('.bin').exists():
            frame = read_spectrum(filename.with_suffix('.bin'))
            frame.update(record)
            frame['rois'] = record.get('roi_analyses', [])
            if not frame['rois'] and record.get('roi_analysis'):
                result = record['roi_analysis']
                frame['rois'] = [dict(name='Peak 1', bounds=result['bounds_keV'],
                                     method='gaussian' if 'Gaussian' in result['method'] else 'endpoint',
                                     smoothing=result.get('smoothing_native_bins', 1))]
            else:
                frame['rois'] = [dict(name=roi.get('name', f'Peak {index+1}'), bounds=roi['bounds_keV'],
                                     method='gaussian' if 'Gaussian' in roi['method'] else 'endpoint',
                                     smoothing=roi.get('smoothing_native_bins', 1)) for index, roi in enumerate(frame['rois'])]
        else:
            raise ValueError('Spectrum JSON has no counts or companion .bin')
    else:
        raise ValueError(f'Unsupported spectrum extension: {suffix}')
    frame['path'] = str(filename)
    frame['loaded'] = True
    frame.setdefault('title', filename.stem)
    if 'calibration' not in frame:
        frame['calibration'], frame['calibrated'] = linear_calibration(None, len(frame['counts']))
    else:
        frame['calibration'], _ = linear_calibration(frame['calibration'], len(frame['counts']))
    validate_rois(frame.get('rois', []))
    return frame


def write_spectrum(filename, frame, calibration, calibrated=True, rois=None, sample_metadata=None):
    filename = Path(filename)
    counts = validate_counts(frame['counts'])
    calibration, _ = linear_calibration(calibration, len(counts))
    offset, gain = calibration
    real = finite_time(frame.get('real_time', frame.get('stats', {}).get('acquisition_ticks', 0)/50000000))
    live = finite_time(frame.get('live_time', 0))
    extras = dict(calibrated=bool(calibrated), rois=rois or [], sample_metadata=sample_metadata or {},
                  time_basis=frame.get('time_basis', 'detector live/real time recorded' if live > 0 else 'real/acquisition-enabled time; corrected live time unavailable'))
    title = frame.get('title') or (sample_metadata or {}).get('sample') or f'DSW channel {frame.get("channel", 1)}'
    acquired = None
    if frame.get('acquisition_date'):
        text = frame['acquisition_date']
        for parse in (lambda: datetime.fromisoformat(text.replace('Z', '+00:00')), lambda: datetime.strptime(text, '%m/%d/%Y %H:%M:%S')):
            try:
                acquired = parse()
                break
            except ValueError:
                pass
    extras['exported_at'] = datetime.now().astimezone().isoformat()
    suffix = filename.suffix.lower()
    if suffix == '.spe':
        lines = ['$SPEC_ID:', title.replace('\n', ' '), '$SPEC_REM:', extras['time_basis'],
                 *(['$DATE_MEA:', acquired.strftime('%m/%d/%Y %H:%M:%S')] if acquired else []), '$MEAS_TIM:', f'{live:.9g} {real:.9g}',
                 '$DATA:', f'0 {len(counts)-1}', *(str(int(value)) for value in counts),
                 '$ENER_FIT:', f'{offset:.17g} {gain:.17g}', '$MCA_CAL:', '3', f'{offset:.17g} {gain:.17g} 0 keV',
                 '$DSW_METADATA:', json.dumps(extras, allow_nan=False), '$ENDRECORD:']
        _atomic_write(filename, '\n'.join(lines)+'\n')
    elif suffix == '.chn':
        if len(counts) > 65535 or counts.max() > np.iinfo(np.uint32).max or max(real, live)*50 > 2**32-1:
            raise ValueError('CHN exceeds its channel/count/time limits; use SPE, N42 or DSW JSON')
        date = (acquired.strftime('%d%b%y')+('1' if acquired.year >= 2000 else '0')) if acquired else '00   000'
        header = struct.pack('<hhh2sII8s4sHH', -1, int(frame.get('channel', 1)), 1, acquired.strftime('%S').encode() if acquired else b'00',
                             round(real*50), round(live*50), date.encode(), acquired.strftime('%H%M').encode() if acquired else b'0000', 0, len(counts))
        trailer = bytearray(512)
        struct.pack_into('<hh3f', trailer, 0, -102, 0, offset, gain, 0.)
        for position, text in ((256, (sample_metadata or {}).get('detector', 'DSW')), (320, title)):
            encoded = text.encode('latin-1', errors='replace')[:63]
            trailer[position] = len(encoded)
            trailer[position+1:position+1+len(encoded)] = encoded
        _atomic_write(filename, header+counts.astype('<u4').tobytes()+trailer)
    elif suffix in ('.n42', '.xml'):
        namespace = 'http://physics.nist.gov/N42/2011/N42'
        ET.register_namespace('', namespace)
        tag = lambda name: f'{{{namespace}}}{name}'
        root = ET.Element(tag('RadInstrumentData'))
        instrument = ET.SubElement(root, tag('RadInstrumentInformation'), id='dsw')
        ET.SubElement(instrument, tag('RadInstrumentManufacturerName')).text = 'DNRI'
        ET.SubElement(instrument, tag('RadInstrumentModelName')).text = 'DSW'
        detector = ET.SubElement(root, tag('RadDetectorInformation'), id='detector')
        ET.SubElement(detector, tag('RadDetectorCategoryCode')).text = 'Gamma'
        energy = ET.SubElement(root, tag('EnergyCalibration'), id='calibration')
        ET.SubElement(energy, tag('CoefficientValues')).text = f'{offset:.17g} {gain:.17g}'
        node = ET.SubElement(root, tag('RadMeasurement'), id='measurement')
        ET.SubElement(node, tag('MeasurementClassCode')).text = 'Foreground'
        if acquired:
            ET.SubElement(node, tag('StartDateTime')).text = acquired.isoformat()
        ET.SubElement(node, tag('RealTimeDuration')).text = f'PT{real:.9g}S'
        ET.SubElement(node, tag('MeasurementDescription')).text = title
        ET.SubElement(node, tag('Remark')).text = extras['time_basis']
        ET.SubElement(node, tag('Remark')).text = 'DSW_METADATA:'+json.dumps(extras, allow_nan=False)
        spectrum = ET.SubElement(node, tag('Spectrum'), id='spectrum', radDetectorInformationReference='detector', energyCalibrationReference='calibration')
        ET.SubElement(spectrum, tag('LiveTimeDuration')).text = f'PT{live:.9g}S'
        ET.SubElement(spectrum, tag('ChannelData'), compressionCode='None').text = ' '.join(str(int(value)) for value in counts)
        ET.indent(root)
        _atomic_write(filename, ET.tostring(root, encoding='utf-8', xml_declaration=True))
    elif suffix == '.csv':
        output = io.StringIO(newline='')
        writer = csv.writer(output)
        writer.writerow(['energy_low_keV', 'energy_high_keV', 'counts'])
        writer.writerows((offset+gain*i, offset+gain*(i+1), int(value)) for i, value in enumerate(counts))
        _atomic_write(filename, output.getvalue())
    elif suffix == '.json':
        record = dict(frame, calibration=calibration, calibrated=calibrated, rois=rois or [], sample_metadata=sample_metadata or {})
        _atomic_write(filename, json.dumps(dict(format='DSW spectrum', version=1, frame=frame_record(record)), indent=2, allow_nan=False))
    else:
        raise ValueError('Choose .spe, .chn, .n42, .csv or .json for spectrum export')
