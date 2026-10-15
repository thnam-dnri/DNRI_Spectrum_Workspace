import json
import tempfile
import unittest
from pathlib import Path

import numpy as np

from spectrum_io import (measurement, read_spectrum, read_spectra, write_spectrum, frame_record, restore_frame,
                         write_session, read_session, validate_counts)


class SpectrumIO(unittest.TestCase):
    def test_popular_format_round_trips_native_counts_calibration_and_time(self):
        frame = measurement(np.arange(256, dtype=np.uint32)**2, (12.5, .25), title='test sample', live_time=90.25, real_time=100.5)
        with tempfile.TemporaryDirectory() as directory:
            for extension in ('.spe', '.chn', '.n42', '.csv', '.json'):
                with self.subTest(extension=extension):
                    filename = Path(directory)/f'spectrum{extension}'
                    write_spectrum(filename, frame, frame['calibration'])
                    loaded = read_spectrum(filename)
                    np.testing.assert_array_equal(loaded['counts'], frame['counts'])
                    np.testing.assert_allclose(loaded['calibration'], frame['calibration'], atol=1e-6)
                    if extension != '.csv':
                        self.assertAlmostEqual(loaded['live_time'], frame['live_time'], delta=.011)
                        self.assertAlmostEqual(loaded['real_time'], frame['real_time'], delta=.011)

    def test_independent_iaea_spe_fixture_and_channel_offset(self):
        text = '$SPEC_ID:\nReference\n$MEAS_TIM:\n12.5 15\n$DATA:\n10 13\n1\n2 3\n4\n$ENER_FIT:\n2 0.5\n$MCA_CAL:\n3\n2 0.5 0 keV\n$ENDRECORD:\n'
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory)/'independent.SPE'
            filename.write_text(text)
            loaded = read_spectrum(filename)
        np.testing.assert_array_equal(loaded['counts'], [1, 2, 3, 4])
        self.assertEqual(loaded['calibration'], [7., .5])
        self.assertEqual(loaded['live_time'], 12.5)

    def test_spe_preserves_rois_notes_and_uncalibrated_flag(self):
        frame = measurement([0, 12, 3, 0], (0, 1), calibrated=False)
        rois = [dict(name='Reference line', bounds=[1., 3.], method='gaussian', smoothing=1)]
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory)/'example.spe'
            write_spectrum(filename, frame, frame['calibration'], False, rois, {'sample': 'Cs reference'})
            loaded = read_spectrum(filename)
        self.assertEqual(loaded['rois'], rois)
        self.assertFalse(loaded['calibrated'])
        self.assertEqual(loaded['sample_metadata']['sample'], 'Cs reference')

    def test_truncated_and_nonlinear_spe_rejected(self):
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory)/'bad.spe'
            for text in ('$DATA:\n0 3\n1 2\n', '$DATA:\n0 3\n1 2 3 4\n$ENER_FIT:\n0 1 0.001\n'):
                filename.write_text(text)
                with self.assertRaises(ValueError):
                    read_spectrum(filename)

    def test_n42_counted_zeroes(self):
        xml = '<RadInstrumentData><EnergyCalibration id="e"><CoefficientValues>1 0.5</CoefficientValues></EnergyCalibration><RadMeasurement><RealTimeDuration>PT1M30S</RealTimeDuration><Spectrum energyCalibrationReference="e"><LiveTimeDuration>PT80S</LiveTimeDuration><ChannelData compressionCode="CountedZeroes">1 0 3 2</ChannelData></Spectrum></RadMeasurement></RadInstrumentData>'
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory)/'reference.n42'
            filename.write_text(xml)
            loaded = read_spectrum(filename)
        np.testing.assert_array_equal(loaded['counts'], [1, 0, 0, 0, 2])
        self.assertEqual(loaded['real_time'], 90.)

    def test_n42_exposes_multiple_measurements_for_selection(self):
        xml = '<RadInstrumentData><EnergyCalibration id="e"><CoefficientValues>0 1</CoefficientValues></EnergyCalibration>'
        for name, counts in (('background', '1 2 3'), ('sample', '10 20 30')):
            xml += f'<RadMeasurement><MeasurementDescription>{name}</MeasurementDescription><RealTimeDuration>PT10S</RealTimeDuration><Spectrum energyCalibrationReference="e"><ChannelData>{counts}</ChannelData></Spectrum></RadMeasurement>'
        xml += '</RadInstrumentData>'
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory)/'multiple.n42'
            filename.write_text(xml)
            frames = read_spectra(filename)
        self.assertEqual(len(frames), 2)
        self.assertEqual(frames[1]['title'], 'sample')
        np.testing.assert_array_equal(frames[1]['counts'], [10, 20, 30])

    def test_large_counts_are_exact_and_chn_overflow_rejected(self):
        frame = measurement([2**32+9, 2**53+1], (0, 1))
        with tempfile.TemporaryDirectory() as directory:
            for extension in ('.spe', '.json', '.n42'):
                filename = Path(directory)/f'large{extension}'
                write_spectrum(filename, frame, frame['calibration'])
                np.testing.assert_array_equal(read_spectrum(filename)['counts'], frame['counts'])
            with self.assertRaises(ValueError):
                write_spectrum(Path(directory)/'overflow.chn', frame, frame['calibration'])

    def test_session_round_trip_includes_native_counts_and_settings(self):
        frame = measurement([1, 2, 3, 4], (2, .5), live_time=12, real_time=13)
        entry = dict(frame=frame_record(frame), calibration=[2, .5], calibrated=True, grouping=1,
                     rois=[dict(name='peak', bounds=[2., 4.])], device_settings={'polarity': 'N'})
        payload = dict(sample_metadata={'notes': 'reference'}, channels={'1': entry})
        with tempfile.TemporaryDirectory() as directory:
            filename = Path(directory)/'session.dsw'
            write_session(filename, payload)
            loaded = read_session(filename)
        np.testing.assert_array_equal(restore_frame(loaded['channels']['1']['frame'])['counts'], frame['counts'])
        self.assertEqual(loaded['channels']['1']['device_settings'], {'polarity': 'N'})

    def test_bad_counts_rejected_without_uint64_wrap_or_rounding(self):
        for values in ([-1], [float('nan')], [1.5], [2**64], [2**64-1, 1]):
            with self.subTest(values=values):
                with self.assertRaises(ValueError):
                    validate_counts(values)


if __name__ == '__main__':
    unittest.main()
