import unittest
from datetime import datetime

from agenda_format import TIMEZONE
from apple_calendar import calendar_rows, render_calendar

NOW = datetime(2026, 10, 5, 12, tzinfo=TIMEZONE)


def event(identifier, title, start, end, **extra):
    return {"id": identifier, "calendar_id": "calendar-a", "title": title,
            "start": start, "end": end, "all_day": False, **extra}


def contents(rows):
    return ["".join(span.text for span in row.spans) for row in rows]


class CalendarTests(unittest.TestCase):
    def test_chronological_local_times_and_all_day(self):
        events = [
            event("late", "Nachmittag", "2026-10-05T13:00:00Z", "2026-10-05T14:00:00Z"),
            event("early", "Früh", "2026-10-05T07:00:00Z", "2026-10-05T08:00:00Z"),
            event("day", "Ganztägiger Termin", "2026-10-04T22:00:00Z", "2026-10-05T22:00:00Z", all_day=True),
        ]
        text = contents(calendar_rows(events, NOW))
        self.assertEqual(text, ["Ganztägig", "Ganztägiger Termin", "09:00–10:00", "Früh",
                                "15:00–16:00", "Nachmittag"])

    def test_overlapping_midnight_events_are_clipped_and_other_days_excluded(self):
        events = [
            event("past", "Gestern", "2026-10-04T08:00:00+02:00", "2026-10-04T09:00:00+02:00"),
            event("ends", "Endet an Tagesbeginn", "2026-10-04T23:00:00+02:00", "2026-10-05T00:00:00+02:00"),
            event("overnight", "Über Nacht", "2026-10-04T23:00:00+02:00", "2026-10-05T01:00:00+02:00"),
            event("long", "Bis morgen", "2026-10-05T22:00:00+02:00", "2026-10-06T02:00:00+02:00"),
            event("future", "Morgen", "2026-10-06T00:00:00+02:00", "2026-10-06T01:00:00+02:00"),
        ]
        self.assertEqual(contents(calendar_rows(events, NOW)),
                         ["00:00–01:00", "Über Nacht", "22:00–24:00", "Bis morgen"])

    def test_recurring_occurrences_keep_their_distinct_start_times(self):
        first = event("series", "Serientermin", "2026-10-05T08:00:00+02:00", "2026-10-05T09:00:00+02:00")
        second = {**first, "start": "2026-10-05T18:00:00+02:00", "end": "2026-10-05T19:00:00+02:00"}
        text = contents(calendar_rows([first, first, second], NOW))
        self.assertEqual(text.count("Serientermin"), 2)

    def test_calendar_titles_are_literal_and_time_stays_with_title(self):
        item = event("title", "**Titel** [Name]", "2026-10-05T08:00:00+02:00", "2026-10-05T09:00:00+02:00")
        rows = calendar_rows([item], NOW)
        self.assertTrue(rows[0].keep_next)
        self.assertIn("**Titel** [Name]", contents(rows))
        self.assertFalse(rows[1].spans[0].style.bold)

    def test_dst_repeated_hour_is_sorted_by_actual_time(self):
        now = datetime(2026, 10, 25, 12, tzinfo=TIMEZONE)
        events = [
            event("later", "Nach Zeitumstellung", "2026-10-25T02:15:00+01:00", "2026-10-25T02:45:00+01:00"),
            event("earlier", "Vor Zeitumstellung", "2026-10-25T02:30:00+02:00", "2026-10-25T02:50:00+02:00"),
        ]
        text = " ".join(contents(calendar_rows(events, now)))
        self.assertLess(text.index("Vor Zeitumstellung"), text.index("Nach Zeitumstellung"))

    def test_empty_calendar_and_permission_message_are_distinct(self):
        self.assertEqual(contents(calendar_rows([], NOW)), ["Keine Termine für heute."])
        normal = render_calendar([], NOW)[0]
        permission = render_calendar(now=NOW, permission_required=True)[0]
        self.assertNotEqual(normal.tobytes(), permission.tobytes())


if __name__ == "__main__":
    unittest.main()
