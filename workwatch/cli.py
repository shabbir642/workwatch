"""Main CLI entry point for WorkWatch."""

import os
import sys
import time
import subprocess
from datetime import datetime, timedelta

from workwatch.config import load_config, load_history, save_history, get_effective_work_hours
from workwatch.mail_reader import fetch_today_emails
from workwatch.parser import get_earliest_entry
from workwatch.timer import (
    run_countdown,
    show_waiting,
    clear_screen,
    format_time_12h,
    render_overtime,
    VERSION,
)
from workwatch.log_display import show_log
from workwatch.daemon import daemonize, stop_daemon, get_running_pid, daemon_status, read_state
from workwatch.overtime import run_overtime_loop
from workwatch.archiver import archive_month, previous_month


RETRY_INTERVAL = 300  # 5 minutes


def put_to_sleep():
    """Put the Mac to sleep using pmset."""
    try:
        result = subprocess.run(
            ["pmset", "sleepnow"],
            capture_output=True,
            text=True,
        )
        if result.returncode != 0:
            print(f"\n  \033[1;33m⚠ pmset sleepnow failed. Trying with sudo...\033[0m")
            print(f"  \033[0;90mYou may be prompted for your password.\033[0m\n")
            os.system("sudo pmset sleepnow")
    except Exception as e:
        print(f"\n  \033[1;31m❌ Failed to sleep: {e}\033[0m")
        print(f"  \033[0;90mTry running: sudo pmset sleepnow\033[0m\n")


def cmd_watch(background: bool = False):
    """Main countdown command."""
    # Check if a daemon is already running
    existing_pid = get_running_pid()
    if existing_pid:
        print(f"\n  \033[1;33m⚠ WorkWatch is already running in background (PID {existing_pid})\033[0m")
        print(f"  \033[0;90mRun 'workwatch stop' to stop it first.\033[0m\n")
        sys.exit(1)

    if background:
        print(f"\n  \033[1;36m⏱  WorkWatch v{VERSION}\033[0m")
        print(f"  Starting in background mode...\n")
        child_pid = daemonize()
        if child_pid:
            # We're in the parent — daemon forked successfully
            print(f"  \033[1;32m✅ Daemon started (PID {child_pid})\033[0m")
            print(f"  \033[0;90mYou'll get macOS notifications for status updates.\033[0m")
            print()
            print(f"  \033[1;37mCommands:\033[0m")
            print(f"    workwatch status   — Check daemon status")
            print(f"    workwatch stop     — Stop the daemon")
            print(f"    workwatch log      — View attendance log")
            print(f"  \033[0;90m")
            print(f"  Log file: ~/.workwatch.log\033[0m\n")
            return
        # If daemonize() returns None/0, we're in the child — it runs _run_daemon() internally
        return

    config = load_config()
    sender = config["sender"]

    retry_count = 0

    while True:
        success, data = fetch_today_emails(sender)

        if not success:
            # Error (e.g., Mail.app not running)
            print(f"\n  {data[0]}\n")
            sys.exit(1)

        if not data:
            # No emails found - retry
            retry_count += 1
            next_retry = datetime.now() + timedelta(seconds=RETRY_INTERVAL)
            show_waiting(retry_count, next_retry)

            try:
                time.sleep(RETRY_INTERVAL)
            except KeyboardInterrupt:
                print(f"\n\n  \033[1;33m👋 Cancelled. Goodbye!\033[0m\n")
                sys.exit(0)
            continue

        # Found emails - parse entry time
        entry_time = get_earliest_entry(data)

        if entry_time is None:
            print(f"\n  \033[1;31m❌ Could not parse entry time from emails.\033[0m")
            print(f"  \033[0;90mExpected format: Entry Allowed at IN First Floor on DD/MM/YYYY HH:MM:SS AM/PM\033[0m\n")
            sys.exit(1)

        break

    # Calculate sleep time (half-day if entry is at/after 2:00 PM)
    work_hours = get_effective_work_hours(entry_time, config)
    sleep_time = entry_time + timedelta(hours=work_hours)
    now = datetime.now()

    # Check if already past sleep time
    if now >= sleep_time:
        elapsed = now - entry_time
        elapsed_hours = elapsed.total_seconds() / 3600

        clear_screen()
        print(f"\n  \033[1;33m⚠ You've already worked {elapsed_hours:.1f} hours!\033[0m")
        print(f"  🕐 Entry Time: \033[1;32m{format_time_12h(entry_time)}\033[0m")
        print(f"  🛑 Sleep was due at: \033[1;31m{format_time_12h(sleep_time)}\033[0m")
        print(f"  ⏰ Current time: \033[1;37m{format_time_12h(now)}\033[0m")
        print()
        print(f"  \033[1;31mPutting your Mac to sleep now...\033[0m\n")

        _save_record(entry_time, now, elapsed_hours)
        time.sleep(2)
        put_to_sleep()
        return

    # Run countdown
    completed = run_countdown(entry_time, sleep_time)

    if completed:
        if config.get("overtime_enabled", True):
            threshold_min = float(config.get("inactive_threshold_minutes", 10))

            def _tick(last_active, idle):
                render_overtime(entry_time, sleep_time, last_active, idle, threshold_min)

            exit_time = run_overtime_loop(
                entry_time, sleep_time, config,
                poll_interval=1.0,
                on_tick=_tick,
            )
        else:
            exit_time = sleep_time

        hours_worked = (exit_time - entry_time).total_seconds() / 3600
        _save_record(entry_time, exit_time, hours_worked)
        time.sleep(1)
        put_to_sleep()


def _save_record(entry_time: datetime, exit_time: datetime, hours_worked: float):
    """Save today's attendance record to history."""
    history = load_history()
    date_key = datetime.now().strftime("%Y-%m-%d")

    history[date_key] = {
        "entry_time": entry_time.strftime("%I:%M:%S %p"),
        "exit_time": exit_time.strftime("%I:%M:%S %p"),
        "hours_worked": round(hours_worked, 2),
    }

    save_history(history)
    print(f"  \033[1;32m✅ Attendance record saved.\033[0m")


def cmd_status():
    """Show daemon status with live countdown."""
    info = daemon_status()
    if info is None:
        print(f"\n  \033[0;90mNo WorkWatch daemon is running.\033[0m")
        print(f"  \033[0;90mStart one with: workwatch --bg\033[0m\n")
        return

    state = read_state()
    pid = info["pid"]

    print(f"\n  \033[1;32m●\033[0m \033[1;37mWorkWatch daemon running\033[0m (PID {pid})")

    if state is None:
        print(f"  \033[0;90mNo state file found.\033[0m\n")
        return

    phase = state.get("phase", "unknown")

    if phase == "waiting":
        print(f"  \033[1;33m⏳ Waiting for attendance email...\033[0m")
        print(f"  \033[0;90mRetrying every 5 minutes.\033[0m\n")
        return

    if phase == "countdown":
        entry_str = state.get("entry_time", "—")
        sleep_str = state.get("sleep_time", "—")
        sleep_iso = state.get("sleep_iso")
        today_str = datetime.now().strftime("%d/%m/%Y")

        print()
        print(f"  📅 Date:       \033[1;37m{today_str}\033[0m")
        print(f"  🕐 Entry Time: \033[1;32m{entry_str}\033[0m")
        print(f"  🛑 Sleep At:   \033[1;31m{sleep_str}\033[0m")

        if sleep_iso:
            sleep_dt = datetime.fromisoformat(sleep_iso)
            remaining = (sleep_dt - datetime.now()).total_seconds()

            if remaining > 0:
                h = int(remaining) // 3600
                m = (int(remaining) % 3600) // 60
                s = int(remaining) % 60

                if remaining <= 300:
                    color = "\033[1;31m"
                elif remaining <= 1800:
                    color = "\033[1;33m"
                else:
                    color = "\033[1;32m"

                print(f"  ⏳ Remaining:  {color}{h:02d}:{m:02d}:{s:02d}\033[0m")
            else:
                print(f"  \033[1;31m⏰ Time's up! Sleep imminent.\033[0m")

        print()
        return

    if phase == "overtime":
        entry_str = state.get("entry_time", "—")
        sleep_str = state.get("sleep_time", "—")
        entry_iso = state.get("entry_iso")
        ot_start_iso = state.get("overtime_start_iso") or state.get("sleep_iso")
        last_active_iso = state.get("last_active_iso")
        idle_seconds = state.get("idle_seconds", 0)
        today_str = datetime.now().strftime("%d/%m/%Y")

        print(f"\n  \033[1;33m● OVERTIME\033[0m — tracking while active")
        print(f"  📅 Date:        \033[1;37m{today_str}\033[0m")
        print(f"  🕐 Entry Time:  \033[1;32m{entry_str}\033[0m")
        print(f"  ✅ Base Done:   \033[1;32m{sleep_str}\033[0m")

        if entry_iso and last_active_iso:
            entry_dt = datetime.fromisoformat(entry_iso)
            last_active_dt = datetime.fromisoformat(last_active_iso)
            ot_start_dt = datetime.fromisoformat(ot_start_iso) if ot_start_iso else entry_dt
            ot_sec = max(0, int((last_active_dt - ot_start_dt).total_seconds()))
            total_sec = int((last_active_dt - entry_dt).total_seconds())
            total_h = total_sec // 3600
            total_m = (total_sec % 3600) // 60
            total_s = total_sec % 60
            ot_h = ot_sec // 3600
            ot_m = (ot_sec % 3600) // 60
            ot_s = ot_sec % 60
            print(f"  ⏰ Overtime:    \033[1;33m+{ot_h:02d}:{ot_m:02d}:{ot_s:02d}\033[0m")
            print(f"  🟢 Total:       \033[1;37m{total_h:02d}:{total_m:02d}:{total_s:02d}\033[0m")
            print(f"  🎯 Last active: \033[1;37m{last_active_dt.strftime('%I:%M:%S %p')}\033[0m"
                  f"  \033[0;90m(idle {int(idle_seconds)}s)\033[0m")

        print()
        return

    print(f"  \033[0;90mPhase: {phase}\033[0m\n")


def cmd_stop():
    """Stop the background daemon."""
    stopped = stop_daemon()
    if stopped:
        print(f"\n  \033[1;32m✅ WorkWatch daemon stopped.\033[0m\n")
    else:
        print(f"\n  \033[0;90mNo WorkWatch daemon is running.\033[0m\n")


def cmd_log(args: list[str]):
    """Monthly log command."""
    year = None
    month = None

    # Parse optional --month YYYY-MM argument
    if "--month" in args:
        idx = args.index("--month")
        if idx + 1 < len(args):
            try:
                parts = args[idx + 1].split("-")
                year = int(parts[0])
                month = int(parts[1])
            except (ValueError, IndexError):
                print(f"  \033[1;31m❌ Invalid month format. Use: --month YYYY-MM\033[0m\n")
                sys.exit(1)

    show_log(year, month)


def cmd_archive(args: list[str]):
    """Email a month's attendance (JSON + analytics) and delete it from history."""
    config = load_config()
    email = config.get("archive_email", "") or ""
    year = month = None
    dry_run = False

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--month" and i + 1 < len(args):
            try:
                parts = args[i + 1].split("-")
                year, month = int(parts[0]), int(parts[1])
            except (ValueError, IndexError):
                print(f"  \033[1;31m❌ Invalid --month format. Use YYYY-MM.\033[0m\n")
                sys.exit(1)
            i += 2
            continue
        if a == "--email" and i + 1 < len(args):
            email = args[i + 1]
            i += 2
            continue
        if a == "--dry-run":
            dry_run = True
            i += 1
            continue
        print(f"  \033[1;31m❌ Unknown argument: {a}\033[0m\n")
        sys.exit(1)

    if year is None or month is None:
        year, month = previous_month()

    if not email and not dry_run:
        print(f"\n  \033[1;31m❌ No archive email configured.\033[0m")
        print(f"  \033[0;90mSet 'archive_email' in ~/.workwatch.json or pass --email.\033[0m\n")
        sys.exit(1)

    label = f"{year:04d}-{month:02d}"
    if dry_run:
        print(f"\n  \033[1;36m⏱  Preview archive for {label}\033[0m\n")
    else:
        print(f"\n  \033[1;36m⏱  Archiving {label} → {email}\033[0m")

    ok, msg = archive_month(year, month, email, dry_run=dry_run)
    if not ok:
        print(f"\n  \033[1;31m❌ {msg}\033[0m\n")
        sys.exit(1)

    if dry_run:
        print("  \033[0;90m--- email body ---\033[0m")
        print(msg)
        print("  \033[0;90m--- end ---\033[0m\n")
        return

    print(f"  \033[1;32m✅ {msg}\033[0m\n")


def cmd_recap(args: list[str]):
    """Time-windowed 'what I did' summary (git + Claude sessions), emailed."""
    from workwatch.recap import run_recap

    config = load_config()
    window = config.get("recap_default_window", "24h")
    email = config.get("archive_email", "") or ""
    author = config.get("recap_author", "") or ""
    include_claude = config.get("recap_include_claude", True)
    repos: list[str] = []
    dry_run = False

    i = 0
    while i < len(args):
        a = args[i]
        if a == "--email" and i + 1 < len(args):
            email = args[i + 1]
            i += 2
            continue
        if a == "--repo" and i + 1 < len(args):
            repos.append(args[i + 1])
            i += 2
            continue
        if a == "--dry-run":
            dry_run = True
            i += 1
            continue
        if a == "--no-claude":
            include_claude = False
            i += 1
            continue
        if not a.startswith("-"):
            window = a
            i += 1
            continue
        print(f"  \033[1;31m❌ Unknown argument: {a}\033[0m\n")
        sys.exit(1)

    if not repos:
        repos = [os.path.expanduser(p) for p in config.get("recap_repos", []) or []]

    if not repos:
        print(f"\n  \033[1;31m❌ No repos to scan.\033[0m")
        print(f"  \033[0;90mSet 'recap_repos' in ~/.workwatch.json or pass --repo DIR.\033[0m\n")
        sys.exit(1)

    if not email and not dry_run:
        print(f"\n  \033[1;31m❌ No recipient email configured.\033[0m")
        print(f"  \033[0;90mSet 'archive_email' in ~/.workwatch.json or pass --email.\033[0m\n")
        sys.exit(1)

    if dry_run:
        print(f"\n  \033[1;36m⏱  Recap preview (last {window})\033[0m\n")
    else:
        print(f"\n  \033[1;36m⏱  Building recap (last {window}) → {email}\033[0m")

    ok, msg = run_recap(window, repos, author, email,
                        dry_run=dry_run, include_claude=include_claude)
    if not ok:
        print(f"\n  \033[1;31m❌ {msg}\033[0m\n")
        sys.exit(1)

    if dry_run:
        print(msg)
        print()
        return

    print(f"  \033[1;32m✅ {msg}\033[0m\n")


def cmd_version():
    """Show version."""
    print(f"WorkWatch v{VERSION}")


def cmd_help():
    """Show help text."""
    print(f"""
\033[1;36mWorkWatch v{VERSION}\033[0m — Auto-sleep after your work hours

\033[1;37mUsage:\033[0m
  workwatch              Start the countdown timer (foreground)
  workwatch --bg         Start in background (daemon mode)
  workwatch status       Check if daemon is running
  workwatch stop         Stop the background daemon
  workwatch log          Show monthly attendance log
  workwatch log --month YYYY-MM
                         Show log for a specific month
  workwatch archive      Email + delete last month's records
  workwatch archive --month YYYY-MM [--email addr] [--dry-run]
                         Archive a specific month (or preview)
  workwatch recap [WINDOW] [--repo DIR] [--email addr] [--dry-run] [--no-claude]
                         "What I did" summary (git + Claude sessions), emailed.
                         WINDOW: 24h (default), 90m, 7d, 2w, 1mo
  workwatch version      Show version
  workwatch help         Show this help message

\033[1;37mBackground mode:\033[0m
  Runs silently, sends macOS notifications, sleeps Mac when done.
  Log: ~/.workwatch.log | PID: ~/.workwatch.pid

\033[1;37mConfig:\033[0m
  ~/.workwatch.json      Edit work_hours and sender email

\033[1;37mHistory:\033[0m
  ~/.workwatch_history.json
                         Attendance records (auto-managed)
""")


def main():
    """CLI entry point."""
    args = sys.argv[1:]

    if not args:
        cmd_watch(background=False)
    elif args[0] == "--bg":
        cmd_watch(background=True)
    elif args[0] == "status":
        cmd_status()
    elif args[0] == "stop":
        cmd_stop()
    elif args[0] == "log":
        cmd_log(args[1:])
    elif args[0] == "archive":
        cmd_archive(args[1:])
    elif args[0] == "recap":
        cmd_recap(args[1:])
    elif args[0] == "version":
        cmd_version()
    elif args[0] in ("help", "--help", "-h"):
        cmd_help()
    else:
        print(f"  \033[1;31m❌ Unknown command: {args[0]}\033[0m")
        cmd_help()
        sys.exit(1)


if __name__ == "__main__":
    main()
