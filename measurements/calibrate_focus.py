import os
import pickle
import time

from core import core

import numpy
from scipy.optimize import curve_fit
from scipy.special import erf
from matplotlib.figure import Figure
from matplotlib.backends.backend_agg import FigureCanvasAgg
from PyQt5.QtWidgets import *

import core
from core import *
from measurements.async_worker import AsyncWorker

def fit_current(x, i0, omega0, x0, omega):
    return i0 * ((numpy.pi *omega0**2)/4)*(1 + erf(-numpy.sqrt(2)*(x-x0)/omega))

def fit_width(z, omega0, z0, zR):
    return omega0 * numpy.sqrt(1 + ((z-z0)/zR)**2)


class CalibrateFocusWorker(AsyncWorker):

    description = 'Focus calibration'
    has_dialog = True

    def action(self):

        self.result = {}
        self.result['settings'] = self.settings
        self.result['currents'] = {}
        self.result['widths'] = {}
        self.result['fit_failed_z'] = []
        self.result['width_fit_failed'] = False
        xrange = numpy.arange(*self.settings['xrange'])
        zrange = numpy.arange(*self.settings['zrange'])
        
        current_steps = 0
        total_steps = len(xrange) * len(zrange)
        core.measurement_state = 0, total_steps

        widths = []

        for z in zrange:

            magnitudes = []
            currents = []

            for x in xrange:
                    
                if QThread.currentThread().isInterruptionRequested():
                    self.save_data()
                    return

                if core.motors is not None:
                    core.motors.move_abs('x', x)
                    core.motors.move_abs('z', z)

                    # Delay between move command and state check is crucial, the latter fails otherwise
                    # Controller response time is several ms

                    time.sleep(0.05)

                    while core.motors.is_moving('x') or core.motors.is_moving('y') or core.motors.is_moving('z'):
                        time.sleep(0.05)

                    time.sleep(0.4)

                # Calibration always runs on channel 1
                if core.oscilloscope is not None:
                    t, v = core.oscilloscope.get_waveform(1)
                    magnitudes.append(numpy.sum(v))

                if core.hv_source is not None:
                    with core.hv_source.lock:
                        i = core.hv_source.get_current()[1]
                    i = 1
                    currents.append(i)
                    self.result['currents'][(x, z)] = i
                currents.append(1)
                self.result['currents'][(x, z)] = 1

                time.sleep(0.2)
                current_steps += 1
                core.measurement_state = current_steps, total_steps

            p0 = [currents[0], 1, 0, 1]
            try:
                popt, pcov = curve_fit(fit_current, xrange, currents, p0=p0)
                width = popt[3]
            except RuntimeError:
                self.result['fit_failed_z'].append(z)
                width = numpy.nan
            widths.append(width)
            self.result['widths'][(x, z)] = width

        widths = numpy.array(widths)
        valid = ~numpy.isnan(widths)

        self.result['zrange'] = zrange
        self.result['width_vs_z'] = widths

        if numpy.any(valid):
            p0 = [1, 1, 1]
            try:
                popt, pcov = curve_fit(fit_width, zrange[valid], widths[valid], p0=p0)
                self.result['width_fit_params'] = popt
            except RuntimeError:
                self.result['width_fit_failed'] = True
        else:
            self.result['width_fit_failed'] = True

        self.save_data()

    # Save data and images in path
    def save_data(self):
        with open(self.settings['path'], 'wb') as f:
            pickle.dump(self.result, f)

        if 'zrange' in self.result and 'width_vs_z' in self.result:
            zrange = self.result['zrange']
            widths = self.result['width_vs_z']

            fig = Figure()
            FigureCanvasAgg(fig)
            ax = fig.add_subplot(111)
            ax.plot(zrange, widths, 'o', label='measured')

            if 'width_fit_params' in self.result:
                z_fit = numpy.linspace(zrange[0], zrange[-1], 200)
                ax.plot(z_fit, fit_width(z_fit, *self.result['width_fit_params']), '-', label='fit')
                ax.legend()

            ax.set_xlabel('z')
            ax.set_ylabel('beam width')
            ax.set_title('Focus calibration')

            image_path = os.path.splitext(self.settings['path'])[0] + '.png'
            fig.savefig(image_path)

        

    class Dialog(QDialog):
        default_settings = {'xrange': (0, 1, 0.01),
                            'zrange': (0, 2, 0.1),
                            'path': '/home/tct/output/focus_calibration.pickle'}
        cached_settings = default_settings

        def __init__(self, parent=None):
            super().__init__(parent)
            self.setModal(True)
            self.setWindowTitle('Focus Calibration Configuration')

            self.ret = None

            self.inputs = []

            if core.motors is not None:
                x0 = core.motors.get_position('x')
                z0 = core.motors.get_position('z')
                CalibrateFocusWorker.Dialog.cached_settings = dict(CalibrateFocusWorker.Dialog.cached_settings)
                CalibrateFocusWorker.Dialog.cached_settings['xrange'] = (x0, x0 + 2, 0.01)
                CalibrateFocusWorker.Dialog.cached_settings['zrange'] = (z0, z0 + 2, 0.1)

            self.instructions_label = QLabel(
                'Set the x/z scan ranges below, then click "Start scan". '
                'Ranges default to the current motor position when motors are connected. '
                'Please align the laser such that the focus is to the left of the knife edge of the PCB ' \
                'in the x-axis, looking downstream of the beam. ')
            self.instructions_label.setWordWrap(True)

            texts = ['xstart', 'xstop', 'xstep', 'zstart', 'zstop', 'zstep']
            values = ['xrange'] * 3 + ['zrange'] * 3
            values = list(zip(values, [0, 1, 2] * 2))

            range_box = QGroupBox()
            range_box.setTitle('Coordinate ranges')
            range_box_layout = QGridLayout()
            for i in range(len(values)):
                range_box_layout.addWidget(QLabel(texts[i]), i // 3, (i % 3) * 2)
                spinbox = QDoubleSpinBox()
                spinbox.setDecimals(4)
                spinbox.setRange(0, 50)
                if (i % 3 == 2):
                    spinbox.setRange(-50, 50)
                spinbox.setValue(CalibrateFocusWorker.Dialog.cached_settings[values[i][0]][values[i][1]])

                self.inputs.append(spinbox)
                range_box_layout.addWidget(spinbox, i // 3, (i % 3) * 2 + 1)
            range_box.setLayout(range_box_layout)

            file_box = QGroupBox()
            file_box.setTitle('Output file')
            file_box_layout = QHBoxLayout()
            self.filename = QLineEdit(CalibrateFocusWorker.Dialog.cached_settings['path'])
            file_box_layout.addWidget(self.filename)
            self.file_button = QPushButton('Select...')
            self.file_button.clicked.connect(self.choose_file)
            file_box_layout.addWidget(self.file_button)
            file_box.setLayout(file_box_layout)

            run_box = QWidget()
            run_layout = QHBoxLayout()
            self.ok_button = QPushButton('Start scan')
            self.ok_button.clicked.connect(self.finalize)
            run_layout.addWidget(self.ok_button)
            self.cancel_button = QPushButton('Cancel')
            self.cancel_button.clicked.connect(self.decline)
            run_layout.addWidget(self.cancel_button)
            run_box.setLayout(run_layout)

            layout = QVBoxLayout()
            layout.addWidget(self.instructions_label)
            layout.addWidget(range_box)
            layout.addWidget(file_box)
            layout.addWidget(run_box)
            self.setLayout(layout)

        def __del__(self):
            CalibrateFocusWorker.Dialog.cached_settings = self.extract_settings()

        def choose_file(self):
            self.filename.setText(QFileDialog.getSaveFileName(
                None,
                'Select output file',
                '/home/tct/output',
                '', '')[0])

        def extract_settings(self):
            f = lambda x: tuple([i.value() for i in self.inputs[x:x + 3]])
            xrange, zrange = f(0), f(3)
            output_path = self.filename.text()

            ret = {'xrange': xrange,
                   'zrange': zrange,
                   'path': output_path}
            return ret

        def decline(self):
            """
            Executes on 'cancel' click. Just updates cached settings.
            """
            CalibrateFocusWorker.Dialog.cached_settings = self.extract_settings()
            self.reject()

        def finalize(self):
            """
            Executes on 'ok' click. Check correctness of user input. If correct, exit dialog and store data in its self.ret attribute
            """

            f = lambda x: tuple([i.value() for i in self.inputs[x:x + 3]])
            xrange, zrange = f(0), f(3)
            output_path = self.filename.text()

            if not (xrange[2] and zrange[2]):
                QMessageBox(QMessageBox.Warning, 'Configuration error', 'Step can\'t be 0!', parent=self).exec()
                return
            if not (((xrange[1]-xrange[0])/xrange[2]) >= 10) or not (((zrange[1]-zrange[0])/zrange[2]) >= 10):
                QMessageBox(QMessageBox.Warning, "Configuration error", "Number of steps needs to be 10 or more!", parent=self).exec()
                return
            if not (len(numpy.arange(*xrange)) and len(numpy.arange(*zrange))):
                QMessageBox(QMessageBox.Warning, 'Configuration error', 'Must be at least one step!',
                            parent=self).exec()
                return
            self.ret = self.extract_settings()
            CalibrateFocusWorker.Dialog.cached_settings = self.ret
            self.accept()