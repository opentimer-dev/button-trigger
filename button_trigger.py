#!/usr/bin/env python3
"""
Log button presses on GPIO27 (BCM numbering) to /var/log/button-press
and broadcast each press as a D-Bus signal on the system bus.

The pin is configured with the internal pull-up enabled, so it idles HIGH.
When it is pulled LOW (e.g. a button wired between GPIO27 and GND), a
timestamp is appended to the log file and emitted as:

    path:      /dev/opentimer/ButtonPress
    interface: dev.opentimer.ButtonPress
    signal:    Pressed(s timestamp)

Watch for it with:
    dbus-monitor --system "type='signal',interface='dev.opentimer.ButtonPress'"

Requires: gpiozero (preinstalled on Raspberry Pi OS) and jeepney
    sudo apt install python3-jeepney
Run as root (or a user with write access to /var/log/button-press):
    sudo python3 button_press.py
"""

import os
import shutil
import signal
import sys
import tempfile
import threading
import time
from datetime import datetime

from gpiozero import Button
from jeepney import DBusAddress, new_signal
from jeepney.io.blocking import open_dbus_connection

GPIO_PIN = 27                      # BCM pin number (physical header pin 13)
LOG_PATH = "/var/log/button-press"
LOG_MAX_LINES = 200                # at startup, trim the log if it exceeds this...
LOG_KEEP_LINES = 50                # ...down to this many most recent lines
DEBOUNCE_S = 0.05                  # 50 ms debounce; set to None to disable

DBUS_OBJECT_PATH = "/dev/opentimer/ButtonPress"
DBUS_INTERFACE = "dev.opentimer.ButtonPress"
DBUS_SIGNAL = "Pressed"


def trim_log(path: str, max_lines: int, keep_lines: int) -> None:
    """If the log has more than max_lines lines, keep only the last keep_lines.

    Writes to a temp file in the same directory and atomically replaces the
    original, so a crash or power loss mid-trim can't leave a half-written log.
    """
    try:
        with open(path, "r", errors="replace") as f:
            lines = f.readlines()
    except FileNotFoundError:
        return  # nothing to trim; it will be created on open

    if len(lines) <= max_lines:
        return

    directory = os.path.dirname(path) or "."
    fd, tmp_path = tempfile.mkstemp(dir=directory, prefix=".button-press.")
    try:
        with os.fdopen(fd, "w") as tmp:
            tmp.writelines(lines[-keep_lines:])
            tmp.flush()
            os.fsync(tmp.fileno())
        shutil.copymode(path, tmp_path)  # keep the original file's permissions
        os.replace(tmp_path, path)
    except Exception:
        try:
            os.unlink(tmp_path)
        except OSError:
            pass
        raise


class DBusEmitter:
    """Sends Pressed(d) signals on the system bus, reconnecting if needed."""

    def __init__(self) -> None:
        self._addr = DBusAddress(DBUS_OBJECT_PATH, interface=DBUS_INTERFACE)
        self._conn = None
        self._lock = threading.Lock()

    def _connect(self) -> None:
        self._conn = open_dbus_connection(bus="SYSTEM")

    def emit(self, timestamp: str) -> None:
        msg = new_signal(self._addr, DBUS_SIGNAL, "s", (timestamp,))
        with self._lock:
            for attempt in (1, 2):  # one retry with a fresh connection
                try:
                    if self._conn is None:
                        self._connect()
                    print(f'emitting message {msg}')
                    self._conn.send(msg)
                    return
                except Exception as exc:
                    self.close_unlocked()
                    if attempt == 2:
                        print(f"D-Bus emit failed: {exc}", file=sys.stderr)

    def close_unlocked(self) -> None:
        if self._conn is not None:
            try:
                self._conn.close()
            except Exception:
                pass
            self._conn = None

    def close(self) -> None:
        with self._lock:
            self.close_unlocked()


def main() -> None:
    try:
        trim_log(LOG_PATH, LOG_MAX_LINES, LOG_KEEP_LINES)
    except PermissionError:
        sys.exit(f"Cannot write to {LOG_PATH}: run with sudo or fix permissions.")
    except OSError as exc:
        # Not fatal: keep logging even if the trim failed.
        print(f"Could not trim {LOG_PATH}: {exc}", file=sys.stderr)

    try:
        log = open(LOG_PATH, "a", buffering=1)  # line-buffered
    except PermissionError:
        sys.exit(f"Cannot write to {LOG_PATH}: run with sudo or fix permissions.")

    dbus = DBusEmitter()

    # pull_up=True -> internal pull-up, pin idles HIGH, "pressed" == LOW
    button = Button(GPIO_PIN, pull_up=True, bounce_time=DEBOUNCE_S)

    def on_press() -> None:
        now = datetime.now().isoformat()
        mon = int(time.monotonic() * 1_000_000_000)
        # File first, so a D-Bus problem never costs us a log entry.
        log.write(f"{now} {mon}\n")
        log.flush()
        dbus.emit(now)

    button.when_pressed = on_press

    def shutdown(signum, frame):
        button.close()
        dbus.close()
        log.close()
        sys.exit(0)

    signal.signal(signal.SIGTERM, shutdown)
    signal.signal(signal.SIGINT, shutdown)

    signal.pause()  # sleep until a signal arrives; callbacks run in the background


if __name__ == "__main__":
    main()