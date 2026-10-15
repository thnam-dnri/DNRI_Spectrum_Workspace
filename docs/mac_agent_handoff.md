# Mac agent update handoff — 2026-10-15

## Target and current status

Update the macOS DSW GUI to match the Linux GUI source in this repository.
The FPGA boot image is UART **v6**, verified on Linux after persistent flash
programming on 2026-10-15. Both channels reset to charge integration, PZ off,
**0.50 µs pre-trigger + 4.00 µs post-trigger = 4.50 µs total**. This is 225 total
samples with 25 pre-trigger samples at 50 MHz; area shift is 6 (divisor 64).
Named NaI/CsI presets remain optional and have different gate values.

This Git repository was empty before this initial source publication. There is
no Mac branch here to compare against. Compare your existing Mac working tree
with this snapshot, preserving any Mac-specific packaging/UI changes you need.
All eleven `src/dsw/*.py` runtime modules match the current Linux source; use
the complete set so transport, UI, persistence and calibration agree.

## Changes the older Mac GUI needs

| File | Required behavior |
|---|---|
| `src/dsw/protocol.py` | Accept v5/v6 identity; decode v6 D/L sizes and processing metadata; read M, encode Z with channel-bound CRC; retain CRC/accounting and receive-deadline checks. |
| `src/dsw/app.py` | Device → Channel1/2: Energy processing; mode/presets plus separate pre/post controls; read values from device and show computed total. Enable only for v6 with both channels stopped and settings available. |
| `src/dsw/uart_worker.py` | Execute serial work in the existing spawned subprocess; preserve measurements before settings/Clear; read settings back after application. |
| `src/dsw/spectrum_io.py` | Retain exact v6 raw frames and energy metadata in DSW sessions; reject inconsistent restored metadata. |
| `src/dsw/workspace.py` | Bind calibration to energy-processing identity, invalidate on changed mode/gate/scale, and preserve old measurements before mutation. |
| Other `src/dsw` modules | Keep spectrum display, calibration, ROI, comparison and acquisition behavior aligned with the Linux baseline. |

The older v5 frame offsets and lengths will fail on v6. Do not work around that
by dropping CRC or accounting checks. Details are in [firmware_v6.md](firmware_v6.md).
The old firmware constants for trapezoidal PZ/normalization remain in v6 settings
readback even when charge mode has PZ disabled; use `pole_zero_applied`/mode.

At the default settings, the energy dialog must show Pre-trigger 0.50, Post-trigger
4.00 and Total 4.50 µs (225 samples). Values use 0.02 µs steps. Pre-trigger is
0–1.26 µs, post-trigger is at least 0.02 µs, and total is at most 81.92 µs.
Changing the mode, gate or area divisor requires fresh energy calibration.

## Mac update sequence

1. Save/commit your existing Mac source and packaging changes, then fetch this
   repository. Compare the runtime modules above with your current checkout.
2. Create a native Python environment with `bash scripts/setup_dsw.sh`. Python
   3.11+ is required. `config/dsw/requirements_tested_macos.txt` records the
   original source Mac snapshot; it has not been retested for this v6 update.
   Do not force Linux dependency pins onto macOS.
3. Run `outputs/dsw/venv/bin/python scripts/test_dsw.py`. The copied shared
   protocol/mock modules in `src/` and the small `tests/fixtures/frames/` files
   are test support; the application runtime lives in `src/dsw/`.
4. Launch the simulated session from the README and inspect layout, foreground/
   background selection, ROI controls, menus and Cmd-key shortcuts on your Mac.
   The source can open offline documents without any connected FPGA.
5. Build with `outputs/dsw/venv/bin/python scripts/build_dsw.py` on the Mac.
   The helper uses PyInstaller's macOS windowed app mode and the interpreter's
   native architecture. It is a starting build recipe; validate the resulting
   `.app`, multiprocessing startup, Qt plugins and any signing requirements.
6. With the board connected and acquisition stopped, select the actual Mac
   serial device from the GUI (normally `/dev/cu.*`, not Linux `/dev/ttyUSB0`).
   Confirm identity v6 and both 225/25 charge settings before any acquisition.
   Keep initial Connect/readback free of implicit Start, Stop, Clear or Z writes.
7. Verify that energy actions are disabled for v5 and while either channel runs.
   On a disposable, explicitly authorized measurement, verify save-before-Clear,
   settings readback, channel independence and calibration invalidation.
   Preserve physical UART/detector evidence separately from mock tests.

Keep `multiprocessing.freeze_support()` in `scripts/run_dsw.py` and the spawned
UART process in `app.py`/`uart_worker.py`. Check close/reconnect while a frame is
in progress; closing/disconnecting the GUI must not implicitly stop acquisition.
The transport suppresses DTR/RTS before open and uses serial exclusivity; verify
these behaviors with your Mac's adapter/driver. Rebuild on macOS rather than
copying the Linux executable.

## Qualification boundary

Linux offline tests and the new source build are recorded in [validation.md](validation.md).
The board's v6 flash boot, both charge defaults, physical UART at 115200/2 Mbaud,
and L/D CRC/accounting passed on Linux. Native macOS build, GUI behavior, USB
serial and real detector performance have not been verified by this export.
This repository intentionally contains no firmware build/programming tooling;
GUI development and the Mac update do not require reflashing the board.
