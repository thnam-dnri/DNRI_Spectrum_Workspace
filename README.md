# DNRI Spectrum Workspace

Standalone Python/PySide6 spectrum GUI by T. H. Nam for Linux and macOS.
This repository contains the DSW application, its offline tests and native build
helpers. It communicates with the ALINX dual-channel MCA over UART. FPGA source,
Vivado projects, bitstreams, flash utilities and detector recordings are excluded.

**Mac agent: start with [the update handoff](docs/mac_agent_handoff.md).**
The Linux source snapshot is from 2026-10-15 and supports UART v5 and v6.
Current firmware boots both channels in charge mode with **0.5 µs pre-trigger +
4 µs post-trigger**, 4.5 µs total, PZ off. The GUI reads these values from the
board; it does not program them automatically on Connect.

## Run from source

Use Python 3.11 or newer on the target host:

```bash
bash scripts/setup_dsw.sh
bash scripts/run_dsw.sh
# Open a simulated session without connecting hardware:
bash scripts/run_dsw.sh --open tests/dsw/fixtures/examples/simulated-measurement.dsw
```

On macOS, create the environment with a native Apple Silicon Python when building
an arm64 application. Linux setup applies the reproduced Linux dependency pins.
The source-Mac dependency snapshot is reference only; record and validate the
versions actually installed on your Mac.

## Test and build

```bash
outputs/dsw/venv/bin/python scripts/test_dsw.py
outputs/dsw/venv/bin/python scripts/build_dsw.py
```

Tests use mocks and fixtures and do not connect to physical serial hardware.
Build on each target OS. Linux output is `outputs/dsw/dist/DSW/DSW`; macOS output
is `outputs/dsw/dist/DSW.app`. Keep the complete Linux directory or Mac app bundle.
No executable, environment or generated output is committed here.

[User guide](docs/dsw/user_guide.md) · [Build guide](docs/dsw/standalone_gui.md) ·
[Firmware v6 interface](docs/firmware_v6.md) · [Validation](docs/validation.md) ·
[Codebase map](CODEBASE_MAP.md) · [Source provenance](docs/source_provenance.json).
