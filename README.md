# TickTick Display

Show your TickTick tasks and habits alongside today's Apple Calendar events on a
USB-connected **LUCKFOX / Waveshare ESP32-S3-RLCD-4.2-EN** reflective LCD.

The Mac reads and renders the agenda. The ESP32 displays it in black on white,
with independent pages for the two halves of its 400 × 300 landscape screen.
After setup, a small Mac app starts the background service automatically at login;
you do not need to keep Terminal open.

## What's on the display

| Left: Calendar | Right: Tasks |
| --- | --- |
| Today's events from calendars available in Apple Calendar | Overdue tasks, followed by today's open tasks |
| All-day events first, then timed events in chronological order | Today's unfinished habits below a horizontal divider |
| Recurring and overnight events included | Habit progress and weekly/monthly targets included |

Both headers show a compact refresh indicator, such as **↻ 19:42**, indicating
when the data was fetched. Both halves refresh independently every 60 seconds.
Your current page is preserved when new data arrives. Text wraps automatically;
checkboxes, bold, italic, strikethrough, code, links, lists, tables, separators and
TickTick's `==highlight==` markup are rendered.

Completed habits and fulfilled weekly/monthly goals are hidden. Habit schedules,
start dates, exclusions and check-in history are taken into account. Overdue tasks
are read from all task lists, including the inbox, so older items are included.

**Version 0.0.1 uses Europe/Berlin and German display text** such as "Kalender",
"Überfällig" and "Heute". The task-pane header is "Tasks". Calendar data comes
from macOS EventKit; calendars available only inside TickTick are not part of the
left pane.

## Buttons

Directions refer to the board in the landscape orientation used by this project.

| Button | Single press | Double press |
| --- | --- | --- |
| Left: KEY, GPIO 18 | Next calendar page | Open the default calendar app on the Mac |
| Right: BOOT, GPIO 0 | Next task/habit page | Open TickTick's Today view on the Mac |
| Middle: PWR | Hardware power button | Hardware power button |

Only KEY and BOOT are programmable. The calendar button resolves the default
application for `.ics` files. When macOS selects Apple's calendar import helper,
the button opens Apple Calendar itself. No file is imported. TickTick is opened
using its [Today URL scheme](https://blog.ticktick.com/2018/07/16/ticktick-ios-url-scheme/).

## Requirements

- macOS 14 or later, with the calendars you want visible in Apple Calendar.
- Python 3.12 and Apple's Xcode Command Line Tools (`xcode-select --install`).
- A TickTick account with access to the official `https://mcp.ticktick.com/` server.
- The **ESP32-S3-RLCD-4.2-EN**, connected to your Mac over a data-capable USB cable.
- Arduino CLI for the initial firmware upload: tested with Arduino CLI 1.5.1,
  Arduino-ESP32 3.3.0 and U8g2 2.36.19.

The hardware pinout and controls are documented in the
[manufacturer's board guide](https://docs.waveshare.com/ESP32-S3-RLCD-4.2).
Other ESP32 displays need their own driver, pinout and button mapping.
The Mac must remain powered on and connected over USB. This version does not
fetch data over Wi-Fi on the ESP32.

## First-time setup

### 1. Download and install Python dependencies

Clone this repository, or download and extract the source package from
[release 0.0.1](https://github.com/kpaede/TickTick-Display/releases/tag/0.0.1):

```sh
git clone https://github.com/kpaede/TickTick-Display.git
cd TickTick-Display
python3.12 -m venv .venv
.venv/bin/python -m pip install -r requirements.lock
```

`requirements.txt` lists direct dependencies. `requirements.lock` pins the full
dependency set used for this release.

### 2. Authorize TickTick

```sh
./anzeigen --mcp-login --allow-write-scope
```

Approve the TickTick authorization in the browser. The local OAuth callback is
`http://127.0.0.1:8765/callback`. Tokens are saved locally with file permissions
`0600` and refreshed when necessary.

TickTick's MCP server currently requires both `tasks:read` and `tasks:write`, even
to establish a connection. The flag explicitly allows this scope request.
**The resulting token has write privileges; this project's client permits only
the read operations listed under Privacy below.** No language model is involved.

### 3. Upload the display firmware

Install [Arduino CLI](https://arduino.github.io/arduino-cli/) if needed, then:

```sh
arduino-cli core update-index \
  --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli core install esp32:esp32@3.3.0 \
  --additional-urls https://espressif.github.io/arduino-esp32/package_esp32_index.json
arduino-cli lib install 'U8g2@2.36.19'
arduino-cli compile \
  --fqbn esp32:esp32:esp32s3:CDCOnBoot=cdc,USBMode=hwcdc,FlashSize=16M,PSRAM=opi,PartitionScheme=app3M_fat9M_16MB \
  --build-path build/firmware firmware
arduino-cli board list
```

Use the USB port shown by `board list` in the upload command below; replace the
example `/dev/cu.usbmodemXXXX` with your actual port:

```sh
arduino-cli upload \
  --fqbn esp32:esp32:esp32s3:CDCOnBoot=cdc,USBMode=hwcdc,FlashSize=16M,PSRAM=opi,PartitionScheme=app3M_fat9M_16MB \
  --port /dev/cu.usbmodemXXXX --input-dir build/firmware firmware
```

This replaces the board's application firmware. Back up any existing firmware
you need before uploading. Stop an already-running display service with
`./anzeigen --stop` before another firmware upload. The updated board initially
shows "USB bereit" while waiting for data from the Mac.

### 4. Install the Mac app and login service

```sh
.venv/bin/python install_mac.py
```

The installer builds the calendar helper locally and creates:

| Location | Purpose |
| --- | --- |
| `~/Applications/TickTick Display.app` | App you can double-click to start or restart the service |
| `~/Library/Application Support/TickTick Display/` | Independent program files, Python environment, settings and OAuth credentials |
| `~/Library/LaunchAgents/local.ticktick-display.plist` | Automatic startup when you log in |

Double-click **TickTick Display** in `~/Applications`. To find that folder in
Finder, press **⌘⇧G** and enter `~/Applications`.

At the first start, macOS asks for calendar access for **TickTick Display Kalender**.
Approve **Full Access**. EventKit requires that permission to read events; our
helper only reads them and does not modify calendars. If access was denied,
change it under **System Settings → Privacy & Security → Calendars**.

The installed service automatically discovers the connected ESP32 and resumes
after USB reconnection. After installation, it no longer depends on your clone
or Downloads folder. Existing installed credentials are preserved by updates.
If you later need to authorize TickTick again, use the `anzeigen` script in the
installed program folder, or a fresh source checkout with the dependencies set up.

## Optional Terminal commands

These are for setup and development; normal operation uses the app and login service.

```sh
./anzeigen                         # Fetch both halves; start background updates
./anzeigen --check                 # Check data access without a USB upload
./anzeigen --refresh-seconds 30     # Change the refresh interval to 30 seconds
./anzeigen --refresh-seconds 0      # Disable periodic updates
./anzeigen --port /dev/cu.usbmodemXXXX
./anzeigen --stop                  # Stop the service and button listener
./anzeigen --version
```

`./anzeigen` means "run the program named anzeigen in the current folder".
An explicit start or USB reconnection still triggers a fetch when periodic
updates are disabled. Resume a stopped installed service by opening the Mac app.

There is an optional legacy Shortcuts source:

```sh
./anzeigen --source shortcut --shortcut 'Ticktick Agenda anzeigen'
```

That shortcut must return actual task-name text, rather than only open a TickTick
view. The original shortcut uses **Get tasks from Smart List Today**, followed by
**Stop and Output → Task Name**. It does not supply habits; MCP is the default.

## Privacy and publication

The repository and release source package contain **code, documentation and
synthetic test fixtures**. They exclude local tokens, OAuth client registrations,
calendar/task output, logs, virtual environments, firmware backups, locally built
apps and other build products.

At runtime:

- TickTick credentials are kept locally in `data/ticktick-oauth.json` under the
  runtime folder, with permissions `0600`. They are never embedded in firmware.
- Before app installation, the runtime folder is the source checkout. Afterwards,
  it is `~/Library/Application Support/TickTick Display`. The developer override
  is `TICKTICK_DISPLAY_RUNTIME`.
- Task and habit data go from the official TickTick server to the Mac, then to the
  display over USB. Today's calendar event titles and times are read locally from
  EventKit; no calendar data is sent to TickTick or GitHub.
- Agenda data is not saved persistently. The calendar helper uses a private
  temporary file that is deleted after reading. Display pages stay in ESP32 RAM
  until replaced or power is lost.
- The MCP tool allowlist is `list_undone_tasks_by_time_query`, `list_habits`,
  `get_habit`, `get_habit_checkins`, `list_habit_sections`, `list_projects` and
  `get_project_with_undone_tasks`. Write-tool calls are rejected before contacting
  the server.

`.gitignore` protects local runtime files, but publication additionally uses an
explicit list of reviewed source files. Release `0.0.1` ships source, rather than
the developer's installed app or compiled firmware: those local artifacts may
contain personal build or installation paths. Each user builds their app locally
and authorizes their own accounts.

## Reliability and limitations

Each transmitted display page has a CRC32 checksum. Firmware commits a complete
transfer atomically; a partial or damaged transfer keeps the previous contents.
Updating one half leaves the other half intact. USB button events are handled
during transfers, and launching a Mac application does not block the USB reader.

Calendar and TickTick fetches run independently. Failed reads keep the last
displayed data and are retried automatically. A TickTick fetch has a three-minute
overall timeout; data fetched across midnight is discarded to avoid showing the
previous day's agenda as current. Token refreshes are serialized between the
background service and manual commands.

The refresh indicator is the **last successful fetch time**, not a promise that
data is current. TickTick/network delays can make a refresh take longer than a
minute. A firmware restart clears the display's RAM, so fresh data must be fetched
again. The installer and background service are specific to macOS; the optional
Shortcuts source and app-opening buttons also require macOS.

## Development and tests

```sh
.venv/bin/python -m unittest -v test_agenda test_display test_calendar test_buttons
```

The 37 tests cover overdue tasks, habit recurrence and progress, local-day
boundaries, calendar recurrence, daylight-saving transitions, Markdown rendering,
bitmap packing, queued USB buttons, OAuth storage/locking and the read-only tool
allowlist. Button tests compile the actual firmware button logic with `clang++`
and simulate GPIO and clock transitions, including single clicks, double clicks
and switch bounce.

Main components:

| File | Responsibility |
| --- | --- |
| `ticktick_mcp.py`, `agenda_format.py` | TickTick authorization, reads and task/habit formatting |
| `calendar/CalendarReader.swift`, `apple_calendar.py` | Read-only EventKit access and calendar formatting |
| `display_render.py` | Render both display halves into monochrome pages |
| `anzeigen.py`, `button_bridge.py` | USB uploads, button actions and independent refresh timers |
| `firmware/firmware.ino` | Display driver integration, checked transfers and physical buttons |
| `install_mac.py`, `runtime_paths.py` | Local app installation, login service and private runtime paths |

The ST7305 driver is adapted from the
[manufacturer's U8g2 example](https://github.com/waveshareteam/ESP32-S3-RLCD-4.2/tree/main/02_Example/Arduino/10_U8G2_Test).
Its license is preserved in [`firmware/LICENSE.vendor`](firmware/LICENSE.vendor).
This is an independent project, not an official TickTick or Waveshare product.
