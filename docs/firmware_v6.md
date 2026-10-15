# UART interface needed by the GUI

This is the host-facing interface for the current dual-channel MCA firmware.
All multibyte fields are little-endian. No FPGA build or programming files are
required by this GUI repository. The implementation is `src/dsw/protocol.py`;
`tests/dsw/test_energy_processing.py` covers GUI/transport/persistence behavior.

## Identity and frames

`I` returns `<4sBBBBII>`: `DMC2`, version 6, 2 channels, 14 address bits,
3 fractional bits, 16384 bins and nominal clock 50000000 Hz. GUI identity
continues to accept v5. The GUI does not support older v1–v4 devices; the shared
codec in `src/dual_mca_protocol.py` supports those only for regression fixtures.

| Layout | v5 | v6 |
|---|---:|---:|
| Coherent D bytes (`SPC2`) | 65676 | 65692 |
| Live L bytes (`SPL5` / `SPL6`) | 65748 | 65764 |
| Spectrum start offset | 136 | 152 |
| Spectrum payload | 16384 × uint32 | 16384 × uint32 |
| Energy-processing block | absent | offsets 136–151, 16 bytes |

CRC32 occupies the last four bytes and covers every preceding byte, including
v6 processing settings. A live frame includes nine uint64 end counters after
the spectrum, in addition to start counters in the header. Verify committed
accounting and the histogram sum bracket. LIVE is non-coherent; stopped coherent
D frames are authoritative. Retain exact raw bytes on save/export and preserve
partial/invalid reads for diagnosis. One native fine bin retains the Q3 energy
coordinate; do not change the existing fractional calibration convention.

## Per-channel processing readback and updates

Select CH1 with `J`, CH2 with `K`; selection acknowledges `K`.
`M` returns 16 bytes `<BBHHHII>`:

| Field | Meaning |
|---|---|
| mode uint8 | 0 charge, 1 trapezoidal |
| area_shift uint8 | Area divisor exponent, 0–15 |
| gate_samples uint16 | Total width, 1–4096 samples |
| pretrigger_samples uint16 | 0–63 and smaller than total gate |
| reserved uint16 | Must be zero |
| pole_zero_m_q12 uint32 | Immutable existing trapezoidal PZ constant |
| trapezoidal_normalizer uint32 | Immutable existing trapezoidal normalizer |

The same block is inserted into each v6 D/L frame at offset 136. Preserve this
processing identity alongside calibration and invalidate calibration when it
changes. Charge mode bypasses PZ; the stored PZ constant is not proof it is active.

`Z` is command + 8-byte `<BBHHH>` payload (the first five fields above) + CRC32.
CRC covers the one-based selected-channel byte followed by those eight payload
bytes. Responses: `K` accepted, `X` invalid/CRC/timeout, `B` busy or changed
populated measurement. Configuration requires both channels stopped; a changed
populated measurement must first be preserved and explicitly cleared. The
existing GUI worker saves both measurements/settings before its Clear path,
then applies and verifies readback. An unchanged setting can be accepted on a
stopped populated measurement. Never send Z merely to synchronize the display.

At 50 MHz, one sample is 0.02 µs. Current reset geometry is **225 total / 25 pre**:
0.5 µs pre-trigger + 4 µs post-trigger. Relative to the registered integration
trigger, the input window is samples −25 through +199 inclusive. The last sample
is +3.98 µs; the exclusive end boundary is +4 µs. This timing is not a direct
measurement of analog pulse onset. Baseline is frozen during charge integration;
signed sample area is summed and scaled by `area_shift`, which resets to 6.

## Existing controls to preserve

`H` reports independent-channel capability (`1` on the deployed image), and `Q`
returns ASCII mask `0..3` (bit0 CH1, bit1 CH2). Selected-channel `A`/`O` starts/
stops that channel; global `S`/`E` remains supported. Gate independent controls
on H capability rather than the version number alone. `?` reports aggregate run
state. D dumps do not implicitly Stop. Both channels start stopped after boot.

Initial baud is 115200; existing U negotiation supports 921600 and 2000000.
Preserve baud keepalive/recovery, CRC validation, bounded read/retry behavior,
raw-byte retention and serialized command execution in the spawned worker.
No firmware mutating command should be an incidental result of opening an
analysis window, choosing a display role, reading settings or importing a file.
