# DSW - DNRI Spectrum Workspace

Author: **T. H. Nam** · [thnam@dnri.vn](mailto:thnam@dnri.vn).

Desktop spectrum acquisition and analysis for the dual-channel ALINX UART firmware v5/v6. The app supports detected independent channel Start/Stop, v6 energy-processing presets, offline analysis of common spectrum files, saved workspaces, independent ADC input settings, multiple ROIs, uncertainty reporting, reference comparison and acquisition presets.

## Run

For Linux/macOS source setup and native builds, see [the build/run guide](standalone_gui.md).

```sh
bash scripts/run_dsw.sh
```

The standalone build bundles Python and Qt. Its source/build environment lives in `outputs/dsw/venv/`. See the Linux guide for this repository's paths and validation. This GUI does not build or program FPGA firmware.

## Energy processing on v6

With both channels stopped, select **Device → Channel1/2: Energy processing**.
Charge integration is the new firmware default, with PZ off and the existing
baseline tracker held constant during each gate. The15 October firmware reset
default on both channels is4.5µs total:500ns pre-trigger and4µs after the
trigger. The energy dialog reads these values from the device. Set **Pre-trigger
integration** and **Post-trigger integration** independently inµs; **Total
integration** updates automatically. Values use0.02µs steps. Pre-trigger may
be0–1.26µs; post-trigger must be at least0.02µs; total must not exceed81.92µs.
Invalid values disable Apply. Editing either time selects custom settings. Optional
starting presets are NaI(Tl)
2µs, CsI(Tl)8µs and CsI(Na)4µs, each including100ns before the trigger. Gate
and pre-trigger settings advance in20ns sampling steps. Trapezoidal processing
retains the existing electrical-pulser PZ profile. The area normalization shift
sets a power-of-two divisor; changing it changes the native spectrum scale.

**Save measurement and apply** retains previous spectra, clears both channels,
applies/reads back the selected channel's settings and marks its energy
calibration as needing renewal. Gate presets require tuning on actual detector
waveforms. The controls stay disabled for v5 firmware, which remains supported.
See [charge-integration details](../firmware_v6.md).

## Open, save and export

**File → Open session or spectrum** (Cmd/Ctrl+O) opens a DSW session or loads a spectrum into the selected foreground channel (CH1 if none is selected). The channel-specific File actions load into CH1 or CH2 and select that channel as foreground. Foreground files open while disconnected; reference/background files can load during acquisition. Opening a saved session never writes its recorded settings to the FPGA.

| Extension | Support |
|---|---|
| `.dsw` | Read/write complete DSW sessions: both spectra, foreground/background selections, independent calibration, grouping, named ROIs, analysis choices, references/backgrounds, sample/detector notes, recorded device settings and preset configuration. Retained FPGA bytes are included and CRC-checked when reopened. |
| `.spe` | Read/write IAEA/ORTEC **ASCII SPE**: native integer counts, measurement times, linear energy calibration and measurement description. DSW metadata also preserves ROIs, notes and the calibrated/uncalibrated flag. Binary/vendor-specific SPE variants are rejected explicitly. |
| `.chn` | Read/write ORTEC binary CHN: uint32 counts, linear calibration and 20 ms time resolution. Files exceeding CHN count/channel/time limits must use SPE, N42 or DSW JSON. |
| `.n42`, `.xml` | Read/write gamma-spectrum N42 XML. Supports polynomial linear calibration, counted-zero channel compression on import, live/real time and multiple measurements: select the measurement/detector to open. DSW remarks preserve ROIs, notes and calibration status on its exports. |
| `.csv` | Read/write integer counts with `energy_low_keV,energy_high_keV,counts`; import also accepts a `counts` column without calibration. Energy bins must be uniform. For clipped/nonuniform legacy grouped exports, open the companion JSON or FPGA `.bin` to retain native data. |
| `.json` | Read/write DSW spectrum JSON with full native counts and metadata; also reopens prior DSW export JSON with its companion `.bin`. |
| `.bin`, `.npy` | Import recorded v5/v6 FPGA frames or one-dimensional integer count arrays. FPGA CRC/identity/accounting checks remain enforced; `.npy` loads without pickle. |

SPE/N42/CHN coefficients map native channel edges to energy. DSW measures at bin centers `E = offset + gain × (i + 0.5)`. Imported channel offsets are included in calibration. The current analysis supports **linear** calibration; nonlinear calibration is rejected rather than silently approximated. Native bin counts and their full calibrated range are retained in exports, including counts beyond the visible 0–4000 keV range.

**File → Save DSW session** (Cmd/Ctrl+S) preserves the complete analysis. **File → Export channel … as SPE / CHN / N42 / CSV** writes the original foreground counts. Background-subtracted curves remain derived data and do not overwrite those counts. **Export foreground + ROI** saves the selected foreground's retained `.bin` when available, baseline `.npy`, grouped foreground CSV, JSON analysis metadata and native `.spe` together.

## Shared spectrum display

The workspace has one spectrum plot. **Foreground** selects Channel 1 or Channel 2; **Background** selects the other channel, a loaded background/reference, or **None — foreground only**. Background selection is enabled after selecting a foreground; the foreground channel cannot also be selected as background. Switching foreground to the current background resets background to None.

Foreground is blue and background is orange (a loaded reference uses purple), with a legend identifying both sources. Display grouping, ROI/FWHM analysis and the controls below the plot always follow the foreground. Each channel keeps its own grouping and calibration when you switch foreground. Background is conservatively aligned to the foreground's energy bin edges using its own calibration; changing its stored grouping does not change the shared display grid. The vertical scale fits both spectra, and hover shows foreground/background counts in the same display bin. The visible energy range remains 0–4000 keV.

Choose **Background → Load spectrum file…** to overlay SPE, CHN, N42 or other supported spectra directly without opening the comparison dock. Cancelling preserves the current background. **Background calibration…** edits only the selected background's offset/gain. Foreground calibration controls and the per-channel two-point wizards remain independent. Only foreground ROIs/fit curves are shown; the other channel's ROIs and analysis are retained and update with incoming frames.

Selecting display roles or changing calibration never sends acquisition commands. The channel acquisition buttons control physical CH1/CH2 independently of their foreground/background display roles.

**File → Sample and detector notes** records sample ID, detector/geometry and notes in sessions and compatible exports.

Try the supplied simulation without connecting hardware: open [examples/simulated-measurement.dsw](../../tests/dsw/fixtures/examples/simulated-measurement.dsw), or [examples/simulated-source.spe](../../tests/dsw/fixtures/examples/simulated-source.spe). The example reference and background files are also in `examples/`. These are explicitly simulated Gaussian spectra, not physical measurements.

## ADC source, trigger and noise

Connect verifies the board at 115200, probes `H` for independent-control support, reads both channels' settings/state and negotiates 2 Mbaud. Documented baud recovery takes approximately 10 seconds. One dedicated UART subprocess serializes all requests and maintains the high-baud lease; GUI rendering and its Python interpreter cannot block that subprocess's reads. The subprocess starts on Connect and closes cooperatively on application exit. Running channels refresh at about 1 Hz at 2 Mbaud; stopped channels keep their coherent FINAL spectrum while the peer runs.

## UART receive reliability

Spectrum reads drain chunks of at most 4096 bytes and allow progress within a bounded total deadline. Incomplete frames are saved as `partial-*.bin`; invalid complete frames are saved as `invalid-frame-*.bin`. They are never used as spectra. Every accepted frame still passes exact length, identity, CRC, committed-accounting and count-continuity checks.

For an interrupted or CRC-damaged spectrum read, DSW saves the failed bytes, closes the serial link, waits the documented recovery interval, identifies the FPGA at 115200 and reconnects at **921600 baud**. It retries the read once and permits at most two recoveries in one connection. Recovery preserves the session directory, count-continuity history, control-event records, calibration, ROIs and acquisition preset. It never replays Start/Stop/Clear or source/trigger/polarity writes. Enable bits are read again; no channel is started/stopped to restore an assumed state. A persistent failure shows an error and requires an explicit reconnect.

During recovery the GUI disables device controls and shows recovery status. At 921600, paired frames take about 1.43 s, so achieved cadence is approximately 0.7 Hz/channel; the displayed status reports the actual baud/cadence. Normal new connections still use 2 Mbaud. The original failing frame reported 4288/65748 bytes; its saved header was valid CH2 LIVE data. Tests reproduce that truncation, verify bounded retries and preserve partial bytes.

Historical Mac-source evidence (not repeated on Ubuntu): 2026-10-12 read-only hardware validation received 44 complete, validated live frames with both channels already running, including three deliberate 500 ms stalls of the GUI interpreter. Both enable bits stayed true throughout; no Start/Stop/Clear or source configuration commands were sent. Evidence: `isolated-gui-read-probe.json` (historical Mac-source evidence, not included in this transfer). Earlier diagnostic reads received another 24 direct and 40 GUI frames successfully, so the original interruption was intermittent and its precise cause was not reproduced.

A separate historical Mac-source fault-injection check deliberately reported only 4288 bytes from a fully received live frame. The original complete frame and artificial prefix were both retained. Recovery on the actual FPGA completed in about 11.18 s at 921600 baud; nine subsequent live frames passed validation and both channel enables remained true. No Start/Stop/Clear or source/trigger/polarity commands were sent. Evidence: `recovery-read-probe.json` (historical Mac-source evidence, not included in this transfer). The complete regression suite passes 106 tests.

## Independent channel Start/Stop

**Start CH1 / Stop CH1** and **Start CH2 / Stop CH2** act only on the named physical channel. CH1/CH2 state badges show verified RUNNING/STOPPED state even when that channel is not the foreground. **Start both / Stop both** remain available for global control; Start both can start a stopped channel while its peer is already running. Starting/resuming preserves existing histograms and enabled-time ticks. Clear is a separate operation.

The GUI enables individual controls only after `H` returns ASCII `1`. It reads `Q` bit0/bit1, selects CH1/CH2 with `J`/`K`, sends `A`/`O` and verifies both enable bits, including the untouched peer. Original v5 (`H` → `?`) keeps global controls; individual states remain unknown and the channel buttons are disabled. The extension cannot be inferred from the v5 identity. Detection runs on every reconnect: the current image is loaded in SRAM, so a flash reboot/power cycle may restore firmware with global controls only.

On a channel Stop, the worker finishes any in-flight UART frame, captures `D` for the stopped channel and `L` for a running peer, and saves a DSW session. That archive contains coherent FINAL data for the stopped channel and clearly marked LIVE data for its peer. The peer continues accumulating and refreshing. Periodic polling reuses a stopped channel's final frame instead of saving a new identical raw dump every second; explicit Read requests refresh both channels. Verified control events are written to `outputs/<connection>/channel-control.jsonl` and included in DSW sessions; per-channel FPGA ticks remain authoritative enabled time. Opening a saved session does not restore acquisition enables to hardware.

Clear, source, ADC polarity and trigger changes still require **both channels stopped**. Disconnect/Close do not stop either channel.

Select **ADC** from Source, then select **+ Positive** or **− Negative** independently in each channel's **ADC pulse** menu. Selecting ADC preserves the current per-channel polarity. Synthetic configures positive generator/DSP polarity, CH1 amplitude 101 at 2 kHz and CH2 amplitude 401 at 1 kHz; ADC polarity editing is disabled in Synthetic mode. Reselecting the current source or polarity sends no command and preserves spectra.

**Device → Channel 1/2: ADC trigger and noise** exposes each channel's trigger floor (1–4095 ADC codes) and noise multiplier N (0–16 in 1/256 increments). Effective threshold is `max(floor, ceil(N × fast-filter noise sigma))`; N=0 selects a fixed threshold. The dialog displays measured noise, effective threshold, noise-valid state, completed noise blocks and histogram count rate. Refresh reads hardware settings through the serialized worker. Apply requires connected ADC input and both channels stopped; `W` configuration CRC and `G` readback are verified. Polarity writes are verified through `F` readback. Shaping/decay/gain firmware parameters remain compiled and are not editable here.

## Preserving measurements and acquisition presets

Before **Clear**, source, polarity or trigger changes, the worker reads coherent final spectra from **both** channels and saves a `.dsw` snapshot with current analysis context and actual device settings under `outputs/<connection>/autosave/`. Empty, never-acquired spectra need no archive. A failed snapshot/save prevents the mutation. The v5 clear command always clears both histograms; a channel setting change preserves the other channel's polarity/trigger configuration. Explicit Clear still asks for confirmation.

The **Start both preset** strip applies to Start both/Stop both. Dedicated CH1/CH2 commands use manual control and cancel an active shared preset without stopping the peer. It offers:

- **Manual**: run until Stop.
- **Duration**: stop after the configured host elapsed time from Start.
- **Counts**: stop after the requested number of **new** counts on CH1, CH2 or both combined, relative to the Start baseline.

The progress bar displays elapsed time or new counts. Stop requests are serialized behind the current UART operation, so duration/count targets can overshoot by the read/command latency; they are not hardware-exact gates. On Stop both, both coherent final frames are captured and saved automatically under `outputs/autosave/`. On a channel Stop, only that channel is FINAL; a running peer is retained as LIVE in the same session. **File → Open latest automatic save** reopens the latest archive while disconnected. Connecting with an existing offline workspace preserves that workspace first.

Disconnect and Close release the serial link without stopping or clearing the FPGA. Disconnect cancels the host preset. Closing waits for the serial worker to finish before Qt disposes of its objects.

Successful LIVE raw storage remains bounded to the latest 20 frames/channel per connection. FINAL, partial/error and automatic-save files are retained. No prior connection's data is automatically deleted.

## Multiple ROIs and peak table

- **Double-click** selects a nearby distinct peak in the active ROI.
- **Shift + double-click** adds another ROI around the clicked peak.
- **Peaks / compare → Add ROI** adds the entered energy interval; drag its edges to refine it.
- **Find peaks** adds distinct peaks within the visible range (up to 32 per search, 64 ROIs/channel).
- Click a peak-table row to select its ROI and analysis settings. Edit its name or low/high energy cells; choose Direct or Gaussian Fit in the main panel.
- **Remove selected** removes only that ROI. **Zoom ROI** focuses it; **Full range** returns to 0–4000 keV.
- **Export peak table** saves all named peak results, statistical errors, background-subtraction flags, exposure, calibration/LIVE/FINAL state and sample/detector identifiers to CSV.

Each ROI has a color and its own method/smoothing. Other ROI regions and Gaussian curves remain visible. Results update with incoming frames. Calibration changes move every ROI to follow the same native bins, clipping/removing regions outside the visible energy range. Display grouping (1/8/16/24/32/40 bins, default 8 for FPGA data, native 1 for imported spectra) does not change peak analysis.

Use **View → Peaks, references and background** to show/hide the dock. The panel can be resized or undocked; peak analysis scrolls when needed in smaller windows. Mouse hover displays energy, foreground/background displayed counts and shared bin edges.

## Statistical errors and fit diagnostics

**Direct (Endpoint BG)** integrates native counts above a line through the mean of 1–8 bins at each ROI endpoint. Gross counts remain an exact foreground sum. Net-area errors propagate endpoint-background correlations; centroid errors use local propagation. Optional 3/5/9-bin triangular smoothing changes peak position/FWHM only. Direct FWHM is obtained by interpolation at half maximum; its uncertainty is not inferred from a Gaussian model.

**Gaussian Fit** fits a Gaussian plus linear background to native bins using a Poisson-likelihood objective. Background-subtracted data use weighted least squares with foreground/reference errors. The solver checks convergence, requires at least six bins for five parameters, and reports centroid, FWHM and Gaussian area with statistical **one-sigma** uncertainties, parameter covariance, iterations and reduced chi-squared with `n − 5` degrees of freedom. **Fit diagnostics** displays the native spectrum, fitted curve and standardized residuals. Failed/nonconverged fits do not report successful peak widths.

Statistical errors do not include calibration uncertainty, detector efficiency uncertainty or model/systematic errors. Isolate individual peaks and inspect residuals; this is a single-Gaussian model, not overlapping-peak deconvolution. Truncated and sampling-limited peaks are flagged. Assumed/default energy calibration remains visibly **UNCALIBRATED**. LIVE results are provisional; stopped FINAL frames are coherent.

## Reference overlay and background subtraction

In **Peaks / compare**, load a reference or background; loading a file for the foreground automatically selects it in the main **Background** dropdown. Use **Subtract selected background** to display/analyze foreground minus the selected channel or loaded file. Choosing None restores foreground counts. Each file has its own calibration; **Calibration…** edits it or copies the foreground calibration for matching native bin counts.

Normalize by exposure, use unscaled counts, or enter a manual scale. Automatic normalization uses detector-corrected live time only when **both** spectra supply it; otherwise it uses real/acquisition-enabled time. The FPGA supplies acquisition-enabled time, not corrected detector live time. When measurement time is unavailable, the overlay uses unscaled counts and says so; enter a manual scale when a known exposure ratio is needed. The displayed scale/readout states the basis used.

Different energy grids are conservatively rebinned by bin overlap, assuming uniform density within a reference bin. Statistical propagation retains the correlations produced when one reference bin is split across several target bins. Foreground and reference measurements are assumed independent, and exposure scales exact. Signed subtraction values are retained, including negative bins; linear Y scaling accommodates them. Raw foreground data remain unchanged.

## Verification and development

```sh
QT_QPA_PLATFORM=offscreen PYTHONPATH=src/dsw:tests/dsw outputs/dsw/venv/bin/python -m unittest discover -s tests/dsw -v
```

Tests cover common-format round trips and independent SPE/N42 fixtures, exact large integer counts, malformed/truncated/nonlinear file rejection, native FPGA identity/CRC accounting, session restoration and channel mapping, multiple ROI calibration, actual Qt double-click/drag interactions, fit errors and residuals, reference rebinning covariance, ADC trigger CRC/readback, automatic preservation and save-failure mutation guards, count/duration presets and final autosaves. Shared-view tests exercise foreground/background constraints, differing calibration and bin counts, foreground grouping, live background subtraction, independent background calibration, hidden-channel ROI retention, direct file loading/cancellation and view/session restoration. Offscreen renders verify the desktop and smaller-window layout. New ADC controls/presets are tested through simulated UART operations; a physical interactive acquisition check remains to be done.

Independent-control tests use simulated UART bytes/CRC frames to check `H`/`Q`, both Start/Stop directions, untouched-peer verification, frozen stopped counts/ticks, resume without Clear, global commands and original-v5 fallback. Worker/GUI checks cover mixed LIVE/FINAL frames, bounded stopped-channel raw retention, automatic save including a running peer, save failures, manual/shared preset behavior, fresh count baselines when a peer already runs, and deferring Stop until a live frame finishes. No physical Start/Stop/Clear commands are sent by the test suite.

Reliability tests cover chunked/progress-based deadlines, the exact 4288-byte truncation, persistent failures, retained bad-CRC bytes, transport-only recovery, and commands that must not be replayed. Spawned-process tests hold the GUI interpreter's GIL while the UART service continues receiving, exercise cooperative shutdown, and verify that the latest preservation context reaches the service before Clear. GUI checks verify retained analysis/preset state and control enablement during recovery.

Historical Mac-source evidence (not repeated on this Ubuntu import): 2026-10-12 read-only FPGA verification over CP2102N `/dev/cu.usbserial-11110` detected independent control and both channels stopped, with identical before/after states and no acquisition commands. Evidence: `capability-status.json` (not included in transfer). GUI Start/Stop transitions are verified in simulation; no physical acquisition was started/stopped for this GUI change.

Historical Mac-source evidence (not included except the two retained frames): 2026-10-11 physical v5 synthetic bench evidence is retained in `outputs/20261011-025802/bench.json`: eight paired live reads at 2 Mbaud, CRC/bracket/monotonicity checks, coherent final CH1 15343 and CH2 7671 counts, all rejection/drop/saturation counters zero, original settings restored. `scripts/dsw_bench.py` is a destructive physical synthetic test that preserves measurements, clears, acquires and restores settings; do not run it during another measurement.

File format references: [Sandia SpecUtils SPE implementation](https://github.com/sandialabs/SpecUtils/blob/master/src/SpecFile_spe.cpp), [CHN implementation](https://github.com/sandialabs/SpecUtils/blob/master/src/SpecFile_chn.cpp). Wire contract and energy controls: [UART v5/v6 interface](../firmware_v6.md).

GUI update notes: [Mac agent handoff](../mac_agent_handoff.md).
