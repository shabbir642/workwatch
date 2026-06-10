"""Per-repository git activity collection for `workwatch recap`.

Each repo is probed with a few `git -C <repo>` calls. Failures (missing git,
not a repo, no upstream, detached HEAD) are caught per-repo and surfaced as a
note rather than crashing the whole recap.
"""

import subprocess
from pathlib import Path
from typing import Optional

# Field separator for `git log --pretty`. A literal unit-separator (0x1f) is
# used instead of a comma/pipe because commit subjects routinely contain those.
SEP = "\x1f"

_GIT_TIMEOUT = 15


def _git(repo: str, args: list[str]) -> tuple[bool, str]:
    """Run a git command in `repo`. Returns (ok, stdout-or-stderr)."""
    try:
        result = subprocess.run(
            ["git", "-C", repo, *args],
            capture_output=True,
            text=True,
            timeout=_GIT_TIMEOUT,
        )
    except FileNotFoundError:
        return False, "git not found"
    except subprocess.TimeoutExpired:
        return False, "git timed out"
    except Exception as exc:  # pragma: no cover - defensive
        return False, str(exc)

    if result.returncode != 0:
        return False, result.stderr.strip()
    return True, result.stdout


def _resolve_author(repo: str, author: str) -> str:
    """Return the configured author filter, or this repo's git user.email."""
    if author:
        return author
    ok, out = _git(repo, ["config", "user.email"])
    return out.strip() if ok else ""


def collect_repo(repo: str, since: str, author: str = "") -> dict:
    """Collect git activity for one repo since the given timestamp.

    `since` is any string git's --since understands (e.g. an ISO timestamp).
    Returns a dict with the repo's finished/in-progress state, or a dict with
    a "skipped" note if the path isn't a usable git repo.
    """
    # Resolve the configured path. Bare/relative names (e.g. "jobs-api-es")
    # are resolved against $HOME rather than the CWD, since recap_repos are
    # expected to be home-dir clones and `workwatch recap` may run anywhere.
    repo_path = Path(repo).expanduser()
    if not repo_path.is_absolute():
        repo_path = Path.home() / repo_path
    repo = str(repo_path)
    name = repo_path.name or repo

    base = {
        "repo": repo,
        "name": name,
        "branch": None,
        "finished": [],          # list of (hash, iso_time, subject)
        "unpushed": 0,
        "dirty": {"staged": 0, "modified": 0, "untracked": 0},
        "skipped": None,         # reason string if unusable
        "notes": [],             # non-fatal notes (e.g. "no upstream")
    }

    # Is this a git work tree at all?
    ok, _ = _git(repo, ["rev-parse", "--is-inside-work-tree"])
    if not ok:
        base["skipped"] = "not a git repository"
        return base

    eff_author = _resolve_author(repo, author)

    # Current branch (detached HEAD → "HEAD")
    ok, out = _git(repo, ["rev-parse", "--abbrev-ref", "HEAD"])
    if ok:
        branch = out.strip()
        base["branch"] = branch
        if branch == "HEAD":
            base["notes"].append("detached HEAD")

    # Finished: commits in the window (optionally filtered by author)
    log_args = [
        "log", f"--since={since}",
        f"--pretty=%h{SEP}%cI{SEP}%s",
    ]
    if eff_author:
        log_args.append(f"--author={eff_author}")
    ok, out = _git(repo, log_args)
    if ok:
        for line in out.splitlines():
            parts = line.split(SEP)
            if len(parts) == 3:
                base["finished"].append((parts[0], parts[1], parts[2]))

    # Unpushed: commits ahead of upstream (may have no upstream configured)
    ok, out = _git(repo, ["log", "@{u}..HEAD", "--oneline"])
    if ok:
        base["unpushed"] = len([l for l in out.splitlines() if l.strip()])
    else:
        base["notes"].append("no upstream")

    # In progress: working-tree status
    ok, out = _git(repo, ["status", "--porcelain"])
    if ok:
        staged = modified = untracked = 0
        for line in out.splitlines():
            if not line:
                continue
            x, y = line[0], line[1]
            if line.startswith("??"):
                untracked += 1
            else:
                if x != " ":
                    staged += 1
                if y != " ":
                    modified += 1
        base["dirty"] = {"staged": staged, "modified": modified, "untracked": untracked}

    return base


def collect_repos(repos: list[str], since: str, author: str = "") -> list[dict]:
    """Collect activity for a list of repos. Order preserved."""
    return [collect_repo(r, since, author) for r in repos]


# Untracked-only repos with fewer than this many untracked files are treated
# as clean — a lone stray file (.DS_Store, a scratch note) shouldn't flag a
# repo as dirty. Staged/modified changes always count, regardless of this.
UNTRACKED_FLOOR = 2


def repo_is_dirty(repo: dict) -> bool:
    d = repo.get("dirty", {})
    if d.get("staged") or d.get("modified"):
        return True
    return d.get("untracked", 0) >= UNTRACKED_FLOOR


def repo_dirty_count(repo: dict) -> int:
    d = repo.get("dirty", {})
    return d.get("staged", 0) + d.get("modified", 0) + d.get("untracked", 0)
