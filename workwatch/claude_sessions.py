"""The "Explored" bucket for `workwatch recap`: a summary of recent
Claude Code sessions.

Claude Code stores each session as a JSONL transcript under
``~/.claude/projects/<sanitized-project-path>/<session-uuid>.jsonl``. The
schema is internal and undocumented, so every line is parsed defensively and
unrecognized lines are skipped — a format change should degrade the bucket,
never crash the recap.

SECURITY: transcripts contain everything typed and every file Claude read.
This module emits only *summaries* (title, project, counts) — never raw
transcript content. Callers must not put transcript bodies into emails.
"""

import json
from datetime import datetime
from pathlib import Path

PROJECTS_DIR = Path.home() / ".claude" / "projects"

# Prefixes that mark a "user" line as machinery, not a human prompt.
_NON_PROMPT_PREFIXES = ("<command-message>", "<command-name>", "<command-args>",
                        "<system-reminder>", "<local-command-stdout>")


def _content_text(content) -> str | None:
    """Extract human text from a message `content` (str or block list).

    Returns None if the content is purely tool plumbing (tool_result, etc.).
    """
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        for block in content:
            if isinstance(block, dict) and block.get("type") == "text":
                return block.get("text", "")
    return None


def _is_human_prompt(text: str | None) -> bool:
    if not text:
        return False
    stripped = text.lstrip()
    return not stripped.startswith(_NON_PROMPT_PREFIXES)


def _summarize_file(path: Path) -> dict | None:
    """Parse one session JSONL into a summary dict, or None if unreadable."""
    summary = {
        "session": path.stem,
        "project": None,
        "title": None,
        "first_prompt": None,
        "prompts": 0,
        "tools": 0,
        "first_ts": None,
        "last_ts": None,
    }

    try:
        raw = path.read_text(errors="replace")
    except OSError:
        return None

    saw_any = False
    for line in raw.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            d = json.loads(line)
        except (json.JSONDecodeError, ValueError):
            continue  # skip unrecognized line, keep going
        if not isinstance(d, dict):
            continue
        saw_any = True

        ts = d.get("timestamp")
        if ts:
            if summary["first_ts"] is None:
                summary["first_ts"] = ts
            summary["last_ts"] = ts

        dtype = d.get("type")

        # Claude-generated session title — the nicest one-liner we can show.
        if dtype == "ai-title" and d.get("aiTitle"):
            summary["title"] = str(d["aiTitle"]).strip()
            continue

        # Capture the project cwd from any line that carries it.
        if summary["project"] is None and d.get("cwd"):
            summary["project"] = Path(str(d["cwd"])).name

        msg = d.get("message")
        if not isinstance(msg, dict):
            continue

        if dtype == "user" and msg.get("role") == "user":
            text = _content_text(msg.get("content"))
            if _is_human_prompt(text):
                summary["prompts"] += 1
                if summary["first_prompt"] is None:
                    summary["first_prompt"] = " ".join(text.split())[:140]

        elif dtype == "assistant":
            content = msg.get("content")
            if isinstance(content, list):
                summary["tools"] += sum(
                    1 for b in content
                    if isinstance(b, dict) and b.get("type") == "tool_use"
                )

    if not saw_any:
        return None

    if summary["project"] is None:
        # Fall back to a best-effort decode of the sanitized dir name.
        summary["project"] = path.parent.name.split("-")[-1] or "unknown"
    return summary


def collect_sessions(since: datetime, now: datetime | None = None) -> dict:
    """Summarize Claude Code sessions whose transcript was modified in-window.

    Returns {"available": bool, "skipped": str|None, "sessions": [summary,...]}
    sorted by last activity (most recent first).
    """
    result = {"available": True, "skipped": None, "sessions": []}

    if not PROJECTS_DIR.is_dir():
        return {"available": False, "skipped": "no ~/.claude/projects", "sessions": []}

    since_ts = since.timestamp()
    sessions = []
    try:
        files = list(PROJECTS_DIR.glob("**/*.jsonl"))
    except OSError as exc:
        return {"available": False, "skipped": str(exc), "sessions": []}

    for path in files:
        try:
            if path.stat().st_mtime < since_ts:
                continue
        except OSError:
            continue
        s = _summarize_file(path)
        if s:
            sessions.append(s)

    sessions.sort(key=lambda s: s.get("last_ts") or "", reverse=True)
    result["sessions"] = sessions
    return result
