import tempfile
import unittest
from contextlib import asynccontextmanager
from datetime import date, datetime
from pathlib import Path
from unittest.mock import AsyncMock, patch

from agenda_format import TIMEZONE, format_agenda, habit_due, habit_period, result_items, task_overdue
from ticktick_mcp import FileStorage, read_tool, agenda_text, oauth_lock
from mcp.shared.auth import OAuthToken


TODAY = date(2026, 10, 7)  # Wednesday
NOW = datetime(2026, 10, 7, 12, tzinfo=TIMEZONE)


def habit(**extra):
    return {"id": "habit-a", "name": "Bewegung", "status": 0, "type": "Boolean",
            "repeatRule": "RRULE:FREQ=DAILY;INTERVAL=1", "targetStartDate": 20261005, **extra}


class AgendaTests(unittest.TestCase):
    def test_task_order_during_repeated_dst_hour_uses_actual_time(self):
        now = datetime(2026, 10, 25, 12, tzinfo=TIMEZONE)
        tasks = [{"title": "Später", "start_date": "2026-10-25T02:15:00+01:00"},
                 {"title": "Früher", "start_date": "2026-10-25T02:30:00+02:00"}]
        text = format_agenda(tasks, [], [], now)
        self.assertLess(text.index("Früher"), text.index("Später"))

    def test_recurring_days_and_exclusions(self):
        self.assertTrue(habit_due(habit(), TODAY))
        self.assertFalse(habit_due(habit(exDates=["20261007"]), TODAY))
        self.assertFalse(habit_due(habit(targetStartDate=20261008), TODAY))
        self.assertTrue(habit_due(habit(repeatRule="RRULE:FREQ=DAILY;INTERVAL=2"), TODAY))
        self.assertFalse(habit_due(habit(repeatRule="RRULE:FREQ=DAILY;INTERVAL=2"), date(2026, 10, 6)))
        self.assertFalse(habit_due(habit(repeatRule="RRULE:FREQ=WEEKLY;BYDAY=MO,FR"), TODAY))

    def test_weekly_target_is_not_a_fixed_weekday(self):
        item = habit(repeatRule="RRULE:FREQ=WEEKLY;TT_TIMES=3")
        self.assertTrue(habit_due(item, TODAY))
        self.assertEqual(habit_period(item, TODAY), (date(2026, 10, 5), 3, "Woche"))

    def test_yesterday_does_not_complete_todays_habit(self):
        records = [{"habitId": "habit-a", "checkins": [{"stamp": 20261006, "status": 2}]}]
        self.assertIn("[ ] Bewegung", format_agenda([], [habit()], records, NOW))
        records[0]["checkins"].append({"stamp": 20261007, "status": 2})
        text = format_agenda([], [habit()], records, NOW)
        self.assertNotIn("Bewegung", text)
        self.assertIn("Keine offenen Habits für heute.", text)

    def test_weekly_progress_excludes_previous_week_and_deduplicates(self):
        item = habit(repeatRule="RRULE:FREQ=WEEKLY;TT_TIMES=3")
        records = [{"habitId": "habit-a", "checkins": [
            {"stamp": 20261004, "status": 2},
            {"stamp": 20261005, "status": 2},
            {"stamp": 20261005, "status": 2},
            {"stamp": 20261006, "status": 2},
        ]}]
        self.assertIn("[ ] Bewegung (2/3 Woche)", format_agenda([], [item], records, NOW))

    def test_fulfilled_weekly_target_is_hidden_even_without_today_checkin(self):
        item = habit(repeatRule="RRULE:FREQ=WEEKLY;TT_TIMES=2")
        records = [{"habitId": "habit-a", "checkins": [
            {"stamp": 20261005, "status": 2},
            {"stamp": 20261006, "status": 2},
        ]}]
        text = format_agenda([], [item], records, NOW)
        self.assertNotIn("Bewegung", text)
        self.assertIn("Keine offenen Habits für heute.", text)

    def test_numeric_habit_progress(self):
        item = habit(type="Real", goal=8, unit="Gläser")
        records = [{"habitId": "habit-a", "checkins": [{"stamp": 20261007, "status": 1, "value": 3, "goal": 8}]}]
        self.assertIn("[ ] Bewegung (3/8 Gläser)", format_agenda([], [item], records, NOW))

    def test_task_time_uses_berlin_and_supports_server_field_names(self):
        task = {"title": "Termin", "start_date": "2026-10-07T08:00:00Z", "is_all_day": False}
        self.assertIn("[ ] 10:00 Termin", format_agenda([task], [], [], NOW))
        task = {"title": "Ganztägig", "startDate": "2026-10-07T00:00:00+0200", "isAllDay": True}
        self.assertIn("[ ] Ganztägig", format_agenda([task], [], [], NOW))

    def test_unknown_response_does_not_become_an_empty_agenda(self):
        with self.assertRaises(RuntimeError):
            result_items({"error": "Service unavailable"})

    def test_overdue_uses_end_date_and_berlin_day(self):
        self.assertFalse(task_overdue({"start_date": "2026-10-01T08:00:00+02:00",
                                     "due_date": "2026-10-07T08:00:00+02:00"}, TODAY))
        self.assertFalse(task_overdue({"dueDate": "2026-10-06T23:30:00Z"}, TODAY))
        self.assertTrue(task_overdue({"dueDate": "2026-08-01T12:00:00Z"}, TODAY))

    def test_overdue_then_today_then_habits_with_compact_separator(self):
        old = {"title": "Alte Aufgabe", "dueDate": "2026-08-01T00:00:00+02:00", "isAllDay": True}
        recent = {"title": "Jüngere Aufgabe", "dueDate": "2026-10-06T00:00:00+02:00", "isAllDay": True}
        current = {"title": "Heute Aufgabe", "isAllDay": True}
        text = format_agenda([current], [habit()], [], NOW, overdue_tasks=[recent, old])
        self.assertLess(text.index("Alte Aufgabe"), text.index("Jüngere Aufgabe"))
        self.assertLess(text.index("Jüngere Aufgabe"), text.index("Heute Aufgabe"))
        self.assertLess(text.index("Heute Aufgabe"), text.index("____"))
        self.assertLess(text.index("____"), text.index("Bewegung"))
        self.assertNotIn("Aufgaben (", text)
        self.assertNotIn("Habits (", text)


class AuthorizationTests(unittest.IsolatedAsyncioTestCase):
    async def test_concurrent_connections_cannot_refresh_the_same_token(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / "auth.json"
            async with oauth_lock(path):
                with self.assertRaises(RuntimeError):
                    async with oauth_lock(path, timeout=0):
                        self.fail("Concurrent refresh must be blocked")
            async with oauth_lock(path, timeout=0):
                pass  # The lock is released even after a failed acquisition.

    async def test_write_is_rejected_before_contacting_server(self):
        class Client:
            async def call_tool(self, *args, **kwargs):
                raise AssertionError("Server must not be called")
        with self.assertRaises(RuntimeError):
            await read_tool(Client(), "upsert_habit_checkins", {})

    async def test_stored_token_expires_instead_of_resetting_lifetime(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "auth.json"
            storage = FileStorage(path)
            await storage.set_tokens(OAuthToken(access_token="test", expires_in=3600))
            self.assertEqual(path.stat().st_mode & 0o777, 0o600)
            storage = FileStorage(path)
            storage.data["expires_at"] = 1
            self.assertLess((await storage.get_tokens()).expires_in, 0)
            with patch("ticktick_mcp.time.time", return_value=1):
                self.assertLess((await storage.get_tokens()).expires_in, 0)


class RetrievalTests(unittest.IsolatedAsyncioTestCase):
    async def test_midnight_fetch_is_rejected_instead_of_displaying_yesterdays_data(self):
        @asynccontextmanager
        async def connected():
            yield object()
        with patch("ticktick_mcp.connection", connected), patch("ticktick_mcp.datetime") as clock, \
             patch("ticktick_mcp.read_tool", AsyncMock(return_value={"result": []})):
            clock.now.side_effect = [datetime(2026, 10, 7, 23, 59, tzinfo=TIMEZONE),
                                     datetime(2026, 10, 8, 0, 1, tzinfo=TIMEZONE)]
            with self.assertRaisesRegex(RuntimeError, "Tageswechsel"):
                await agenda_text()

    async def test_old_overdue_tasks_and_inbox_are_included_once(self):
        old = {"id": "old", "title": "August Aufgabe", "dueDate": "2026-08-01T00:00:00+02:00",
               "status": 0, "isAllDay": True}
        inbox = {"id": "inbox-task", "title": "Inbox Aufgabe", "start_date": "2026-10-04T00:00:00+02:00",
                 "status": 0, "is_all_day": True}
        current = {"id": "today", "title": "Heute Aufgabe", "startDate": "2026-10-07T00:00:00+02:00",
                   "status": 0, "isAllDay": True}

        @asynccontextmanager
        async def connected():
            yield object()

        async def respond(client, name, arguments):
            if name == "list_undone_tasks_by_time_query":
                return {"result": [old, current]}
            if name == "list_habits":
                return {"result": []}
            if name == "list_projects":
                return {"result": [{"id": "list", "kind": "TASK"}, {"id": "inbox"},
                                   {"id": "notes", "kind": "NOTE"}]}
            if name == "get_project_with_undone_tasks":
                if arguments["project_id"] == "list":
                    return {"tasks": [old, {**old, "id": "done", "title": "Erledigt", "status": 2},
                                      {**old, "id": "future", "title": "Zukunft", "dueDate": "2026-11-01T00:00:00+02:00"}]}
                if arguments["project_id"] == "inbox":
                    return {"tasks": [inbox]}
            raise AssertionError((name, arguments))

        with patch("ticktick_mcp.connection", connected), patch("ticktick_mcp.datetime") as clock, \
             patch("ticktick_mcp.read_tool", AsyncMock(side_effect=respond)):
            clock.now.return_value = NOW
            text = await agenda_text()
        self.assertEqual(text.count("August Aufgabe"), 1)
        self.assertIn("Inbox Aufgabe", text)
        self.assertIn("Heute Aufgabe", text)
        self.assertNotIn("Erledigt", text)
        self.assertNotIn("Zukunft", text)


if __name__ == "__main__":
    unittest.main()
