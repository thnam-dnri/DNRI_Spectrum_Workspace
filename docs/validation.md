# Source snapshot validation — 2026-10-15

This GUI-only checkout is tested independently from the FPGA workspace. The
current source build uses the Linux Python/Qt environment; no physical UART is
opened by the offline acceptance checks.

- Exported offline suite: **115 tests passed**, zero failures/errors/skips.
- Native Linux PyInstaller build: passed.
- Relocated frozen Linux app against a private simulated v6 UART: passed.
  Both FINAL frames retained byte-for-byte; clean exit status 0. The host Python
  environment was removed for the frozen-app launch.
- Shell launch/setup scripts pass bash syntax checks.
- All 11 runtime modules match the current Linux source exactly.

The packaging check uses derived synthetic v6 transport fixtures; it is not a
detector-energy measurement. The 225/25 reset defaults and custom pre/post dialog
are covered by the exported v6 mock/GUI tests. Source-Mac build/driver acceptance
remains to be run on macOS.

The source runtime is copied byte-for-byte from the current Linux GUI. The
provenance manifest records copied-file SHA256 values and the few adapted
export-specific helpers. No FPGA RTL, vendor content, programming scripts,
firmware binaries, flash backups or detector recordings are in this repository.

Linux firmware-side evidence predating this export: UART v6 flash boot passed
DONE1/CRC0/IDCODE0; both channels report charge225 total/25 pre/shift6/PZ off,
0.5 µs pre + 4 µs post. Both physical L/D frames passed CRC/accounting at
2 Mbaud; retained coherent frames passed the GUI decoder. That evidence remains
in the hardware project, not this GUI source publication.

macOS build, rendering, USB UART and physical detector qualification must be
performed on the Mac. Follow [mac_agent_handoff.md](mac_agent_handoff.md).
