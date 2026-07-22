"""AppleScript integration for reading emails from Mail.app."""

import subprocess
from datetime import datetime


DELIMITER = "|||WORKWATCH_SEP|||"


def _build_applescript(sender: str) -> str:
    """Build AppleScript to fetch today's emails from the given sender.

    Searches the unified inbox as well as any archive-style mailbox across
    accounts. Gmail's "Archive" action simply removes the Inbox label and
    leaves the message in "All Mail", so archived attendance mails would be
    missed if we only looked at the inbox. Messages are de-duplicated by
    message id (the same mail can appear in both the inbox and "All Mail").

    Uses Mail.app's native `whose` filtering on date received to avoid
    iterating through the entire mailbox.
    """
    return f'''
tell application "System Events"
    set mailRunning to (name of processes) contains "Mail"
end tell

if not mailRunning then
    return "ERROR:MAIL_NOT_RUNNING"
end if

tell application "Mail"
    -- Build today's date boundaries for filtering
    set todayStart to current date
    set hours of todayStart to 0
    set minutes of todayStart to 0
    set seconds of todayStart to 0

    set tomorrowStart to todayStart + (1 * days)

    -- Collect candidate message lists from the unified inbox and any
    -- archive-style mailbox (Gmail archives into "All Mail"). Mail.app
    -- filters each set by sender AND date natively.
    set msgSets to {{}}
    try
        set end of msgSets to (messages of inbox whose sender contains "{sender}" and date received >= todayStart and date received < tomorrowStart)
    end try
    repeat with acct in accounts
        repeat with mb in mailboxes of acct
            set mbName to name of mb
            if (mbName contains "All Mail") or (mbName contains "Archive") then
                try
                    set end of msgSets to (messages of mb whose sender contains "{sender}" and date received >= todayStart and date received < tomorrowStart)
                end try
            end if
        end repeat
    end repeat

    set resultBodies to {{}}
    set seenIds to {{}}
    repeat with msgs in msgSets
        repeat with msg in msgs
            set mid to (message id of msg)
            if seenIds does not contain mid then
                set end of seenIds to mid
                set msgContent to content of msg
                if msgContent contains "Entry Allowed" then
                    set end of resultBodies to msgContent
                end if
            end if
        end repeat
    end repeat

    if (count of resultBodies) = 0 then
        return "NO_EMAILS_FOUND"
    end if

    set AppleScript's text item delimiters to "{DELIMITER}"
    set resultText to resultBodies as text
    set AppleScript's text item delimiters to ""
    return resultText
end tell
'''


def fetch_today_emails(sender: str) -> tuple[bool, list[str]]:
    """Fetch today's attendance emails from Mail.app.

    Returns:
        (success, data) where:
        - success=True, data=list of email bodies
        - success=False, data=list with single error message string
    """
    script = _build_applescript(sender)

    try:
        result = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True,
            text=True,
            timeout=60,
        )
    except FileNotFoundError:
        return False, ["osascript not found. This tool requires macOS."]
    except subprocess.TimeoutExpired:
        return False, ["Timed out querying Mail.app. Is it responding?"]

    output = result.stdout.strip()
    stderr = result.stderr.strip()

    if result.returncode != 0 or not output:
        if "not running" in stderr.lower() or "ERROR:MAIL_NOT_RUNNING" in output:
            return False, [
                "❌ Mail.app is not running. Please open it and make sure your Gmail is synced."
            ]
        return False, [f"AppleScript error: {stderr or 'Unknown error'}"]

    if output == "ERROR:MAIL_NOT_RUNNING":
        return False, [
            "❌ Mail.app is not running. Please open it and make sure your Gmail is synced."
        ]

    if output == "NO_EMAILS_FOUND":
        return True, []

    bodies = [b.strip() for b in output.split(DELIMITER) if b.strip()]
    return True, bodies
