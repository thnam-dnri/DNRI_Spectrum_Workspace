"""One spectrum view, with independent foreground and background calibrations."""
import pyqtgraph as pg
from PySide6 import QtCore, QtWidgets

from comparison import exposure_scale, project_reference
from histogram import DEFAULT_GAIN, GROUPS, energy_histogram


class SpectrumViewMixin:
    def build_spectrum_view(self, layout, plot_class):
        self.foreground_channel = 1
        self.roi_enabled = {1: False, 2: False}
        self.background_histogram = None
        self.plots, self.curves, self.labels, self.inputs = {}, {}, {}, {}
        card = QtWidgets.QFrame()
        card.setObjectName('channelCard')
        column = QtWidgets.QVBoxLayout(card)
        column.setSizeConstraint(QtWidgets.QLayout.SizeConstraint.SetMinimumSize)
        column.setContentsMargins(12, 10, 12, 10)
        column.setSpacing(8)
        layout.addWidget(card, 1)

        header = QtWidgets.QHBoxLayout()
        header.addWidget(QtWidgets.QLabel('Foreground'))
        self.foreground_menu = QtWidgets.QComboBox()
        for label, value in (('Select foreground…', None), ('Channel 1', 1), ('Channel 2', 2)):
            self.foreground_menu.addItem(label, value)
        self.foreground_menu.setCurrentIndex(1)
        self.foreground_menu.setToolTip('The foreground controls display binning, ROI analysis and plot settings.')
        header.addWidget(self.foreground_menu)
        header.addWidget(QtWidgets.QLabel('Background'))
        self.background_menu = QtWidgets.QComboBox()
        self.background_menu.setMinimumWidth(170)
        self.background_menu.setToolTip('Overlay a second spectrum using its own calibration and the foreground bin edges.')
        header.addWidget(self.background_menu)
        self.background_calibration_button = QtWidgets.QPushButton('Background calibration…')
        self.background_calibration_button.clicked.connect(self.edit_selected_background_calibration)
        header.addWidget(self.background_calibration_button)
        header.addStretch()
        self.compare_button = QtWidgets.QPushButton('Peaks / compare')
        self.compare_button.clicked.connect(lambda: self.show_peaks(self.foreground_channel) if self.foreground_channel else None)
        header.addWidget(self.compare_button)
        export = QtWidgets.QPushButton('Export foreground + ROI')
        export.clicked.connect(lambda: self.save(self.foreground_channel) if self.foreground_channel else None)
        self.export_buttons = {1: export, 2: export}
        header.addWidget(export)
        column.addLayout(header)

        adc_row = QtWidgets.QHBoxLayout()
        for ch in (1, 2):
            adc_row.addWidget(QtWidgets.QLabel(f'ADC CH{ch} pulse'))
            menu = QtWidgets.QComboBox()
            menu.addItem('+ Positive', 'P')
            menu.addItem('− Negative', 'N')
            menu.setToolTip(f'Independent ADC pulse polarity for channel {ch}. Stop acquisition to edit; changing polarity clears both channels.')
            menu.activated.connect(lambda index, ch=ch, menu=menu: self.polarity_selected(ch, menu.itemData(index)))
            self.polarity_menus[ch] = menu
            adc_row.addWidget(menu)
        adc_row.addStretch()
        hint = QtWidgets.QLabel('Double-click: ROI · Shift + double-click: add ROI')
        hint.setObjectName('muted')
        adc_row.addWidget(hint)
        column.addLayout(adc_row)

        content = QtWidgets.QHBoxLayout()
        column.addLayout(content, 1)
        plot_column = QtWidgets.QVBoxLayout()
        content.addLayout(plot_column, 1)
        self.spectrum_plot = plot = plot_class(background='#101a29')
        plot.setMinimumHeight(240)
        plot.setLabel('bottom', 'Energy', units='keV')
        plot.getAxis('bottom').enableAutoSIPrefix(False)
        plot.setLabel('left', 'Counts / foreground display bin')
        for axis in ('bottom', 'left'):
            plot.getAxis(axis).setPen(pg.mkPen('#6d829f'))
            plot.getAxis(axis).setTextPen(pg.mkPen('#b5c7de'))
        plot.showGrid(x=True, y=True, alpha=.15)
        # Keep complete center-step edge arrays when zooming (pyqtgraph 0.14).
        plot.setClipToView(False)
        plot.setLimits(xMin=0, xMax=4000, yMin=0)
        plot.setXRange(0, 4000, padding=0)
        plot.setYRange(0, 1, padding=0)
        plot.setMouseEnabled(x=True, y=False)
        plot.setMenuEnabled(False)
        plot.getPlotItem().hideButtons()
        self.spectrum_legend = plot.addLegend(offset=(12, 12))
        plot_column.addWidget(plot, 1)
        cursor_label = QtWidgets.QLabel('Move over the spectrum to inspect energy and counts')
        cursor_label.setObjectName('muted')
        plot_column.addWidget(cursor_label)
        self.background_message = QtWidgets.QLabel('Background: none')
        self.background_message.setObjectName('muted')
        # A wrapped summary can exceed the docked plot's allocated height.
        # Keep it to one line; the tooltip carries the complete readout.
        self.background_message.setSizePolicy(QtWidgets.QSizePolicy.Policy.Ignored,
                                              QtWidgets.QSizePolicy.Policy.Fixed)
        plot_column.addWidget(self.background_message)
        self.background_curve = plot.plot(pen=pg.mkPen('#ffba58', width=1.5), stepMode='center')
        self.background_curve.hide()
        self.analysis_stack = QtWidgets.QStackedWidget()
        self.analysis_stack.setSizePolicy(QtWidgets.QSizePolicy.Policy.Preferred, QtWidgets.QSizePolicy.Policy.Minimum)
        self.analysis_scroll = QtWidgets.QScrollArea()
        self.analysis_scroll.setFixedWidth(350)
        self.analysis_scroll.setWidgetResizable(True)
        self.analysis_scroll.setFrameShape(QtWidgets.QFrame.Shape.NoFrame)
        self.analysis_scroll.setStyleSheet('QScrollArea, QScrollArea > QWidget > QWidget { background: #182334; }')
        self.analysis_scroll.setWidget(self.analysis_stack)
        content.addWidget(self.analysis_scroll)
        empty = QtWidgets.QLabel('Select a foreground channel to analyze peaks.')
        empty.setWordWrap(True)
        self.analysis_stack.addWidget(empty)
        self.display_stack = QtWidgets.QStackedWidget()
        column.addWidget(self.display_stack)
        self.display_stack.addWidget(QtWidgets.QLabel('Select a foreground channel to set display parameters.'))
        for ch in (1, 2):
            self.plots[ch] = plot
            fill = pg.mkColor('#47b9ff'); fill.setAlpha(22)
            self.curves[ch] = plot.plot(pen=pg.mkPen('#47b9ff', width=1.4), stepMode='center', fillLevel=0, brush=pg.mkBrush(fill))
            self.curves[ch].hide()
            self.fit_curves[ch] = plot.plot(pen=pg.mkPen('#75dfb8', width=2))
            self.fit_curves[ch].hide()
            self.cursor_labels[ch] = cursor_label
            cursor = pg.InfiniteLine(angle=90, movable=False, pen=pg.mkPen('#7086a4', style=QtCore.Qt.PenStyle.DotLine))
            cursor.hide()
            plot.addItem(cursor, ignoreBounds=True)
            self.cursors[ch] = cursor
            page = QtWidgets.QWidget()
            page_column = QtWidgets.QVBoxLayout(page)
            page_column.setContentsMargins(0, 0, 0, 0)
            panel_row = QtWidgets.QHBoxLayout()
            page_column.addLayout(panel_row)
            self.build_roi_panel(ch, '#47b9ff', panel_row, page_column)
            self.analysis_stack.addWidget(page)

            page = QtWidgets.QWidget()
            page_column = QtWidgets.QVBoxLayout(page)
            page_column.setContentsMargins(0, 0, 0, 0)
            stats_row = QtWidgets.QHBoxLayout()
            mode = QtWidgets.QLabel('NO DATA'); mode.setObjectName('mode')
            self.mode_labels[ch] = mode
            stats_row.addWidget(mode)
            self.labels[ch] = QtWidgets.QLabel('No data')
            self.labels[ch].setObjectName('muted'); self.labels[ch].setWordWrap(True)
            stats_row.addWidget(self.labels[ch], 1)
            page_column.addLayout(stats_row)
            row = QtWidgets.QHBoxLayout()
            row.addWidget(QtWidgets.QLabel('Group bins'))
            grouping = QtWidgets.QComboBox()
            for size in GROUPS:
                grouping.addItem(str(size), size)
            grouping.setCurrentIndex(GROUPS.index(8))
            grouping.setToolTip('Group foreground native bins. Background is aligned to the same energy edges; ROI analysis uses native bins.')
            grouping.currentIndexChanged.connect(lambda index, ch=ch, menu=grouping: self.regroup(ch, menu.currentData()))
            self.grouping_menus[ch] = grouping
            row.addWidget(grouping)
            row.addWidget(QtWidgets.QLabel(f'CH{ch} offset (keV)'))
            offset, gain = QtWidgets.QDoubleSpinBox(), QtWidgets.QDoubleSpinBox()
            offset.setRange(-1e6, 1e6); gain.setRange(.000001, 1e6)
            for widget in (offset, gain):
                widget.setDecimals(9); widget.setMinimumWidth(120); widget.setMaximumWidth(155)
            gain.setValue(DEFAULT_GAIN)
            self.inputs[ch] = (offset, gain)
            row.addWidget(offset)
            row.addWidget(QtWidgets.QLabel('Gain (keV/bin)'))
            row.addWidget(gain)
            apply = QtWidgets.QPushButton('Apply keV')
            apply.clicked.connect(lambda checked=False, ch=ch: self.calibrate(ch))
            row.addWidget(apply)
            wizard = QtWidgets.QPushButton('2-Point Calibrate…')
            wizard.clicked.connect(lambda checked=False, ch=ch: self.open_calibration_wizard(ch))
            row.addWidget(wizard)
            row.addStretch()
            label = QtWidgets.QLabel('DEFAULT SCALE · UNCALIBRATED')
            label.setStyleSheet('color: #ffcf82; font-size: 11px;')
            self.scale_labels[ch] = label
            row.addWidget(label)
            page_column.addLayout(row)
            self.display_stack.addWidget(page)

        self.mouse_proxies[1] = pg.SignalProxy(plot.scene().sigMouseMoved, rateLimit=30,
                                             threadSafe=False,
                                             slot=lambda args: self.cursor_moved(self.foreground_channel, args[0]) if self.foreground_channel else None)
        self.mouse_proxies[1].setParent(self)
        self.mouse_proxies[1].timer.setParent(self.mouse_proxies[1])
        plot.peak_requested.connect(lambda energy: self.auto_roi(self.foreground_channel, energy) if self.foreground_channel else None)
        plot.add_peak_requested.connect(lambda energy: self.add_peak_at(self.foreground_channel, energy) if self.foreground_channel else None)
        plot.getViewBox().sigXRangeChanged.connect(lambda *args: self.scale_y(self.foreground_channel))
        self.foreground_menu.currentIndexChanged.connect(self.foreground_selected)
        self.background_menu.currentIndexChanged.connect(self.background_selected)
        self.refresh_background_choices()
        self.analysis_stack.setCurrentIndex(1)
        self.display_stack.setCurrentIndex(1)

    def refresh_background_choices(self, selected=None, preserve=True):
        if preserve:
            selected = self.background_menu.currentData()
        ch = self.foreground_channel
        blocker = QtCore.QSignalBlocker(self.background_menu)
        self.background_menu.clear()
        self.background_menu.addItem('None — foreground only', None)
        for channel in (1, 2):
            self.background_menu.addItem(f'Channel {channel}', channel)
            self.background_menu.model().item(self.background_menu.count()-1).setEnabled(ch is not None and channel != ch)
        for kind, store in (('background', getattr(self, 'backgrounds', {})), ('reference', getattr(self, 'references', {}))):
            if ch in store:
                self.background_menu.addItem(f'Loaded {kind}', kind)
        if ch is not None:
            self.background_menu.insertSeparator(self.background_menu.count())
            self.background_menu.addItem('Load spectrum file…', 'load')
        index = self.background_menu.findData(selected)
        self.background_menu.setCurrentIndex(index if ch and selected != ch and index >= 0 else 0)
        self.background_menu.setEnabled(ch is not None)
        self._last_background = self.background_menu.currentData()
        del blocker

    def foreground_selected(self):
        self.foreground_channel = self.foreground_menu.currentData()
        self.analysis_stack.setCurrentIndex(self.foreground_channel or 0)
        self.display_stack.setCurrentIndex(self.foreground_channel or 0)
        for cursor in self.cursors.values():
            cursor.hide()
        self.cursor_labels[1].setText('Move over the spectrum to inspect energy and counts')
        self.refresh_background_choices()
        if self.foreground_channel in self.frames:
            self.draw(self.frames[self.foreground_channel])
        else:
            self.refresh_spectrum_view()
        self.controls()

    def background_selected(self):
        if self.foreground_channel is not None and self.background_menu.currentData() == 'load':
            self.refresh_background_choices(selected=self._last_background, preserve=False)
            self.load_reference(self.foreground_channel, 'background', show_panel=False)
            return
        # Enforce the constraint for programmatic selection as well as mouse input.
        if self.foreground_channel is None or self.background_menu.currentData() == self.foreground_channel:
            self.refresh_background_choices(selected=None, preserve=False)
        self._last_background = self.background_menu.currentData()
        if self.foreground_channel in self.frames:
            self.draw(self.frames[self.foreground_channel])
        else:
            self.refresh_spectrum_view()
        self.controls()

    def selected_background(self, ch=None):
        ch = self.foreground_channel if ch is None else ch
        if ch != self.foreground_channel or ch is None:
            return None
        selected = self.background_menu.currentData()
        if selected in (1, 2) and selected != ch and selected in self.frames:
            frame = self.frames[selected]
            return dict(frame, counts=self.displayed_counts(frame), calibration=self.calibrations[selected], calibrated=self.calibrated[selected])
        store = self.backgrounds if selected == 'background' else self.references if selected == 'reference' else {}
        return store.get(ch)

    def selected_background_scale(self, ch):
        frame = self.selected_background(ch)
        kind = 'reference' if self.background_menu.currentData() == 'reference' else 'background'
        norm, scale = self.comparison_widgets[ch][kind]
        mode = norm.currentData()
        scale.setEnabled(mode == 'manual')
        try:
            result = exposure_scale(self.frames[ch], frame, 1. if mode == 'unscaled' else scale.value() if mode == 'manual' else None)
        except ValueError:
            if mode != 'exposure':
                raise
            result = 1., 'unscaled counts (measurement time unavailable)'
        if mode != 'manual':
            blocker = QtCore.QSignalBlocker(scale)
            scale.setValue(result[0])
            del blocker
        return result

    def refresh_spectrum_items(self):
        """Only the foreground owns interactive ROIs, fits and cursor markers."""
        foreground = self.foreground_channel
        for ch in (1, 2):
            shown = ch == foreground and ch in self.frames
            self.curves[ch].setVisible(shown)
            enabled = shown and self.roi_enabled[ch]
            self.roi_regions[ch].setVisible(enabled)
            result = self.roi_results.get(ch, {})
            self.peak_markers[ch].setVisible(enabled and result.get('peak_keV') is not None)
            for marker, key in zip(self.width_markers[ch], ('fwhm_low_keV', 'fwhm_high_keV')):
                marker.setVisible(enabled and result.get(key) is not None)
            self.fit_curves[ch].setVisible(enabled and self.roi_fit_curves.get(ch) is not None)
            if not shown:
                self.cursors[ch].hide()
            if self.workspace_ready:
                for entry in self.rois[ch]:
                    inactive = shown and entry['id'] != self.active_roi[ch]
                    entry['region'].setVisible(inactive)
                    entry['curve'].setVisible(inactive and entry.get('fit') is not None)

    def refresh_spectrum_view(self):
        self.refresh_spectrum_items()
        ch = self.foreground_channel
        self.background_histogram = None
        self.background_curve.hide()
        self.spectrum_legend.clear()
        if self.workspace_ready:
            for curve in self.reference_curves.values():
                curve.hide()
        if ch not in self.frames:
            self.background_message.setText('Select a foreground channel.' if ch is None else f'Foreground CH{ch}: awaiting spectrum')
            self.spectrum_plot.setYRange(0, 1, padding=0)
            self.spectrum_view_controls()
            return
        self.spectrum_legend.addItem(self.curves[ch], f'Foreground · CH{ch}')
        background = self.selected_background(ch) if self.workspace_ready else None
        selection = self.background_menu.currentData()
        if background is None:
            self.background_message.setText(f'Background CH{selection}: awaiting spectrum' if selection in (1, 2) else 'Background: none')
        else:
            try:
                scale, basis = self.selected_background_scale(ch)
                projected, *_ = project_reference(background['counts'], background['calibration'], len(self.frames[ch]['counts']), self.calibrations[ch], scale)
                edges, counts, _, _ = energy_histogram(projected, self.calibrations[ch], self.grouping[ch])
                self.background_histogram = (edges, counts)
                curve = self.reference_curves[ch] if selection == 'reference' else self.background_curve
                curve.setData(edges, counts)
                curve.show()
                name = f'CH{selection}' if selection in (1, 2) else f'loaded {selection}'
                self.spectrum_legend.addItem(curve, f'Background · {name}')
                offset, gain = background['calibration']
                self.background_message.setText(f'Background {name} × {scale:.6g} · {basis} · own calibration: {offset:.6g} + {gain:.6g} × bin keV · aligned to foreground bins')
                self.background_message.setToolTip(self.background_message.text())
            except ValueError as exc:
                self.background_message.setText(f'Background overlay unavailable: {exc}')
        self.scale_y(ch)
        self.spectrum_view_controls()

    def spectrum_view_controls(self):
        ch = self.foreground_channel
        self.background_menu.setEnabled(ch is not None)
        self.background_calibration_button.setEnabled(self.workspace_ready and self.selected_background() is not None)
        self.compare_button.setEnabled(ch is not None)
        self.export_buttons[1].setEnabled(ch in self.frames)

    def edit_selected_background_calibration(self):
        selected = self.background_menu.currentData()
        if selected not in (1, 2):
            if selected in ('background', 'reference'):
                self.edit_reference_calibration(self.foreground_channel, selected)
            return
        ch = selected
        dialog = QtWidgets.QDialog(self)
        dialog.setWindowTitle(f'Background CH{ch} calibration')
        layout = QtWidgets.QFormLayout(dialog)
        offset, gain = QtWidgets.QDoubleSpinBox(), QtWidgets.QDoubleSpinBox()
        offset.setRange(-1e6, 1e6); gain.setRange(.000001, 1e6)
        for widget, value in zip((offset, gain), self.calibrations[ch]):
            widget.setDecimals(9); widget.setValue(value)
        layout.addRow('Offset (keV)', offset)
        layout.addRow('Gain (keV/bin)', gain)
        buttons = QtWidgets.QDialogButtonBox(QtWidgets.QDialogButtonBox.StandardButton.Apply | QtWidgets.QDialogButtonBox.StandardButton.Cancel)
        buttons.button(QtWidgets.QDialogButtonBox.StandardButton.Apply).clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addRow(buttons)
        if dialog.exec() == QtWidgets.QDialog.DialogCode.Accepted:
            self.apply_wizard_calibration(ch, offset.value(), gain.value())
