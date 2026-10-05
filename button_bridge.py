"""Keep USB button events connected to TickTick while the Mac is running."""

import fcntl
import argparse
import hashlib
import json
import os
import signal
import subprocess
import sys
import tempfile
import threading
import time
from contextlib import contextmanager
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import serial
from serial.tools import list_ports

from runtime_paths import runtime_root

TODAY_URL = "ticktick://v1/show?smartlist=today"
TOOLS = runtime_root() / ".tools"
APP_OPENER = ThreadPoolExecutor(max_workers=1, thread_name_prefix="display-app")
CALENDAR_APP_LOOKUP = '''ObjC.import("AppKit");
function run(argv) {
    const app = $.NSWorkspace.sharedWorkspace.URLForApplicationToOpenURL($.NSURL.fileURLWithPath(argv[0]));
    return app ? ObjC.unwrap(app.path) : "";
}'''


def default_calendar_app():
    fallback = "/System/Applications/Calendar.app"
    # Query the default .ics app without importing or opening a calendar file.
    # webcal: can be registered to a browser rather than a calendar application.
    with tempfile.TemporaryDirectory(prefix="ticktick-calendar-app-") as folder:
        probe = Path(folder) / "calendar.ics"
        probe.touch()
        try:
            result = subprocess.run(
                ["/usr/bin/osascript", "-l", "JavaScript", "-e", CALENDAR_APP_LOOKUP, str(probe)],
                capture_output=True, text=True, timeout=5,
            )
        except (OSError, subprocess.TimeoutExpired):
            return fallback
    app = Path(result.stdout.strip())
    # Apple's ICS import helper has no calendar window of its own.
    if result.returncode or app.name == "CalendarFileHandler.app" or app.suffix != ".app" or not app.is_dir():
        return fallback
    return str(app)


def lock_path(port, purpose):
    TOOLS.mkdir(exist_ok=True)
    identifier = hashlib.sha256(port.encode()).hexdigest()[:12]
    return TOOLS / f"{purpose}-{identifier}.lock"


@contextmanager
def port_lock(port, timeout=15):
    with lock_path(port, "usb").open("a") as lock:
        deadline = time.monotonic() + timeout
        while True:
            try:
                fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                break
            except BlockingIOError:
                if time.monotonic() >= deadline:
                    raise RuntimeError("Der USB-Anschluss ist gerade belegt.")
                time.sleep(0.05)
        try:
            yield
        finally:
            fcntl.flock(lock, fcntl.LOCK_UN)


def open_event_app(line):
    if line not in {b"BUTTON TODAY", b"BUTTON CALENDAR"}:
        return
    try:
        command = (["/usr/bin/open", TODAY_URL] if line == b"BUTTON TODAY"
                   else ["/usr/bin/open", "-a", default_calendar_app()])
        result = subprocess.run(command, capture_output=True, timeout=10)
        failed = result.returncode != 0
    except (OSError, subprocess.TimeoutExpired):
        failed = True
    if failed:
        app_name = "TickTick Today" if line == b"BUTTON TODAY" else "Kalender"
        print(f"{app_name} konnte nicht geöffnet werden.", file=sys.stderr, flush=True)


def handle_event(line):
    # Opening an app must not delay USB acknowledgments or other button reads.
    if line not in {b"BUTTON TODAY", b"BUTTON CALENDAR"}:
        return False
    APP_OPENER.submit(open_event_app, line)
    return True


def settings_path(port):
    return lock_path(port, "settings").with_suffix(".json")


def save_settings(port, *, refresh_seconds=60, source="mcp", shortcut="Ticktick Agenda anzeigen", text_file=None):
    settings = {"refresh_seconds": refresh_seconds, "source": source,
                "shortcut": shortcut, "text_file": str(text_file.resolve()) if text_file else None}
    path = settings_path(port)
    fd, temporary = tempfile.mkstemp(dir=path.parent)
    try:
        with os.fdopen(fd, "w") as file:
            json.dump(settings, file)
        os.replace(temporary, path)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def start_listener(port, **settings):
    save_settings(port, **settings)
    # One worker per port, enforced by an OS lock (also released after a crash).
    with lock_path(port, "listener").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
    with (TOOLS / "buttons.log").open("a") as log:
        subprocess.Popen(
            [sys.executable, str(Path(__file__).resolve()), port],
            stdin=subprocess.DEVNULL, stdout=log, stderr=log, start_new_session=True,
        )


def stop_listener(port):
    target = f"gui/{os.getuid()}/local.ticktick-display"
    running = subprocess.run(["/bin/launchctl", "print", target], capture_output=True)
    if running.returncode == 0:
        subprocess.run(["/bin/launchctl", "bootout", target], check=True, capture_output=True)
    if not port:
        return
    with lock_path(port, "listener").open("a+") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except BlockingIOError:
            lock.seek(0)
            pid = lock.read().strip()
            if not pid.isdecimal():
                raise RuntimeError("Der bisherige Tastenempfänger muss einmal neu gestartet werden.")
            os.kill(int(pid), signal.SIGTERM)
            deadline = time.monotonic() + 5
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
                    return
                except BlockingIOError:
                    if time.monotonic() >= deadline:
                        raise RuntimeError("Der Tastenempfänger konnte nicht beendet werden.")
                    time.sleep(0.05)


def refresh_display(port, settings):
    import asyncio
    from anzeigen import agenda_text as shortcut_text, send
    from ticktick_mcp import agenda_text as mcp_text
    from datetime import datetime
    from agenda_format import TIMEZONE
    if settings.get("text_file"):
        text = Path(settings["text_file"]).read_text(encoding="utf-8")
    elif settings.get("source") == "shortcut":
        text = shortcut_text(settings["shortcut"])
    else:
        text = asyncio.run(mcp_text())
    send(text, port)
    print(f"{datetime.now(TIMEZONE):%H:%M:%S} TickTick-Aktualisierung vom Display bestätigt.", flush=True)


def refresh_calendar(port):
    from apple_calendar import CalendarPermissionRequired, render_calendar, today_events
    from anzeigen import send_frames
    from display_render import pack_page
    from datetime import datetime
    from agenda_format import TIMEZONE
    try:
        images = render_calendar(today_events())
        message = "Kalender-Aktualisierung vom Display bestätigt."
    except CalendarPermissionRequired as exc:
        images = render_calendar(permission_required=True, permission_status=exc.status)
        message = "Kalenderfreigabe noch erforderlich."
    send_frames(None, port, left_frames=[pack_page(image) for image in images])
    print(f"{datetime.now(TIMEZONE):%H:%M:%S} {message}", flush=True)


class AutoRefresh:
    """Poll without blocking USB buttons; allow only one refresh at a time."""
    def __init__(self, action, interval=60, clock=time.monotonic):
        self.action, self.interval, self.clock = action, interval, clock
        self.next_due = clock() + interval
        self.worker = None
        self.requested = False

    def set_interval(self, interval):
        if interval != self.interval:
            self.interval = interval
            self.next_due = self.clock() + interval

    def request_now(self):
        self.next_due = self.clock()
        self.requested = True

    def poll(self, connected=True):
        now = self.clock()
        if not connected or now < self.next_due or (self.interval <= 0 and not self.requested):
            return
        if self.worker is not None and self.worker.is_alive():
            return
        self.next_due = now + self.interval
        self.requested = False
        self.worker = threading.Thread(target=self.run, daemon=True)
        self.worker.start()

    def run(self):
        try:
            self.action()
        except Exception as exc:
            # Keep the previous display contents and try again at the next tick.
            print(f"Automatische Aktualisierung fehlgeschlagen ({type(exc).__name__}); nächster Versuch folgt.",
                  file=sys.stderr, flush=True)


def discover_port():
    ports = [p.device.replace("/dev/tty.", "/dev/cu.") for p in list_ports.comports() if p.vid == 0x303A]
    return ports[0] if len(ports) == 1 else None


def listen(port, initial_refresh=False, rediscover=False):
    with lock_path(port, "listener").open("a") as owner:
        try:
            fcntl.flock(owner, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            return
        owner.seek(0)
        owner.truncate()
        owner.write(str(os.getpid()))
        owner.flush()
        settings = json.loads(settings_path(port).read_text())
        refreshes = [
            AutoRefresh(lambda: refresh_display(port, dict(settings)), settings["refresh_seconds"]),
            AutoRefresh(lambda: refresh_calendar(port), settings["refresh_seconds"]),
        ]
        if initial_refresh:
            for refresh in refreshes:
                refresh.request_now()
        next_settings = time.monotonic() + 1
        device = None
        disconnected = False
        try:
            while True:
                if time.monotonic() >= next_settings:
                    settings = json.loads(settings_path(port).read_text())
                    for refresh in refreshes:
                        refresh.set_interval(settings["refresh_seconds"])
                    marker = TOOLS / "refresh-now"
                    if marker.exists():
                        marker.unlink(missing_ok=True)
                        for refresh in refreshes:
                            refresh.request_now()
                    next_settings = time.monotonic() + 1
                try:
                    # Shared I/O lock keeps this reader from consuming transfer
                    # acknowledgments. Both handles use identical serial settings.
                    with port_lock(port, timeout=0):
                        if device is None:
                            device = serial.Serial(port, 115200, timeout=0.1, exclusive=False)
                            if disconnected:
                                print("USB wieder verbunden.", flush=True)
                                for refresh in refreshes:
                                    refresh.request_now()
                            disconnected = False
                        if device.in_waiting:
                            handle_event(device.read_until(b"\n", size=128).strip())
                except RuntimeError:  # An agenda upload owns the I/O lock.
                    pass
                except (OSError, serial.SerialException):
                    if device is not None:
                        device.close()
                        device = None
                    if not disconnected:
                        print("USB nicht verbunden; warte auf das Board.", flush=True)
                    disconnected = True
                    if rediscover and (replacement := discover_port()) and replacement != port:
                        return  # The service will reopen the board under its new USB name.
                    time.sleep(1)
                for refresh in refreshes:
                    refresh.poll(connected=not disconnected)
                time.sleep(0.05)
        finally:
            if device is not None:
                device.close()


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("port", nargs="?", default="auto")
    parser.add_argument("--service", action="store_true")
    args = parser.parse_args()
    if args.service:
        while True:
            port = discover_port() if args.port == "auto" else args.port
            if port:
                if not settings_path(port).exists():
                    save_settings(port)
                listen(port, initial_refresh=True, rediscover=args.port == "auto")
            time.sleep(2)
    else:
        listen(args.port)
