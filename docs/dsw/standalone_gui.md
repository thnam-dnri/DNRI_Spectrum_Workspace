# DSW source setup and native builds

Start at the repository README and [Mac agent handoff](../mac_agent_handoff.md).
The GUI needs Python 3.11+, PySide6, pyqtgraph, NumPy and pyserial. Setup creates
`outputs/dsw/venv/` inside this GUI-only checkout; nothing is installed globally.

```bash
bash scripts/setup_dsw.sh
outputs/dsw/venv/bin/python scripts/test_dsw.py
bash scripts/run_dsw.sh --open tests/dsw/fixtures/examples/simulated-measurement.dsw
outputs/dsw/venv/bin/python scripts/build_dsw.py
```

Set `DSW_PYTHON` to a Python executable before setup when needed. The launcher
also accepts `DSW_PYTHON` as a source interpreter override. Setup applies Linux
pins only on Linux; macOS resolves the runtime/build requirements natively.
The original Mac dependency file is reference only. Installed versions are
recorded in `outputs/dsw/validation/requirements_host.txt`.

The build helper invokes PyInstaller in onedir mode. Linux output:
`outputs/dsw/dist/DSW/DSW`; macOS output: `outputs/dsw/dist/DSW.app`.
Use a native arm64 interpreter to build an Apple Silicon app, and validate
architecture, Qt plugins, subprocess startup and signing on the Mac. macOS
packaging is not qualified by the Linux source/export tests.

`bash scripts/run_dsw.sh` prefers an existing local native bundle, otherwise it
runs the source environment. Rebuild or run the interpreter directly after
changing source if a stale bundle exists:

```bash
outputs/dsw/venv/bin/python scripts/run_dsw.py
```

The source/frozen launcher supports `--open <spectrum-or-session>` and
`--port <serial-device>`. Selecting a port does not automatically Connect.
Examples and documentation are copied alongside build output. Keep the complete
native app/directory and rebuild separately on each operating system. This repo
contains no Linux executable or Mac application bundle.

Acquisitions, partial/invalid frames, autosaves and build/test artifacts go to
ignored `outputs/` under the working directory. The application keeps all file
operations in the user-selected workspace and does not build/program firmware.
