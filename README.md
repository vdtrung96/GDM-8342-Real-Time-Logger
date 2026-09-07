# GDM-8342 Real-Time Logger

Real-time data acquisition for the **GW Instek GDM-8342** dual-display digital
multimeter. Read measurements live and export them to CSV — either from a simple
command-line script or a Tkinter GUI with a live table and plot.

- **X axis = Time (s)**, **Y axis = the measured signal**
- Selectable signal unit (kV / V / mV / µV, mA / µA / nA, kΩ / MΩ, …) or **Auto**
  (automatic engineering prefix)
- CSV is written next to the script; auto-saved when you press **Stop**
- Serial (USB / RS-232) or VISA (GPIB / USB / RS-232) backends
- **Demo mode** to try the interface without any hardware

> **Interface note:** the GDM-8342's standard remote ports are a USB device port
> (which enumerates as a virtual COM / CDC serial port) and RS-232. GPIB is *not*
> a standard interface on this model — use the VISA backend only if you have a
> GPIB-USB adapter.

## Requirements

- Python 3.8+
- `matplotlib` (GUI only)
- `pyserial` — for the Serial backend (USB virtual COM / RS-232)
- `pyvisa` + `pyvisa-py` — only if you use the VISA backend

```bash
pip install -r requirements.txt
```

## Usage

### GUI

```bash
python gdm8342_gui.py
```

1. Pick **Backend** (`Serial` is the default for the USB port) and click **Scan**
   to list available ports, then choose your COM port.
2. Choose the **Function** (DC Voltage, Resistance, …), the **Signal unit**, and
   the **Interval** (seconds between samples).
3. Click **Start**. Data streams into the table and plot in real time.
4. Click **Stop** — the CSV is auto-saved next to the script. Use **Export CSV**
   to save an extra copy elsewhere.

Tick **Demo mode** to explore the interface with simulated data first.

### Command line

Edit the configuration block at the top of `gdm8342_logger.py`
(`BACKEND`, `SERIAL_PORT`, `PRIMARY_QUERY`, `SAMPLE_INTERVAL`, …), then run:

```bash
python gdm8342_logger.py
```

Press **Ctrl + C** once to stop; the CSV is finalized on exit.

## Finding your serial port

```bash
python -m serial.tools.list_ports -v
```

On Windows the GDM-8342 appears under *Device Manager → Ports (COM & LPT)*
(e.g. `COM5`). If it shows up with a yellow warning, install the GW Instek USB
CDC driver first.

## CSV format

Two comment lines with the run metadata, then:

```
Index, Time_s, Clock, Primary_<unit>[, Secondary]
```

`Time_s` is the elapsed time; `Primary_<unit>` carries the value in the selected
unit (a fixed unit exports in that unit; **Auto** exports in the base SI unit).

## SCPI commands used

`*IDN?`, `SYST:REM` / `SYST:LOC`, `CONF:<function>`, and `VAL1?` / `VAL2?` to read
the primary / secondary displays. If `VAL1?` returns nothing on your firmware, try
`MEAS:VOLT:DC?` or `FETC?` (edit `measure()` in `GDM8342`).

MIT — see [LICENSE](LICENSE).

## Author

**Vuong Dinh Trung (Jicky)** 
