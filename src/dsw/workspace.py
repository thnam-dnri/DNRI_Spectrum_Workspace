"""Session, multi-ROI, reference and acquisition workflows for the DSW window."""
import csv
import json
import time
from pathlib import Path

import numpy as np
import pyqtgraph as pg
from PySide6 import QtCore, QtGui, QtWidgets

from acquisition import AcquisitionPreset
from comparison import exposure_scale, subtract_background
from peak_analysis import analyze_roi, automatic_roi, _smooth
from spectrum_io import (OPEN_FILTER, SAVE_FILTER, duration, frame_record, restore_frame,
                         read_session, read_spectrum, read_spectra, write_session, write_spectrum)


ROI_COLORS = ('#47b9ff', '#ffba58', '#75dfb8', '#f58ed0', '#b79cff', '#e3d96c', '#8ad7e8')


def clean_result(result):
    return {key: value for key, value in result.items() if key not in ('fit_curve', 'residual_curve')}


class WorkspaceMixin:
    def init_workspace(self):
        self.workspace_ready = False
        self.rois = {1: [], 2: []}
        self.active_roi = {1: None, 2: None}
        self.roi_residuals = {}
        self.references, self.backgrounds = {}, {}
        self.reference_curves = {ch: self.plots[ch].plot(pen=pg.mkPen('#c0a0ff', width=1.5), stepMode='center') for ch in (1, 2)}
        self.comparison_widgets = {}
        self._roi_sync = self._resetting_rois = False
        self._next_roi = 0
        self.sample_metadata = dict(sample='', detector='', notes='')
        self.last_autosave = None
        self._final_autosave_pending = False
        self.preset = AcquisitionPreset()
        self.last_rates, self._rate_samples = {}, {}

        file_menu = QtWidgets.QMenu('File', self)
        self.menuBar().insertMenu(self.menuBar().actions()[0], file_menu)
        self.open_session_action = file_menu.addAction('Open session or spectrum…', self.open_document)
        self.open_session_action.setShortcut(QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.Open))
        self.save_session_action = file_menu.addAction('Save DSW session…', self.save_session)
        self.save_session_action.setShortcut(QtGui.QKeySequence(QtGui.QKeySequence.StandardKey.Save))
        file_menu.addAction('Sample and detector notes…', self.edit_notes)
        file_menu.addSeparator()
        self.open_spectrum_actions, self.save_spectrum_actions, self.adc_actions = {}, {}, {}
        for ch in (1, 2):
            self.open_spectrum_actions[ch] = file_menu.addAction(f'Open spectrum into channel {ch}…', lambda checked=False, ch=ch: self.open_spectrum(ch))
            self.save_spectrum_actions[ch] = file_menu.addAction(f'Export channel {ch} as SPE / CHN / N42 / CSV…', lambda checked=False, ch=ch: self.export_spectrum(ch))
        file_menu.addSeparator()
        file_menu.addAction('Open latest automatic save', self.open_last_autosave)
        device_menu = self.device_menu
        device_menu.addSeparator()
        for ch in (1, 2):
            self.adc_actions[ch] = device_menu.addAction(f'Channel {ch}: ADC trigger and noise…', lambda checked=False, ch=ch: self.show_adc_setup(ch))

        row = QtWidgets.QHBoxLayout()
        row.addWidget(QtWidgets.QLabel('Start both preset'))
        self.preset_mode = QtWidgets.QComboBox()
        self.preset_mode.setToolTip('This preset starts/stops both channels. Dedicated CH1/CH2 buttons use manual control and cancel this target.')
        for name, value in (('Manual', 'manual'), ('Duration', 'duration'), ('Counts', 'counts')):
            self.preset_mode.addItem(name, value)
        row.addWidget(self.preset_mode)
        self.preset_target = QtWidgets.QDoubleSpinBox()
        self.preset_target.setRange(.1, 1e12)
        self.preset_target.setValue(60.)
        row.addWidget(self.preset_target)
        self.preset_channel = QtWidgets.QComboBox()
        for title, value in (('CH1', 1), ('CH2', 2), ('Combined', 0)):
            self.preset_channel.addItem(title, value)
        row.addWidget(self.preset_channel)
        self.preset_progress = QtWidgets.QProgressBar()
        self.preset_progress.setRange(0, 1000)
        self.preset_progress.setValue(0)
        self.preset_progress.setFormat('Manual acquisition')
        row.addWidget(self.preset_progress, 1)
        saved = QtWidgets.QLabel('Final spectra save automatically')
        saved.setObjectName('muted')
        row.addWidget(saved)
        self.centralWidget().layout().insertLayout(2, row)
        self.preset_mode.currentIndexChanged.connect(self.preset_controls)
        self.preset_target.valueChanged.connect(self.preset_summary)
        self.preset_channel.currentIndexChanged.connect(self.preset_summary)
        self.preset_controls()
        self.preset_timer = QtCore.QTimer(self)
        self.preset_timer.setInterval(100)
        self.preset_timer.timeout.connect(self.check_preset)
        self.preset_timer.start()

        self.peaks_dock = QtWidgets.QDockWidget('Peaks, references and background', self)
        self.peaks_dock.setObjectName('peaksDock')
        self.peaks_tabs = QtWidgets.QTabWidget()
        self.peaks_scroll = QtWidgets.QScrollArea()
        self.peaks_scroll.setWidgetResizable(True)
        self.peaks_scroll.setMinimumHeight(80)
        self.peaks_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.peaks_scroll.setStyleSheet('QScrollArea, QScrollArea > QWidget > QWidget { background: #182334; }')
        self.peaks_scroll.setWidget(self.peaks_tabs)
        self.peaks_dock.setWidget(self.peaks_scroll)
        self.peaks_dock.visibilityChanged.connect(self.update_workspace_minimum)
        self.addDockWidget(QtCore.Qt.DockWidgetArea.BottomDockWidgetArea, self.peaks_dock)
        self.peaks_dock.hide()
        view_menu = self.menuBar().addMenu('View')
        view_menu.addAction(self.peaks_dock.toggleViewAction())
        self.peak_tables = {}
        for ch in (1, 2):
            tab = QtWidgets.QWidget()
            tab.setObjectName('workspaceTab')
            column = QtWidgets.QVBoxLayout(tab)
            actions = QtWidgets.QHBoxLayout()
            for title, function in (('Add ROI', lambda checked=False, ch=ch: self.add_roi(ch)),
                                    ('Find peaks', lambda checked=False, ch=ch: self.find_all_peaks(ch)),
                                    ('Remove selected', lambda checked=False, ch=ch: self.remove_roi(ch)),
                                    ('Fit diagnostics', lambda checked=False, ch=ch: self.show_diagnostics(ch)),
                                    ('Export peak table', lambda checked=False, ch=ch: self.export_peak_table(ch))):
                button = QtWidgets.QPushButton(title)
                button.clicked.connect(function)
                actions.addWidget(button)
            actions.addStretch()
            column.addLayout(actions)
            table = QtWidgets.QTableWidget(0, 11)
            table.setHorizontalHeaderLabels(['ROI', 'Low keV', 'High keV', 'Method', 'Centroid ± σ keV',
                                             'FWHM ± σ keV', 'Net area ± σ', 'Resolution %', 'Rate / s', 'χ² / dof', 'Status'])
            table.setSelectionBehavior(QtWidgets.QAbstractItemView.SelectionBehavior.SelectRows)
            table.setSelectionMode(QtWidgets.QAbstractItemView.SelectionMode.SingleSelection)
            table.setMinimumHeight(130)
            table.horizontalHeader().setSectionResizeMode(QtWidgets.QHeaderView.ResizeMode.ResizeToContents)
            table.itemSelectionChanged.connect(lambda ch=ch: self.table_selection_changed(ch))
            table.itemChanged.connect(lambda item, ch=ch: self.table_item_changed(ch, item))
            self.peak_tables[ch] = table
            column.addWidget(table)
            widgets = {}
            for kind, store in (('reference', self.references), ('background', self.backgrounds)):
                compare = QtWidgets.QHBoxLayout()
                load = QtWidgets.QPushButton(f'Load {kind}…')
                load.clicked.connect(lambda checked=False, ch=ch, kind=kind: self.load_reference(ch, kind))
                compare.addWidget(load)
                remove = QtWidgets.QPushButton('Remove')
                remove.clicked.connect(lambda checked=False, ch=ch, kind=kind: self.clear_reference(ch, kind))
                compare.addWidget(remove)
                norm = QtWidgets.QComboBox()
                for name, value in (('Normalize by exposure', 'exposure'), ('Unscaled counts', 'unscaled'), ('Manual scale', 'manual')):
                    norm.addItem(name, value)
                compare.addWidget(norm)
                scale = QtWidgets.QDoubleSpinBox()
                scale.setDecimals(8)
                scale.setRange(.00000001, 1e8)
                scale.setValue(1.)
                compare.addWidget(scale)
                calibration = QtWidgets.QPushButton('Calibration…')
                calibration.setStyleSheet('padding: 5px 7px;')
                calibration.clicked.connect(lambda checked=False, ch=ch, kind=kind: self.edit_reference_calibration(ch, kind))
                compare.addWidget(calibration)
                norm.currentIndexChanged.connect(lambda index, ch=ch: self.redraw_channel(ch))
                scale.valueChanged.connect(lambda value, ch=ch: self.redraw_channel(ch))
                if kind == 'background':
                    subtract = QtWidgets.QCheckBox('Subtract selected background')
                    subtract.toggled.connect(lambda value, ch=ch: self.redraw_channel(ch))
                    compare.addWidget(subtract)
                    widgets['subtract'] = subtract
                compare.addStretch()
                widgets[kind] = (norm, scale)
                column.addLayout(compare)
            message = QtWidgets.QLabel('Choose the background above the spectrum. Loaded files retain their own calibration. Statistical errors are 1σ.')
            message.setWordWrap(True)
            message.setObjectName('muted')
            widgets['message'] = message
            column.addWidget(message)
            self.comparison_widgets[ch] = widgets
            self.peaks_tabs.addTab(tab, f'Channel {ch}')
        self.workspace_ready = True
        self.worker.preservation_context = self.session_payload(include_frames=False)
        self.workspace_controls()

    def show_peaks(self, ch):
        self.peaks_tabs.setCurrentIndex(ch-1)
        self.peaks_dock.show()

    def update_workspace_minimum(self, visible):
        self.layout().activate()
        self.setMinimumHeight(max(780, self.minimumSizeHint().height()))

    def workspace_controls(self):
        self.open_session_action.setEnabled(not self.connected and not self.busy)
        self.save_session_action.setEnabled(bool(self.frames))
        for ch in (1, 2):
            self.open_spectrum_actions[ch].setEnabled(not self.connected and not self.busy)
            self.save_spectrum_actions[ch].setEnabled(ch in self.frames)
            self.adc_actions[ch].setEnabled(self.connected and self.device_settings.get(ch, {}).get('source', (None,))[0] == 0)
        for widget in (self.preset_mode, self.preset_target, self.preset_channel):
            widget.setEnabled(not self.running and not self.busy)

    def redraw_channel(self, ch):
        if self.workspace_ready and ch in self.frames:
            self.draw(self.frames[ch])
        elif self.workspace_ready:
            self.refresh_spectrum_view()

    def reference_scale(self, ch, kind):
        norm, scale = self.comparison_widgets[ch][kind]
        override = 1. if norm.currentData() == 'unscaled' else scale.value() if norm.currentData() == 'manual' else None
        store = self.backgrounds if kind == 'background' else self.references
        result = exposure_scale(self.frames[ch], store[ch], override)
        scale.setEnabled(norm.currentData() == 'manual')
        if norm.currentData() != 'manual':
            blocker = QtCore.QSignalBlocker(scale)
            scale.setValue(result[0])
            del blocker
        return result

    def annotate_background_result(self, ch, result, bounds, noise):
        if noise is None:
            result['background_subtracted'] = False
            return
        frame = self.frames[ch]
        energy = self.calibrations[ch][0]+self.calibrations[ch][1]*(np.arange(len(frame['counts']))+.5)
        selected = (energy >= max(0., bounds[0])) & (energy < min(4000., bounds[1]))
        foreground = int(self.displayed_counts(frame)[selected].sum(dtype=np.uint64))
        result['subtracted_roi_counts'] = result['gross_counts']
        result['spectrum_background_counts'] = foreground-result['gross_counts']
        result['gross_counts'] = foreground
        result['background_subtracted'] = True

    def analysis_data(self, ch):
        counts = self.displayed_counts(self.frames[ch])
        background = self.selected_background(ch) if self.workspace_ready else None
        if ch != self.foreground_channel:
            background = self.backgrounds.get(ch)
        if background is None or not self.comparison_widgets[ch]['subtract'].isChecked():
            return counts, None
        try:
            scale, basis = self.selected_background_scale(ch) if ch == self.foreground_channel else self.reference_scale(ch, 'background')
            result = subtract_background(counts, background['counts'], self.calibrations[ch],
                                         background['calibration'], scale)
            self.comparison_widgets[ch]['message'].setText(f'Background subtracted × {scale:.6g}; normalized by {basis}. Errors include reference rebinning correlations.')
            return result
        except ValueError as exc:
            self.comparison_widgets[ch]['message'].setText(f'Background subtraction unavailable: {exc}')
            return counts, None

    def update_comparison(self, ch):
        self.refresh_spectrum_view()

    def load_reference(self, ch, kind, filename=None, show_panel=True):
        if not filename:
            filename, _ = QtWidgets.QFileDialog.getOpenFileName(self, f'Load CH{ch} {kind}', '', OPEN_FILTER)
        if not filename:
            return
        try:
            frame = self.choose_spectrum(filename)
            if frame is None:
                return
            (self.backgrounds if kind == 'background' else self.references)[ch] = frame
            if ch == self.foreground_channel:
                self.refresh_background_choices(selected=kind, preserve=False)
            self.redraw_channel(ch)
            if show_panel:
                self.show_peaks(ch)
        except (ValueError, OSError, KeyError) as exc:
            QtWidgets.QMessageBox.warning(self, 'Spectrum import', str(exc))

    def clear_reference(self, ch, kind):
        (self.backgrounds if kind == 'background' else self.references).pop(ch, None)
        self.refresh_background_choices()
        self.redraw_channel(ch)

    def edit_reference_calibration(self, ch, kind):
        store = self.backgrounds if kind == 'background' else self.references
        if ch not in store:
            return
        frame = store[ch]
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f'CH{ch} · {kind} energy calibration')
        layout = QtWidgets.QFormLayout(dialog)
        offset, gain = QtWidgets.QDoubleSpinBox(), QtWidgets.QDoubleSpinBox()
        for widget in (offset, gain):
            widget.setDecimals(9)
        offset.setRange(-1e6, 1e6); gain.setRange(.000001, 1e6)
        offset.setValue(frame['calibration'][0]); gain.setValue(frame['calibration'][1])
        layout.addRow('Offset keV', offset); layout.addRow('Gain keV / native bin', gain)
        copy = QtWidgets.QPushButton('Use foreground calibration')
        copy.setEnabled(ch in self.frames and len(frame['counts']) == len(self.frames[ch]['counts']))
        copy.clicked.connect(lambda: (offset.setValue(self.calibrations[ch][0]), gain.setValue(self.calibrations[ch][1])))
        layout.addRow(copy)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Apply | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Apply).clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject); layout.addRow(buttons)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            frame['calibration'], frame['calibrated'] = [offset.value(), gain.value()], True
            self.redraw_channel(ch)

    def add_peak_at(self, ch, energy):
        if not self.workspace_ready or ch not in self.frames:
            return
        counts, _ = self.analysis_data(ch)
        bounds = self.plots[ch].getViewBox().viewRange()[0]
        try:
            roi = automatic_roi(np.maximum(counts, 0), self.calibrations[ch], target=energy, search_bounds=bounds,
                                search_radius=max(12*self.calibrations[ch][1], .04*(bounds[1]-bounds[0])))
            self.add_roi(ch, roi)
        except ValueError as exc:
            self.roi_messages[ch].setText(str(exc))

    def sync_active_roi(self, ch, result):
        if self._roi_sync or self._resetting_rois:
            return
        entry = next((entry for entry in self.rois[ch] if entry['id'] == self.active_roi[ch]), None)
        if entry is None:
            self._next_roi += 1
            color = ROI_COLORS[(self._next_roi-1) % len(ROI_COLORS)]
            region = pg.LinearRegionItem(bounds=(0, 4000), brush=pg.mkBrush(pg.mkColor(color).red(), pg.mkColor(color).green(), pg.mkColor(color).blue(), 20), pen=pg.mkPen(color))
            region.setZValue(9)
            curve = self.plots[ch].plot(pen=pg.mkPen(color, width=1.5, style=QtCore.Qt.PenStyle.DashLine))
            entry = dict(id=self._next_roi, name=f'Peak {len(self.rois[ch])+1}', color=color, region=region, curve=curve)
            region.sigRegionChanged.connect(lambda item, ch=ch, entry=entry: self.overlay_changed(ch, entry))
            self.plots[ch].addItem(region, ignoreBounds=True)
            self.rois[ch].append(entry)
            self.active_roi[ch] = entry['id']
        entry.update(bounds=list(self.roi_regions[ch].getRegion()), method=self.roi_methods[ch].currentData(),
                     smoothing=self.smoothing[ch].currentData(), result=clean_result(result),
                     fit=self.roi_fit_curves.get(ch), residual=self.roi_residuals.get(ch))
        blocker = QtCore.QSignalBlocker(entry['region'])
        entry['region'].setRegion(entry['bounds'])
        del blocker
        entry['region'].hide()
        entry['curve'].hide()
        self.refresh_peaks(ch)
        self.refresh_spectrum_items()

    def overlay_changed(self, ch, entry):
        if self._roi_sync:
            return
        entry['bounds'] = list(entry['region'].getRegion())
        self.activate_roi(ch, entry['id'])

    def add_roi(self, ch, bounds=None, name=None, method=None, smoothing=None, color=None):
        if ch not in self.frames or len(self.rois[ch]) >= 64:
            return
        for entry in self.rois[ch]:
            if entry['id'] == self.active_roi[ch]:
                entry['region'].show()
        self.active_roi[ch] = None
        if method is not None:
            blocker = QtCore.QSignalBlocker(self.roi_methods[ch])
            self.roi_methods[ch].setCurrentIndex(self.roi_methods[ch].findData(method))
            del blocker
        if smoothing is not None:
            blocker = QtCore.QSignalBlocker(self.smoothing[ch])
            self.smoothing[ch].setCurrentIndex(self.smoothing[ch].findData(smoothing))
            del blocker
        self.set_roi(ch, *(bounds or [widget.value() for widget in self.roi_inputs[ch]]))
        if name is not None:
            self.rois[ch][-1]['name'] = name
        if color is not None:
            entry = self.rois[ch][-1]
            entry['color'] = color
            shade = pg.mkColor(color); shade.setAlpha(24)
            entry['region'].setBrush(pg.mkBrush(shade))
            for line in entry['region'].lines:
                line.setPen(pg.mkPen(color))
            entry['curve'].setPen(pg.mkPen(color, width=1.5, style=QtCore.Qt.PenStyle.DashLine))
        self.update_all_rois(ch)
        self.show_peaks(ch)

    def activate_roi(self, ch, identity):
        entry = next((entry for entry in self.rois[ch] if entry['id'] == identity), None)
        if entry is None:
            return
        for candidate in self.rois[ch]:
            candidate['region'].setVisible(candidate['id'] != identity)
        self.active_roi[ch] = identity
        shade = pg.mkColor(entry['color']); shade.setAlpha(28)
        self.roi_regions[ch].setBrush(pg.mkBrush(shade))
        for line in self.roi_regions[ch].lines:
            line.setPen(pg.mkPen(entry['color'], width=1.2))
        self._roi_sync = True
        for widget, value in ((self.roi_methods[ch], entry['method']), (self.smoothing[ch], entry['smoothing'])):
            blocker = QtCore.QSignalBlocker(widget)
            widget.setCurrentIndex(widget.findData(value))
            del blocker
        self._roi_sync = False
        self.set_roi(ch, *entry['bounds'])
        self.update_all_rois(ch)

    def update_all_rois(self, ch):
        if self._roi_sync or ch not in self.frames:
            return
        counts, noise = self.analysis_data(ch)
        for entry in self.rois[ch]:
            if entry['id'] == self.active_roi[ch]:
                continue
            result = analyze_roi(counts, entry['bounds'], self.calibrations[ch], entry['smoothing'], entry['method'], noise=noise)
            self.annotate_background_result(ch, result, entry['bounds'], noise)
            entry['fit'] = result.pop('fit_curve', None)
            entry['residual'] = result.pop('residual_curve', None)
            result['calibrated'] = self.calibrated[ch]
            result['spectrum_mode'] = 'LIVE' if self.frames[ch]['live'] else 'FINAL'
            entry['result'] = result
            if entry['fit'] is not None:
                x, y = entry['fit']
                entry['curve'].setData(x, y*self.grouping[ch])
                entry['curve'].show()
            else:
                entry['curve'].hide()
        self.refresh_peaks(ch)
        self.refresh_spectrum_items()

    def remove_active_roi(self, ch):
        if self._resetting_rois:
            return
        retained = []
        for entry in self.rois[ch]:
            if entry['id'] == self.active_roi[ch]:
                self.plots[ch].removeItem(entry['region'])
                self.plots[ch].removeItem(entry['curve'])
            else:
                retained.append(entry)
        self.rois[ch] = retained
        self.active_roi[ch] = None
        if retained:
            self.activate_roi(ch, retained[-1]['id'])
        self.refresh_peaks(ch)

    def reset_workspace_spectra(self):
        self._resetting_rois = True
        for ch in (1, 2):
            for entry in self.rois[ch]:
                self.plots[ch].removeItem(entry['region'])
                self.plots[ch].removeItem(entry['curve'])
            self.rois[ch] = []
            self.active_roi[ch] = None
            self.reference_curves[ch].hide()
            self.refresh_peaks(ch)
        self._rate_samples.clear()
        self.last_rates.clear()
        self._resetting_rois = False

    def recalibrate_rois(self, ch, old, new):
        for entry in self.rois[ch]:
            if entry['id'] == self.active_roi[ch]:
                continue
            bounds = [new[0]+new[1]*(value-old[0])/old[1] for value in entry['bounds']]
            entry['bounds'] = [max(0., bounds[0]), min(4000., bounds[1])]
            blocker = QtCore.QSignalBlocker(entry['region'])
            entry['region'].setRegion(entry['bounds'])
            del blocker
        removed = [entry for entry in self.rois[ch] if entry['bounds'][0] >= entry['bounds'][1]]
        for entry in removed:
            self.plots[ch].removeItem(entry['region']); self.plots[ch].removeItem(entry['curve'])
            self.rois[ch].remove(entry)

    def refresh_peaks(self, ch):
        if ch not in self.peak_tables:
            return
        table = self.peak_tables[ch]
        self._roi_sync = True
        blocker = QtCore.QSignalBlocker(table)
        table.setRowCount(len(self.rois[ch]))
        exposure = duration(self.frames[ch]) if ch in self.frames else 0.
        for row, entry in enumerate(self.rois[ch]):
            result = entry.get('result', {})
            def measured(key, uncertainty=None):
                value = result.get(key)
                if value is None:
                    return '—'
                error = result.get(uncertainty) if uncertainty else None
                return f'{value:.3f} ± {error:.3f}' if error is not None else f'{value:.3f}'
            values = [entry['name'], f'{entry["bounds"][0]:.4f}', f'{entry["bounds"][1]:.4f}',
                      'Gaussian' if entry['method'] == 'gaussian' else 'Direct',
                      measured('centroid_keV', 'centroid_uncertainty_keV'), measured('fwhm_keV', 'fwhm_uncertainty_keV'),
                      measured('net_counts', 'net_counts_uncertainty'), measured('resolution_percent'),
                      f'{result["net_counts"]/exposure:.3f}' if exposure > 0 and result.get('net_counts') is not None else '—',
                      measured('chi2_red'), result.get('warning') or result.get('status', '')]
            for column, value in enumerate(values):
                item = QtWidgets.QTableWidgetItem(value)
                item.setToolTip(value)
                if column == 0:
                    item.setForeground(pg.mkBrush(entry['color']))
                if column == 8:
                    item.setToolTip('Net area / detector live time when recorded; otherwise real or acquisition-enabled time (uncorrected).')
                if column not in (0, 1, 2):
                    item.setFlags(item.flags() & ~QtCore.Qt.ItemFlag.ItemIsEditable)
                table.setItem(row, column, item)
            if entry['id'] == self.active_roi[ch]:
                table.selectRow(row)
        del blocker
        self._roi_sync = False

    def table_selection_changed(self, ch):
        if not self._roi_sync and self.peak_tables[ch].currentRow() >= 0:
            self.activate_roi(ch, self.rois[ch][self.peak_tables[ch].currentRow()]['id'])

    def table_item_changed(self, ch, item):
        if self._roi_sync:
            return
        entry = self.rois[ch][item.row()]
        if item.column() == 0:
            entry['name'] = item.text().strip() or 'Peak'
        else:
            try:
                low = float(self.peak_tables[ch].item(item.row(), 1).text())
                high = float(self.peak_tables[ch].item(item.row(), 2).text())
                if not 0 <= low < high <= 4000:
                    raise ValueError()
                entry['bounds'] = [low, high]
                self.activate_roi(ch, entry['id'])
            except ValueError:
                self.refresh_peaks(ch)

    def find_all_peaks(self, ch):
        if ch not in self.frames:
            return
        counts, _ = self.analysis_data(ch)
        y = _smooth(np.maximum(counts, 0), 5)
        x = self.calibrations[ch][0]+self.calibrations[ch][1]*(np.arange(len(y))+.5)
        bounds = self.plots[ch].getViewBox().viewRange()[0]
        candidates = np.flatnonzero((y > np.r_[-np.inf, y[:-1]]) & (y >= np.r_[y[1:], -np.inf]) & (x >= bounds[0]) & (x < bounds[1]))
        visible = y[(x >= bounds[0]) & (x < bounds[1])]
        floor = float(np.percentile(visible, 20)) if len(visible) else 0.
        threshold = floor+max(5*np.sqrt(max(0., floor)), .005*(float(visible.max())-floor) if len(visible) else 0.)
        candidates = sorted((index for index in candidates if y[index] > threshold), key=lambda index: y[index], reverse=True)[:256]
        added = []
        for index in candidates:
            if len(added) >= 32 or len(self.rois[ch]) >= 64:
                break
            if any(entry['bounds'][0] <= x[index] <= entry['bounds'][1] for entry in self.rois[ch]):
                continue
            try:
                roi = automatic_roi(np.maximum(counts, 0), self.calibrations[ch], target=x[index], search_bounds=bounds,
                                    search_radius=max(80., 12*self.calibrations[ch][1]))
            except ValueError:
                continue
            if any(low <= x[index] <= high for low, high in added) or any(entry['bounds'][0] <= x[index] <= entry['bounds'][1] for entry in self.rois[ch]):
                continue
            self.add_roi(ch, roi)
            added.append(roi)
        self.status.setText(f'CH{ch}: added {len(added)} distinct peaks in the visible range')

    def show_diagnostics(self, ch):
        residual = self.roi_residuals.get(ch)
        if residual is None:
            QtWidgets.QMessageBox.information(self, 'Fit diagnostics', 'Select Gaussian Fit and measure a valid ROI to inspect residuals and statistical errors.')
            return
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f'CH{ch} · Gaussian fit diagnostics')
        dialog.resize(850, 600)
        layout = QtWidgets.QVBoxLayout(dialog)
        result = self.roi_results[ch]
        layout.addWidget(QtWidgets.QLabel(f'{result.get("fit_objective")} · converged: {result.get("converged")} · '
                                         f'{result.get("fit_iterations")} iterations · {result.get("degrees_of_freedom")} degrees of freedom · '
                                         f'χ²/dof {result.get("chi2_red", 0):.3f}'))
        plot = pg.PlotWidget(background='#101a29')
        counts, _ = self.analysis_data(ch)
        x = self.calibrations[ch][0]+self.calibrations[ch][1]*(np.arange(len(counts))+.5)
        low, high = self.roi_regions[ch].getRegion()
        selected = (x >= low) & (x < high)
        plot.addLegend()
        plot.plot(x[selected], counts[selected], pen=None, symbol='o', symbolSize=4, name='Native counts')
        fit = self.roi_fit_curves[ch]
        plot.plot(*fit, pen=pg.mkPen('#75dfb8', width=2), name='Gaussian + background')
        plot.setLabel('left', 'Counts / native bin')
        layout.addWidget(plot, 2)
        residual_plot = pg.PlotWidget(background='#101a29')
        residual_plot.plot(*residual, pen=None, symbol='o', symbolSize=4)
        residual_plot.addItem(pg.InfiniteLine(pos=0, angle=0, pen=pg.mkPen('#94a8c1')))
        residual_plot.setLabel('left', 'Residual / σ')
        residual_plot.setLabel('bottom', 'Energy', units='keV')
        residual_plot.setXLink(plot)
        layout.addWidget(residual_plot, 1)
        dialog.exec()

    def roi_records(self, ch):
        return [{key: entry[key] for key in ('name', 'bounds', 'method', 'smoothing', 'color')} for entry in self.rois[ch]]

    def session_payload(self, include_frames=True):
        channels = {}
        for ch in (1, 2):
            if include_frames and ch not in self.frames:
                continue
            entry = dict(calibration=self.calibrations[ch], calibrated=self.calibrated[ch], calibration_energy=self.calibration_energy.get(ch), grouping=self.grouping[ch],
                         rois=self.roi_records(ch), device_settings=self.device_settings.get(ch, {}),
                         analysis_method=self.roi_methods[ch].currentData(), smoothing=self.smoothing[ch].currentData(),
                         baseline=self.baselines[ch].tolist() if ch in self.baselines else None,
                         active_roi_index=next((index for index, roi in enumerate(self.rois[ch]) if roi['id'] == self.active_roi[ch]), None))
            if include_frames:
                entry['frame'] = frame_record(self.frames[ch])
            for kind, store in (('reference', self.references), ('background', self.backgrounds)):
                entry[kind] = frame_record(store[ch]) if ch in store else None
                mode, value = self.comparison_widgets[ch][kind]
                entry[f'{kind}_mode'], entry[f'{kind}_scale'] = mode.currentData(), value.value()
            entry['subtract_background'] = self.comparison_widgets[ch]['subtract'].isChecked()
            channels[str(ch)] = entry
        return dict(application='DSW - DNRI Spectrum Workspace', author='T. H. Nam', email='thnam@dnri.vn',
                    saved_at=time.strftime('%Y-%m-%dT%H:%M:%S%z'), sample_metadata=self.sample_metadata,
                    view=dict(foreground=self.foreground_channel, background=self.background_menu.currentData()),
                    acquisition=dict(independent_channel_control=self.independent_channel_control,
                                     channel_acquiring=self.channel_acquiring if self.connected else None,
                                     control_events=self.control_events),
                    preset=dict(mode=self.preset_mode.currentData(), target=self.preset_target.value(), channel=self.preset_channel.currentData()),
                    channels=channels)

    def save_session(self, checked=False, filename=None):
        if not self.frames:
            return
        if not filename:
            filename, _ = QtWidgets.QFileDialog.getSaveFileName(self, 'Save DSW measurement session', '', 'DSW session (*.dsw)')
        if not filename:
            return
        try:
            write_session(filename, self.session_payload())
            self.status.setText(f'Saved session: {filename}')
        except (ValueError, OSError) as exc:
            QtWidgets.QMessageBox.warning(self, 'Session save', str(exc))

    def open_session(self, checked=False, filename=None):
        if self.connected or self.busy:
            self.status.setText('Disconnect before opening a foreground spectrum/session. References can be loaded during acquisition.')
            return
        if not filename:
            filename, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Open DSW measurement session', '', 'DSW session (*.dsw)')
        if not filename:
            return
        try:
            payload = read_session(filename)
            self.reset_spectra('Opening saved session')
            self.references.clear(); self.backgrounds.clear(); self.device_settings.clear()
            self.sample_metadata = payload.get('sample_metadata', dict(sample='', detector='', notes=''))
            self.control_events = payload.get('acquisition', {}).get('control_events', [])
            for channel, entry in payload['channels'].items():
                ch = int(channel)
                frame = restore_frame(entry['frame'])
                frame['channel'] = ch
                frame['calibration'], frame['calibrated'] = entry['calibration'], entry.get('calibrated', False)
                self.load_frame(ch, frame, entry.get('grouping', 8))
                if entry.get('baseline') is not None:
                    self.baselines[ch] = np.asarray(entry['baseline'], dtype=np.uint64)
                self.device_settings[ch] = entry.get('device_settings', {})
                for kind, store in (('reference', self.references), ('background', self.backgrounds)):
                    if entry.get(kind):
                        store[ch] = restore_frame(entry[kind])
                    mode, scale = self.comparison_widgets[ch][kind]
                    for widget, value in ((mode, mode.findData(entry.get(f'{kind}_mode', 'exposure'))), (scale, entry.get(f'{kind}_scale', 1.))):
                        blocker = QtCore.QSignalBlocker(widget)
                        widget.setCurrentIndex(value) if isinstance(widget, QtWidgets.QComboBox) else widget.setValue(value)
                        del blocker
                blocker = QtCore.QSignalBlocker(self.comparison_widgets[ch]['subtract'])
                self.comparison_widgets[ch]['subtract'].setChecked(entry.get('subtract_background', False))
                del blocker
                for roi in entry.get('rois', []):
                    self.add_roi(ch, roi['bounds'], roi.get('name', 'Peak'), roi.get('method', 'endpoint'), roi.get('smoothing', 1), roi.get('color'))
                active = entry.get('active_roi_index')
                if active is not None and 0 <= active < len(self.rois[ch]):
                    self.activate_roi(ch, self.rois[ch][active]['id'])
                self.redraw_channel(ch)
            preset = payload.get('preset', {})
            self.preset_mode.setCurrentIndex(self.preset_mode.findData(preset.get('mode', 'manual')))
            self.preset_target.setValue(preset.get('target', 60.))
            self.preset_channel.setCurrentIndex(self.preset_channel.findData(preset.get('channel', 1)))
            view = payload.get('view', {})
            foreground = view.get('foreground', 1 if '1' in payload['channels'] else 2)
            self.foreground_menu.setCurrentIndex(max(0, self.foreground_menu.findData(foreground)))
            legacy_background = 'background' if foreground in self.backgrounds else 'reference' if foreground in self.references else None
            self.refresh_background_choices(selected=view.get('background', legacy_background), preserve=False)
            self.background_selected()
            self.update_device_settings({})
            self.status.setText(f'Opened session: {filename} · offline analysis')
            self.controls()
        except (ValueError, OSError, KeyError, TypeError) as exc:
            QtWidgets.QMessageBox.warning(self, 'Session import', str(exc))

    def open_document(self, checked=False, filename=None):
        if self.connected or self.busy:
            self.status.setText('Disconnect before opening a foreground spectrum/session. References can be loaded during acquisition.')
            return
        if not filename:
            filename, _ = QtWidgets.QFileDialog.getOpenFileName(self, 'Open session or spectrum', '', OPEN_FILTER)
        if filename:
            self.open_session(filename=filename) if Path(filename).suffix.lower() == '.dsw' else self.open_spectrum(self.foreground_channel or 1, filename)

    def load_frame(self, ch, frame, grouping=1):
        frame = dict(frame, original_channel=frame.get('original_channel', frame.get('channel', ch)), channel=ch, loaded=True)
        self.calibrations[ch] = list(frame['calibration'])
        self.calibrated[ch] = frame.get('calibrated', False)
        self.calibration_energy[ch] = frame.get('energy')
        self.grouping[ch] = grouping
        if ch in self.grouping_menus:
            blocker = QtCore.QSignalBlocker(self.grouping_menus[ch])
            self.grouping_menus[ch].setCurrentIndex(self.grouping_menus[ch].findData(grouping))
            del blocker
        for widget, value in zip(self.inputs[ch], self.calibrations[ch]):
            widget.setValue(value)
        self.scale_labels[ch].setText('FILE CALIBRATION' if self.calibrated[ch] else 'DEFAULT SCALE · UNCALIBRATED')
        self.frames[ch] = frame
        self.draw(frame)
        self.controls()

    def open_spectrum(self, ch, filename=None):
        if self.connected or self.busy:
            self.status.setText('Disconnect before opening a foreground spectrum. Use Load reference to compare during acquisition.')
            return
        if not filename:
            filename, _ = QtWidgets.QFileDialog.getOpenFileName(self, f'Open spectrum into CH{ch}', '', OPEN_FILTER)
        if not filename:
            return
        if Path(filename).suffix.lower() == '.dsw':
            self.open_session(filename=filename)
            return
        try:
            frame = self.choose_spectrum(filename)
            if frame is None:
                return
            self._resetting_rois = True
            for entry in self.rois[ch]:
                self.plots[ch].removeItem(entry['region']); self.plots[ch].removeItem(entry['curve'])
            self.rois[ch] = []; self.active_roi[ch] = None
            self.remove_roi(ch)
            self._resetting_rois = False
            self.baselines.pop(ch, None)
            self.last_rates.pop(ch, None)
            self._rate_samples.pop(ch, None)
            self.load_frame(ch, frame)
            self.foreground_menu.setCurrentIndex(self.foreground_menu.findData(ch))
            for roi in frame.get('rois', []):
                low, high = max(0., roi['bounds'][0]), min(4000., roi['bounds'][1])
                if low < high:
                    self.add_roi(ch, (low, high), roi.get('name'), roi.get('method', 'endpoint'), roi.get('smoothing', 1), roi.get('color'))
            self.sample_metadata.update(frame.get('sample_metadata', {}))
            self.status.setText(f'Opened CH{ch}: {filename} · {len(frame["counts"]):,} bins · offline analysis')
        except (ValueError, OSError, KeyError, TypeError) as exc:
            self._resetting_rois = False
            QtWidgets.QMessageBox.warning(self, 'Spectrum import', str(exc))

    def choose_spectrum(self, filename):
        frames = read_spectra(filename)
        if len(frames) == 1:
            return frames[0]
        choices = [f'{index+1}: {frame.get("title", "spectrum")} · {frame.get("measurement_class", "")} · '
                   f'{frame.get("detector_id", "")} · {frame["total"]:,} counts · {duration(frame):g} s'
                   for index, frame in enumerate(frames)]
        selected, accepted = QtWidgets.QInputDialog.getItem(self, 'Choose N42 spectrum', 'Measurement / detector', choices, 0, False)
        return frames[choices.index(selected)] if accepted else None

    def export_spectrum(self, ch, filename=None):
        if ch not in self.frames:
            return
        if not filename:
            filename, _ = QtWidgets.QFileDialog.getSaveFileName(self, f'Export CH{ch} native spectrum', f'ch{ch}.spe', SAVE_FILTER)
        if not filename:
            return
        try:
            frame = dict(self.frames[ch], counts=self.displayed_counts(self.frames[ch]))
            if ch in self.baselines and np.any(self.baselines[ch]):
                frame.pop('raw', None)
            write_spectrum(filename, frame, self.calibrations[ch], self.calibrated[ch], self.roi_records(ch), self.sample_metadata)
            self.status.setText(f'Exported CH{ch}: {filename} · original foreground counts')
        except (ValueError, OSError) as exc:
            QtWidgets.QMessageBox.warning(self, 'Spectrum export', str(exc))

    def export_peak_table(self, ch, filename=None):
        if not filename:
            filename, _ = QtWidgets.QFileDialog.getSaveFileName(self, f'Export CH{ch} peaks', f'ch{ch}-peaks.csv', 'Peak table (*.csv)')
        if not filename:
            return
        fields = ['name', 'low_keV', 'high_keV', 'method', 'centroid_keV', 'centroid_uncertainty_keV', 'fwhm_keV',
                  'fwhm_uncertainty_keV', 'resolution_percent', 'gross_counts', 'net_counts', 'net_counts_uncertainty',
                  'background_subtracted', 'spectrum_background_counts', 'subtracted_roi_counts',
                  'chi2_red', 'converged', 'calibrated', 'spectrum_mode', 'sample', 'detector', 'exposure_seconds', 'status', 'warning']
        with Path(filename).open('w', newline='') as handle:
            writer = csv.DictWriter(handle, fieldnames=fields, extrasaction='ignore')
            writer.writeheader()
            for entry in self.rois[ch]:
                writer.writerow(dict(entry['result'], name=entry['name'], low_keV=entry['bounds'][0], high_keV=entry['bounds'][1],
                                     sample=self.sample_metadata.get('sample', ''), detector=self.sample_metadata.get('detector', ''),
                                     exposure_seconds=duration(self.frames[ch]) if ch in self.frames else 0))

    def edit_notes(self):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle('Sample and detector notes')
        layout = QtWidgets.QFormLayout(dialog)
        sample, detector = QtWidgets.QLineEdit(self.sample_metadata.get('sample', '')), QtWidgets.QLineEdit(self.sample_metadata.get('detector', ''))
        notes = QtWidgets.QPlainTextEdit(self.sample_metadata.get('notes', ''))
        layout.addRow('Sample', sample); layout.addRow('Detector / geometry', detector); layout.addRow('Notes', notes)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Save | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.sample_metadata = dict(sample=sample.text(), detector=detector.text(), notes=notes.toPlainText())

    def autosave_session(self, reason):
        if not self.frames:
            return None
        filename = Path('outputs')/'autosave'/f'{time.time_ns()}-{reason}.dsw'
        write_session(filename, self.session_payload())
        self.last_autosave = str(filename)
        return filename

    def open_last_autosave(self):
        if self.last_autosave:
            self.open_session(filename=self.last_autosave)
        else:
            self.status.setText('No automatic save in this application session yet')

    def preset_controls(self):
        mode = self.preset_mode.currentData()
        self.preset_target.setDecimals(0 if mode == 'counts' else 1)
        self.preset_target.setMinimum(1 if mode == 'counts' else .1)
        self.preset_target.setSuffix(' counts' if mode == 'counts' else ' s')
        self.preset_target.setVisible(mode != 'manual')
        self.preset_channel.setVisible(mode == 'counts')
        self.preset_summary()

    def preset_summary(self, *args):
        if self.running:
            return
        mode = self.preset_mode.currentData()
        message = 'Manual acquisition' if mode == 'manual' else (
            f'Ready · {self.preset_target.value():g} s duration' if mode == 'duration' else
            f'Ready · {int(self.preset_target.value()):,} new counts · {self.preset_channel.currentText()}')
        self.preset_progress.setValue(0)
        self.preset_progress.setFormat(message)

    def arm_preset(self, counts=None):
        self.preset = AcquisitionPreset(self.preset_mode.currentData(), self.preset_target.value(), self.preset_channel.currentData())
        if counts is None:
            counts = {ch: int(self.frames[ch]['total']) if ch in self.frames else int(self.device_settings.get(ch, {}).get('stats', {}).get('histogrammed', 0)) for ch in (1, 2)}
        self.preset.arm(time.monotonic(), counts)
        self._final_autosave_pending = True

    def check_preset(self, now=None):
        if not self.workspace_ready or not self.running or self.closing or self.preset.started is None:
            return
        counts = {ch: frame['total'] for ch, frame in self.frames.items()}
        fraction, reached, message = self.preset.progress(time.monotonic() if now is None else now, counts)
        self.preset_progress.setValue(round(fraction*1000))
        self.preset_progress.setFormat(message)
        if reached and not self.busy:
            self.preset.stop_requested = True
            self.action('stop')
            self.preset_progress.setFormat(f'Target reached · stopping after current UART frame · {message}')

    def finish_acquisition(self):
        stopped = self._stopped_channels
        final_ready = (all(not frame['live'] for frame in self.frames.values()) if stopped == (1, 2) else
                       all(ch in self.frames and not self.frames[ch]['live'] for ch in stopped))
        if self._final_autosave_pending and self.frames and final_ready:
            try:
                filename = self.autosave_session('final' if stopped == (1, 2) else f'final-ch{stopped[0]}')
                scope = 'Both channels' if stopped == (1, 2) else f'CH{stopped[0]}'
                self.status.setText(f'{self.acquisition_summary()} · {scope} FINAL saved to {filename}')
                self._final_autosave_pending = False
            except (OSError, ValueError) as exc:
                self.status.setText(f'{self.acquisition_summary()}; automatic save failed: {exc}. Export retained spectra manually.')
        self.preset.started = None

    def update_rate(self, frame):
        if frame.get('loaded'):
            return
        ch = frame['channel']
        seconds = frame['stats']['acquisition_ticks']/50000000
        previous = self._rate_samples.get(ch)
        if previous and seconds > previous[0] and frame['total'] >= previous[1]:
            self.last_rates[ch] = (frame['total']-previous[1])/(seconds-previous[0])
        self._rate_samples[ch] = (seconds, frame['total'])

    def show_adc_setup(self, ch):
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f'CH{ch} · ADC trigger and noise')
        layout = QtWidgets.QFormLayout(dialog)
        floor, multiplier = QtWidgets.QSpinBox(), QtWidgets.QDoubleSpinBox()
        floor.setRange(1, 4095); floor.setSuffix(' ADC codes')
        multiplier.setRange(0., 16.); multiplier.setDecimals(4); multiplier.setSingleStep(1/256)
        trigger = self.device_settings.get(ch, {}).get('trigger', (25, 1280, 0, 25, 0))
        floor.setValue(trigger[0]); multiplier.setValue(trigger[1]/256)
        layout.addRow('Trigger floor', floor); layout.addRow('Noise multiplier N', multiplier)
        explanation = QtWidgets.QLabel('Effective threshold = max(floor, ceil(N × fast-filter noise σ)).\n'
                                      'N = 0 selects a fixed threshold. Apply saves both final spectra and clears both histograms.')
        explanation.setWordWrap(True); layout.addRow(explanation)
        diagnostics = QtWidgets.QLabel()
        diagnostics.setWordWrap(True); layout.addRow('Latest readback', diagnostics)
        refresh = QtWidgets.QPushButton('Refresh hardware readback')
        refresh.clicked.connect(lambda: self.action('read_settings'))
        layout.addRow(refresh)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Apply | QtWidgets.QDialogButtonBox.StandardButton.Close)
        apply = buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Apply)
        apply.clicked.connect(lambda: (self.apply_trigger(ch, floor.value(), multiplier.value()), dialog.accept()))
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        def update():
            settings = self.device_settings.get(ch, {})
            values = settings.get('trigger')
            if values:
                diagnostics.setText(f'Noise σ: {values[2]/256:.4f} ADC codes · effective threshold: {values[3]} codes\n'
                                    f'Noise valid: {bool(values[4]&1)} · noise blocks: {values[4]>>1}\n'
                                    f'Histogram rate: {self.last_rates.get(ch, 0):,.1f} counts/s (acquisition-enabled time)')
            else:
                diagnostics.setText('Trigger/noise readback not available yet')
            apply.setEnabled(self.connected and not self.busy and not self.running and settings.get('source', (None,))[0] == 0)
            refresh.setEnabled(self.connected and not self.busy)
        timer = QtCore.QTimer(dialog)
        timer.timeout.connect(update); timer.start(300); update()
        dialog.exec()

    def apply_trigger(self, ch, floor, multiplier):
        settings = self.device_settings.get(ch, {})
        if not self.connected or self.busy or self.running or settings.get('source', (None,))[0] != 0:
            return
        previous = settings.get('trigger')
        if previous and tuple(previous[:2]) == (floor, round(multiplier*256)):
            return
        self.action('trigger', (ch, floor, multiplier))
