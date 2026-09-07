"""
GW Instek GDM-8342 - Real-time measurement GUI (dual display)
=============================================================

The CSV is written to the folder that contains THIS script (auto-saved on Stop;
"Export CSV" also lets you save a copy elsewhere). CSV values follow the unit
selected when saved: a fixed unit -> that unit; "Auto" -> base SI unit.

The GDM-8342 only measures. It reads:
  - Primary display   -> VAL1?
  - Secondary display -> VAL2?   (tick "Read secondary")

Backends:
  - Serial (USB / RS-232) : pip install pyserial     (default; GDM-8342 USB = COM port)
  - VISA (GPIB/USB/RS-232): pip install pyvisa pyvisa-py
Also needs:  pip install matplotlib
Run:  python gdm8342_gui.py        (tick "Demo mode" to try without hardware)
"""

import os
import csv
import math
import time
import queue
import threading
from datetime import datetime

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

import matplotlib
matplotlib.use("TkAgg")
from matplotlib.figure import Figure
from matplotlib.backends.backend_tkagg import FigureCanvasTkAgg

try:
    import pyvisa
except ImportError:
    pyvisa = None
try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

# Measurement function -> (SCPI CONF command sent once at start, base SI unit).
FUNCTIONS = {
    "Keep current (front panel)": (None, ""),
    "DC Voltage":       ("CONF:VOLT:DC", "V"),
    "AC Voltage":       ("CONF:VOLT:AC", "V"),
    "DC Current":       ("CONF:CURR:DC", "A"),
    "AC Current":       ("CONF:CURR:AC", "A"),
    "Resistance (2W)":  ("CONF:RES",     "\u03a9"),
    "Resistance (4W)":  ("CONF:FRES",    "\u03a9"),
    "Capacitance":      ("CONF:CAP",     "F"),
    "Frequency":        ("CONF:FREQ",    "Hz"),
    "Period":           ("CONF:PER",     "s"),
    "Diode":            ("CONF:DIOD",    "V"),
    "Continuity":       ("CONF:CONT",    "\u03a9"),
}

# Selectable fixed units per base unit -> (label, size in base units).
UNIT_TABLE = {
    "V":       [("kV", 1e3), ("V", 1.0), ("mV", 1e-3), ("\u00b5V", 1e-6)],
    "A":       [("A", 1.0), ("mA", 1e-3), ("\u00b5A", 1e-6), ("nA", 1e-9)],
    "\u03a9":  [("G\u03a9", 1e9), ("M\u03a9", 1e6), ("k\u03a9", 1e3), ("\u03a9", 1.0)],
    "F":       [("mF", 1e-3), ("\u00b5F", 1e-6), ("nF", 1e-9), ("pF", 1e-12)],
    "Hz":      [("MHz", 1e6), ("kHz", 1e3), ("Hz", 1.0)],
    "s":       [("s", 1.0), ("ms", 1e-3), ("\u00b5s", 1e-6)],
}
SI_PREFIXES = [(1e9, "G"), (1e6, "M"), (1e3, "k"), (1.0, ""),
               (1e-3, "m"), (1e-6, "\u00b5"), (1e-9, "n"), (1e-12, "p")]
VISA_LIBS = {"auto": "", "pyvisa-py": "@py", "NI-VISA": "@ivi"}


def script_dir():
    try:
        return os.path.dirname(os.path.abspath(__file__))
    except NameError:
        return os.getcwd()


def auto_factor(maxabs):
    """Pick (factor, prefix) so a value near maxabs reads in [1, 1000)."""
    if not maxabs:
        return 1.0, ""
    for factor, pre in SI_PREFIXES:
        if maxabs >= factor:
            return factor, pre
    return SI_PREFIXES[-1]


def eng(value, unit):
    """Engineering-notation string for one value, e.g. 0.005 V -> '5 mV'."""
    if value is None or (isinstance(value, float) and math.isnan(value)):
        return "\u2014"
    if value == 0:
        return "0 %s" % unit if unit else "0"
    factor, pre = auto_factor(abs(value))
    return "%.4g %s%s" % (value / factor, pre, unit)


# ----------------------------------------------------------------------------
# Instrument transports
# ----------------------------------------------------------------------------
class SerialTransport:
    def __init__(self, port, baud, timeout, term):
        self.term = term
        self.ser = serial.Serial(
            port, baud, timeout=timeout,
            bytesize=serial.EIGHTBITS, parity=serial.PARITY_NONE,
            stopbits=serial.STOPBITS_ONE,
        )
        time.sleep(0.2)
        self.ser.reset_input_buffer()

    def write(self, cmd):
        self.ser.write((cmd + self.term).encode("ascii"))

    def query(self, cmd):
        self.ser.write((cmd + self.term).encode("ascii"))
        return self.ser.readline().decode("ascii", errors="replace").strip()

    def close(self):
        try:
            self.ser.close()
        except Exception:
            pass


class VisaTransport:
    def __init__(self, resource, backend, baud, timeout, term):
        self.rm = pyvisa.ResourceManager(backend)
        self.inst = self.rm.open_resource(resource)
        self.inst.timeout = int(timeout * 1000)
        self.inst.write_termination = term
        self.inst.read_termination = term
        if resource.upper().startswith("ASRL"):
            import pyvisa.constants as pc
            self.inst.baud_rate = baud
            self.inst.data_bits = 8
            self.inst.parity = pc.Parity.none
            self.inst.stop_bits = pc.StopBits.one

    def write(self, cmd):
        self.inst.write(cmd)

    def query(self, cmd):
        return self.inst.query(cmd).strip()

    def close(self):
        try:
            self.inst.close()
            self.rm.close()
        except Exception:
            pass


class GDM8342:
    def __init__(self, transport, read_secondary):
        self.t = transport
        self.read_secondary = read_secondary

    def configure(self, func_cmd):
        try:
            self.t.write("SYST:REM")
        except Exception:
            pass
        if func_cmd:
            self.t.write(func_cmd)
            time.sleep(0.3)

    def idn(self):
        return self.t.query("*IDN?")

    def measure(self):
        p = self._to_float(self.t.query("VAL1?"))
        s = self._to_float(self.t.query("VAL2?")) if self.read_secondary else None
        return p, s

    @staticmethod
    def _to_float(raw):
        try:
            return float(raw)
        except (ValueError, TypeError):
            return None

    def close(self):
        try:
            self.t.write("SYST:LOC")
        except Exception:
            pass
        self.t.close()


class DemoDMM:
    def __init__(self, read_secondary):
        self.read_secondary = read_secondary
        self._t0 = time.time()

    def configure(self, func_cmd):
        pass

    def idn(self):
        return "DEMO,GDM-8342,SN0,1.00"

    def measure(self):
        import random
        t = time.time() - self._t0
        p = 0.500 + 0.10 * math.sin(t) + random.gauss(0, 0.003)
        s = (1000 + 40 * math.sin(1.3 * t) + random.gauss(0, 1.5)) if self.read_secondary else None
        return p, s

    def close(self):
        pass


# ----------------------------------------------------------------------------
# GUI application
# ----------------------------------------------------------------------------
class App(tk.Tk):
    POLL_MS = 100
    MAX_TABLE_ROWS = 500
    MAX_PLOT_POINTS = 2000

    def __init__(self):
        super().__init__()
        self.title("GW Instek GDM-8342 - Real-time Measurement")
        self.geometry("1180x700")

        self.dmm = None
        self.worker = None
        self._stop_event = threading.Event()
        self._data_q = queue.Queue()
        self._running = False
        self._start_time = 0.0
        self._meta = {}
        self._auto_name = ""
        self.read_secondary = False
        self.base_unit = "V"

        self.rows = []
        self._t, self._p, self._s = [], [], []
        self._line_p = self._line_s = None

        self._build_controls()
        self._build_readout()
        self._build_body()
        self._build_statusbar()

        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self._on_backend_change()
        self._on_func_change()
        self._setup_plot()

    # ----- layout ---------------------------------------------------------
    def _build_controls(self):
        f = ttk.LabelFrame(self, text="Configuration")
        f.pack(fill="x", padx=8, pady=(8, 4))
        left = ttk.Frame(f); left.pack(side="left", fill="x", expand=True)
        bcol = ttk.Frame(f); bcol.pack(side="right", padx=10)

        self.btn_start = ttk.Button(bcol, text="Start", width=10, command=self._start)
        self.btn_start.pack(pady=2)
        self.btn_stop = ttk.Button(bcol, text="Stop", width=10, command=self._stop, state="disabled")
        self.btn_stop.pack(pady=2)
        self.btn_export = ttk.Button(bcol, text="Export CSV", width=10, command=self._export, state="disabled")
        self.btn_export.pack(pady=2)

        # row 0 - connection
        ttk.Label(left, text="Backend:").grid(row=0, column=0, sticky="e", padx=4, pady=4)
        self.backend = ttk.Combobox(left, width=8, state="readonly", values=["Serial", "VISA"])
        self.backend.set("Serial")
        self.backend.grid(row=0, column=1, sticky="w", padx=4)
        self.backend.bind("<<ComboboxSelected>>", self._on_backend_change)

        self.lbl_addr = ttk.Label(left, text="Serial port:")
        self.lbl_addr.grid(row=0, column=2, sticky="e", padx=4)
        self.addr = ttk.Combobox(left, width=22, values=["COM3"])
        self.addr.set("COM3")
        self.addr.grid(row=0, column=3, columnspan=2, sticky="w", padx=4)
        ttk.Button(left, text="Scan", command=self._scan).grid(row=0, column=5, padx=4)
        ttk.Label(left, text="Baud:").grid(row=0, column=6, sticky="e", padx=4)
        self.baud = tk.StringVar(value="115200")
        ttk.Entry(left, textvariable=self.baud, width=8).grid(row=0, column=7, sticky="w", padx=4)

        # row 1 - measurement + signal unit
        ttk.Label(left, text="Function:").grid(row=1, column=0, sticky="e", padx=4, pady=4)
        self.func = ttk.Combobox(left, width=22, state="readonly", values=list(FUNCTIONS.keys()))
        self.func.set("DC Voltage")
        self.func.grid(row=1, column=1, columnspan=3, sticky="w", padx=4)
        self.func.bind("<<ComboboxSelected>>", self._on_func_change)

        ttk.Label(left, text="Signal unit:").grid(row=1, column=4, sticky="e", padx=4)
        self.unit_sel = ttk.Combobox(left, width=8, state="readonly", values=["Auto"])
        self.unit_sel.set("Auto")
        self.unit_sel.grid(row=1, column=5, sticky="w", padx=4)
        self.unit_sel.bind("<<ComboboxSelected>>", self._on_unit_change)

        ttk.Label(left, text="Interval (s):").grid(row=1, column=6, sticky="e", padx=4)
        self.interval = tk.StringVar(value="0.5")
        ttk.Entry(left, textvariable=self.interval, width=8).grid(row=1, column=7, sticky="w", padx=4)

        # row 2 - options
        self.sec_var = tk.BooleanVar(value=False)
        self.chk_sec = ttk.Checkbutton(left, text="Read secondary (VAL2?)", variable=self.sec_var)
        self.chk_sec.grid(row=2, column=0, columnspan=3, sticky="w", padx=4, pady=4)

        self.lbl_visalib = ttk.Label(left, text="VISA lib:")
        self.lbl_visalib.grid(row=2, column=3, sticky="e", padx=4)
        self.visalib = ttk.Combobox(left, width=8, state="readonly", values=list(VISA_LIBS.keys()))
        self.visalib.set("auto")
        self.visalib.grid(row=2, column=4, sticky="w", padx=4)

        self.demo = tk.BooleanVar(value=False)
        ttk.Checkbutton(left, text="Demo mode", variable=self.demo
                        ).grid(row=2, column=5, columnspan=2, sticky="w", padx=8)

        ttk.Label(left,
                  text="CSV auto-saved next to this script on Stop. Signal unit can be changed live.",
                  foreground="#555").grid(row=3, column=0, columnspan=8, sticky="w", padx=6, pady=(2, 4))

    def _build_readout(self):
        f = ttk.Frame(self); f.pack(fill="x", padx=8, pady=2)
        self.lbl_primary = self._big(f, "Primary", "---")
        self.lbl_secondary = self._big(f, "Secondary", "\u2014")
        self.lbl_n = self._big(f, "Samples", "0")

    def _big(self, parent, title, initial):
        box = ttk.LabelFrame(parent, text=title)
        box.pack(side="left", expand=True, fill="x", padx=4)
        lbl = ttk.Label(box, text=initial, font=("Consolas", 22))
        lbl.pack(padx=8, pady=6)
        return lbl

    def _build_body(self):
        paned = ttk.PanedWindow(self, orient="horizontal")
        paned.pack(fill="both", expand=True, padx=8, pady=4)

        leftf = ttk.Frame(paned)
        cols = ("n", "t", "ts", "p", "s")
        heads = ("#", "Time (s)", "Clock", "Primary", "Secondary")
        widths = (55, 90, 120, 140, 140)
        self.tree = ttk.Treeview(leftf, columns=cols, show="headings", height=18)
        for c, h, wdt in zip(cols, heads, widths):
            self.tree.heading(c, text=h)
            self.tree.column(c, width=wdt, anchor="center")
        vsb = ttk.Scrollbar(leftf, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        paned.add(leftf, weight=1)

        rightf = ttk.Frame(paned)
        self.fig = Figure(figsize=(5, 4), dpi=100)
        self.ax_p = self.fig.add_subplot(111)
        self.ax_s = self.ax_p.twinx()
        self.canvas = FigureCanvasTkAgg(self.fig, master=rightf)
        self.canvas.get_tk_widget().pack(fill="both", expand=True)
        paned.add(rightf, weight=2)

    def _build_statusbar(self):
        self.status = tk.StringVar(value="Ready.")
        ttk.Label(self, textvariable=self.status, relief="sunken", anchor="w"
                  ).pack(fill="x", side="bottom")

    # ----- dynamic UI -----------------------------------------------------
    def _on_backend_change(self, *_):
        visa = self.backend.get() == "VISA"
        self.lbl_addr.config(text="VISA resource:" if visa else "Serial port:")
        self.visalib.config(state="readonly" if visa else "disabled")
        self.lbl_visalib.config(foreground="#000" if visa else "#aaa")
        if visa and self.addr.get() in ("COM3", ""):
            self.addr.set("ASRL3::INSTR")
        elif not visa and self.addr.get().startswith(("ASRL", "GPIB")):
            self.addr.set("COM3")

    def _on_func_change(self, *_):
        _, self.base_unit = FUNCTIONS[self.func.get()]
        opts = ["Auto"] + [lab for lab, _ in UNIT_TABLE.get(self.base_unit, [])]
        self.unit_sel["values"] = opts
        self.unit_sel.set("Auto")
        self.tree.heading("p", text=self._primary_heading())

    def _on_unit_change(self, *_):
        self.tree.heading("p", text=self._primary_heading())
        if self.rows:
            self._rerender()

    def _setup_plot(self):
        self.ax_p.clear(); self.ax_s.clear()
        self._line_p = self._line_s = None
        self.ax_p.set_xlabel("Time (s)")
        self.ax_p.set_ylabel(self._signal_label(0.0), color="tab:blue")
        (self._line_p,) = self.ax_p.plot([], [], color="tab:blue")
        if self.read_secondary:
            self.ax_s.set_visible(True)
            self.ax_s.set_ylabel("Secondary", color="tab:red")
            (self._line_s,) = self.ax_s.plot([], [], color="tab:red")
        else:
            self.ax_s.set_visible(False)
        self.ax_p.set_xlim(0, 1)
        self.ax_p.set_ylim(-1, 1)
        self.canvas.draw_idle()

    # ----- unit / formatting helpers -------------------------------------
    def _disp(self):
        """Return (factor, label) for the selected fixed unit, or None for Auto."""
        sel = self.unit_sel.get()
        if sel == "Auto":
            return None
        for lab, factor in UNIT_TABLE.get(self.base_unit, []):
            if lab == sel:
                return factor, lab
        return 1.0, self.base_unit

    def _signal_label(self, maxabs):
        d = self._disp()
        if d is None:
            _, pre = auto_factor(maxabs)
            unit = pre + self.base_unit
        else:
            unit = d[1]
        return "Signal (%s)" % unit if unit else "Signal"

    def _primary_heading(self):
        d = self._disp()
        return "Primary" if d is None else "Primary (%s)" % d[1]

    def _fmt_primary_cell(self, v):
        if v is None:
            return "\u2014"
        d = self._disp()
        if d is None:
            return eng(v, self.base_unit)
        return "%.6g" % (v / d[0])

    def _fmt_primary_readout(self, v):
        if v is None:
            return "\u2014"
        d = self._disp()
        if d is None:
            return eng(v, self.base_unit)
        return "%.6g %s" % (v / d[0], d[1])

    @staticmethod
    def _fmt_secondary(v):
        return ("%.6g" % v) if v is not None else "\u2014"

    # ----- connection helpers --------------------------------------------
    def _floatf(self, var, name):
        try:
            return float(var.get())
        except ValueError:
            raise ValueError("Invalid number for '%s'." % name)

    def _scan(self):
        try:
            if self.backend.get() == "Serial":
                if serial is None:
                    raise RuntimeError("pyserial not installed: pip install pyserial")
                ports = [p.device for p in serial.tools.list_ports.comports()]
                if ports:
                    self.addr["values"] = ports; self.addr.set(ports[0])
                    self.status.set("Found %d serial port(s)." % len(ports))
                else:
                    self.status.set("No serial ports found.")
            else:
                if pyvisa is None:
                    raise RuntimeError("pyvisa not installed: pip install pyvisa")
                res = list(pyvisa.ResourceManager(VISA_LIBS[self.visalib.get()]).list_resources())
                if res:
                    self.addr["values"] = res; self.addr.set(res[0])
                    self.status.set("Found %d VISA resource(s)." % len(res))
                else:
                    self.status.set("No VISA resources found.")
        except Exception as e:
            messagebox.showerror("Scan failed", str(e))

    def _open_transport(self):
        baud = int(self._floatf(self.baud, "Baud"))
        if self.backend.get() == "Serial":
            if serial is None:
                raise RuntimeError("pyserial is not installed. pip install pyserial (or tick Demo mode).")
            return SerialTransport(self.addr.get().strip(), baud, 2.0, "\n")
        if pyvisa is None:
            raise RuntimeError("pyvisa is not installed. pip install pyvisa (or tick Demo mode).")
        return VisaTransport(self.addr.get().strip(), VISA_LIBS[self.visalib.get()], baud, 2.0, "\n")

    # ----- control callbacks ---------------------------------------------
    def _start(self):
        if self._running:
            return
        try:
            self.interval_s = max(0.0, self._floatf(self.interval, "Interval"))
        except ValueError as e:
            messagebox.showerror("Input error", str(e)); return

        self.read_secondary = self.sec_var.get()
        func_cmd, self.base_unit = FUNCTIONS[self.func.get()]

        try:
            if self.demo.get():
                self.dmm = DemoDMM(self.read_secondary)
            else:
                self.dmm = GDM8342(self._open_transport(), self.read_secondary)
            try:
                idn = self.dmm.idn()
            except Exception:
                idn = "(no *IDN? response)"
            self.dmm.configure(func_cmd)
        except Exception as e:
            messagebox.showerror("Connection error", str(e))
            if self.dmm:
                self.dmm.close()
            self.dmm = None
            return

        self._auto_name = "gdm8342_%s.csv" % datetime.now().strftime("%Y%m%d_%H%M%S")
        self._meta = {
            "function": self.func.get(),
            "interval": self.interval_s,
            "start_iso": datetime.now().isoformat(timespec="seconds"),
        }

        self.rows.clear(); self._t.clear(); self._p.clear(); self._s.clear()
        self.tree.delete(*self.tree.get_children())
        self.tree["displaycolumns"] = ("n", "t", "ts", "p", "s") if self.read_secondary else ("n", "t", "ts", "p")
        self._setup_plot()
        self.lbl_primary.config(text="---")
        self.lbl_secondary.config(text="\u2014")
        self.lbl_n.config(text="0")

        self._start_time = time.time()
        self._stop_event.clear()
        self._running = True
        self._set_running(True)
        self.status.set("Measuring... %s" % idn)

        self.worker = threading.Thread(target=self._worker_loop, daemon=True)
        self.worker.start()
        self.after(self.POLL_MS, self._poll_queue)

    def _stop(self):
        if not self._running:
            return
        self._stop_event.set()
        self.btn_stop.config(state="disabled")
        self.status.set("Stopping...")

    def _worker_loop(self):
        n = 0
        next_t = time.time()
        try:
            while not self._stop_event.is_set():
                p, s = self.dmm.measure()
                n += 1
                elapsed = time.time() - self._start_time
                ts = datetime.now().strftime("%H:%M:%S.%f")[:-3]
                self._data_q.put(("data", (n, elapsed, ts, p, s)))
                next_t += self.interval_s
                nap = next_t - time.time()
                if nap > 0:
                    self._stop_event.wait(nap)
                else:
                    next_t = time.time()
        except Exception as e:
            self._data_q.put(("error", str(e)))
        finally:
            self._data_q.put(("done", None))

    # ----- GUI-thread queue handling -------------------------------------
    def _poll_queue(self):
        new = False; err = None; done = False
        try:
            while True:
                kind, payload = self._data_q.get_nowait()
                if kind == "data":
                    self._store_row(payload); new = True
                elif kind == "error":
                    err = payload
                elif kind == "done":
                    done = True
        except queue.Empty:
            pass

        if new:
            self._refresh_plot(); self._refresh_readout()
        if err:
            messagebox.showerror("Measurement error", err)
        if done:
            self._finalize()
        elif self._running:
            self.after(self.POLL_MS, self._poll_queue)

    def _store_row(self, payload):
        n, el, ts, p, s = payload
        self.rows.append(payload)
        self._t.append(el); self._p.append(p); self._s.append(s)
        self.tree.insert("", "end",
                         values=(n, "%.2f" % el, ts, self._fmt_primary_cell(p), self._fmt_secondary(s)))
        kids = self.tree.get_children()
        if len(kids) > self.MAX_TABLE_ROWS:
            for k in kids[:len(kids) - self.MAX_TABLE_ROWS]:
                self.tree.delete(k)
        self.tree.yview_moveto(1.0)

    def _rerender(self):
        """Rebuild table + plot from stored raw values (after a unit change)."""
        self.tree.delete(*self.tree.get_children())
        for n, el, ts, p, s in self.rows[-self.MAX_TABLE_ROWS:]:
            self.tree.insert("", "end",
                             values=(n, "%.2f" % el, ts, self._fmt_primary_cell(p), self._fmt_secondary(s)))
        self.tree.yview_moveto(1.0)
        self._refresh_plot()
        self._refresh_readout()

    def _refresh_plot(self):
        n = len(self._t)
        step = 1 if n <= self.MAX_PLOT_POINTS else (n // self.MAX_PLOT_POINTS + 1)
        t = self._t[::step]

        # primary (selected/auto unit)
        vals = [x for x in self._p if x is not None]
        maxabs = max((abs(x) for x in vals), default=0.0)
        d = self._disp()
        factor = d[0] if d is not None else auto_factor(maxabs)[0]
        ydata = [(x / factor if x is not None else float("nan")) for x in self._p[::step]]
        self._line_p.set_data(t, ydata)
        self.ax_p.set_ylabel(self._signal_label(maxabs), color="tab:blue")
        self._apply_ylim(self.ax_p, ydata)

        # secondary (raw)
        if self._line_s is not None:
            sdata = [(x if x is not None else float("nan")) for x in self._s[::step]]
            self._line_s.set_data(t, sdata)
            self._apply_ylim(self.ax_s, sdata)

        tmax = self._t[-1] if self._t else 1.0
        self.ax_p.set_xlim(0, max(tmax, 1e-3))
        self.canvas.draw_idle()

    def _refresh_readout(self):
        n, el, ts, p, s = self.rows[-1]
        self.lbl_primary.config(text=self._fmt_primary_readout(p))
        self.lbl_secondary.config(text=self._fmt_secondary(s))
        self.lbl_n.config(text=str(n))

    def _finalize(self):
        self._running = False
        if self.dmm:
            self.dmm.close(); self.dmm = None
        self._set_running(False)
        if self.rows:
            path = os.path.join(script_dir(), self._auto_name)
            try:
                self._save_csv(path)
                self.status.set("Stopped - %d points. Auto-saved: %s" % (len(self.rows), path))
            except Exception as e:
                self.status.set("Stopped - %d points. Auto-save FAILED: %s" % (len(self.rows), e))
        else:
            self.status.set("Stopped - no data.")

    # ----- CSV ------------------------------------------------------------
    def _save_csv(self, path):
        m = self._meta
        d = self._disp()
        if d is None:                                   # Auto -> base SI unit
            factor, ulabel = 1.0, (self.base_unit or "value")
        else:
            factor, ulabel = d
        with open(path, "w", newline="") as fh:
            fh.write("# GW Instek GDM-8342 measurement log\n")
            fh.write("# Function: %s | interval=%gs | primary unit=%s | start=%s\n"
                     % (m["function"], m["interval"], ulabel, m["start_iso"]))
            w = csv.writer(fh)
            cols = ["Index", "Time_s", "Clock", "Primary_%s" % ulabel]
            if self.read_secondary:
                cols.append("Secondary")
            w.writerow(cols)
            for n, el, ts, p, s in self.rows:
                row = [n, "%.4f" % el, ts, "%.9g" % (p / factor) if p is not None else ""]
                if self.read_secondary:
                    row.append("%.9g" % s if s is not None else "")
                w.writerow(row)

    def _export(self):
        if not self.rows:
            messagebox.showinfo("No data", "Nothing to export yet."); return
        path = filedialog.asksaveasfilename(
            defaultextension=".csv", initialdir=script_dir(),
            initialfile=self._auto_name or "gdm8342.csv",
            filetypes=[("CSV file", "*.csv"), ("All files", "*.*")])
        if not path:
            return
        try:
            self._save_csv(path)
            self.status.set("Saved: %s" % path)
            messagebox.showinfo("Exported", "Saved %d rows to:\n%s" % (len(self.rows), path))
        except Exception as e:
            messagebox.showerror("Export failed", str(e))

    # ----- helpers --------------------------------------------------------
    @staticmethod
    def _apply_ylim(ax, data):
        vals = [x for x in data if x is not None and not (isinstance(x, float) and math.isnan(x))]
        if not vals:
            return
        lo, hi = min(vals), max(vals)
        span = hi - lo
        if span <= 0:
            span = max(abs(hi), 1.0) * 0.1
        pad = 0.1 * span
        ax.set_ylim(lo - pad, hi + pad)

    def _set_running(self, running):
        self.btn_start.config(state="disabled" if running else "normal")
        self.btn_stop.config(state="normal" if running else "disabled")
        self.btn_export.config(state="disabled" if running else
                               ("normal" if self.rows else "disabled"))
        for w in (self.backend, self.func):
            w.config(state="disabled" if running else "readonly")
        self.chk_sec.config(state="disabled" if running else "normal")

    def _on_close(self):
        self._stop_event.set()
        if self.worker and self.worker.is_alive():
            self.worker.join(timeout=2.0)
        if self.dmm:
            self.dmm.close()
        self.destroy()


if __name__ == "__main__":
    App().mainloop()