"""macOS LaunchAgent management — auto-start the WorkWatch daemon.

Installs a per-user LaunchAgent that runs `workwatch --bg`:
  * every weekday (Mon-Fri) morning at 08:00, and
  * at every login/boot (`RunAtLoad`) — so a day the Mac was powered off at
    08:00 is still covered when you next turn it on.

Deliberately no `KeepAlive`: the daemon is one-shot (it exits after sleeping
the Mac), so relaunching it post-wake would instantly re-sleep. The daemon's
own idempotency guard handles the RunAtLoad-after-clockout case.
"""

from __future__ import annotations

import os
import sys
import shutil
import subprocess
from pathlib import Path

LABEL = "com.workwatch.daemon"
PLIST_PATH = Path.home() / "Library" / "LaunchAgents" / f"{LABEL}.plist"
LAUNCHD_LOG = Path.home() / ".workwatch.launchd.log"

START_HOUR = 8       # 08:00 local
START_MINUTE = 0
WEEKDAYS = (1, 2, 3, 4, 5)  # launchd weekday numbering: Mon=1 .. Fri=5


def _binary_path() -> str:
    """Best-effort absolute path to the installed `workwatch` executable."""
    found = shutil.which("workwatch")
    if found:
        return found
    if getattr(sys, "frozen", False):  # running as the PyInstaller binary
        return sys.executable
    return os.path.abspath(sys.argv[0])


def _domain() -> str:
    return f"gui/{os.getuid()}"


def _calendar_entries() -> str:
    blocks = []
    for weekday in WEEKDAYS:
        blocks.append(
            "    <dict>\n"
            f"      <key>Weekday</key><integer>{weekday}</integer>\n"
            f"      <key>Hour</key><integer>{START_HOUR}</integer>\n"
            f"      <key>Minute</key><integer>{START_MINUTE}</integer>\n"
            "    </dict>"
        )
    return "\n".join(blocks)


def build_plist(binary: str | None = None) -> str:
    """Render the LaunchAgent plist XML."""
    binary = binary or _binary_path()
    return f'''<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{binary}</string>
    <string>--bg</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>StartCalendarInterval</key>
  <array>
{_calendar_entries()}
  </array>
  <key>EnvironmentVariables</key>
  <dict>
    <key>PATH</key>
    <string>/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin</string>
  </dict>
  <key>StandardOutPath</key>
  <string>{LAUNCHD_LOG}</string>
  <key>StandardErrorPath</key>
  <string>{LAUNCHD_LOG}</string>
  <key>ProcessType</key>
  <string>Interactive</string>
</dict>
</plist>
'''


def install() -> tuple[bool, str]:
    """Write the plist and (re)load it. Returns (ok, binary_path_or_error)."""
    binary = _binary_path()
    PLIST_PATH.parent.mkdir(parents=True, exist_ok=True)
    PLIST_PATH.write_text(build_plist(binary))

    # Remove any previous instance first so bootstrap doesn't fail on a
    # "service already loaded" error, then load fresh.
    subprocess.run(
        ["launchctl", "bootout", f"{_domain()}/{LABEL}"],
        capture_output=True, text=True,
    )
    res = subprocess.run(
        ["launchctl", "bootstrap", _domain(), str(PLIST_PATH)],
        capture_output=True, text=True,
    )
    if res.returncode != 0:
        # Fall back to the legacy verb for older macOS releases.
        res = subprocess.run(
            ["launchctl", "load", "-w", str(PLIST_PATH)],
            capture_output=True, text=True,
        )
        if res.returncode != 0:
            return False, (res.stderr.strip() or "launchctl failed to load the agent.")
    return True, binary


def uninstall() -> tuple[bool, str]:
    """Unload and remove the plist. Returns (ok, path_or_message)."""
    existed = PLIST_PATH.exists()
    subprocess.run(
        ["launchctl", "bootout", f"{_domain()}/{LABEL}"],
        capture_output=True, text=True,
    )
    if existed:
        subprocess.run(
            ["launchctl", "unload", str(PLIST_PATH)],
            capture_output=True, text=True,
        )
        PLIST_PATH.unlink(missing_ok=True)
        return True, str(PLIST_PATH)
    return False, "No LaunchAgent is installed."


def is_installed() -> bool:
    return PLIST_PATH.exists()
