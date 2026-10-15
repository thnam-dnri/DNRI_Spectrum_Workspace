# Codebase map

Last updated: 2026-10-15 — GUI-only Linux source snapshot with UART v6 charge controls and Mac update handoff.

```text
.
├── README.md                         Source setup and Linux/macOS entry point
├── CODEBASE_MAP.md                   Living index
├── .gitignore                       Generated environment/build/data exclusions
├── config/dsw/
│   ├── requirements.txt             GUI runtime requirements
│   ├── requirements_build.txt       Optional native PyInstaller build dependency
│   ├── requirements_linux.txt       Reproduced Linux dependency pins
│   └── requirements_tested_macos.txt Historical source-Mac dependency reference
├── src/
│   ├── dsw/
│   │   ├── app.py                   Window, controls, subprocess relay and energy dialog
│   │   ├── protocol.py              Independent v5/v6 UART transport, CRC and processing settings
│   │   ├── uart_worker.py           Serialized subprocess UART work and save-before-mutation
│   │   ├── spectrum_io.py           Spectrum/session formats and raw-frame preservation
│   │   ├── workspace.py             Per-channel sessions, calibration and ROI state
│   │   ├── spectrum_view.py         Shared foreground/background spectrum plot
│   │   ├── acquisition.py           Host duration/count presets
│   │   ├── histogram.py             Count-conserving display grouping
│   │   ├── peak_analysis.py         ROI/peak fits and uncertainties
│   │   ├── comparison.py            Background/reference alignment and uncertainty propagation
│   │   └── calibration_wizard.py    Two-point energy calibration
│   ├── dual_mca_protocol.py         Host-only shared codec used by GUI regression tests
│   ├── energy_calibration.py        Shared codec's calibration/rebinning dependency
│   └── mock_dual_mca.py              In-memory v1–v6 UART emulator for offline tests
├── scripts/
│   ├── run_dsw.py                   Source/frozen GUI entry point with freeze_support
│   ├── run_dsw.sh                   Native bundle/source launcher
│   ├── setup_dsw.sh                 Local native GUI/build environment setup
│   ├── test_dsw.py                  Offline GUI suite and machine-readable test summary
│   └── build_dsw.py                 Native Linux/macOS PyInstaller build helper
├── tests/
│   ├── dsw/test_*.py                GUI, transport, persistence and analysis regressions
│   ├── dsw/fixtures/examples/       Simulated spectra and saved workspace
│   ├── dsw/fixtures/recorded/       Small recorded synthetic UART frame fixtures and provenance
│   └── fixtures/frames/            Immutable v1–v4 codec seeds used by the in-memory mock
├── docs/
│   ├── mac_agent_handoff.md         Required Mac source changes and acceptance steps
│   ├── firmware_v6.md               Host-facing frame and command compatibility contract
│   ├── source_provenance.json       Copied GUI/test/config file checksums and scope
│   ├── validation.md               Export acceptance and platform qualification boundary
│   └── dsw/
│       ├── user_guide.md            GUI features and current gate defaults
│       └── standalone_gui.md        Source setup and native build instructions
└── outputs/                         Ignored environments, builds, tests and user data
```

Runtime imports stay in `src/dsw/`. The other three Python modules in `src/`
are regression-test support; no FPGA/hardware programming implementation is
included. Build the Linux and macOS applications on their respective hosts.
