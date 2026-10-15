import csv
import json
import multiprocessing
import queue
import sys
import threading
import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets
from serial.tools import list_ports

from uart_worker import WorkerCore, process_main
from histogram import energy_histogram, DEFAULT_GAIN
from peak_analysis import analyze_roi, automatic_roi
from calibration_wizard import TwoPointCalibrationDialog
from workspace import WorkspaceMixin
from spectrum_view import SpectrumViewMixin
from spectrum_io import write_spectrum
from protocol import ENERGY_PRESETS, encode_energy

APP_NAME = 'DSW - DNRI Spectrum Workspace'
APP_AUTHOR = 'T. H. Nam'
APP_EMAIL = 'thnam@dnri.vn'


class SpectrumPlot(pg.PlotWidget):
    peak_requested = QtCore.Signal(float)
    add_peak_requested = QtCore.Signal(float)

    def mouseDoubleClickEvent(self, event):
        position = self.mapToScene(event.position().toPoint())
        view = self.getViewBox()
        if event.button() == QtCore.Qt.MouseButton.LeftButton and view.sceneBoundingRect().contains(position):
            signal = self.add_peak_requested if event.modifiers() & QtCore.Qt.KeyboardModifier.ShiftModifier else self.peak_requested
            signal.emit(view.mapSceneToView(position).x())
            event.accept()
            return
        super().mouseDoubleClickEvent(event)


class Worker(QtCore.QObject, WorkerCore):
    event = QtCore.Signal(str, object)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.commands = queue.Queue()
        self.preservation_context = {}
        self.process = None
        self.thread = threading.Thread(target=self.relay, daemon=True)
        self.thread.start()

    def send(self, op, argument=None):
        context = json.loads(json.dumps(self.preservation_context))
        self.commands.put((op, argument, context))

    def relay(self):
        commands = events = None
        quitting = False
        context = multiprocessing.get_context('spawn')
        try:
            while True:
                try:
                    command = self.commands.get(timeout=.02)
                except queue.Empty:
                    command = None
                if command is not None:
                    op = command[0]
                    if commands is None:
                        if op == 'quit':
                            return
                        if op != 'connect':
                            self.event.emit('error', 'Connect before issuing device commands')
                            continue
                        commands, events = context.Queue(), context.Queue()
                        self.process = context.Process(target=process_main, args=(commands, events), daemon=True)
                        self.process.start()
                    quitting = quitting or op == 'quit'
                    commands.put(command)
                if events is not None:
                    ended = False
                    while True:
                        try:
                            kind, value = events.get_nowait()
                        except queue.Empty:
                            break
                        if kind == 'worker_done':
                            ended = True
                            break
                        self.event.emit(kind, value)
                    if ended or not self.process.is_alive():
                        self.process.join(timeout=1)
                        if quitting:
                            return
                        commands.close(); commands.cancel_join_thread()
                        events.close(); events.cancel_join_thread()
                        commands = events = None
                        self.process = None
                        self.event.emit('error', 'UART service exited; reconnect to identify acquisition state')
        except Exception as exc:
            self.event.emit('error', str(exc))
        finally:
            if self.process is not None and self.process.is_alive():
                commands.put(('quit', None, {}))
                deadline = time.monotonic()+30
                while self.process.is_alive() and time.monotonic() < deadline:
                    try:
                        events.get(timeout=.1)
                    except queue.Empty:
                        pass
                    self.process.join(timeout=.1)
                if self.process.is_alive():
                    self.process.terminate()
                    self.process.join(timeout=2)
            if commands is not None:
                commands.close()
                commands.cancel_join_thread()
            if events is not None:
                events.close()
                events.cancel_join_thread()


class Window(QtWidgets.QMainWindow, WorkspaceMixin, SpectrumViewMixin):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_NAME)
        self.resize(1440, 900)
        self.setMinimumSize(1080, 780)
        self.setAttribute(QtCore.Qt.WidgetAttribute.WA_DeleteOnClose, True)
        self.worker = Worker(self)
        self.worker.event.connect(self.receive)
        self.connected = self.running = self.busy = False
        self.protocol_version = 5
        self.independent_channel_control = False
        self.channel_acquiring = None
        self.control_events = []
        self.link_baudrate = 2000000
        self._busy_before_recovery = False
        self._stopped_channels = (1, 2)
        self.frames = {}
        self.baselines = {}
        self.host_histograms = {}
        self.previous_time = {}
        self.calibrations = {1: [0., DEFAULT_GAIN], 2: [0., DEFAULT_GAIN]}
        self.grouping = {1: 8, 2: 8}
        self.range_counts = {}
        self.calibrated = {1: False, 2: False}
        self.calibration_energy = {1: None, 2: None}
        self.roi_results = {}
        self.roi_regions, self.roi_inputs, self.roi_buttons = {}, {}, {}
        self.roi_metrics, self.roi_messages, self.smoothing = {}, {}, {}
        self.roi_methods, self.fit_curves, self.roi_fit_curves = {}, {}, {}
        self.polarity_menus = {}
        self.device_settings = {}
        self.peak_markers, self.width_markers, self.cursors = {}, {}, {}
        self.cursor_labels, self.mode_labels, self.mouse_proxies = {}, {}, {}
        self.scale_labels = {}
        self.grouping_menus = {}
        self.workspace_ready = False
        self.closing = False
        self.cleanup_done = False
        self.close_timer = QtCore.QTimer(self)
        self.close_timer.setSingleShot(True)
        self.close_timer.timeout.connect(self.close)
        self.setStyleSheet('''
            QMainWindow, QWidget#central { background: #111827; }
            QWidget#spectrumViewport, QSplitter#channels { background: #111827; }
            QDialog { background: #182334; }
            QDockWidget, QTabWidget::pane, QTabBar::tab { background: #182334; }
            QWidget#workspaceTab { background: #182334; }
            QTabWidget::pane { border: 1px solid #2c3b50; }
            QTabBar::tab { padding: 6px 14px; }
            QTabBar::tab:selected { background: #29394f; }
            QHeaderView::section { background: #29394f; color: #e5edf6; padding: 5px; }
            QTableCornerButton::section { background: #29394f; border: 0; }
            QLineEdit, QPlainTextEdit, QSpinBox { background: #111c2c; border: 1px solid #3b506b; padding: 4px; }
            QProgressBar { border: 1px solid #3b506b; border-radius: 4px; text-align: center; background: #111c2c; }
            QProgressBar::chunk { background: #2563a6; }
            QWidget { color: #e5edf6; font-size: 12px; }
            QFrame#channelCard { background: #182334; border: 1px solid #2c3b50; border-radius: 10px; }
            QLabel#appTitle { font-size: 22px; font-weight: 700; }
            QLabel#muted, QLabel#metricLabel { color: #94a8c1; }
            QLabel#channelTitle { font-size: 16px; font-weight: 700; }
            QLabel#metricValue { font-size: 17px; font-weight: 600; }
            QLabel#status { background: #1e2b3e; padding: 8px; border-radius: 6px; }
            QLabel#mode { background: #283950; padding: 4px 10px; border-radius: 4px; }
            QPushButton, QToolButton { background: #29394f; border: 1px solid #3b506b;
                border-radius: 5px; padding: 6px 12px; }
            QPushButton:hover, QToolButton:hover { background: #354c69; }
            QPushButton:disabled { color: #667990; background: #202c3d; border-color: #2c3b50; }
            QPushButton#primary { background: #2563a6; border-color: #4388cc; }
            QPushButton#primary:hover { background: #3075bd; }
            QComboBox, QDoubleSpinBox { background: #111c2c; border: 1px solid #3b506b;
                border-radius: 4px; padding: 5px; }
            QComboBox:disabled { color: #667990; }
            QComboBox QAbstractItemView { background: #182334; selection-background-color: #2563a6; }
            QAbstractItemView { background: #111c2c; selection-background-color: #2563a6; }
            QMenuBar, QMenu { background: #182334; }
            QMenuBar::item:selected, QMenu::item:selected { background: #354c69; }
            QSplitter::handle { background: #111827; height: 8px; }
            QScrollBar:vertical { background: #182334; width: 12px; }
            QScrollBar::handle:vertical { background: #3b506b; border-radius: 4px; min-height: 24px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: #111827; }
            QScrollBar:horizontal { background: #182334; height: 12px; }
            QScrollBar::handle:horizontal { background: #3b506b; min-width: 24px; }
            QScrollBar::add-line:horizontal, QScrollBar::sub-line:horizontal { width: 0; }
            QScrollBar::add-page:horizontal, QScrollBar::sub-page:horizontal { background: #111827; }
            QToolTip { background: #29394f; color: #e5edf6; border: 1px solid #3b506b; }
        ''')
        central = QtWidgets.QWidget()
        central.setObjectName('central')
        self.setCentralWidget(central)
        layout = QtWidgets.QVBoxLayout(central)
        layout.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
        layout.setContentsMargins(16, 12, 16, 12)
        layout.setSpacing(10)
        heading = QtWidgets.QHBoxLayout()
        title = QtWidgets.QLabel(APP_NAME)
        title.setObjectName('appTitle')
        heading.addWidget(title)
        heading.addStretch()
        credits = QtWidgets.QVBoxLayout()
        credits.setSpacing(2)
        subtitle = QtWidgets.QLabel('Dual channel MCA  /  UART v5–v6')
        subtitle.setObjectName('muted')
        subtitle.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        credits.addWidget(subtitle)
        author = QtWidgets.QLabel(f'{APP_AUTHOR} · <a href="mailto:{APP_EMAIL}" '
                                  f'style="color: #94bfee;">{APP_EMAIL}</a>')
        author.setObjectName('muted')
        author.setTextFormat(QtCore.Qt.TextFormat.RichText)
        author.setAlignment(QtCore.Qt.AlignmentFlag.AlignRight)
        author.setOpenExternalLinks(True)
        credits.addWidget(author)
        heading.addLayout(credits)
        layout.addLayout(heading)
        toolbar = QtWidgets.QHBoxLayout()
        layout.addLayout(toolbar)
        self.port = QtWidgets.QComboBox()
        self.port.setMinimumWidth(260)
        toolbar.addWidget(self.port, 1)
        self.scan_button = QtWidgets.QPushButton('Rescan')
        self.scan_button.clicked.connect(self.scan)
        toolbar.addWidget(self.scan_button)
        self.connect_button = QtWidgets.QPushButton('Connect')
        self.connect_button.setObjectName('primary')
        self.connect_button.clicked.connect(self.connection)
        toolbar.addWidget(self.connect_button)
        self.buttons = {}
        for label, op in [('Start both', 'start'), ('Stop both', 'stop'), ('Read', 'read')]:
            button = QtWidgets.QPushButton(label)
            button.clicked.connect(lambda checked=False, op=op: self.action(op))
            self.buttons[op] = button
            toolbar.addWidget(button)
        self.source_menu = QtWidgets.QComboBox()
        self.source_menu.addItem('Source…', None)
        self.source_menu.addItem('Synthetic (Positive)', ('synthetic', None))
        self.source_menu.addItem('ADC', ('adc', None))
        self.source_menu.setToolTip('Select the source for both channels. ADC preserves each channel\'s polarity; '
                                   'choose Positive or Negative independently for each ADC channel. '
                                   'Changing source automatically clears both histograms.')
        self.source_menu.activated.connect(self.source_selected)
        toolbar.addWidget(self.source_menu)
        self.device_menu = self.menuBar().addMenu('Device')
        device_menu = self.device_menu
        reset = device_menu.addAction('Clear both FPGA histograms…')
        reset.triggered.connect(lambda: self.action('clear'))
        self.hardware_clear = reset
        self.energy_actions = {}
        for channel in (1, 2):
            action = device_menu.addAction(f'Channel {channel}: Energy processing…')
            action.triggered.connect(lambda checked=False, ch=channel: self.open_energy_settings(ch))
            self.energy_actions[channel] = action
        cal_menu = self.menuBar().addMenu('Calibration')
        cal_menu.addAction('Channel 1: 2-Point Calibration Wizard…', lambda: self.open_calibration_wizard(1))
        cal_menu.addAction('Channel 2: 2-Point Calibration Wizard…', lambda: self.open_calibration_wizard(2))
        help_menu = self.menuBar().addMenu('Help')
        help_menu.addAction('About DSW…', self.show_about)
        self.clear_buttons, self.export_buttons = {}, {}
        clear = QtWidgets.QPushButton('Clear both channels')
        clear.setToolTip('Erase both FPGA histograms and counters after confirmation')
        clear.clicked.connect(lambda checked=False: self.action('clear'))
        toolbar.addWidget(clear)
        self.clear_buttons = {1: clear, 2: clear}
        channel_row = QtWidgets.QHBoxLayout()
        self.channel_buttons, self.channel_status_labels = {}, {}
        for ch in (1, 2):
            label = QtWidgets.QLabel(f'CH{ch} · DISCONNECTED')
            label.setObjectName('mode')
            label.setMinimumWidth(145)
            self.channel_status_labels[ch] = label
            channel_row.addWidget(label)
            for op in ('start', 'stop'):
                button = QtWidgets.QPushButton(f'{op.title()} CH{ch}')
                button.clicked.connect(lambda checked=False, op=op, ch=ch: self.channel_action(op, ch))
                button.setToolTip(f'{op.title()} only CH{ch}; the other channel keeps its acquisition state. Individual starts are manual; the preset applies to Start both.')
                self.channel_buttons[ch, op] = button
                channel_row.addWidget(button)
            channel_row.addSpacing(12)
        channel_row.addStretch()
        self.channel_capability_label = QtWidgets.QLabel('Connect to detect channel control')
        self.channel_capability_label.setObjectName('muted')
        channel_row.addWidget(self.channel_capability_label)
        layout.addLayout(channel_row)
        self.status = QtWidgets.QLabel('Disconnected · live cadence: 1 Hz/channel at 2 Mbaud')
        self.status.setObjectName('status')
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.build_spectrum_view(layout, SpectrumPlot)
        note = QtWidgets.QLabel('Drag ROI edges to refine a peak. Scroll / drag to zoom / pan. '
                               'ROI and calibration are available during acquisition. '
                               'LIVE is provisional; Stop captures FINAL. Disconnect leaves acquisition unchanged.')
        note.setObjectName('muted')
        note.setWordWrap(True)
        layout.addWidget(note)
        self.scan()
        self.controls()
        self.init_workspace()
        self.refresh_spectrum_view()

    def show_about(self):
        QtWidgets.QMessageBox.about(
            self, f'About {APP_NAME}',
            f'<h3>{APP_NAME}</h3><p>Author: {APP_AUTHOR}<br>'
            f'Email: <a href="mailto:{APP_EMAIL}">{APP_EMAIL}</a></p>')

    def build_roi_panel(self, ch, color, content, card_layout):
        panel = QtWidgets.QWidget()
        panel.setFixedWidth(330)
        column = QtWidgets.QVBoxLayout(panel)
        column.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
        column.setContentsMargins(8, 0, 0, 0)
        column.setSpacing(6)
        title = QtWidgets.QLabel('Peak analysis')
        title.setObjectName('channelTitle')
        column.addWidget(title)
        buttons = {}
        row = QtWidgets.QHBoxLayout()
        for key, title in (('auto', 'Auto peak'), ('apply', 'Set ROI')):
            button = QtWidgets.QPushButton(title)
            row.addWidget(button)
            buttons[key] = button
        buttons['auto'].setToolTip('Select the strongest distinct peak in the visible energy range')
        buttons['apply'].setToolTip('Select the manually entered lower and upper energy bounds')
        buttons['auto'].clicked.connect(lambda checked=False, ch=ch: self.auto_roi(ch))
        buttons['apply'].clicked.connect(lambda checked=False, ch=ch: self.manual_roi(ch))
        column.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        low, high = QtWidgets.QDoubleSpinBox(), QtWidgets.QDoubleSpinBox()
        for widget, value, title in ((low, 100., 'Lower ROI bound'), (high, 300., 'Upper ROI bound')):
            widget.setRange(0., 4000.)
            widget.setDecimals(4)
            widget.setValue(value)
            widget.setSuffix(' keV')
            widget.setToolTip(title + '; press Enter or leave the field to apply')
            widget.setKeyboardTracking(False)
            row.addWidget(widget)
            widget.editingFinished.connect(lambda ch=ch: self.manual_roi(ch))
        self.roi_inputs[ch] = (low, high)
        column.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        for key, title in (('zoom', 'Zoom ROI'), ('full', 'Full range'), ('remove', 'Remove')):
            button = QtWidgets.QPushButton(title)
            button.setStyleSheet('padding: 5px 7px;')
            row.addWidget(button)
            buttons[key] = button
        buttons['zoom'].clicked.connect(lambda checked=False, ch=ch: self.zoom_roi(ch))
        buttons['full'].clicked.connect(lambda checked=False, ch=ch: self.plots[ch].setXRange(0, 4000, padding=0))
        buttons['remove'].clicked.connect(lambda checked=False, ch=ch: self.remove_roi(ch))
        self.roi_buttons[ch] = buttons
        column.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel('Analysis method'))
        method = QtWidgets.QComboBox()
        method.addItem('Direct (Endpoint BG)', 'endpoint')
        method.addItem('Gaussian Fit', 'gaussian')
        method.setToolTip('Direct: Linear endpoint background & interpolated half-maximum.\n'
                           'Gaussian Fit: Levenberg-Marquardt nonlinear fit with linear background and curve overlay.')
        method.currentIndexChanged.connect(lambda index, ch=ch: self.update_roi(ch))
        self.roi_methods[ch] = method
        row.addWidget(method)
        column.addLayout(row)
        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel('Width smoothing'))
        smoothing = QtWidgets.QComboBox()
        smoothing.addItem('None', 1)
        for width in (3, 5, 9):
            smoothing.addItem(f'{width} native bins', width)
        smoothing.setToolTip('Optional triangular smoothing for peak position and FWHM only. '
                             'Areas and centroid always use unsmoothed native counts.')
        smoothing.currentIndexChanged.connect(lambda index, ch=ch: self.update_roi(ch))
        self.smoothing[ch] = smoothing
        row.addWidget(smoothing)
        column.addLayout(row)
        grid = QtWidgets.QGridLayout()
        grid.setHorizontalSpacing(10)
        metrics = {}
        for index, (key, title) in enumerate((('peak_keV', 'PEAK'), ('centroid_keV', 'CENTROID'),
                                              ('fwhm_keV', 'FWHM'), ('resolution_percent', 'RESOLUTION'),
                                              ('gross_counts', 'GROSS COUNTS'), ('net_counts', 'NET COUNTS'))):
            cell = QtWidgets.QVBoxLayout()
            cell.setSpacing(1)
            caption = QtWidgets.QLabel(title)
            caption.setObjectName('metricLabel')
            caption.setMinimumHeight(16)
            value = QtWidgets.QLabel('—')
            value.setObjectName('metricValue')
            value.setMinimumHeight(24)
            metrics[key] = value
            cell.addWidget(caption)
            cell.addWidget(value)
            grid.addLayout(cell, index//2, index%2)
            grid.setColumnStretch(index%2, 1)
        metrics['resolution_percent'].setToolTip('100 × FWHM / background-subtracted centroid energy')
        metrics['net_counts'].setToolTip('Gross ROI counts minus the integral of the linear endpoint background')
        self.roi_metrics[ch] = metrics
        column.addLayout(grid)
        message = QtWidgets.QLabel('Select a peak or enter ROI bounds.')
        message.setObjectName('muted')
        message.setWordWrap(True)
        message.setMinimumHeight(42)
        message.setToolTip('Measurements use native FPGA bins. Background is a line through mean counts '
                           'at each ROI endpoint. FWHM uses linearly interpolated half-maximum crossings '
                           'above background; overlapping peaks need manual isolation. '
                           'The default energy scale is uncalibrated.')
        self.roi_messages[ch] = message
        column.addWidget(message)
        column.addStretch()
        content.addWidget(panel)
        shade = pg.mkColor(color)
        shade.setAlpha(28)
        hover = pg.mkColor(color)
        hover.setAlpha(45)
        region = pg.LinearRegionItem(values=(100, 300), bounds=(0, 4000),
                                     brush=pg.mkBrush(shade), hoverBrush=pg.mkBrush(hover),
                                     pen=pg.mkPen(color, width=1.2), swapMode='sort')
        region.setZValue(10)
        region.hide()
        self.plots[ch].addItem(region, ignoreBounds=True)
        self.roi_regions[ch] = region
        region.sigRegionChanged.connect(lambda item, ch=ch: self.region_changed(ch))
        peak = pg.InfiniteLine(angle=90, movable=False,
                               pen=pg.mkPen(color, style=QtCore.Qt.PenStyle.DashLine))
        peak.hide()
        self.plots[ch].addItem(peak, ignoreBounds=True)
        self.peak_markers[ch] = peak
        width_markers = []
        for _ in range(2):
            marker = pg.InfiniteLine(angle=90, movable=False,
                                     pen=pg.mkPen('#e1eaf6', style=QtCore.Qt.PenStyle.DotLine),
                                     span=(0, .85))
            marker.hide()
            self.plots[ch].addItem(marker, ignoreBounds=True)
            width_markers.append(marker)
        self.width_markers[ch] = width_markers

    def set_roi(self, ch, low, high):
        low, high = max(0., float(low)), min(4000., float(high))
        if low >= high:
            self.roi_messages[ch].setText('Lower bound must be below upper bound.')
            return
        region = self.roi_regions[ch]
        blocker = QtCore.QSignalBlocker(region)
        region.setRegion((low, high))
        self.roi_enabled[ch] = True
        region.setVisible(ch == self.foreground_channel)
        del blocker
        self.region_changed(ch)
        self.controls()

    def manual_roi(self, ch):
        if ch in self.frames:
            self.set_roi(ch, *(widget.value() for widget in self.roi_inputs[ch]))

    def auto_roi(self, ch, energy=None):
        if ch not in self.frames:
            return
        bounds = self.plots[ch].getViewBox().viewRange()[0]
        try:
            low, high = automatic_roi(self.displayed_counts(self.frames[ch]), self.calibrations[ch],
                                      target=energy, search_bounds=bounds,
                                      search_radius=max(12 * self.calibrations[ch][1], .04 * (bounds[1]-bounds[0])))
        except ValueError as exc:
            self.roi_messages[ch].setText(str(exc))
            return
        self.set_roi(ch, low, high)

    def region_changed(self, ch):
        for widget, value in zip(self.roi_inputs[ch], self.roi_regions[ch].getRegion()):
            blocker = QtCore.QSignalBlocker(widget)
            widget.setValue(value)
            del blocker
        self.update_roi(ch)

    def remove_roi(self, ch):
        self.roi_enabled[ch] = False
        self.roi_regions[ch].hide()
        self.peak_markers[ch].hide()
        for marker in self.width_markers[ch]:
            marker.hide()
        if ch in self.fit_curves:
            self.fit_curves[ch].hide()
        self.roi_fit_curves.pop(ch, None)
        if self.workspace_ready:
            self.roi_residuals.pop(ch, None)
        self.roi_results.pop(ch, None)
        for value in self.roi_metrics[ch].values():
            value.setText('—')
        self.roi_messages[ch].setText('Select a peak or enter ROI bounds.')
        if self.workspace_ready:
            self.remove_active_roi(ch)
        self.controls()

    def update_roi(self, ch):
        if ch not in self.frames or not self.roi_enabled[ch]:
            return
        method = self.roi_methods[ch].currentData() if ch in self.roi_methods else 'endpoint'
        counts, noise = self.analysis_data(ch) if self.workspace_ready else (self.displayed_counts(self.frames[ch]), None)
        result = analyze_roi(counts, self.roi_regions[ch].getRegion(),
                             self.calibrations[ch], self.smoothing[ch].currentData(),
                             method=method, noise=noise)
        if self.workspace_ready:
            self.annotate_background_result(ch, result, self.roi_regions[ch].getRegion(), noise)
        result['calibrated'] = self.calibrated[ch]
        result['spectrum_mode'] = 'LIVE' if self.frames[ch]['live'] else 'FINAL'
        fit_curve = result.pop('fit_curve', None)
        residual_curve = result.pop('residual_curve', None)
        if self.workspace_ready:
            self.roi_residuals[ch] = residual_curve
        self.roi_fit_curves[ch] = fit_curve
        self.roi_results[ch] = result
        for key, label in self.roi_metrics[ch].items():
            value = result[key]
            if value is None:
                label.setText('—')
            elif key.endswith('_keV'):
                label.setText(f'{value:,.3f} keV')
            elif key == 'resolution_percent':
                label.setText(f'{value:.2f} %')
            elif key == 'gross_counts':
                label.setText(f'{value:,}' if isinstance(value, int) else f'{value:,.1f}')
            else:
                label.setText(f'{value:,.1f}')
        peak = self.peak_markers[ch]
        peak.setVisible(result['peak_keV'] is not None)
        if result['peak_keV'] is not None:
            peak.setValue(result['peak_keV'])
        for marker, key in zip(self.width_markers[ch], ('fwhm_low_keV', 'fwhm_high_keV')):
            marker.setVisible(result[key] is not None)
            if result[key] is not None:
                marker.setValue(result[key])
        if fit_curve is not None and method == 'gaussian':
            x_fit, y_fit = fit_curve
            group_scale = float(self.grouping.get(ch, 1))
            self.fit_curves[ch].setData(x_fit, y_fit * group_scale)
            self.fit_curves[ch].show()
        elif ch in self.fit_curves:
            self.fit_curves[ch].hide()
        qualifier = 'LIVE · provisional' if self.frames[ch]['live'] else 'FINAL'
        message = result['warning'] or result['status']
        if result.get('chi2_red') is not None:
            message += f' · χ²ᵣₑᏧ = {result["chi2_red"]:.2f}'
        self.roi_messages[ch].setText(f'{qualifier} · {result["native_bins"]} native bins · {message}')
        for key, error in (('centroid_keV', 'centroid_uncertainty_keV'), ('fwhm_keV', 'fwhm_uncertainty_keV'), ('net_counts', 'net_counts_uncertainty')):
            uncertainty = result.get(error)
            self.roi_metrics[ch][key].setToolTip(f'Statistical 1σ uncertainty: ± {uncertainty:.6g}' if uncertainty is not None else 'Statistical uncertainty unavailable for this estimate')
        if self.workspace_ready:
            self.sync_active_roi(ch, result)
        self.refresh_spectrum_items()

    def zoom_roi(self, ch):
        if self.roi_enabled[ch] and ch == self.foreground_channel:
            low, high = self.roi_regions[ch].getRegion()
            padding = max((high-low) * .15, self.calibrations[ch][1] * 4)
            self.plots[ch].setXRange(max(0., low-padding), min(4000., high+padding), padding=0)

    def scale_y(self, ch):
        if ch != self.foreground_channel or ch not in self.host_histograms:
            return
        edges, histogram = self.host_histograms[ch]
        low, high = self.plots[ch].getViewBox().viewRange()[0]
        visible = (edges[:-1] < high) & (edges[1:] > low)
        highest = float(histogram[visible].max()) if visible.any() else 0.
        lowest = float(histogram[visible].min()) if visible.any() else 0.
        if self.background_histogram is not None:
            bg_edges, data = self.background_histogram
            bg_visible = (bg_edges[:-1] < high) & (bg_edges[1:] > low)
            if bg_visible.any():
                highest = max(highest, float(data[bg_visible].max()))
        self.plots[ch].setLimits(yMin=None if lowest < 0 else 0)
        self.plots[ch].setYRange(min(0., lowest*1.12), max(1., highest * 1.12), padding=0)

    def cursor_moved(self, ch, position):
        if ch != self.foreground_channel:
            return
        plot = self.plots[ch]
        if ch not in self.host_histograms or not plot.getViewBox().sceneBoundingRect().contains(position):
            self.cursors[ch].hide()
            return
        energy = plot.getViewBox().mapSceneToView(position).x()
        edges, histogram = self.host_histograms[ch]
        index = int(np.searchsorted(edges, energy, side='right') - 1)
        if not 0 <= index < len(histogram):
            self.cursors[ch].hide()
            return
        self.cursors[ch].setValue(energy)
        self.cursors[ch].show()
        background = ''
        if self.background_histogram is not None:
            background = f'  ·  background {self.background_histogram[1][index]:,.3f} counts'
        self.cursor_labels[ch].setText(f'{energy:,.3f} keV  ·  foreground {float(histogram[index]):,.3f} counts{background}  ·  '
                                       f'bin {edges[index]:,.3f}–{edges[index+1]:,.3f} keV')

    def scan(self):
        self.port.clear()
        for item in list_ports.comports():
            self.port.addItem(f'{item.device} · {item.description}', item.device)
            if item.vid == 0x10c4 and item.pid == 0xea60:
                self.port.setCurrentIndex(self.port.count() - 1)

    def update_device_settings(self, settings):
        """Reflect verified source/polarity independently, without sending commands."""
        for ch, values in settings.items():
            if 'energy' in values and self.calibrated[ch] and self.calibration_energy.get(ch) != values['energy']:
                self.calibrated[ch] = False
                self.scale_labels[ch].setText('ENERGY SETTINGS CHANGED · RECALIBRATE')
            self.device_settings[ch] = {**self.device_settings.get(ch, {}), **values}
            polarity = self.device_settings[ch].get('polarity')
            if polarity is not None:
                blocker = QtCore.QSignalBlocker(self.polarity_menus[ch])
                self.polarity_menus[ch].setCurrentIndex(1 if polarity in ('N', 'negative', '-') else 0)
                del blocker
        sources = {self.device_settings.get(ch, {}).get('source', (None,))[0] for ch in (1, 2)}
        index = 2 if sources == {0} else 1 if sources == {1} else 0
        blocker = QtCore.QSignalBlocker(self.source_menu)
        self.source_menu.setCurrentIndex(index)
        del blocker

    def adc_polarity_summary(self):
        return ' · '.join(f'CH{ch}: {self.polarity_menus[ch].currentText()}' for ch in (1, 2))

    def controls(self):
        self.connect_button.setEnabled(not self.busy and (self.connected or self.port.count() > 0))
        self.connect_button.setText('Disconnect' if self.connected else 'Connect')
        self.port.setEnabled(not self.connected and not self.busy)
        self.scan_button.setEnabled(not self.connected and not self.busy)
        self.source_menu.setEnabled(self.connected and not self.busy and not self.running)
        self.hardware_clear.setEnabled(self.connected and not self.busy and not self.running)
        for ch in (1, 2):
            self.energy_actions[ch].setEnabled(self.connected and self.protocol_version >= 6 and not self.busy and not self.running and self.device_settings.get(ch, {}).get('energy') is not None)
            adc_input = self.device_settings.get(ch, {}).get('source', (None,))[0] == 0
            self.polarity_menus[ch].setEnabled(self.connected and not self.busy and not self.running and adc_input)
            self.clear_buttons[ch].setEnabled(self.connected and not self.busy and not self.running)
            self.export_buttons[ch].setEnabled(ch in self.frames)
            for key, button in self.roi_buttons[ch].items():
                button.setEnabled(ch in self.frames if key in ('auto', 'apply') else
                                  self.roi_regions[ch].isVisible() if key in ('zoom', 'remove') else True)
        for op, button in self.buttons.items():
            button.setEnabled(self.connected and not self.busy and
                              (op in ('stop', 'read') or not self.running or
                               self.independent_channel_control and self.channel_acquiring is not None and not all(self.channel_acquiring.values())))
        for ch in (1, 2):
            known = self.connected and self.independent_channel_control and self.channel_acquiring is not None
            acquiring = self.channel_acquiring[ch] if known else None
            title = 'RUNNING' if acquiring else 'STOPPED' if acquiring is False else 'STATE UNKNOWN' if self.connected else 'DISCONNECTED'
            self.channel_status_labels[ch].setText(f'CH{ch} · {title}')
            self.channel_status_labels[ch].setStyleSheet('color: #75dfb8;' if acquiring else 'color: #e5edf6;')
            for op in ('start', 'stop'):
                self.channel_buttons[ch, op].setEnabled(known and not self.busy and acquiring == (op == 'stop'))
        self.channel_capability_label.setText('Independent channel control' if self.connected and self.independent_channel_control else
                                              'Global control only' if self.connected else 'Connect to detect channel control')
        if self.workspace_ready:
            self.workspace_controls()
        self.spectrum_view_controls()

    def connection(self):
        if self.workspace_ready and not self.connected and self.frames:
            try:
                self.autosave_session('before-connect')
            except (ValueError, OSError) as exc:
                QtWidgets.QMessageBox.warning(self, 'Automatic save failed', str(exc))
                return
        self.busy = True
        self.status.setText('Connecting: draining previous frame / baud recovery (10 seconds)…'
                            if not self.connected else 'Disconnecting…')
        self.worker.send('disconnect' if self.connected else 'connect', self.port.currentData())
        self.controls()

    def action(self, op, argument=None):
        if op == 'clear' and QtWidgets.QMessageBox.question(
                self, 'Clear both channels?', 'This erases both FPGA histograms and counters. '
                'Save first if needed. Clear now?') != QtWidgets.QMessageBox.Yes:
            return
        if self.workspace_ready:
            self.worker.preservation_context = self.session_payload(include_frames=False)
            if op == 'stop':
                self._final_autosave_pending = True
                self._stopped_channels = (1, 2) if argument is None else (argument,)
        self.busy = True
        if argument is not None:
            self.worker.send(op, argument)
        else:
            self.worker.send(op)
        self.controls()

    def channel_action(self, operation, channel):
        if not self.connected or self.busy or not self.independent_channel_control or self.channel_acquiring is None:
            return
        if self.channel_acquiring[channel] == (operation == 'start'):
            return
        self.action(operation, channel)

    def reflect_acquisition_state(self, value):
        self.protocol_version = value.get('protocol_version', self.protocol_version)
        self.running = bool(value['running'])
        self.independent_channel_control = bool(value.get('independent_channel_control', False))
        channels = value.get('channel_acquiring')
        self.channel_acquiring = {ch: bool(channels.get(ch, channels.get(str(ch), False))) for ch in (1, 2)} if self.independent_channel_control and channels is not None else None
        if 'control_events' in value:
            self.control_events = value['control_events']
        self.link_baudrate = value.get('baudrate', self.link_baudrate)

    def baud_label(self):
        return '2 Mbaud' if self.link_baudrate == 2000000 else f'{self.link_baudrate:,} baud'

    def acquisition_summary(self):
        if self.channel_acquiring is not None:
            return ' · '.join(f'CH{ch}: {"acquiring" if self.channel_acquiring[ch] else "stopped"}' for ch in (1, 2))
        return 'Acquiring (global state)' if self.running else 'Stopped (global state)'

    def source_selected(self, index):
        item = self.source_menu.itemData(index)
        if not item:
            self.update_device_settings({})
            return
        op, arg = item
        requested = 0 if op == 'adc' else 1
        if (not self.connected or self.busy or self.running or
                all(self.device_settings.get(ch, {}).get('source', (None,))[0] == requested for ch in (1, 2))):
            self.update_device_settings({})
            return
        self.action(op, arg)

    def polarity_selected(self, ch, polarity):
        settings = self.device_settings.get(ch, {})
        if (polarity not in ('P', 'N') or not self.connected or self.busy or self.running or
                settings.get('source', (None,))[0] != 0 or settings.get('polarity') == polarity):
            self.update_device_settings({ch: settings})
            return
        self.action('polarity', (ch, polarity))

    def displayed_counts(self, frame):
        counts = frame['counts'].astype(np.uint64)
        baseline = self.baselines.get(frame['channel'])
        if baseline is not None:
            if np.any(counts < baseline):
                raise ValueError('FPGA counts decreased below the host baseline; reconnect to reset continuity')
            counts = counts - baseline.astype(np.uint64)
        return counts

    def open_energy_settings(self, ch):
        current = self.device_settings.get(ch, {}).get('energy')
        if not current or not self.connected or self.protocol_version < 6 or self.running or self.busy:
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f'Channel {ch}: Energy processing')
        layout = QtWidgets.QVBoxLayout(dialog)
        note = QtWidgets.QLabel('Applying changes saves the current measurement and clears both channels.\nRecalibrate this channel after changing its energy settings.')
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QtWidgets.QFormLayout()
        layout.addLayout(form)
        mode = QtWidgets.QComboBox()
        mode.addItem('Charge integration · PZ off', 'charge')
        mode.addItem('Trapezoidal · existing PZ profile', 'trapezoidal')
        mode.setCurrentIndex(mode.findData(current['mode']))
        preset = QtWidgets.QComboBox()
        preset.addItem('Custom / current settings', None)
        for name in ENERGY_PRESETS:
            preset.addItem(name, name)
        pre = QtWidgets.QDoubleSpinBox()
        pre.setObjectName('pre_trigger_us')
        pre.setRange(0, 1.26); pre.setDecimals(2); pre.setSingleStep(.02); pre.setSuffix(' µs')
        pre.setValue(current['pretrigger_samples']*.02)
        pre.setToolTip('Integration before the trigger: 0–1.26 µs, in 0.02 µs steps.')
        post = QtWidgets.QDoubleSpinBox()
        post.setObjectName('post_trigger_us')
        post.setRange(.02, 81.92); post.setDecimals(2); post.setSingleStep(.02); post.setSuffix(' µs')
        post.setValue((current['gate_samples']-current['pretrigger_samples'])*.02)
        post.setToolTip('Integration after the trigger, in 0.02 µs steps. Pre-trigger plus post-trigger must not exceed 81.92 µs.')
        total = QtWidgets.QLabel()
        total.setObjectName('total_gate')
        validation = QtWidgets.QLabel()
        validation.setWordWrap(True)
        validation.setStyleSheet('color: #ffbc75;')
        shift = QtWidgets.QSpinBox()
        shift.setRange(0, 15); shift.setValue(current['area_shift'])
        shift.setToolTip('Divide pulse area by 2 raised to this value before Q3 histogram mapping. Changing this requires recalibration.')
        form.addRow('Processing', mode); form.addRow('Detector preset', preset)
        form.addRow('Pre-trigger integration', pre); form.addRow('Post-trigger integration', post)
        form.addRow('Total integration', total)
        form.addRow('Area normalization shift', shift)
        layout.addWidget(validation)
        def apply_preset():
            name = preset.currentData()
            if name:
                values = ENERGY_PRESETS[name]
                mode.setCurrentIndex(mode.findData(values['mode']))
                pre.blockSignals(True); post.blockSignals(True)
                pre.setValue(values['pretrigger_samples']*.02)
                post.setValue((values['gate_samples']-values['pretrigger_samples'])*.02)
                pre.blockSignals(False); post.blockSignals(False)
                shift.setValue(values['area_shift'])
                update_total()
        def mode_changed():
            for widget in (post, pre, shift):
                widget.setEnabled(mode.currentData() == 'charge')
        preset.currentIndexChanged.connect(apply_preset)
        mode.currentIndexChanged.connect(mode_changed)
        mode_changed()
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.Ok | QtWidgets.QDialogButtonBox.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.Ok).setText('Save measurement and apply')
        layout.addWidget(buttons)
        def configuration_from_fields():
            pre_samples = round(pre.value()/.02)
            post_samples = round(post.value()/.02)
            for title, value, samples in (('Pre-trigger', pre.value(), pre_samples),
                                           ('Post-trigger', post.value(), post_samples)):
                if abs(value-samples*.02) > 1e-6:
                    raise ValueError(f'{title} must be a multiple of 0.02 µs (20 ns)')
            if pre_samples+post_samples > 4096:
                raise ValueError('Total integration must not exceed 81.92 µs')
            configuration = dict(mode=mode.currentData(), gate_samples=pre_samples+post_samples,
                                 pretrigger_samples=pre_samples, area_shift=shift.value())
            encode_energy(configuration)
            return configuration
        def update_total():
            total.setText(f'{pre.value()+post.value():.2f} µs')
            try:
                configuration = configuration_from_fields()
                total.setText(f'{pre.value()+post.value():.2f} µs ({configuration["gate_samples"]} samples)')
                validation.clear()
                buttons.button(QtWidgets.QDialogButtonBox.Ok).setEnabled(True)
            except ValueError as exc:
                validation.setText(str(exc))
                buttons.button(QtWidgets.QDialogButtonBox.Ok).setEnabled(False)
        def custom_timing():
            preset.blockSignals(True)
            preset.setCurrentIndex(0)
            preset.blockSignals(False)
            update_total()
        pre.valueChanged.connect(custom_timing)
        post.valueChanged.connect(custom_timing)
        update_total()
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        if dialog.exec() != QtWidgets.QDialog.Accepted:
            return
        try:
            configuration = configuration_from_fields()
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, 'Energy settings', str(exc))
            return
        if all(current[key] == value for key, value in configuration.items()):
            return
        self.action('energy', (ch, configuration))

    def calibrate(self, ch):
        calibration = [widget.value() for widget in self.inputs[ch]]
        try:
            energy_histogram(np.zeros(16384, dtype=np.uint64), calibration)
        except ValueError as exc:
            QtWidgets.QMessageBox.warning(self, 'Calibration', str(exc))
            return
        old_offset, old_gain = self.calibrations[ch]
        native_roi = [(value-old_offset)/old_gain for value in self.roi_regions[ch].getRegion()]
        has_roi = self.roi_enabled[ch]
        if self.workspace_ready:
            self.recalibrate_rois(ch, (old_offset, old_gain), calibration)
        self.calibrations[ch] = calibration
        self.calibrated[ch] = True
        self.calibration_energy[ch] = (self.frames.get(ch) or {}).get('energy', self.device_settings.get(ch, {}).get('energy'))
        self.scale_labels[ch].setText('USER CALIBRATION')
        self.scale_labels[ch].setStyleSheet('color: #75dfb8; font-size: 11px;')
        self.plots[ch].setLabel('bottom', 'Energy', units='keV')
        if has_roi:
            bounds = [calibration[0] + calibration[1]*value for value in native_roi]
            if bounds[0] >= 4000 or bounds[1] <= 0:
                self.remove_roi(ch)
                self.roi_messages[ch].setText('Previous ROI is outside the calibrated 0–4000 keV range.')
            else:
                self.set_roi(ch, *bounds)
        if ch in self.frames:
            self.draw(self.frames[ch])

    def open_calibration_wizard(self, ch):
        def get_centroid(channel):
            res = self.roi_results.get(channel)
            if res and res.get('centroid_keV') is not None:
                return res['centroid_keV']
            return None

        dialog = TwoPointCalibrationDialog(
            ch, self.calibrations[ch], get_centroid, parent=self
        )
        dialog.calibration_applied.connect(self.apply_wizard_calibration)
        dialog.exec()

    def apply_wizard_calibration(self, ch, offset, gain):
        self.inputs[ch][0].setValue(offset)
        self.inputs[ch][1].setValue(gain)
        self.calibrate(ch)

    def regroup(self, ch, group):
        self.grouping[ch] = group
        if ch in self.frames:
            self.draw(self.frames[ch])

    def draw(self, frame):
        ch = frame['channel']
        counts, noise = self.analysis_data(ch) if self.workspace_ready else (self.displayed_counts(frame), None)
        edges, histogram, under, over = energy_histogram(counts, self.calibrations[ch], self.grouping[ch])
        self.host_histograms[ch] = (edges, histogram)
        if noise is not None:
            _, _, under, over = energy_histogram(self.displayed_counts(frame), self.calibrations[ch], self.grouping[ch])
        self.range_counts[ch] = (under, over)
        self.curves[ch].setData(edges, histogram)
        mode = ('SAVED LIVE' if frame['live'] else 'FILE') if frame.get('loaded') else ('LIVE' if frame['live'] else 'FINAL')
        self.mode_labels[ch].setText(f'{mode} · #{frame["sequence"]}')
        s = frame['stats']
        seconds = s['acquisition_ticks'] / 50000000
        total = int(self.displayed_counts(frame).sum(dtype=np.uint64))
        integrity = 'CRC OK' if frame.get('raw') else 'imported spectrum'
        rate = f' · {self.last_rates[ch]:,.1f} counts/s' if self.workspace_ready and ch in self.last_rates else ''
        timing = f'acquisition-enabled time {seconds:.3f} s' if not frame.get('loaded') or frame.get('raw') else (
            f'live {frame.get("live_time", 0):.3f} s · real {frame.get("real_time", 0):.3f} s')
        self.labels[ch].setText(f'{total:,} counts · under/over {under:,}/{over:,} · {timing} '
                               f'· pileup {s["pileup_rejected"]} · clip {s["clip_rejected"]} '
                               f'· dropped {s["dropped"]} · saturated {s["bin_saturated"]} · {integrity}{rate}'
                               + (' · BACKGROUND SUBTRACTED' if noise is not None else ''))
        self.update_roi(ch)
        if self.workspace_ready:
            self.update_all_rois(ch)
        if self.workspace_ready and ch != self.foreground_channel and self.background_menu.currentData() == ch and self.foreground_channel in self.frames:
            self.draw(self.frames[self.foreground_channel])
        else:
            self.refresh_spectrum_view()

    def reset_spectra(self, message):
        if self.workspace_ready:
            self.reset_workspace_spectra()
            self._resetting_rois = True
        self.frames.clear()
        self.baselines.clear()
        self.host_histograms.clear()
        self.range_counts.clear()
        self.previous_time.clear()
        for ch in (1, 2):
            self.curves[ch].setData([], [])
            if ch in self.fit_curves:
                self.fit_curves[ch].hide()
            self.plots[ch].setYRange(0, 1, padding=0)
            self.mode_labels[ch].setText('NO DATA')
            self.labels[ch].setText(message)
            self.cursors[ch].hide()
            self.cursor_labels[ch].setText('Move over the spectrum to inspect energy and counts')
            self.remove_roi(ch)
        if self.workspace_ready:
            self._resetting_rois = False
        self.refresh_spectrum_view()

    @QtCore.Slot(str, object)
    def receive(self, kind, value):
        if kind == 'connected':
            self.connected = True
            self.reflect_acquisition_state(value)
            self.control_events = []
            self.reset_spectra('Connected · awaiting spectrum')
            self.status.setText(f'Firmware v{value.get("protocol_version", 5)} verified · {self.baud_label()} · {self.acquisition_summary()}')
            self.device_settings.clear()
            self.update_device_settings(value['settings'])
        elif kind == 'disconnected':
            self.connected = self.running = False
            self.independent_channel_control = False
            self.channel_acquiring = None
            if self.workspace_ready:
                self.preset.started = None
            self.status.setText('Disconnected · FPGA acquisition unchanged')
        elif kind == 'recovering':
            self._busy_before_recovery = self.busy
            self.busy = True
            self.status.setText('UART frame interrupted · resynchronizing (about 10 seconds) · FPGA acquisition continues unchanged…')
        elif kind == 'recovered':
            self.reflect_acquisition_state(value)
            self.busy = self._busy_before_recovery
            self.status.setText(f'UART recovered at {self.baud_label()} · {self.acquisition_summary()} · retrying spectrum read')
        elif kind == 'acquisition_state':
            self.reflect_acquisition_state(value)
            operation, channel = value.get('operation'), value.get('channel')
            if self.workspace_ready and operation == 'start' and channel is None:
                self.arm_preset(value.get('baseline_counts'))
            elif self.workspace_ready and operation in ('start', 'stop') and channel is not None:
                self.preset.started = None
                self.preset_progress.setValue(0)
                self.preset_progress.setFormat('Manual channel control · Start both preset cancelled')
            if operation == 'stop':
                self._stopped_channels = (1, 2) if channel is None else (channel,)
                self._final_autosave_pending = True
            if operation:
                self.status.setText(f'{self.acquisition_summary()} · {"reading FINAL spectrum…" if operation == "stop" else self.baud_label()}')
        elif kind == 'running':
            self.running = value
            if self.workspace_ready and value:
                self.arm_preset()
            self.status.setText('Acquiring · 1 Hz/channel · 2 Mbaud' if value else 'Stopped · reading FINAL spectra…')
        elif kind == 'settings':
            self.reset_spectra('Synthetic ready · automatically cleared · zero counts')
            self.update_device_settings(value)
            self.status.setText('Synthetic ready · automatically cleared · CH1: amplitude 101 / 2 kHz; CH2: amplitude 401 / 1 kHz · positive')
        elif kind == 'adc':
            self.reset_spectra('ADC source ready · automatically cleared · zero counts')
            self.update_device_settings(value)
            self.status.setText(f'ADC source ready · automatically cleared · {self.adc_polarity_summary()}')
        elif kind == 'polarity':
            ch = value['channel']
            self.reset_spectra(f'Channel {ch} pulse polarity changed · automatically cleared · zero counts')
            self.update_device_settings(value['settings'])
            self.status.setText(f'ADC pulse polarity updated · automatically cleared · {self.adc_polarity_summary()}')
        elif kind == 'energy_changed':
            ch = value['channel']
            self.reset_spectra('Energy settings updated · both channels saved and cleared')
            self.calibrated[ch] = False
            self.scale_labels[ch].setText('ENERGY SETTINGS CHANGED · RECALIBRATE')
            self.update_device_settings(value['settings'])
            self.status.setText(f'CH{ch} energy settings verified · recalibration required')
        elif kind == 'cleared':
            msg = value if isinstance(value, str) else 'Explicitly cleared · zero counts'
            self.reset_spectra(msg)
            if not isinstance(value, str):
                self.status.setText('Both channels cleared')
        elif kind == 'frame':
            ch = value['channel']
            if self.calibrated[ch] and self.calibration_energy.get(ch) != value.get('energy'):
                self.calibrated[ch] = False
                self.scale_labels[ch].setText('ENERGY SETTINGS CHANGED · RECALIBRATE')
                self.scale_labels[ch].setStyleSheet('color: #f2c66d; font-size: 11px;')
            self.frames[ch] = value
            if self.workspace_ready:
                self.update_rate(value)
            settings = {}
            if 'source' in value:
                settings['source'] = value['source']
            if 'negative' in value:
                settings['polarity'] = 'N' if value['negative'] else 'P'
            for key in ('trigger', 'stats', 'energy'):
                if key in value:
                    settings[key] = value[key]
            self.update_device_settings({value['channel']: settings})
            self.draw(value)
            if value['live']:
                ch = value['channel']
                now = time.monotonic()
                previous = self.previous_time.get(ch)
                self.previous_time[ch] = now
                if previous:
                    self.status.setText(f'{self.acquisition_summary()} · CH{ch} achieved {1/(now-previous):.2f} Hz · {self.baud_label()}')
            if self.workspace_ready:
                self.check_preset()
        elif kind == 'preserved':
            self.last_autosave = value
            self.status.setText(f'Previous measurement saved: {value}')
        elif kind == 'readback':
            self.update_device_settings(value)
            self.status.setText('ADC settings read back from hardware')
        elif kind == 'error':
            self.connected = self.running = self.busy = False
            self.channel_acquiring = None
            self.status.setText('Link state UNKNOWN · reconnect to identify; hardware may still be acquiring')
            QtWidgets.QMessageBox.warning(self, 'UART error', value)
        elif kind == 'complete':
            self.busy = False
            if self.workspace_ready and value == 'stop':
                self.finish_acquisition()
        self.controls()

    def save(self, ch):
        if ch not in self.frames:
            return
        directory = QtWidgets.QFileDialog.getExistingDirectory(self, f'Export channel {ch} spectrum')
        if not directory:
            return
        root = Path(directory) / f'ch{ch}-{time.time_ns()}'
        root.mkdir(exist_ok=False)
        for ch, frame in [(ch, self.frames[ch])]:
            self.update_roi(ch)
            if frame.get('raw'):
                (root / f'ch{ch}.bin').write_bytes(frame['raw'])
            metadata = {k: v for k, v in frame.items() if k not in ('raw', 'counts')}
            edges, histogram, under, over = energy_histogram(self.displayed_counts(frame), self.calibrations[ch], self.grouping[ch])
            baseline = self.baselines.get(ch, np.zeros(len(frame['counts']), dtype=np.uint64))
            np.save(root / f'ch{ch}-baseline.npy', baseline)
            metadata.update(calibration=self.calibrations[ch], calibrated=self.calibrated[ch],
                            fpga_bins_per_host_bin=self.grouping[ch], host_range_keV=[0, 4000],
                            underflow=under, overflow=over,
                            host_total=int(histogram.sum(dtype=np.uint64)),
                            roi_analysis=self.roi_results.get(ch),
                            roi_analyses=[dict(entry['result'], name=entry['name']) for entry in self.rois[ch]],
                            baseline_file=f'ch{ch}-baseline.npy', raw_frame_contains_full_fpga_counts=bool(frame.get('raw')))
            (root / f'ch{ch}.json').write_text(json.dumps(metadata, indent=2))
            offset, gain = self.calibrations[ch]
            with (root / f'ch{ch}.csv').open('w', newline='') as handle:
                writer = csv.writer(handle)
                writer.writerow(['energy_low_keV', 'energy_high_keV', 'counts'])
                writer.writerows((edges[i], edges[i+1], int(n))
                                 for i, n in enumerate(histogram))
            write_spectrum(root/f'ch{ch}.spe', dict(frame, counts=self.displayed_counts(frame)), self.calibrations[ch], self.calibrated[ch], self.roi_records(ch), self.sample_metadata)
        self.status.setText(f'Saved {root}')

    def closeEvent(self, event):
        if self.worker.thread.is_alive():
            if not self.closing:
                self.closing = True
                self.worker.send('quit')
            event.ignore()
            self.close_timer.start(100)
        else:
            self.close_timer.stop()
            if not self.cleanup_done:
                self.cleanup_done = True
                self.preset_timer.stop()
                self.worker.event.disconnect(self.receive)
                for proxy in self.mouse_proxies.values():
                    proxy.timer.stop()
                    proxy.disconnect()
            event.accept()


if __name__ == '__main__':
    multiprocessing.freeze_support()
    app = QtWidgets.QApplication(sys.argv)
    app.setApplicationName(APP_NAME)
    app.setApplicationDisplayName(APP_NAME)
    app.setOrganizationName('DNRI')
    app.setStyle('Fusion')
    window = Window()
    window.show()
    sys.exit(app.exec())
