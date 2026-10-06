# Changelog

## Unreleased

- Show a visible confirmation window when opening the Mac app.
- Reuse an unchanged, validly signed calendar helper during app updates to preserve calendar access.
- Install the calendar helper separately from the launcher; migration requires one calendar authorization.
- Resynchronize the USB protocol after a partial command or board reset, without discarding button events.

## 0.0.1

Initial source release for the ESP32-S3-RLCD-4.2-EN and macOS.

- Independent calendar and task panes, with black text on a white background.
- Today's Apple Calendar events, including recurring, all-day and overnight events.
- TickTick overdue and today's tasks, plus unfinished habits and habit progress.
- Markdown rendering, wrapping, pagination and compact refresh indicators.
- Single-click paging and double-click app opening on both programmable buttons.
- Independent one-minute refreshes, USB reconnection and automatic Mac login startup.
- CRC32-checked atomic uploads, bounded TickTick reads and serialized OAuth refreshes.
- 37 regression tests, including the actual firmware button logic.

This release contains source code and synthetic tests. It does not distribute the
developer's installed app, compiled firmware, credentials, account registrations,
logs, backups or agenda data. Build the app locally and authorize your own account
as described in the README. Display text remains German and the time zone is
Europe/Berlin in this version.
