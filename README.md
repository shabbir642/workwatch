<p align="center">
  <strong>WorkWatch</strong><br>
  <em>Auto-sleep your Mac when work hours are done.</em>
</p>

<p align="center">
  <a href="#installation">Install</a> &nbsp;&bull;&nbsp;
  <a href="#usage">Usage</a> &nbsp;&bull;&nbsp;
  <a href="#configuration">Config</a> &nbsp;&bull;&nbsp;
  <a href="#how-it-works">How It Works</a>
</p>

---

WorkWatch connects to Apple Mail.app, finds your VMock attendance email, parses the earliest entry time, and automatically puts your Mac to sleep after the configured work hours. It supports a live terminal countdown, a background daemon with macOS notifications, and a color-coded monthly attendance log.

## Quick Start

```bash
curl -fsSL https://sagnikbhowmick.com/workwatch/install.sh | bash
```

```bash
workwatch        # start countdown timer
workwatch --bg   # run in background
```

## Prerequisites

- **macOS** (requires Mail.app and `pmset`)
- Gmail account added to **Apple Mail** (System Settings > Internet Accounts)
- Mail.app running and synced before launching WorkWatch
- Python 3.9+ (only needed for source install — the binary has no dependencies)

## Installation

### One-line Install (Recommended)

```bash
curl -fsSL https://sagnikbhowmick.com/workwatch/install.sh | bash
```

Downloads a prebuilt native binary to `/usr/local/bin`. The installer auto-detects your architecture (Apple Silicon / Intel), verifies SHA-256 checksums, and falls back to a source install via pip if the binary download fails.

To install to a custom directory:

```bash
WORKWATCH_INSTALL_DIR=~/.local/bin curl -fsSL https://sagnikbhowmick.com/workwatch/install.sh | bash
```

### Install from Source

```bash
git clone https://github.com/sagnikb7/workwatch.git
cd workwatch
pip install -e .
```

Installs `workwatch` as a Python package. Edits to the source take effect immediately.

### Build Binary Locally

```bash
git clone https://github.com/sagnikb7/workwatch.git
cd workwatch
make build
sudo rm -f /usr/local/bin/workwatch
sudo ditto dist/workwatch /usr/local/bin/workwatch   # ditto preserves the code signature; cp breaks it on macOS 26+
sudo chown $USER:staff /usr/local/bin/workwatch
```

> `make install` runs these three commands for you (it will prompt for `sudo`).

### Verify

```bash
workwatch version
# WorkWatch v1.0.0
```

## Usage

### Foreground Mode

```bash
workwatch
```

1. Queries Mail.app for today's attendance email from the configured sender
2. Parses the earliest "Entry Allowed" timestamp
3. Shows a live countdown timer in the terminal
4. When the countdown hits zero, saves the record and puts your Mac to sleep

If no email is found yet, it retries automatically every 5 minutes.

### Background Mode (Daemon)

```bash
workwatch --bg
```

Runs silently in the background — you can close the terminal. Sends macOS notifications for:

- Entry email found and countdown started
- 5-minute warning before sleep
- Putting Mac to sleep

Manage the daemon:

```bash
workwatch status   # check if daemon is running + live countdown
workwatch stop     # stop the daemon
```

Logs: `~/.workwatch.log` &nbsp;|&nbsp; PID file: `~/.workwatch.pid`

### Run Automatically (Auto-start)

So you never have to launch it by hand, install a macOS **LaunchAgent**:

```bash
workwatch install-agent     # set it up
workwatch uninstall-agent   # remove it
```

This starts the daemon `--bg`:

- **Mon–Fri at 8:00 AM**, and
- **at every login/boot** — so a day the Mac was off at 8 AM is still covered when you next turn it on.

Once started, the daemon polls every 5 minutes until your attendance mail arrives, then schedules sleep. Safe by design:

- **No re-sleep after clock-out** — if the day's already recorded, a relaunch exits immediately (no accidental second sleep).
- **Leave / holiday days** — if no attendance mail has arrived by `give_up_after` (default `16:00`, set in `~/.workwatch.json`), the daemon stands down instead of polling all day.
- **Half-days** — auto-detected from your entry time (≥ 2 PM → `half_day_hours`); nothing extra to configure.

Startup output goes to `~/.workwatch.launchd.log`.

### Monthly Attendance Log

```bash
workwatch log                    # current month
workwatch log --month 2026-02    # specific month
```

Color-coded table with entry/exit times, hours worked, and a monthly summary:

| Indicator | Meaning |
|-----------|---------|
| Green | 9+ hours (done) |
| Yellow | In progress (today) |
| Red | Under 9 hours (short) |
| Gray | Weekend / no data |

### Monthly Archive (email + purge)

```bash
workwatch archive                         # emails last month, deletes those entries
workwatch archive --month 2026-02         # specific month
workwatch archive --email me@example.com  # override configured address
workwatch archive --dry-run               # preview the email body, send nothing
```

Sends the month's records as an **HTML email** (color-coded analytics badges at the top, a daily log table below) with a nicely-formatted plain-text fallback, through **Mail.app** (same account stack as the attendance reader — no SMTP setup needed). Before deleting from `~/.workwatch_history.json`, a snapshot is written to `~/.workwatch_archives/YYYY-MM.{json,html}` as a safety net in case email delivery silently fails.

Analytics included: month, working days, total hours, average/day, longest day, shortest day, overtime day count + total OT hours, short/half-day count. Each row is tagged with a color level (🟢 good / 🟡 warn / 🔴 bad / ⭐ primary / 🔵 info).

Each daily log row shows Date, Day, Entry, Exit, Hours, Overtime, and a status badge (Full / Short / Half).

Schedule via cron to run automatically on the 1st of each month:

```cron
0 9 1 * * /usr/local/bin/workwatch archive >> ~/.workwatch.log 2>&1
```

### Command Reference

| Command | Description |
|---------|-------------|
| `workwatch` | Start foreground countdown timer |
| `workwatch --bg` | Start background daemon |
| `workwatch status` | Show daemon status with live countdown |
| `workwatch stop` | Stop background daemon |
| `workwatch install-agent` | Auto-start daemon on workdays (Mon–Fri 8 AM + login) |
| `workwatch uninstall-agent` | Remove the auto-start LaunchAgent |
| `workwatch log` | Show current month's attendance log |
| `workwatch log --month YYYY-MM` | Show log for a specific month |
| `workwatch archive` | Email last month's records + analytics to `archive_email`, then delete |
| `workwatch archive --month YYYY-MM [--email addr] [--dry-run]` | Archive a specific month (or preview) |
| `workwatch version` | Print version |
| `workwatch help` | Show help text |

## Configuration

Config file: `~/.workwatch.json` (auto-created on first run)

```json
{
  "work_hours": 9,
  "half_day_hours": 4.5,
  "sender": "attendance@vmock.com",
  "overtime_enabled": true,
  "inactive_threshold_minutes": 10
}
```

| Key | Description | Default |
|-----|-------------|---------|
| `work_hours` | Hours to work before auto-sleep (supports decimals like `8.5`) | `9` |
| `half_day_hours` | Hours to work when entry is at/after 2:00 PM (half-day) | `4.5` |
| `sender` | Email address to search for in Mail.app | `attendance@vmock.com` |
| `overtime_enabled` | After countdown ends, keep tracking while you're active instead of sleeping immediately | `true` |
| `inactive_threshold_minutes` | Minutes of keyboard/mouse idle that ends overtime and triggers sleep | `10` |
| `archive_email` | Destination address for `workwatch archive` monthly emails (leave blank to require `--email`) | `""` |

## Files

| Path | Purpose |
|------|---------|
| `~/.workwatch.json` | Configuration (work hours, sender) |
| `~/.workwatch_history.json` | Attendance records (auto-managed) |
| `~/.workwatch.log` | Daemon log (background mode only) |
| `~/.workwatch.pid` | Daemon PID file (background mode only) |
| `~/.workwatch_state.json` | Daemon state (inter-process, auto-managed) |
| `~/.workwatch_archives/YYYY-MM.json` | Local JSON backup written by `workwatch archive` before deleting from history |
| `~/.workwatch_archives/YYYY-MM.html` | Local HTML rendering of the email body (open in a browser to re-view) |

## How It Works

1. Queries Apple Mail.app via AppleScript for today's emails from the configured sender
2. Uses Mail.app's native `whose` filtering by sender and date for fast lookups
3. Parses "Entry Allowed" timestamps from email bodies using regex
4. If multiple entry emails exist, uses the earliest (first check-in)
5. Calculates sleep time = earliest entry + work hours (uses `half_day_hours` if entry is at/after 2:00 PM, else `work_hours`)
6. **Foreground**: live terminal countdown &nbsp;|&nbsp; **Background**: silent daemon with macOS notifications
7. When the countdown hits zero:
   - If `overtime_enabled` (default): enters overtime tracking — polls the macOS HID idle counter (`ioreg -c IOHIDSystem`) and keeps logging hours while you're active. When keyboard/mouse has been idle for `inactive_threshold_minutes` (default 10), the last-active moment becomes the exit time.
   - Otherwise: exit time = scheduled sleep time.
8. Saves the attendance record to `~/.workwatch_history.json` with `hours_worked` = `(exit_time − entry_time)` (base + OT folded in)
9. Triggers `pmset sleepnow` to put the Mac to sleep

## Building & Releasing

### Build Commands

```bash
make build       # Build standalone binary -> dist/workwatch
make install     # Build + copy to /usr/local/bin
make release     # Build + create tarball + SHA-256 checksum
make dev         # pip install -e . (editable source install)
make run         # Run directly from source
make run-log     # Run log view from source
make clean       # Remove build artifacts
```

### Creating a Release

```bash
./scripts/release.sh
```

Produces:

```
release/
├── workwatch-1.0.0-darwin-arm64.tar.gz
└── workwatch-1.0.0-darwin-arm64.tar.gz.sha256
```

### Deploying

Upload the release artifacts to your server so they're accessible at:

```
https://sagnikbhowmick.com/workwatch/install.sh
https://sagnikbhowmick.com/workwatch/releases/workwatch-<version>-darwin-<arch>.tar.gz
https://sagnikbhowmick.com/workwatch/releases/workwatch-<version>-darwin-<arch>.tar.gz.sha256
```

Then anyone on a Mac can install with:

```bash
curl -fsSL https://sagnikbhowmick.com/workwatch/install.sh | bash
```

## Project Structure

```
workwatch/
├── Makefile                  # Build, install, release targets
├── pyproject.toml            # Python package config
├── workwatch.spec            # PyInstaller binary spec
├── scripts/
│   ├── install.sh            # Remote installer (curl | bash)
│   └── release.sh            # Build + package release artifacts
└── workwatch/
    ├── __init__.py           # Package version
    ├── __main__.py           # python -m workwatch support
    ├── cli.py                # CLI entry point & command routing
    ├── config.py             # Config & history file management
    ├── daemon.py             # Background mode (fork, notifications, PID)
    ├── log_display.py        # ANSI color-coded monthly table
    ├── mail_reader.py        # AppleScript <-> Mail.app integration
    ├── notifier.py           # macOS notification helper
    ├── parser.py             # Regex extraction of entry times
    └── timer.py              # Live countdown display
```

## License

MIT
