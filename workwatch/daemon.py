"""Background daemon mode for WorkWatch.

Forks into background, sleeps until the target time, then triggers
pmset sleepnow. Sends macOS notifications for status updates.
Writes a PID file so the user can check status or stop it.
"""

import os
import sys
import time
import signal
import logging
from datetime import datetime, timedelta
from pathlib import Path

from workwatch.config import load_config, load_history, save_history, get_effective_work_hours
from workwatch.mail_reader import fetch_today_emails
from workwatch.parser import get_earliest_entry
from workwatch.notifier import notify as _notify
from workwatch.overtime import run_overtime_loop

PID_FILE = Path.home() / ".workwatch.pid"
LOG_FILE = Path.home() / ".workwatch.log"
STATE_FILE = Path.home() / ".workwatch_state.json"
RETRY_INTERVAL = 300  # 5 minutes
GRACE_SECONDS = 30 * 60  # only force-sleep if within 30 min of the sleep mark


def _put_to_sleep():
    """Put the Mac to sleep."""
    import subprocess
    result = subprocess.run(["pmset", "sleepnow"], capture_output=True, text=True)
    if result.returncode != 0:
        os.system("sudo pmset sleepnow")


def _setup_logging():
    """Set up file-based logging for daemon mode."""
    logging.basicConfig(
        filename=str(LOG_FILE),
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )


def _write_pid():
    """Write the current PID to the PID file."""
    PID_FILE.write_text(str(os.getpid()) + "\n")


def _remove_pid():
    """Remove the PID and state files."""
    try:
        PID_FILE.unlink(missing_ok=True)
    except Exception:
        pass
    try:
        STATE_FILE.unlink(missing_ok=True)
    except Exception:
        pass


def _write_state(phase: str, entry_time: datetime = None, sleep_time: datetime = None,
                 overtime_start: datetime = None, last_active: datetime = None,
                 idle_seconds: float = None):
    """Write daemon state to a JSON file so `workwatch status` can read it."""
    import json
    state = {
        "phase": phase,
        "updated_at": datetime.now().isoformat(),
    }
    if entry_time:
        state["entry_time"] = entry_time.strftime("%I:%M:%S %p")
        state["entry_iso"] = entry_time.isoformat()
    if sleep_time:
        state["sleep_time"] = sleep_time.strftime("%I:%M:%S %p")
        state["sleep_iso"] = sleep_time.isoformat()
    if overtime_start:
        state["overtime_start_iso"] = overtime_start.isoformat()
    if last_active:
        state["last_active_iso"] = last_active.isoformat()
        state["last_active"] = last_active.strftime("%I:%M:%S %p")
    if idle_seconds is not None:
        state["idle_seconds"] = round(idle_seconds, 1)
    STATE_FILE.write_text(json.dumps(state, indent=2) + "\n")


def read_state() -> dict | None:
    """Read daemon state file. Returns None if not found."""
    import json
    if not STATE_FILE.exists():
        return None
    try:
        return json.loads(STATE_FILE.read_text())
    except Exception:
        return None


def _handle_signal(signum, frame):
    """Handle termination signals gracefully."""
    logging.info("Received signal %d — shutting down.", signum)
    _remove_pid()
    sys.exit(0)


def get_running_pid() -> int | None:
    """Return the PID of a running daemon, or None."""
    if not PID_FILE.exists():
        return None
    try:
        pid = int(PID_FILE.read_text().strip())
        # Check if process is alive
        os.kill(pid, 0)
        return pid
    except (ValueError, ProcessLookupError, PermissionError):
        _remove_pid()
        return None


def stop_daemon() -> bool:
    """Stop a running daemon. Returns True if one was stopped."""
    pid = get_running_pid()
    if pid is None:
        return False
    try:
        os.kill(pid, signal.SIGTERM)
        # Wait briefly for it to exit
        for _ in range(10):
            try:
                os.kill(pid, 0)
                time.sleep(0.1)
            except ProcessLookupError:
                break
        _remove_pid()
        return True
    except ProcessLookupError:
        _remove_pid()
        return False


def daemon_status() -> dict | None:
    """Return status info about a running daemon, or None."""
    pid = get_running_pid()
    if pid is None:
        return None

    info = {"pid": pid}

    # Try to read the log for the latest entry time / sleep time
    if LOG_FILE.exists():
        try:
            lines = LOG_FILE.read_text().strip().split("\n")
            for line in reversed(lines):
                if "Sleep scheduled at" in line:
                    info["status_line"] = line
                    break
        except Exception:
            pass

    return info


def daemonize():
    """Fork the process into a background daemon (Unix double-fork)."""
    # First fork
    pid = os.fork()
    if pid > 0:
        # Parent — exit and let the child continue
        return pid

    # Child — create new session
    os.setsid()

    # Second fork — prevent reacquiring a terminal
    pid = os.fork()
    if pid > 0:
        os._exit(0)

    # Grandchild — this is the actual daemon
    # Redirect stdio to /dev/null
    devnull = os.open(os.devnull, os.O_RDWR)
    os.dup2(devnull, 0)  # stdin
    os.dup2(devnull, 1)  # stdout
    os.dup2(devnull, 2)  # stderr
    os.close(devnull)

    # Run the daemon loop
    _run_daemon()
    os._exit(0)


def _save_record(entry_time: datetime, exit_time: datetime, hours_worked: float):
    """Save today's attendance record to history."""
    history = load_history()
    # Key by the ENTRY date, not now(): a late entry whose hours cross midnight
    # must still be filed under the day it started.
    date_key = entry_time.strftime("%Y-%m-%d")

    history[date_key] = {
        "entry_time": entry_time.strftime("%I:%M:%S %p"),
        "exit_time": exit_time.strftime("%I:%M:%S %p"),
        "hours_worked": round(hours_worked, 2),
    }

    save_history(history)
    logging.info("Attendance record saved.")


def _completed_today() -> bool:
    """True if today's attendance is already recorded (sleep already fired).

    Guards against a re-launch (e.g. launchd `RunAtLoad` on a later login)
    scheduling a second sleep for a day that's already been handled.
    """
    history = load_history()
    key = datetime.now().strftime("%Y-%m-%d")
    record = history.get(key)
    return bool(record and record.get("exit_time"))


def _give_up_time(config: dict):
    """Parse `give_up_after` ("HH:MM") into today's datetime, or None."""
    raw = config.get("give_up_after", "")
    if not raw:
        return None
    try:
        hour, minute = (int(part) for part in str(raw).split(":"))
        return datetime.now().replace(hour=hour, minute=minute, second=0, microsecond=0)
    except (ValueError, TypeError):
        logging.warning("Invalid give_up_after %r; ignoring cutoff.", raw)
        return None


def _run_daemon():
    """Main daemon loop — runs in the forked background process."""
    _setup_logging()
    _write_pid()

    signal.signal(signal.SIGTERM, _handle_signal)
    signal.signal(signal.SIGINT, _handle_signal)

    logging.info("WorkWatch daemon started (PID %d).", os.getpid())

    config = load_config()
    sender = config["sender"]

    # Idempotency guard: if today is already done, do nothing. Keeps an
    # auto-start relaunch from re-sleeping a Mac that already clocked out.
    if _completed_today():
        logging.info("Today's attendance already recorded; nothing to do. Exiting.")
        _remove_pid()
        return

    _write_state("waiting")

    # Phase 1: Find the attendance email (giving up past the configured cutoff
    # so leave/holiday days don't leave the daemon polling forever).
    give_up_at = _give_up_time(config)
    retry_count = 0
    entry_time = None

    while entry_time is None:
        if give_up_at is not None and datetime.now() >= give_up_at:
            logging.info(
                "No attendance email by %s — standing down (leave/holiday?).",
                give_up_at.strftime("%I:%M %p"),
            )
            _notify("WorkWatch", "No attendance email today — standing down.")
            _remove_pid()
            return

        success, data = fetch_today_emails(sender)

        if not success:
            logging.error("Mail error: %s", data[0] if data else "Unknown")
            _notify("WorkWatch", "Cannot reach Mail.app. Will retry in 5 min.")
            _write_state("waiting")
            time.sleep(RETRY_INTERVAL)
            continue

        if not data:
            retry_count += 1
            logging.info("No attendance email found (attempt %d). Retrying in 5 min.", retry_count)
            if retry_count == 1:
                _notify("WorkWatch", "No attendance email yet. Watching in background...")
            _write_state("waiting")
            time.sleep(RETRY_INTERVAL)
            continue

        entry_time = get_earliest_entry(data)
        if entry_time is None:
            logging.error("Could not parse entry time from email body.")
            _notify("WorkWatch Error", "Could not parse entry time from email.")
            _remove_pid()
            return

    # Phase 2: Schedule sleep (half-day if entry is at/after 2:00 PM)
    work_hours = get_effective_work_hours(entry_time, config)
    sleep_time = entry_time + timedelta(hours=work_hours)
    now = datetime.now()

    entry_str = entry_time.strftime("%I:%M:%S %p")
    sleep_str = sleep_time.strftime("%I:%M:%S %p")

    logging.info("Entry time: %s", entry_str)
    logging.info("Sleep scheduled at %s (%.1f hours)", sleep_str, work_hours)
    _write_state("countdown", entry_time, sleep_time)

    _notify(
        "WorkWatch Active",
        f"Entry: {entry_str} | Sleep at: {sleep_str}"
    )

    # Already past sleep time when we got here (started late / woke up later).
    if now >= sleep_time:
        past_seconds = (now - sleep_time).total_seconds()
        logging.info("Started past the %s mark (by %.0f min).", sleep_str, past_seconds / 60)
        # Always record the exact end (entry + work_hours), never `now`.
        _save_record(entry_time, sleep_time, work_hours)
        if past_seconds <= GRACE_SECONDS:
            _notify("WorkWatch", f"Past {sleep_str} — sleeping now...")
            time.sleep(2)
            _put_to_sleep()
        else:
            # Long past the mark — don't sleep a machine that's just been
            # opened; the day is already logged.
            logging.info("Well past sleep mark; logged only, not sleeping.")
            _notify("WorkWatch", f"Logged today's hours (ended {sleep_str}).")
        _remove_pid()
        return

    # Wait until sleep time — check every 30 seconds
    while True:
        now = datetime.now()
        remaining = (sleep_time - now).total_seconds()

        if remaining <= 0:
            break

        # Send a 5-minute warning notification
        if 270 < remaining <= 300:
            _notify("WorkWatch", "5 minutes remaining before auto-sleep!")

        time.sleep(min(30, remaining))

    # Countdown complete.
    logging.info("Countdown complete.")

    # --- Optional extra feature (OFF by default): overtime tracking ---------
    # Kept in the codebase but not triggered. Instead of sleeping at the exact
    # entry + work_hours mark, this keeps tracking active time and ends the day
    # after `inactive_threshold_minutes` of inactivity. It is unreliable when
    # the Mac sleeps (the idle probe can't observe sleep), so it is disabled in
    # favour of the exact-hours model. Re-enable via "overtime_enabled": true.
    if config.get("overtime_enabled", False):
        threshold_min = float(config.get("inactive_threshold_minutes", 10))
        _notify("WorkWatch", "Work hours done. Tracking overtime while you're active...")
        _write_state("overtime", entry_time, sleep_time,
                     overtime_start=sleep_time, last_active=sleep_time, idle_seconds=0)

        def _ot_tick(last_active, idle):
            _write_state("overtime", entry_time, sleep_time,
                         overtime_start=sleep_time,
                         last_active=last_active, idle_seconds=idle)

        exit_time = run_overtime_loop(
            entry_time, sleep_time, config,
            poll_interval=30.0,
            on_tick=_ot_tick,
        )
        ot_minutes = max(0, (exit_time - sleep_time).total_seconds() / 60)
        logging.info("Overtime ended. OT: %.1f min. Exit: %s",
                     ot_minutes, exit_time.strftime("%I:%M:%S %p"))
        _notify(
            "WorkWatch",
            f"Inactive {int(threshold_min)} min. Ending day at "
            f"{exit_time.strftime('%I:%M %p')}, sleeping...",
        )
    else:
        exit_time = sleep_time
        _notify("WorkWatch", "Work hours done! Putting Mac to sleep...")

    hours_worked = (exit_time - entry_time).total_seconds() / 3600
    _save_record(entry_time, exit_time, hours_worked)
    time.sleep(3)  # Give notification time to show
    _put_to_sleep()
    _remove_pid()
