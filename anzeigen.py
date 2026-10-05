#!/usr/bin/env python3
"""TickTick-Aufgaben und Habits → USB → RLCD."""

import argparse
import asyncio
import subprocess
import sys
import tempfile
import time
import zlib
from pathlib import Path

import serial
from serial.tools import list_ports

from button_bridge import discover_port, handle_event, port_lock, start_listener, stop_listener
from display_render import MAX_PAGES, PAGE_BYTES, pack_page, render_pages

SHORTCUT = "Ticktick Agenda anzeigen"
MAX_BYTES = 16384


def agenda_text(name: str) -> str:
    # A private temporary directory; shortcut output is never kept in the repo.
    with tempfile.TemporaryDirectory(prefix="ticktick-display-") as directory:
        output = Path(directory) / "agenda.txt"
        try:
            result = subprocess.run(
                ["/usr/bin/shortcuts", "run", name, "--output-type", "public.plain-text",
                 "--output-path", str(output)],
                capture_output=True, timeout=120,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Der Kurzbefehl wartet noch auf eine Eingabe oder antwortet nicht.") from exc
        if result.returncode:
            raise RuntimeError(f"Der Kurzbefehl „{name}“ konnte nicht ausgeführt werden. Bitte im Kurzbefehle-Editor prüfen.")
        # Shortcuts exports a list of task names as one text file per item.
        if output.is_dir():
            files = sorted(output.iterdir(), key=lambda p: p.name.casefold())
            if any(not p.is_file() or p.suffix.lower() != ".txt" for p in files):
                raise RuntimeError("Die Kurzbefehlausgabe enthält andere Daten als Text.")
            raw = b"\n".join(p.read_bytes().strip() for p in files)
            if not files:
                raw = "Keine Einträge für heute.".encode("utf-8")
        else:
            raw = output.read_bytes() if output.is_file() else result.stdout
        if not raw.strip():
            raise RuntimeError("Der Kurzbefehl hat keinen Agenda-Text zurückgegeben. Die angezeigte Ansicht allein ist keine Textausgabe.")
        try:
            return raw.decode("utf-8-sig").replace("\r\n", "\n").replace("\r", "\n").strip()
        except UnicodeDecodeError as exc:
            raise RuntimeError("Die Kurzbefehlausgabe muss Text sein.") from exc


def find_port() -> str:
    deadline = time.monotonic() + 3
    while True:
        ports = [port.device for port in list_ports.comports() if port.vid == 0x303A]
        if ports or time.monotonic() >= deadline:
            break
        time.sleep(0.2)  # macOS can briefly hide the device during re-enumeration.
    if len(ports) != 1:
        raise RuntimeError("ESP32 nicht eindeutig gefunden. USB verbinden oder --port /dev/cu.usbmodem… angeben.")
    # On macOS use the callout device instead of the dial-in device.
    return ports[0].replace("/dev/tty.", "/dev/cu.")


def wait_for(device, expected: bytes, timeout: float = 10) -> bytes:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        line = device.read_until(b"\n", size=128).strip()
        if handle_event(line):
            continue
        if line == expected or line.startswith(expected + b" "):
            return line
        if line.startswith(b"ERROR"):
            raise RuntimeError(f"Das Display hat die Übertragung abgelehnt ({line.decode('ascii', errors='replace')}).")
    raise RuntimeError("Keine Bestätigung vom Display. Die USB-Empfänger-Firmware muss auf dem Board laufen.")


def send(text: str, port: str) -> bytes:
    data = text.encode("utf-8")
    if not data or len(data) > MAX_BYTES or b"\x00" in data:
        raise RuntimeError(f"Die Agenda muss 1 bis {MAX_BYTES} UTF-8-Bytes ohne Nullzeichen enthalten.")
    frames = [pack_page(image) for image in render_pages(text)]
    return send_frames(frames, port)


def send_frames(frames, port, left_frames=None):
    panes = [left_frames or [], frames or []]
    if not any(panes) or any(len(pane) > MAX_PAGES for pane in panes) or any(
        len(frame) != PAGE_BYTES for pane in panes for frame in pane
    ):
        raise RuntimeError("Ungültige Displayseiten.")
    with port_lock(port), serial.Serial(port, 115200, timeout=0.25, write_timeout=10, exclusive=False) as device:
        # Read through old acknowledgments until HELLO is answered. Clearing
        # the input buffer here would also discard queued button events.
        device.write(b"HELLO\n")
        wait_for(device, b"AGENDA3")
        device.write(f"BEGIN {len(panes[0])} {len(panes[1])}\n".encode("ascii"))
        wait_for(device, b"READY")
        for pane_index, pane in enumerate(panes):
            for index, data in enumerate(pane):
                device.write(f"PAGE {pane_index} {index} {zlib.crc32(data):08x}\n".encode("ascii"))
                wait_for(device, b"READY")
                for offset in range(0, len(data), 256):
                    chunk = data[offset:offset + 256]
                    if device.write(chunk) != len(chunk):
                        raise RuntimeError("Die USB-Übertragung war unvollständig.")
                device.flush()
                wait_for(device, f"STORED {pane_index} {index}".encode("ascii"))
        device.write(b"COMMIT\n")
        return wait_for(device, b"OK")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--version", action="version", version=(Path(__file__).parent / "VERSION").read_text().strip())
    parser.add_argument("--shortcut", default=SHORTCUT)
    parser.add_argument("--source", choices=["mcp", "shortcut"], default="mcp", help="Datenquelle (Standard: mcp)")
    parser.add_argument("--port")
    parser.add_argument("--text-file", type=Path, help="Textdatei statt Kurzbefehl verwenden")
    parser.add_argument("--check", action="store_true", help="Nur Datenabruf prüfen, ohne USB-Übertragung")
    parser.add_argument("--refresh-seconds", type=int, default=60,
                        help="Automatisch alle N Sekunden aktualisieren (Standard: 60; 0 schaltet den Timer aus)")
    parser.add_argument("--stop", action="store_true", help="Automatische Aktualisierung und Tastenempfänger beenden")
    parser.add_argument("--mcp-login", action="store_true", help="Bei TickTick MCP anmelden und verfügbare Daten prüfen")
    parser.add_argument("--allow-write-scope", action="store_true", help="Beim MCP-Login TickTicks erforderliche Schreibberechtigung anfordern; Werkzeugaufrufe bleiben auf Lesen beschränkt")
    args = parser.parse_args()
    try:
        if args.refresh_seconds < 0:
            raise RuntimeError("Das Aktualisierungsintervall darf nicht negativ sein.")
        if args.stop:
            stop_listener(args.port or discover_port())
            print("Automatische Aktualisierung und Tastenempfänger beendet.")
            return 0
        if args.mcp_login:
            from ticktick_mcp import inspect_tools
            asyncio.run(inspect_tools(allow_write_scope=args.allow_write_scope))
            return 0
        if args.allow_write_scope:
            raise RuntimeError("--allow-write-scope ist nur zusammen mit --mcp-login vorgesehen.")
        if args.text_file:
            text = args.text_file.read_text(encoding="utf-8")
        elif args.source == "shortcut":
            text = agenda_text(args.shortcut)
        else:
            from ticktick_mcp import agenda_text as mcp_agenda_text
            text = asyncio.run(mcp_agenda_text())
        if args.check:
            from apple_calendar import today_events
            events = today_events()
            print(f"Daten empfangen: {len(text.splitlines())} Zeilen, {len(text.encode('utf-8'))} Bytes.")
            print(f"Kalendertermine: {len(events)}.")
        else:
            port = args.port or find_port()
            ack = send(text, port)
            from button_bridge import refresh_calendar
            refresh_calendar(port)
            start_listener(port, refresh_seconds=args.refresh_seconds, source=args.source,
                           shortcut=args.shortcut, text_file=args.text_file)
            print("Agenda vom Display bestätigt.")
            if ack.startswith(b"OK "):
                pages = ack.split()[-1].decode('ascii')
                print(f"Seiten: {pages}." + (" Rechts drücken: weiterblättern." if pages != "1" else ""))
            print("Rechts doppelt drücken: TickTick Today am Mac öffnen.")
            if args.refresh_seconds:
                print(f"Automatische Aktualisierung alle {args.refresh_seconds} Sekunden im Hintergrund.")
        return 0
    except (RuntimeError, OSError, serial.SerialException) as exc:
        print(f"Fehler: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
