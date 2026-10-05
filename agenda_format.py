"""Format the verified TickTick MCP responses for the text display."""

from datetime import datetime, time, timedelta
from zoneinfo import ZoneInfo

from dateutil.rrule import rrulestr

TIMEZONE = ZoneInfo("Europe/Berlin")


def result_items(response):
    items = response.get("result") if isinstance(response, dict) else None
    if not isinstance(items, list) or any(not isinstance(item, dict) for item in items):
        raise RuntimeError("TickTick hat ein unerwartetes Listenformat geliefert.")
    return items


def rule_parts(habit):
    rule = habit.get("repeatRule") or "RRULE:FREQ=DAILY"
    if not rule.startswith("RRULE:"):
        raise RuntimeError("Eine Habit-Wiederholungsregel wird noch nicht unterstützt.")
    return dict(part.split("=", 1) for part in rule[6:].split(";") if "=" in part)


def habit_period(habit, today):
    """Counting goals have a flexible week/month instead of a fixed weekday."""
    parts = rule_parts(habit)
    if "TT_TIMES" not in parts:
        return today, None, ""
    target = int(parts["TT_TIMES"])
    if target < 1:
        raise RuntimeError("Ungültiges Habit-Ziel.")
    if parts.get("FREQ") == "WEEKLY":
        week_start = parts.get("WKST", "MO")
        weekdays = ["MO", "TU", "WE", "TH", "FR", "SA", "SU"]
        if week_start not in weekdays:
            raise RuntimeError("Unbekannter Wochenbeginn im Habit-Plan.")
        start = today - timedelta(days=(today.weekday() - weekdays.index(week_start)) % 7)
        return start, target, "Woche"
    if parts.get("FREQ") == "MONTHLY":
        return today.replace(day=1), target, "Monat"
    raise RuntimeError("Dieses mengenbasierte Habit-Intervall wird noch nicht unterstützt.")


def habit_due(habit, today):
    if habit.get("status") not in (None, 0):
        return False
    stamp = today.strftime("%Y%m%d")
    if stamp in {str(value).replace("-", "")[:8] for value in habit.get("exDates") or []}:
        return False
    start = datetime.strptime(str(habit.get("targetStartDate") or stamp), "%Y%m%d")
    if today < start.date():
        return False
    parts = rule_parts(habit)
    if "TT_TIMES" in parts:
        habit_period(habit, today)  # Validate the counting rule.
        return True
    rule = "RRULE:" + ";".join(f"{key}={value}" for key, value in parts.items())
    try:
        schedule = rrulestr(rule, dtstart=start, ignoretz=True)
        midnight = datetime.combine(today, time.min)
        occurrence = schedule.after(midnight, inc=True)
        return occurrence is not None and occurrence.date() == today
    except (ValueError, TypeError) as exc:
        raise RuntimeError("Eine Habit-Wiederholungsregel konnte nicht gelesen werden.") from exc


def checkin_index(records):
    result = {}
    for aggregate in records:
        habit_id = aggregate.get("habitId")
        for entry in aggregate.get("checkins") or []:
            stamp = entry.get("stamp")
            if habit_id is None or stamp is None:
                raise RuntimeError("Ein Habit-Check-in enthält kein Datum oder keine Habit-ID.")
            key = (habit_id, int(stamp))
            previous = result.get(key)
            if previous is None or (entry.get("opTime") or "") >= (previous.get("opTime") or ""):
                result[key] = entry
    return result


def task_time(task):
    raw = task.get("start_date") or task.get("startDate") or task.get("due_date") or task.get("dueDate")
    if not raw:
        return None
    value = datetime.fromisoformat(raw)
    if value.tzinfo is None:
        value = value.replace(tzinfo=TIMEZONE)
    return value.astimezone(TIMEZONE)


def task_deadline(task):
    raw = task.get("due_date") or task.get("dueDate")
    return task_time({"start_date": raw}) if raw else task_time(task)


def task_overdue(task, today):
    deadline = task_deadline(task)
    return deadline is not None and deadline.date() < today


def format_agenda(tasks, habits, checkins, now, overdue_tasks=None):
    today = now.astimezone(TIMEZONE).date()
    stamp = int(today.strftime("%Y%m%d"))
    records = checkin_index(checkins)
    text = [f"<!-- agenda-stamp: {now.astimezone(TIMEZONE):%d.%m.%Y %H:%M} -->"]
    for label, group in (("Überfällig", overdue_tasks or []), ("Heute", tasks)):
        if not group:
            continue
        text.extend(["", f"### {label}"])
        ordered_tasks = sorted(group, key=lambda t: (
            (task_deadline(t) if label == "Überfällig" else task_time(t)).timestamp()
            if (task_deadline(t) if label == "Überfällig" else task_time(t)) else float("-inf"),
            t.get("sort_order", t.get("sortOrder")) or 0,
        ))
        for task in ordered_tasks:
            title = task.get("title")
            if not isinstance(title, str) or not title.strip():
                raise RuntimeError("Eine Aufgabe enthält keinen Titel.")
            when = task_deadline(task) if label == "Überfällig" else task_time(task)
            all_day = task.get("is_all_day", task.get("isAllDay", False))
            prefix = f"{when:%d.%m.} " if when and label == "Überfällig" else ""
            prefix += f"{when:%H:%M} " if when and not all_day else ""
            # Each item is its own block, so a long title can wrap without joining
            # the next checkbox. Inline Markdown in titles remains renderable.
            text.extend(["", f"[ ] {prefix}{title.strip()}"])
    if not tasks and not overdue_tasks:
        text.extend(["", "Keine offenen Aufgaben."])
    habit_rows = []
    for habit in sorted(habits, key=lambda h: h.get("sortOrder") or 0):
        name = habit.get("name")
        if not isinstance(name, str) or not name.strip():
            raise RuntimeError("Ein Habit enthält keinen Namen.")
        start, target, period = habit_period(habit, today)
        entry = records.get((habit["id"], stamp), {})
        done = entry.get("status") == 2
        progress = ""
        if target:
            first = int(start.strftime("%Y%m%d"))
            count = sum(1 for (identifier, day), record in records.items()
                        if identifier == habit["id"] and first <= day <= stamp and record.get("status") == 2)
            progress = f" ({count}/{target} {period})"
            done = done or count >= target
        elif habit.get("type") != "Boolean":
            value = entry.get("value") or 0
            goal = entry.get("goal") or habit.get("goal") or 1
            unit = habit.get("unit") or ""
            progress = f" ({value:g}/{goal:g} {unit})".replace(" )", ")")
        if not done:
            habit_rows.append(f"[ ] {name.strip()}{progress}")
    text.extend(["", "____"])
    for row in habit_rows or ["Keine offenen Habits für heute."]:
        text.extend(["", row])
    return "\n".join(text)
