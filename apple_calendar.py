"""Read today's calendar through the bundled, read-only EventKit helper."""

import json
import subprocess
import tempfile
from datetime import datetime, time, timedelta
from pathlib import Path

from agenda_format import TIMEZONE
from display_render import Row, Span, Style, render_rows, wrap_spans

HELPER_NAME = "Calendar Reader.app"


class CalendarPermissionRequired(RuntimeError):
    def __init__(self, status="not_determined"):
        super().__init__("Kalenderzugriff für TickTick Display Kalender erforderlich.")
        self.status = status


def helper_app():
    installed = Path.home() / "Applications" / "TickTick Display.app" / "Contents" / "Helpers" / HELPER_NAME
    if installed.is_dir():
        return installed
    return Path(__file__).parent / "build" / "TickTick Display.app" / "Contents" / "Helpers" / HELPER_NAME


def today_events():
    helper = helper_app()
    if not helper.is_dir():
        raise RuntimeError("Der Kalenderleser ist noch nicht installiert.")
    # Launch as an app so macOS consistently attributes its Calendar permission
    # to the helper, both from Terminal and from the login background service.
    with tempfile.TemporaryDirectory(prefix="ticktick-calendar-") as directory:
        output = Path(directory) / "events.json"
        try:
            result = subprocess.run(
                ["/usr/bin/open", "-W", "-n", str(helper), "--args", "--output", str(output)],
                capture_output=True, timeout=45,
            )
        except subprocess.TimeoutExpired as exc:
            raise RuntimeError("Apple Kalender antwortet gerade nicht.") from exc
        if result.returncode or not output.is_file():
            raise RuntimeError("Apple Kalender konnte nicht gelesen werden.")
        data = json.loads(output.read_text())
    if data.get("status") in {"not_determined", "denied", "restricted", "write_only"}:
        raise CalendarPermissionRequired(data["status"])
    if data.get("status") != "authorized" or not isinstance(data.get("events"), list):
        raise RuntimeError("Apple Kalender hat ein unerwartetes Ergebnis geliefert.")
    return data["events"]


def event_time(raw):
    value = datetime.fromisoformat(raw)
    return value.replace(tzinfo=TIMEZONE) if value.tzinfo is None else value.astimezone(TIMEZONE)


def calendar_rows(events, now):
    today = now.astimezone(TIMEZONE).date()
    start = datetime.combine(today, time.min, TIMEZONE)
    end = datetime.combine(today + timedelta(days=1), time.min, TIMEZONE)
    valid = {}
    for event in events:
        begin, finish = event_time(event["start"]), event_time(event["end"])
        if finish <= start or begin >= end:
            continue
        key = (event.get("calendar_id"), event.get("id"), begin.timestamp())
        if not event.get("id"):
            key += (event.get("title"), finish.timestamp())
        valid[key] = event
    ordered = sorted(valid.values(), key=lambda e: (
        not e.get("all_day", False), event_time(e["start"]).timestamp(),
        event_time(e["end"]).timestamp(), e.get("title") or "",
    ))
    rows = []
    for event in ordered:
        begin, finish = event_time(event["start"]), event_time(event["end"])
        if event.get("all_day"):
            label = "Ganztägig"
        else:
            begin_label = "00:00" if begin < start else begin.strftime("%H:%M")
            end_label = "24:00" if finish >= end else finish.strftime("%H:%M")
            label = f"{begin_label}–{end_label}"
        rows.append(Row([Span(label, Style(bold=True, size=11))], keep_next=True))
        # Calendar titles are literal text; '*' and '[' aren't Markdown here.
        title = event.get("title") or "Termin"
        rows.extend(wrap_spans([Span(title.replace("\r", " ").replace("\n", " "))]))
    return rows or [Row([Span("Keine Termine für heute.")])]


def render_calendar(events=None, now=None, permission_required=False, permission_status="not_determined"):
    now = now or datetime.now(TIMEZONE)
    if permission_required:
        hint = ("Kalenderzugriff in den Systemeinstellungen erlauben.\nDatenschutz & Sicherheit > Kalender."
                if permission_status in {"denied", "restricted"}
                else "Kalenderzugriff erlauben.\nTickTick Display am Mac öffnen.")
        rows = wrap_spans([Span(hint)])
    else:
        rows = calendar_rows(events or [], now)
    return render_rows(rows, "Kalender", now.strftime("%H:%M"))
