"""Exercise ADC/polarity UART operations against simulated per-channel firmware state."""
import struct
import unittest
import zlib

from protocol import Link


def simulated_link(polarities=('P', 'N'), accept_polarity=True):
    link = Link.__new__(Link)
    link.last = {1: [12], 2: [34]}
    state = dict(channel=1, polarities=dict(zip((1, 2), polarities)), commands=[],
                 sources={ch: [1, 1, 100*ch, 25000, 16770683, 0, 0, 0] for ch in (1, 2)},
                 triggers={1: [25, 1280, 512, 25, 9], 2: [30, 1024, 768, 30, 11]})

    def ack(command, expected=b'K'):
        state['commands'].append(command)
        if command in (b'J', b'K'):
            state['channel'] = 1 if command == b'J' else 2
        elif command in (b'P', b'N'):
            if accept_polarity:
                state['polarities'][state['channel']] = command.decode()
        elif command.startswith(b'X'):
            state['sources'][state['channel']][:6] = struct.unpack('<BBHIII', command[1:17])
        elif command.startswith(b'W'):
            fields, checksum = command[1:5], struct.unpack('<I', command[5:9])[0]
            if checksum != zlib.crc32(bytes([state['channel']])+fields):
                raise AssertionError('Trigger packet CRC does not cover selected channel and configuration')
            state['triggers'][state['channel']][:2] = struct.unpack('<HH', fields)

    def query(command, size=1):
        state['commands'].append(command)
        if command == b'?':
            return b'0'
        if command == b'R':
            return b'R'
        if command == b'F':
            return state['polarities'][state['channel']].encode()
        if command == b'V':
            return struct.pack('<BBHIIIQQ', *state['sources'][state['channel']])
        if command == b'G':
            return struct.pack('<HHIII', *state['triggers'][state['channel']])
        if command == b'T':
            return bytes(72)
        raise AssertionError(f'Unexpected simulated UART command: {command!r}')

    link.ack, link.query = ack, query
    return link, state


class InputSettings(unittest.TestCase):
    def test_trigger_updates_only_selected_channel_and_verifies_q8_readback(self):
        link, state = simulated_link()
        before = state['triggers'][1].copy()
        link.set_trigger(2, 45, 3.125)
        self.assertEqual(state['triggers'][1], before)
        self.assertEqual(state['triggers'][2][:2], [45, 800])
        self.assertEqual(state['commands'].count(b'C'), 1)
        self.assertIn(b'G', state['commands'])

    def test_invalid_or_running_trigger_changes_send_no_clear(self):
        for channel, floor, multiplier in ((3, 25, 5), (1, 0, 5), (1, 4096, 5), (1, 25, -1), (1, 25, 17), (1, 25, float('nan'))):
            link, state = simulated_link()
            with self.assertRaises(ValueError):
                link.set_trigger(channel, floor, multiplier)
            self.assertEqual(state['commands'], [])
        link, state = simulated_link()
        link.state = lambda: True
        with self.assertRaisesRegex(RuntimeError, 'Stop'):
            link.set_trigger(1, 25, 5)
        self.assertNotIn(b'C', state['commands'])

    def test_adc_preserves_mixed_polarities(self):
        for polarities in (('P', 'N'), ('N', 'P'), ('P', 'P'), ('N', 'N')):
            with self.subTest(polarities=polarities):
                link, state = simulated_link(polarities)
                link.adc()
                self.assertEqual(state['polarities'], dict(zip((1, 2), polarities)))
                self.assertEqual([state['sources'][ch][0] for ch in (1, 2)], [0, 0])
                self.assertNotIn(b'P', state['commands'])
                self.assertNotIn(b'N', state['commands'])
                self.assertEqual(state['commands'].count(b'C'), 1)
                self.assertEqual(link.last, {})

    def test_polarity_changes_only_selected_channel(self):
        for channel, polarity in ((1, 'N'), (2, 'P')):
            with self.subTest(channel=channel, polarity=polarity):
                link, state = simulated_link()
                before = state['polarities'].copy()
                link.set_polarity(channel, polarity)
                self.assertEqual(state['polarities'][channel], polarity)
                self.assertEqual(state['polarities'][3-channel], before[3-channel])
                self.assertEqual(state['commands'].count(b'C'), 1)
                self.assertIn(b'F', state['commands'])
                expected_select = b'J' if channel == 1 else b'K'
                write_index = state['commands'].index(polarity.encode())
                self.assertEqual(state['commands'][write_index-1], expected_select)
                self.assertEqual(state['commands'][write_index+1], b'F')

    def test_adc_accepts_independent_polarity_map(self):
        link, state = simulated_link()
        link.adc({1: 'negative', 2: 'positive'})
        self.assertEqual(state['polarities'], {1: 'N', 2: 'P'})
        self.assertEqual([state['sources'][ch][0] for ch in (1, 2)], [0, 0])
        self.assertEqual(state['commands'].count(b'C'), 1)

    def test_adc_partial_map_preserves_other_channel(self):
        link, state = simulated_link()
        link.adc({1: 'N'})
        self.assertEqual(state['polarities'], {1: 'N', 2: 'N'})
        self.assertNotIn(b'P', state['commands'])
        self.assertEqual(state['commands'].count(b'N'), 1)

    def test_polarity_readback_mismatch_is_rejected(self):
        link, state = simulated_link(accept_polarity=False)
        with self.assertRaisesRegex(RuntimeError, 'polarity readback mismatch'):
            link.set_polarity(1, 'N')
        self.assertEqual(state['polarities'], {1: 'P', 2: 'N'})

    def test_invalid_input_does_not_clear_or_send_commands(self):
        for channel, polarity in ((0, 'P'), (3, 'N'), (1, 'invalid'), (2, None)):
            with self.subTest(channel=channel, polarity=polarity):
                link, state = simulated_link()
                with self.assertRaises(ValueError):
                    link.set_polarity(channel, polarity)
                self.assertEqual(state['commands'], [])
        for polarities in ('invalid', {3: 'N'}, {1: 'unknown'}):
            with self.subTest(polarities=polarities):
                link, state = simulated_link()
                with self.assertRaises(ValueError):
                    link.adc(polarities)
                self.assertEqual(state['commands'], [])


if __name__ == '__main__':
    unittest.main()
