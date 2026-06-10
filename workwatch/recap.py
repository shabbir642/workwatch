"""`workwatch recap` — a time-windowed "what I did" summary.

Collects git activity (Shipped / In progress) and recent Claude Code sessions
(Explored) over a window like 24h / 7d / 2w / 1mo, renders an HTML + plain-text
report with a blunt rule-based verdict, and emails it via the existing Mail.app
pipeline (reused from `archiver.py`).

All free text (commit subjects, session titles, the verdict) flows only into the
email *body*, which `archiver._send_via_mail_app` reads from a temp file with no
escaping — so it can never inject AppleScript.
"""

import json
import re
from datetime import datetime, timedelta
from pathlib import Path

from workwatch.archiver import (
    _send_via_mail_app,
    _html_badge,
    _HTML_BADGE,
    _EMOJI,
)
from workwatch import git_activity
from workwatch.git_activity import repo_is_dirty, repo_dirty_count
from workwatch import claude_sessions

RECAP_DIR = Path.home() / ".workwatch_recaps"

# window token → minutes multiplier
_UNIT_MINUTES = {
    "m": 1,
    "h": 60,
    "d": 60 * 24,
    "w": 60 * 24 * 7,
    "mo": 60 * 24 * 30,
}
_WINDOW_RE = re.compile(r"^(\d+)(mo|m|h|d|w)$")


class WindowError(ValueError):
    """Raised for an unparseable window string."""


def parse_window(spec: str, now: datetime | None = None) -> tuple[datetime, str]:
    """Parse a window like '24h', '90m', '7d', '2w', '1mo'.

    Returns (since_datetime, normalized_label). Raises WindowError on bad input.
    """
    now = now or datetime.now()
    m = _WINDOW_RE.match(spec.strip().lower())
    if not m:
        raise WindowError(
            f"Invalid window '{spec}'. Use a number + unit: m, h, d, w, mo "
            f"(e.g. 90m, 24h, 7d, 2w, 1mo)."
        )
    n, unit = int(m.group(1)), m.group(2)
    if n <= 0:
        raise WindowError("Window must be a positive number.")
    since = now - timedelta(minutes=n * _UNIT_MINUTES[unit])
    return since, f"{n}{unit}"


# ---------- collection ----------

def build_recap(window: str, repos: list[str], author: str = "",
                include_claude: bool = True,
                now: datetime | None = None) -> dict:
    """Assemble the neutral recap data structure."""
    now = now or datetime.now()
    since, label = parse_window(window, now)
    since_iso = since.isoformat(timespec="seconds")

    repo_data = git_activity.collect_repos(repos, since_iso, author)

    sessions = ({"available": False, "skipped": "disabled", "sessions": []}
                if not include_claude
                else claude_sessions.collect_sessions(since, now))

    usable = [r for r in repo_data if not r.get("skipped")]
    total_commits = sum(len(r["finished"]) for r in usable)
    total_unpushed = sum(r["unpushed"] for r in usable)
    total_stashes = sum(r["stashes"] for r in usable)
    dirty_repos = [r for r in usable if repo_is_dirty(r)]
    repos_with_commits = [r for r in usable if r["finished"]]

    return {
        "window": label,
        "since": since,
        "now": now,
        "repos": repo_data,
        "usable_repos": usable,
        "sessions": sessions,
        "totals": {
            "commits": total_commits,
            "unpushed": total_unpushed,
            "stashes": total_stashes,
            "dirty_repos": len(dirty_repos),
            "repos_touched": len(repos_with_commits) + len(dirty_repos),
            "repos_scanned": len(repo_data),
            "sessions": len(sessions["sessions"]),
        },
    }


# ---------- blunt verdict (rule-based) ----------

def rule_based_verdict(data: dict) -> list[dict]:
    """Return a list of {text, level} verdict lines. Always non-empty."""
    t = data["totals"]
    usable = data["usable_repos"]
    lines: list[dict] = []

    dirty_files = sum(repo_dirty_count(r) for r in usable)

    if t["commits"] == 0 and dirty_files > 0:
        lines.append({
            "level": "bad",
            "text": (f"Nothing shipped. {dirty_files} file(s) sitting uncommitted "
                     f"across {t['dirty_repos']} repo(s) — that's not 'in progress', "
                     f"that's untracked risk."),
        })
    elif t["commits"] == 0 and t["sessions"] == 0:
        lines.append({
            "level": "neutral",
            "text": "Quiet window — nothing committed, nothing in flight.",
        })

    if t["unpushed"] > 0:
        lines.append({
            "level": "warn",
            "text": (f"{t['unpushed']} commit(s) never left your laptop. "
                     f"They don't exist until they're pushed."),
        })

    if t["stashes"] > 3:
        lines.append({
            "level": "warn",
            "text": f"{t['stashes']} stashes rotting. Resolve them or delete them.",
        })

    # Focus check: all commits landed in one repo while others sat dirty/stale.
    repos_with_commits = [r for r in usable if r["finished"]]
    if len(repos_with_commits) == 1 and t["commits"] > 0 and len(usable) > 1:
        other_dirty = any(repo_is_dirty(r) for r in usable if not r["finished"])
        if other_dirty:
            lines.append({
                "level": "info",
                "text": (f"Focus was on {repos_with_commits[0]['name']}; "
                         f"the rest you only poked."),
            })

    # Exploring vs landing.
    if t["sessions"] >= 3 and t["commits"] <= 1:
        lines.append({
            "level": "warn",
            "text": (f"{t['sessions']} Claude sessions, {t['commits']} commit(s) — "
                     f"lots of exploring, little landing."),
        })

    if t["commits"] > 0 and t["unpushed"] == 0 and dirty_files == 0:
        lines.append({
            "level": "good",
            "text": "Clean: everything committed and pushed. Good window.",
        })

    if not lines:
        lines.append({"level": "neutral", "text": "Steady. Nothing alarming."})
    return lines


# ---------- rendering helpers ----------

def _fmt_time(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).strftime("%b %d %H:%M")
    except ValueError:
        return iso[:16]


def _repo_status(repo: dict) -> tuple[str, str]:
    """(label, level) badge for a repo's state."""
    if repo.get("skipped"):
        return ("skipped", "neutral")
    has_commits = bool(repo["finished"])
    dirty = repo_is_dirty(repo)
    if has_commits and repo["unpushed"] == 0 and not dirty:
        return ("shipped", "good")
    if has_commits and repo["unpushed"] > 0:
        return ("unpushed", "warn")
    if dirty and not has_commits:
        return ("dirty only", "bad")
    if has_commits:
        return ("in progress", "warn")
    return ("idle", "neutral")


# ---------- plain-text renderer ----------

def build_plain_body(data: dict) -> str:
    t = data["totals"]
    win = data["window"]
    L = [
        f"⏱  WorkWatch Recap — last {win}",
        "=" * 52,
        f"  {data['since'].strftime('%b %d %H:%M')}  →  {data['now'].strftime('%b %d %H:%M')}",
        "",
        "VERDICT",
        "-" * 52,
    ]
    for v in rule_based_verdict(data):
        L.append(f"  {_EMOJI.get(v['level'], '•')}  {v['text']}")

    L += [
        "",
        "AT A GLANCE",
        "-" * 52,
        f"  Commits: {t['commits']}   Unpushed: {t['unpushed']}   "
        f"Dirty repos: {t['dirty_repos']}   Stashes: {t['stashes']}   "
        f"Sessions: {t['sessions']}",
        "",
        "SHIPPED & IN PROGRESS",
        "-" * 52,
    ]
    any_repo = False
    for r in data["repos"]:
        if r.get("skipped"):
            L.append(f"  ⚪ {r['name']}: skipped ({r['skipped']})")
            continue
        any_repo = True
        label, level = _repo_status(r)
        d = r["dirty"]
        notes = f"  [{', '.join(r['notes'])}]" if r["notes"] else ""
        L.append(
            f"  {_EMOJI.get(level, '•')} {r['name']} ({r['branch'] or '—'}) — "
            f"{len(r['finished'])} commit(s), {r['unpushed']} unpushed, "
            f"{d['staged']}S/{d['modified']}M/{d['untracked']}U, "
            f"{r['stashes']} stash(es){notes}"
        )
        for h, ts, subj in r["finished"][:10]:
            L.append(f"        · {subj}  ({h})")
        if len(r["finished"]) > 10:
            L.append(f"        … and {len(r['finished']) - 10} more")
    if not any_repo:
        L.append("  (no usable repos scanned)")

    sess = data["sessions"]
    L += ["", "EXPLORED (Claude Code sessions)", "-" * 52]
    if not sess["available"]:
        L.append(f"  (skipped: {sess['skipped']})")
    elif not sess["sessions"]:
        L.append("  (no sessions in this window)")
    else:
        for s in sess["sessions"][:15]:
            title = s["title"] or s["first_prompt"] or "(untitled session)"
            L.append(
                f"  🔵 {s['project']}: {title}  "
                f"({s['prompts']} prompt(s), {s['tools']} tool use(s))"
            )
        if len(sess["sessions"]) > 15:
            L.append(f"  … and {len(sess['sessions']) - 15} more session(s)")

    L += ["", "─" * 52, "🤖 Auto-generated by WorkWatch recap"]
    return "\n".join(L)


# ---------- HTML renderer ----------

def _esc(s: str) -> str:
    return (s.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;"))


def build_html_body(data: dict) -> str:
    t = data["totals"]
    win = data["window"]

    verdict_html = "".join(
        f'<div style="margin:6px 0;">{_html_badge(v["level"].upper(), v["level"])}'
        f'<span style="margin-left:10px;">{_esc(v["text"])}</span></div>'
        for v in rule_based_verdict(data)
    )

    glance = (
        f'Commits <b>{t["commits"]}</b> · Unpushed <b>{t["unpushed"]}</b> · '
        f'Dirty repos <b>{t["dirty_repos"]}</b> · Stashes <b>{t["stashes"]}</b> · '
        f'Sessions <b>{t["sessions"]}</b>'
    )

    repo_rows = []
    for r in data["repos"]:
        label, level = _repo_status(r)
        if r.get("skipped"):
            detail = _esc(r["skipped"])
        else:
            d = r["dirty"]
            detail = (f'{len(r["finished"])} commit(s), {r["unpushed"]} unpushed, '
                      f'{d["staged"]}S/{d["modified"]}M/{d["untracked"]}U, '
                      f'{r["stashes"]} stash(es)')
            if r["notes"]:
                detail += f' · <span style="color:#888;">{_esc(", ".join(r["notes"]))}</span>'
        commits = ""
        if not r.get("skipped") and r["finished"]:
            items = "".join(
                f'<li style="color:#444;font-size:13px;">{_esc(subj)} '
                f'<span style="color:#999;">({h})</span></li>'
                for h, ts, subj in r["finished"][:10]
            )
            more = (f'<li style="color:#999;">… {len(r["finished"]) - 10} more</li>'
                    if len(r["finished"]) > 10 else "")
            commits = f'<ul style="margin:6px 0 0 0;padding-left:20px;">{items}{more}</ul>'
        repo_rows.append(
            '<div style="padding:12px 0;border-bottom:1px solid #eef1f5;">'
            f'<div>{_html_badge(label, level)}'
            f'<b style="margin-left:10px;">{_esc(r["name"])}</b>'
            f'<span style="color:#888;"> ({_esc(r["branch"] or "—")})</span></div>'
            f'<div style="color:#555;font-size:13px;margin-top:4px;">{detail}</div>'
            f'{commits}</div>'
        )

    sess = data["sessions"]
    if not sess["available"]:
        sess_html = f'<div style="color:#999;">skipped: {_esc(sess["skipped"] or "")}</div>'
    elif not sess["sessions"]:
        sess_html = '<div style="color:#999;">no sessions in this window</div>'
    else:
        rows = []
        for s in sess["sessions"][:15]:
            title = _esc(s["title"] or s["first_prompt"] or "(untitled session)")
            rows.append(
                '<div style="padding:8px 0;border-bottom:1px solid #f3f5f8;">'
                f'{_html_badge("session", "info")}'
                f'<b style="margin-left:10px;">{_esc(s["project"])}</b>: {title}'
                f'<span style="color:#999;font-size:12px;"> '
                f'({s["prompts"]} prompt(s), {s["tools"]} tool use(s))</span></div>'
            )
        extra = (f'<div style="color:#999;">… {len(sess["sessions"]) - 15} more</div>'
                 if len(sess["sessions"]) > 15 else "")
        sess_html = "".join(rows) + extra

    return f'''<!DOCTYPE html><html><body style="margin:0;padding:24px;background:#f6f8fb;
font-family:-apple-system,BlinkMacSystemFont,'Segoe UI',Helvetica,Arial,sans-serif;color:#1b1b1b;">
<div style="max-width:680px;margin:0 auto;background:#fff;border-radius:12px;
padding:28px;box-shadow:0 1px 3px rgba(0,0,0,0.06);">
  <h2 style="margin:0 0 4px 0;color:#0b5cad;font-size:22px;">⏱ WorkWatch Recap</h2>
  <div style="color:#888;font-size:14px;margin-bottom:20px;">last {win} ·
  {data["since"].strftime("%b %d %H:%M")} → {data["now"].strftime("%b %d %H:%M")}</div>

  <div style="background:#f0f3f7;border-radius:10px;padding:14px 16px;margin-bottom:22px;">
    {verdict_html}
  </div>

  <div style="color:#555;font-size:14px;margin-bottom:24px;">{glance}</div>

  <h3 style="margin:0 0 4px 0;font-size:14px;color:#333;letter-spacing:0.04em;
text-transform:uppercase;">Shipped &amp; in progress</h3>
  {''.join(repo_rows) or '<div style="color:#999;">no usable repos scanned</div>'}

  <h3 style="margin:24px 0 4px 0;font-size:14px;color:#333;letter-spacing:0.04em;
text-transform:uppercase;">Explored</h3>
  {sess_html}

  <p style="color:#aaa;font-size:12px;margin-top:28px;text-align:center;">
    🤖 Auto-generated by WorkWatch recap</p>
</div></body></html>'''


# ---------- local backup ----------

def _write_backup(data: dict, html_body: str) -> Path:
    RECAP_DIR.mkdir(parents=True, exist_ok=True)
    stamp = data["now"].strftime("%Y%m%d-%H%M%S")
    path = RECAP_DIR / f"recap-{stamp}-{data['window']}.html"
    path.write_text(html_body)
    return path


# ---------- orchestrator ----------

def run_recap(window: str, repos: list[str], author: str, to_email: str,
              dry_run: bool = False, include_claude: bool = True) -> tuple[bool, str]:
    """Build the recap and either print (dry-run) or email it.

    Returns (ok, message). For dry_run, message is the plain-text body.
    """
    try:
        data = build_recap(window, repos, author, include_claude)
    except WindowError as exc:
        return False, str(exc)

    plain = build_plain_body(data)
    if dry_run:
        return True, plain

    html = build_html_body(data)
    subject = (f"WorkWatch Recap — last {data['window']} "
               f"({data['totals']['commits']} commits)")

    backup = _write_backup(data, html)
    ok, msg = _send_via_mail_app(to_email, subject, html, plain)
    if not ok:
        return False, f"send failed ({msg}); backup kept at {backup}"
    return True, f"recap for last {data['window']} sent to {to_email} (backup: {backup})"
