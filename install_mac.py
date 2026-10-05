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
DOMAIN = f"gui/{os.getuid()}"
TARGET = DOMAIN + "/" + LABEL
FILES = ["anzeigen", "anzeigen.py", "ticktick_mcp.py", "agenda_format.py", "display_render.py",
         "button_bridge.py", "runtime_paths.py", "apple_calendar.py",
         "requirements.txt", "requirements.lock", "README.md", "VERSION"]


def run(arguments):
    subprocess.run([str(value) for value in arguments], check=True)


def prepare():
    tools = SOURCE / ".tools"
    tools.mkdir(exist_ok=True)
    PREPARED_APP.parent.mkdir(exist_ok=True)
    launcher = tools / "launcher.applescript"
    start = f"/bin/launchctl kickstart -k {shlex.quote(TARGET)}"
    load = f"/bin/launchctl bootstrap {shlex.quote(DOMAIN)} {shlex.quote(str(AGENT))}"
    calendar_helper = APP / "Contents" / "Helpers" / "Calendar Reader.app"
    authorize = f"/usr/bin/open -n {shlex.quote(str(calendar_helper))} --args --authorize"
    # Escape AppleScript strings independently of their shell quoting.
    def literal(value):
        return '"' + value.replace('\\', '\\\\').replace('"', '\\"') + '"'
    launcher.write_text(
        "on run\n"
        f"    do shell script {literal(authorize)}\n"
        "    try\n"
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
        '    display notification "Aktualisiert das USB-Display automatisch jede Minute." '
        'with title "TickTick Display"\n'
        "end run\n"
    )
    run(["/usr/bin/osacompile", "-o", PREPARED_APP, launcher])
    info_path = PREPARED_APP / "Contents" / "Info.plist"
    info = plistlib.loads(info_path.read_bytes())
    info.update(CFBundleIdentifier=BUNDLE_ID, CFBundleName="TickTick Display",
                CFBundleDisplayName="TickTick Display", LSUIElement=True,
                CFBundleShortVersionString=VERSION, CFBundleVersion="1")
    info_path.write_bytes(plistlib.dumps(info))
    helper = PREPARED_APP / "Contents" / "Helpers" / "Calendar Reader.app"
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
    run(["/usr/bin/codesign", "--force", "--deep", "--sign", "-", PREPARED_APP])
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
