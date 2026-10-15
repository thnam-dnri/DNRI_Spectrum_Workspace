"""Two-point energy calibration calculation and interactive dialog for DSW."""
from PySide6 import QtCore, QtWidgets

CALIBRATION_SOURCES = [
    ('Custom energy…', None),
    ('Am-241 · 59.54 keV', 59.54),
    ('Ba-133 · 81.00 keV', 81.00),
    ('Co-57 · 122.06 keV', 122.06),
    ('Ba-133 · 356.02 keV', 356.02),
    ('Na-22 · 511.00 keV (annihilation)', 511.00),
    ('Cs-137 · 661.66 keV', 661.66),
    ('Co-60 · 1,173.23 keV', 1173.23),
    ('Na-22 · 1,274.54 keV', 1274.54),
    ('Co-60 · 1,332.49 keV', 1332.49),
    ('K-40 · 1,460.82 keV', 1460.82),
    ('Tl-208 · 2,614.51 keV', 2614.51),
]


def calculate_two_point_calibration(ch1, e1, ch2, e2):
    """Compute (offset, gain) from two reference points (native bin, energy in keV).

    E = offset + gain * bin
    gain = (e2 - e1) / (ch2 - ch1)
    offset = e1 - gain * ch1
    """
    ch1, e1, ch2, e2 = float(ch1), float(e1), float(ch2), float(e2)
    if ch1 == ch2:
        raise ValueError('Points must have different channel/bin coordinates')
    gain = (e2 - e1) / (ch2 - ch1)
    if gain <= 0:
        raise ValueError('Calculated gain must be positive (higher bin requires higher energy)')
    offset = e1 - gain * ch1
    return offset, gain


class TwoPointCalibrationDialog(QtWidgets.QDialog):
    calibration_applied = QtCore.Signal(int, float, float)

    def __init__(self, channel, current_calibration, get_centroid_callback, parent=None):
        super().__init__(parent)
        self.channel = channel
        self.current_calibration = current_calibration
        self.get_centroid = get_centroid_callback
        self.setWindowTitle(f'Channel {channel} · 2-Point Energy Calibration Wizard')
        self.resize(520, 520)
        self.setMinimumSize(480, 480)
        self.setStyleSheet('''
            QDialog { background: #182334; color: #e5edf6; }
            QGroupBox { border: 1px solid #3b506b; border-radius: 6px; margin-top: 10px;
                        font-weight: 600; padding-top: 12px; color: #94a8c1; }
            QGroupBox::title { subcontrol-origin: margin; left: 10px; padding: 0 4px; }
            QLabel { color: #e5edf6; }
            QLabel#muted { color: #94a8c1; }
            QLabel#highlight { color: #75dfb8; font-weight: 600; }
            QLabel#error { color: #ff6b6b; font-weight: 600; }
            QPushButton { background: #29394f; border: 1px solid #3b506b;
                          border-radius: 5px; padding: 6px 12px; color: #e5edf6; }
            QPushButton:hover { background: #354c69; }
            QPushButton#primary { background: #2563a6; border-color: #4388cc; font-weight: 600; }
            QPushButton#primary:hover { background: #3075bd; }
            QComboBox, QDoubleSpinBox { background: #111c2c; border: 1px solid #3b506b;
                                       border-radius: 4px; padding: 5px; color: #e5edf6; }
        ''')
        layout = QtWidgets.QVBoxLayout(self)
        layout.setSpacing(12)

        desc = QtWidgets.QLabel(
            f'Calibrate Channel {channel} using two known gamma-ray reference lines.\n'
            'Set the native FPGA bin coordinate and known energy for each peak, or '
            'click "Capture ROI centroid" to load from the active region.'
        )
        desc.setObjectName('muted')
        desc.setWordWrap(True)
        layout.addWidget(desc)

        # Point 1 & Point 2 group boxes
        self.bin_spinboxes = {}
        self.energy_spinboxes = {}
        self.source_menus = {}

        cur_offset, cur_gain = current_calibration
        # Default reasonable starting points
        defaults = [
            (511.0 / cur_gain, 511.0, 5),   # Na-22 511
            (1274.54 / cur_gain, 1274.54, 8) # Na-22 1274
        ]

        for pt_idx in (1, 2):
            box = QtWidgets.QGroupBox(f'Reference Peak {pt_idx}')
            box_layout = QtWidgets.QVBoxLayout(box)
            box_layout.setSpacing(8)

            row_top = QtWidgets.QHBoxLayout()
            row_top.addWidget(QtWidgets.QLabel('Source isotope:'))
            source_menu = QtWidgets.QComboBox()
            for label, energy in CALIBRATION_SOURCES:
                source_menu.addItem(label, energy)
            source_menu.setCurrentIndex(defaults[pt_idx - 1][2])
            source_menu.currentIndexChanged.connect(
                lambda idx, p=pt_idx: self.source_changed(p, idx))
            self.source_menus[pt_idx] = source_menu
            row_top.addWidget(source_menu, 1)

            btn_roi = QtWidgets.QPushButton('Capture ROI centroid')
            btn_roi.setToolTip('Read native bin and energy from current active ROI on this channel')
            btn_roi.clicked.connect(lambda checked=False, p=pt_idx: self.capture_roi(p))
            row_top.addWidget(btn_roi)
            box_layout.addLayout(row_top)

            grid = QtWidgets.QGridLayout()
            grid.addWidget(QtWidgets.QLabel('Native FPGA Bin:'), 0, 0)
            bin_spin = QtWidgets.QDoubleSpinBox()
            bin_spin.setRange(0.0, 16383.999)
            bin_spin.setDecimals(3)
            bin_spin.setValue(defaults[pt_idx - 1][0])
            bin_spin.valueChanged.connect(self.recalculate)
            self.bin_spinboxes[pt_idx] = bin_spin
            grid.addWidget(bin_spin, 0, 1)

            grid.addWidget(QtWidgets.QLabel('Known Energy (keV):'), 0, 2)
            energy_spin = QtWidgets.QDoubleSpinBox()
            energy_spin.setRange(0.001, 100000.0)
            energy_spin.setDecimals(3)
            energy_spin.setSuffix(' keV')
            energy_spin.setValue(defaults[pt_idx - 1][1])
            energy_spin.valueChanged.connect(self.recalculate)
            self.energy_spinboxes[pt_idx] = energy_spin
            grid.addWidget(energy_spin, 0, 3)
            box_layout.addLayout(grid)

            layout.addWidget(box)

        # Calculation card
        calc_box = QtWidgets.QGroupBox('Calculated Calibration')
        calc_layout = QtWidgets.QVBoxLayout(calc_box)
        calc_layout.setSpacing(6)

        self.formula_label = QtWidgets.QLabel('E(keV) = Offset + Gain × Bin')
        self.formula_label.setObjectName('muted')
        calc_layout.addWidget(self.formula_label)

        self.result_label = QtWidgets.QLabel('—')
        self.result_label.setObjectName('highlight')
        calc_layout.addWidget(self.result_label)

        self.range_label = QtWidgets.QLabel('—')
        self.range_label.setObjectName('muted')
        calc_layout.addWidget(self.range_label)

        self.error_label = QtWidgets.QLabel('')
        self.error_label.setObjectName('error')
        self.error_label.setWordWrap(True)
        self.error_label.hide()
        calc_layout.addWidget(self.error_label)

        layout.addWidget(calc_box)

        # Button row
        btn_row = QtWidgets.QHBoxLayout()
        btn_row.addStretch()

        self.cancel_button = QtWidgets.QPushButton('Cancel')
        self.cancel_button.clicked.connect(self.reject)
        btn_row.addWidget(self.cancel_button)

        self.apply_button = QtWidgets.QPushButton('Apply Calibration')
        self.apply_button.setObjectName('primary')
        self.apply_button.clicked.connect(self.apply_calibration)
        btn_row.addWidget(self.apply_button)

        layout.addLayout(btn_row)

        self.computed = None
        self.recalculate()

    def source_changed(self, pt_idx, menu_idx):
        energy = self.source_menus[pt_idx].currentData()
        if energy is not None:
            self.energy_spinboxes[pt_idx].setValue(energy)

    def capture_roi(self, pt_idx):
        centroid_kev = self.get_centroid(self.channel)
        if centroid_kev is None:
            QtWidgets.QMessageBox.information(
                self, 'No ROI Centroid',
                f'Channel {self.channel} does not currently have an active ROI with a measured peak.\n'
                'Double-click a peak in the spectrum or set ROI bounds first.'
            )
            return
        cur_offset, cur_gain = self.current_calibration
        native_bin = (centroid_kev - cur_offset) / cur_gain
        self.bin_spinboxes[pt_idx].setValue(max(0.0, min(16383.999, native_bin)))
        self.recalculate()

    def recalculate(self):
        ch1 = self.bin_spinboxes[1].value()
        e1 = self.energy_spinboxes[1].value()
        ch2 = self.bin_spinboxes[2].value()
        e2 = self.energy_spinboxes[2].value()

        try:
            offset, gain = calculate_two_point_calibration(ch1, e1, ch2, e2)
            self.computed = (offset, gain)
            gain_sign = '+' if gain >= 0 else '−'
            self.result_label.setText(
                f'Offset = {offset:+.4f} keV   ·   Gain = {gain:.6f} keV/bin'
            )
            self.formula_label.setText(
                f'E(keV) = {offset:.4f} {gain_sign} {abs(gain):.6f} × Bin'
            )
            max_e = offset + gain * 16384
            self.range_label.setText(
                f'Full-scale span (0 – 16,384 bins): {offset:.1f} – {max_e:.1f} keV'
            )
            self.error_label.hide()
            self.apply_button.setEnabled(True)
        except ValueError as err:
            self.computed = None
            self.result_label.setText('Calibration invalid')
            self.range_label.setText('—')
            self.error_label.setText(str(err))
            self.error_label.show()
            self.apply_button.setEnabled(False)

    def apply_calibration(self):
        if self.computed:
            offset, gain = self.computed
            self.calibration_applied.emit(self.channel, offset, gain)
            self.accept()
