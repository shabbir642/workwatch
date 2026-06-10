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

    # Count only substantive sessions (those with tool use) — pure-chat
    # sessions are dropped from the recap, so they shouldn't inflate totals.
    substantive_sessions = [s for s in sessions["sessions"] if s.get("tools")]

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
            "sessions": len(substantive_sessions),
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


def repo_has_updates(repo: dict) -> bool:
    """True if a repo has anything worth showing in the recap body.

    "Worth showing" = activity in the window (commits) or live risk
    (uncommitted changes, unpushed commits, stashes). Idle repos — and
    skipped / non-git ones — return False so they're hidden from the mail
    rather than cluttering it with empty rows.
    """
    if repo.get("skipped"):
        return False
    return bool(
        repo["finished"]
        or repo_is_dirty(repo)
        or repo["unpushed"]
        or repo["stashes"]
    )


def _visible_notes(repo: dict) -> list[str]:
    """Notes worth showing. 'no upstream' is suppressed — nearly every repo
    has it, so it's noise; genuinely useful notes (e.g. detached HEAD) stay.
    """
    return [n for n in repo.get("notes", []) if n != "no upstream"]


def _clean_session_title(s: dict, limit: int = 60) -> str:
    """A short, label-like title for a session.

    Prefers Claude's generated title; falls back to the first prompt with any
    absolute paths stripped, collapsed whitespace, and truncated at a word
    boundary so it reads as a label rather than a dumped prompt.
    """
    raw = (s.get("title") or s.get("first_prompt") or "").strip()
    if not raw:
        return "(untitled session)"
    # Strip absolute paths (e.g. /Users/.../repo) that leak into raw prompts.
    raw = re.sub(r"/\S+", "", raw)
    raw = " ".join(raw.split())
    if not raw:
        return "(untitled session)"
    if len(raw) <= limit:
        return raw
    cut = raw[:limit].rsplit(" ", 1)[0] or raw[:limit]
    return cut + "…"


def _group_sessions(sessions: list[dict]) -> list[dict]:
    """Drop trivial sessions and group the rest by project.

    Trivial = no tool use at all (pure chat like "Hi") — nothing was explored
    or built, so it has no place in a work recap. Returns a list of
    {project, count, titles[up to 3]} sorted by session count desc.
    """
    by_project: dict[str, dict] = {}
    for s in sessions:
        if not s.get("tools"):
            continue
        proj = s.get("project") or "unknown"
        g = by_project.setdefault(proj, {"project": proj, "count": 0, "titles": []})
        g["count"] += 1
        if len(g["titles"]) < 3:
            g["titles"].append(_clean_session_title(s))
    return sorted(by_project.values(), key=lambda g: g["count"], reverse=True)


def _hidden_summary(idle: int, skipped: int) -> str:
    """One-line note of repos hidden from the body, or '' if none.

    Kept so nothing is silently dropped — the counts stay visible even
    though the empty rows don't.
    """
    bits = []
    if idle:
        bits.append(f"{idle} idle (no work in window)")
    if skipped:
        bits.append(f"{skipped} not a git repo")
    return f"{' · '.join(bits)} — hidden" if bits else ""


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
    hidden_idle = 0
    hidden_skipped = 0
    for r in data["repos"]:
        if r.get("skipped"):
            hidden_skipped += 1
            continue
        if not repo_has_updates(r):
            hidden_idle += 1
            continue
        any_repo = True
        label, level = _repo_status(r)
        d = r["dirty"]
        vn = _visible_notes(r)
        notes = f"  [{', '.join(vn)}]" if vn else ""
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
        L.append("  (nothing touched in this window)")
    hidden = _hidden_summary(hidden_idle, hidden_skipped)
    if hidden:
        L.append(f"  ⋯ {hidden}")

    sess = data["sessions"]
    L += ["", "EXPLORED (Claude Code sessions)", "-" * 52]
    if not sess["available"]:
        L.append(f"  (skipped: {sess['skipped']})")
    else:
        groups = _group_sessions(sess["sessions"])
        if not groups:
            L.append("  (no substantive sessions in this window)")
        else:
            for g in groups:
                L.append(f"  🔵 {g['project']} — {g['count']} session(s)")
                for title in g["titles"]:
                    L.append(f"        · {title}")
                if g["count"] > len(g["titles"]):
                    L.append(f"        … +{g['count'] - len(g['titles'])} more")

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
    hidden_idle = 0
    hidden_skipped = 0
    for r in data["repos"]:
        if r.get("skipped"):
            hidden_skipped += 1
            continue
        if not repo_has_updates(r):
            hidden_idle += 1
            continue
        label, level = _repo_status(r)
        d = r["dirty"]
        detail = (f'{len(r["finished"])} commit(s), {r["unpushed"]} unpushed, '
                  f'{d["staged"]}S/{d["modified"]}M/{d["untracked"]}U, '
                  f'{r["stashes"]} stash(es)')
        vn = _visible_notes(r)
        if vn:
            detail += f' · <span style="color:#888;">{_esc(", ".join(vn))}</span>'
        commits = ""
        if r["finished"]:
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

    hidden = _hidden_summary(hidden_idle, hidden_skipped)
    hidden_html = (f'<div style="color:#aaa;font-size:12px;margin-top:8px;">⋯ {_esc(hidden)}</div>'
                   if hidden else "")

    sess = data["sessions"]
    if not sess["available"]:
        sess_html = f'<div style="color:#999;">skipped: {_esc(sess["skipped"] or "")}</div>'
    else:
        groups = _group_sessions(sess["sessions"])
        if not groups:
            sess_html = '<div style="color:#999;">no substantive sessions in this window</div>'
        else:
            rows = []
            for g in groups:
                titles = "".join(
                    f'<li style="color:#444;font-size:13px;">{_esc(tt)}</li>'
                    for tt in g["titles"]
                )
                more = (f'<li style="color:#999;">… +{g["count"] - len(g["titles"])} more</li>'
                        if g["count"] > len(g["titles"]) else "")
                rows.append(
                    '<div style="padding:8px 0;border-bottom:1px solid #f3f5f8;">'
                    f'{_html_badge("session", "info")}'
                    f'<b style="margin-left:10px;">{_esc(g["project"])}</b>'
                    f'<span style="color:#999;font-size:12px;"> — {g["count"]} session(s)</span>'
                    f'<ul style="margin:6px 0 0 0;padding-left:20px;">{titles}{more}</ul></div>'
                )
            sess_html = "".join(rows)

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
  {''.join(repo_rows) or '<div style="color:#999;">nothing touched in this window</div>'}
  {hidden_html}

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
