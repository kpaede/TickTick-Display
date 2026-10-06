"""Install the app and its login service in permanent user-owned Mac folders."""

import argparse
import os
import plistlib
import platform
import shlex
import shutil
import subprocess
import sys
from pathlib import Path

from runtime_paths import INSTALLED_RUNTIME

SOURCE = Path(__file__).resolve().parent
VERSION = (SOURCE / "VERSION").read_text().strip()
LABEL = "local.ticktick-display"
BUNDLE_ID = LABEL + ".app"
APP = Path.home() / "Applications" / "TickTick Display.app"
AGENT = Path.home() / "Library" / "LaunchAgents" / (LABEL + ".plist")
PREPARED_APP = SOURCE / "build" / "TickTick Display.app"
PREPARED_HELPER = SOURCE / "build" / "Calendar Reader.app"
CALENDAR_HELPER = INSTALLED_RUNTIME / "Calendar Reader.app"
DOMAIN = f"gui/{os.getuid()}"
TARGET = DOMAIN + "/" + LABEL
FILES = ["anzeigen", "anzeigen.py", "ticktick_mcp.py", "agenda_format.py", "display_render.py",
         "button_bridge.py", "runtime_paths.py", "apple_calendar.py",
         "requirements.txt", "requirements.lock", "README.md", "VERSION"]


def run(arguments):
    subprocess.run([str(value) for value in arguments], check=True)


def reusable_calendar_helper():
    previous_source = INSTALLED_RUNTIME / "calendar" / "CalendarReader.swift"
    if not previous_source.is_file():
        return None
    if previous_source.read_bytes() != (SOURCE / "calendar" / "CalendarReader.swift").read_bytes():
        return None
    for helper in [CALENDAR_HELPER, APP / "Contents" / "Helpers" / "Calendar Reader.app"]:
        info_path = helper / "Contents" / "Info.plist"
        if not info_path.is_file():
            continue
        info = plistlib.loads(info_path.read_bytes())
        if info.get("CFBundleIdentifier") != LABEL + ".calendar":
            continue
        valid = subprocess.run(["/usr/bin/codesign", "--verify", "--strict", str(helper)], capture_output=True)
        if valid.returncode == 0:
            return helper
    return None


def prepare():
    tools = SOURCE / ".tools"
    tools.mkdir(exist_ok=True)
    PREPARED_APP.parent.mkdir(exist_ok=True)
    launcher = tools / "launcher.applescript"
    start = f"/bin/launchctl kickstart -k {shlex.quote(TARGET)}"
    load = f"/bin/launchctl bootstrap {shlex.quote(DOMAIN)} {shlex.quote(str(AGENT))}"
    authorize = f"/usr/bin/open -W -n {shlex.quote(str(CALENDAR_HELPER))} --args --authorize"
    # Escape AppleScript strings independently of their shell quoting.
    def literal(value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    launcher.write_text(
        "on run\n"
        "    try\n"
        f"        do shell script {literal(authorize)}\n"
        f"        do shell script {literal(start)}\n"
        "    on error\n"
        "        try\n"
        f"            do shell script {literal(load)}\n"
        "        on error errorMessage\n"
        '            display dialog "TickTick Display konnte nicht gestartet werden.\\n" & errorMessage '
        'buttons {"OK"} default button "OK" with icon caution\n'
        "            return\n"
        "        end try\n"
        "    end try\n"
        "    activate\n"
        '    display dialog "TickTick Display ist gestartet.\\n\\n'
        'Das Programm läuft im Hintergrund und aktualisiert Kalender und Tasks jede Minute.\\n\\n'
        'Du kannst dieses Fenster schließen; die Anzeige läuft weiter. Das Display muss per USB verbunden sein." '
        'with title "TickTick Display" buttons {"OK"} default button "OK" with icon note\n'
        "end run\n"
        "on reopen\n"
        "    activate\n"
        "end reopen\n"
    )
    run(["/usr/bin/osacompile", "-o", PREPARED_APP, launcher])
    info_path = PREPARED_APP / "Contents" / "Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info.update(CFBundleIdentifier=BUNDLE_ID, CFBundleName="TickTick Display",
                CFBundleDisplayName="TickTick Display", LSUIElement=True,
                CFBundleShortVersionString=VERSION, CFBundleVersion="1")
    info_path.write_bytes(plistlib.dumps(info))
    helper = PREPARED_HELPER
    previous_helper = reusable_calendar_helper()
    if previous_helper:
        # Keep the signed binary unchanged to preserve its existing macOS grant.
        shutil.copytree(previous_helper, helper, dirs_exist_ok=True)
    else:
        executable = helper / "Contents" / "MacOS" / "calendar-reader"
        executable.parent.mkdir(parents=True, exist_ok=True)
        helper_info = {
            "CFBundleIdentifier": LABEL + ".calendar", "CFBundleExecutable": "calendar-reader",
            "CFBundleName": "TickTick Display Kalender", "CFBundleDisplayName": "TickTick Display Kalender",
            "CFBundlePackageType": "APPL", "CFBundleVersion": "1", "LSUIElement": True,
            "LSMinimumSystemVersion": "14.0",
            "NSCalendarsFullAccessUsageDescription":
                "Zeigt die heutigen Kalendertermine auf der linken Hälfte deines USB-Displays. Kalender werden nicht verändert.",
        }
        (helper / "Contents" / "Info.plist").write_bytes(plistlib.dumps(helper_info))
        run(["/usr/bin/xcrun", "swiftc", "-O", "-target", f"{platform.machine()}-apple-macosx14.0",
             "-module-cache-path", tools / "swift-cache", "-framework", "EventKit", "-framework", "AppKit",
             SOURCE / "calendar" / "CalendarReader.swift", "-o", executable])
        run(["/usr/bin/codesign", "--force", "--sign", "-", "--identifier", LABEL + ".calendar", helper])
    legacy_helper = PREPARED_APP / "Contents" / "Helpers" / "Calendar Reader.app"
    if legacy_helper.exists():
        shutil.rmtree(legacy_helper)
    # Keep the calendar app outside the launcher bundle so GUI updates cannot
    # change the enclosing app identity used by macOS calendar authorization.
    run(["/usr/bin/codesign", "--force", "--sign", "-", PREPARED_APP])
    root = INSTALLED_RUNTIME
    job = {
        "Label": LABEL,
        "ProgramArguments": [str(root / ".venv" / "bin" / "python"),
                             str(root / "button_bridge.py"), "--service"],
        "WorkingDirectory": str(root),
        "EnvironmentVariables": {"TICKTICK_DISPLAY_RUNTIME": str(root), "PYTHONUNBUFFERED": "1"},
        "RunAtLoad": True, "KeepAlive": True, "ThrottleInterval": 10,
        "StandardOutPath": str(root / ".tools" / "service.log"),
        "StandardErrorPath": str(root / ".tools" / "service.log"),
    }
    (tools / (LABEL + ".plist")).write_bytes(plistlib.dumps(job))
    print(f"App vorbereitet: {PREPARED_APP}", flush=True)


def install():
    if APP.exists():
        info = plistlib.loads((APP / "Contents" / "Info.plist").read_bytes())
        if info.get("CFBundleIdentifier") != BUNDLE_ID:
            raise RuntimeError("An dieser Stelle liegt bereits eine andere App.")
    if AGENT.exists() and plistlib.loads(AGENT.read_bytes()).get("Label") != LABEL:
        raise RuntimeError("An dieser Stelle liegt bereits ein anderer Autostart.")
    # Stop only our own prior installation before replacing executable files.
    subprocess.run(["/bin/launchctl", "bootout", TARGET], capture_output=True)
    root = INSTALLED_RUNTIME
    root.mkdir(parents=True, exist_ok=True, mode=0o700)
    for name in FILES:
        shutil.copy2(SOURCE / name, root / name)
    shutil.copytree(SOURCE / "firmware", root / "firmware", dirs_exist_ok=True)
    shutil.copytree(SOURCE / "calendar", root / "calendar", dirs_exist_ok=True)
    python = root / ".venv" / "bin" / "python"
    if not python.exists():
        run([sys.executable, "-m", "venv", root / ".venv"])
    run([python, "-m", "pip", "install", "--disable-pip-version-check", "-r", root / "requirements.lock"])
    run([python, "-m", "pip", "check"])
    data = root / "data"
    data.mkdir(exist_ok=True, mode=0o700)
    auth = data / "ticktick-oauth.json"
    if not auth.exists():
        shutil.copyfile(SOURCE / "data" / "ticktick-oauth.json", auth)
    auth.chmod(0o600)
    logs = root / ".tools"
    logs.mkdir(exist_ok=True, mode=0o700)
    log = logs / "service.log"
    log.touch(exist_ok=True)
    log.chmod(0o600)
    (root / ".installed").write_text("1\n")
    APP.parent.mkdir(exist_ok=True)
    shutil.copytree(PREPARED_APP, APP, dirs_exist_ok=True)
    shutil.copytree(PREPARED_HELPER, CALENDAR_HELPER, dirs_exist_ok=True)
    legacy_helper = APP / "Contents" / "Helpers" / "Calendar Reader.app"
    if legacy_helper.exists():
        info = plistlib.loads((legacy_helper / "Contents" / "Info.plist").read_bytes())
        if info.get("CFBundleIdentifier") != LABEL + ".calendar":
            raise RuntimeError("Der alte Kalenderhelfer gehört zu einer anderen App.")
        shutil.rmtree(legacy_helper)
    AGENT.parent.mkdir(exist_ok=True)
    shutil.copyfile(SOURCE / ".tools" / AGENT.name, AGENT)
    AGENT.chmod(0o644)
    run(["/bin/launchctl", "enable", TARGET])
    run(["/bin/launchctl", "bootstrap", DOMAIN, AGENT])
    print(f"Installiert: {APP}\nAutostart bei Anmeldung aktiviert.\nProgrammdateien: {root}", flush=True)


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--prepare-only", action="store_true")
    args = parser.parse_args()
    prepare()
    if not args.prepare_only:
        install()
