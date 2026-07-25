"""GitHub connection and sync.

Auth uses whatever the host tool already has, in this order:

1. ``gh`` CLI (Claude Code and Codex environments usually ship it logged in)
2. ``GITHUB_TOKEN`` / ``GH_TOKEN`` in the environment
3. an existing git credential helper (plain ``git`` over https/ssh)

No token is ever written into the config file or the journal repo.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
from pathlib import Path
from typing import Any

from .config import Config, read_state, write_state

GITATTRIBUTES = """# Append-only journal files: union merge resolves concurrent appends
# from two machines without a manual conflict resolution step.
*.jsonl merge=union
manifest.json -merge
"""


class SyncError(RuntimeError):
    pass


def run(args: list[str], cwd: Path | None = None, *, check: bool = True,
        env: dict[str, str] | None = None) -> subprocess.CompletedProcess:
    proc = subprocess.run(
        args, cwd=str(cwd) if cwd else None, capture_output=True, text=True,
        env={**os.environ, **(env or {})},
    )
    if check and proc.returncode != 0:
        raise SyncError(
            f"command failed: {' '.join(args)}\n{proc.stderr.strip() or proc.stdout.strip()}")
    return proc


def git(cfg: Config, *args: str, check: bool = True) -> subprocess.CompletedProcess:
    return run(["git", *args], cwd=cfg.repo_dir, check=check)


# ---------------------------------------------------------------------------
# auth / account discovery
# ---------------------------------------------------------------------------

def gh_available() -> bool:
    return shutil.which("gh") is not None


def gh_authenticated() -> bool:
    if not gh_available():
        return False
    return run(["gh", "auth", "status"], check=False).returncode == 0


def token() -> str | None:
    return os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or None


def github_user() -> str | None:
    """Login of the connected GitHub account, or None if not connected."""
    if gh_authenticated():
        proc = run(["gh", "api", "user", "--jq", ".login"], check=False)
        if proc.returncode == 0 and proc.stdout.strip():
            return proc.stdout.strip()
    tok = token()
    if tok and shutil.which("curl"):
        proc = run(["curl", "-sS", "-H", f"Authorization: Bearer {tok}",
                    "-H", "Accept: application/vnd.github+json",
                    "https://api.github.com/user"], check=False)
        if proc.returncode == 0:
            try:
                return json.loads(proc.stdout).get("login")
            except json.JSONDecodeError:
                return None
    return None


def auth_status() -> dict[str, Any]:
    user = github_user()
    return {
        "gh_cli": gh_available(),
        "gh_authenticated": gh_authenticated(),
        "token_env": bool(token()),
        "user": user,
        "connected": bool(user),
    }


def remote_url(slug: str) -> str:
    tok = token()
    if tok and not gh_authenticated():
        return f"https://x-access-token:{tok}@github.com/{slug}.git"
    return f"https://github.com/{slug}.git"


# ---------------------------------------------------------------------------
# repo creation / clone
# ---------------------------------------------------------------------------

def repo_exists(slug: str) -> bool:
    if gh_authenticated():
        return run(["gh", "repo", "view", slug], check=False).returncode == 0
    proc = run(["git", "ls-remote", remote_url(slug)], check=False)
    return proc.returncode == 0


def create_private_repo(slug: str, description: str) -> None:
    if not gh_authenticated():
        raise SyncError(
            "Creating a repo needs the gh CLI logged in. Run `gh auth login`, "
            f"or create https://github.com/{slug} manually as a PRIVATE repo and rerun init.")
    run(["gh", "repo", "create", slug, "--private", "--description", description])


def clone(slug: str, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    if dest.exists() and any(dest.iterdir()):
        raise SyncError(f"{dest} already exists and is not empty")
    run(["git", "clone", remote_url(slug), str(dest)])


def visibility(slug: str) -> str | None:
    if not gh_authenticated():
        return None
    proc = run(["gh", "repo", "view", slug, "--json", "visibility", "--jq", ".visibility"],
               check=False)
    return proc.stdout.strip().lower() if proc.returncode == 0 else None


def ensure_identity(cfg: Config) -> None:
    """Give commits an author even on a machine with no global git identity."""
    if not git(cfg, "config", "user.email", check=False).stdout.strip():
        git(cfg, "config", "user.email", "tradegit@localhost")
    if not git(cfg, "config", "user.name", check=False).stdout.strip():
        git(cfg, "config", "user.name", "TradeGit")


# ---------------------------------------------------------------------------
# sync state
# ---------------------------------------------------------------------------

def local_head(cfg: Config) -> str | None:
    proc = git(cfg, "rev-parse", "HEAD", check=False)
    return proc.stdout.strip() or None if proc.returncode == 0 else None


def remote_head(cfg: Config) -> str | None:
    """Remote HEAD without fetching objects — cheap enough for every write."""
    branch = current_branch(cfg) or "HEAD"
    proc = git(cfg, "ls-remote", "origin", branch, check=False)
    if proc.returncode != 0 or not proc.stdout.strip():
        return None
    return proc.stdout.split()[0]


def current_branch(cfg: Config) -> str | None:
    proc = git(cfg, "rev-parse", "--abbrev-ref", "HEAD", check=False)
    name = proc.stdout.strip()
    return name if name and name != "HEAD" else None


def dirty(cfg: Config) -> list[str]:
    proc = git(cfg, "status", "--porcelain", check=False)
    return [line for line in proc.stdout.splitlines() if line.strip()]


def check(cfg: Config) -> dict[str, Any]:
    """Is the local clone in sync with the private repo?

    Called before any write, and by ``tradegit check`` before analysis, so a
    trade logged from another machine is never silently missed.
    """
    if not cfg.initialized:
        return {"initialized": False, "in_sync": False,
                "message": "not initialized — run `tradegit init`"}
    local = local_head(cfg)
    remote = remote_head(cfg)
    state = read_state(cfg)
    result = {
        "initialized": True,
        "repo": cfg.repo_slug,
        "branch": current_branch(cfg),
        "local_head": local,
        "remote_head": remote,
        "remote_reachable": remote is not None,
        "uncommitted": dirty(cfg),
        "last_sync": state.get("last_sync"),
    }
    if remote is None:
        result["in_sync"] = False
        result["message"] = "remote unreachable — working offline; changes stay local"
    elif local == remote:
        result["in_sync"] = True
        result["message"] = "up to date with the private repo"
    else:
        result["in_sync"] = False
        result["message"] = "the private repo has changes not in the local copy — pull first"
    return result


# ---------------------------------------------------------------------------
# pull / push
# ---------------------------------------------------------------------------

def pull(cfg: Config) -> dict[str, Any]:
    if not cfg.initialized:
        raise SyncError("not initialized — run `tradegit init`")
    before = local_head(cfg)
    proc = git(cfg, "pull", "--rebase", "--autostash", "origin",
               current_branch(cfg) or "main", check=False)
    if proc.returncode != 0:
        conflicts = _resolve_conflicts(cfg)
        if conflicts is None:
            raise SyncError(
                "pull failed and could not be auto-resolved:\n"
                + (proc.stderr or proc.stdout)
                + f"\nResolve by hand in {cfg.repo_dir}")
    after = local_head(cfg)
    return {"changed": before != after, "before": before, "after": after,
            "output": (proc.stdout + proc.stderr).strip()}


def _resolve_conflicts(cfg: Config) -> bool | None:
    """Auto-resolve the only conflicts this layout can produce.

    ``*.jsonl`` uses git's union merge driver so appends never conflict.
    ``manifest.json`` is fully derived, so a conflict there is resolved by
    regenerating it. Anything else is handed back to the user.
    """
    proc = git(cfg, "diff", "--name-only", "--diff-filter=U", check=False)
    files = [f for f in proc.stdout.splitlines() if f.strip()]
    if not files:
        return None
    if any(not (f.endswith(".jsonl") or f.endswith("manifest.json")) for f in files):
        return None
    from . import store  # local import: store imports config, not sync
    for name in files:
        if name.endswith("manifest.json"):
            git(cfg, "checkout", "--ours", "--", name, check=False)
        git(cfg, "add", "--", name, check=False)
    store.write_manifest(cfg)
    git(cfg, "add", "manifest.json", check=False)
    cont = git(cfg, "-c", "core.editor=true", "rebase", "--continue", check=False)
    if cont.returncode != 0:
        git(cfg, "rebase", "--abort", check=False)
        return None
    return True


def commit_and_push(cfg: Config, message: str, *, push: bool | None = None) -> dict[str, Any]:
    if not cfg.initialized:
        raise SyncError("not initialized — run `tradegit init`")
    ensure_identity(cfg)
    git(cfg, "add", "-A")
    staged = git(cfg, "diff", "--cached", "--name-only", check=False).stdout.split()
    result: dict[str, Any] = {}
    if staged:
        git(cfg, "commit", "-m", message)
        result = {"committed": True, "commit": local_head(cfg), "files": staged}
    else:
        result = {"committed": False, "message": "nothing to commit"}

    should_push = cfg.auto_push if push is None else push
    if not should_push:
        result["pushed"] = False
        result["message"] = "committed locally (auto_push disabled)"
        return result

    # Nothing new to commit does not mean nothing to push: an earlier
    # --no-push write, or a failed push, leaves local commits behind.
    remote = remote_head(cfg)
    if remote is not None and local_head(cfg) == remote:
        result["pushed"] = False
        result.setdefault("message", "already up to date")
        return result

    branch = current_branch(cfg) or "main"
    proc = git(cfg, "push", "origin", branch, check=False)
    if proc.returncode != 0:
        pull(cfg)
        proc = git(cfg, "push", "origin", branch, check=False)
    result["pushed"] = proc.returncode == 0
    if not result["pushed"]:
        result["message"] = ("committed locally but push failed — run `tradegit sync` "
                             "when back online:\n" + (proc.stderr or proc.stdout).strip())
    else:
        state = read_state(cfg)
        state.update({"last_sync": _now(), "last_pushed_head": local_head(cfg)})
        write_state(cfg, state)
    return result


def _now() -> str:
    from .schema import now_iso
    return now_iso()


def ensure_fresh(cfg: Config) -> dict[str, Any]:
    """Pull if the remote moved. Safe to call before every write."""
    status = check(cfg)
    if status.get("initialized") and status.get("remote_reachable") and not status["in_sync"]:
        status["pull"] = pull(cfg)
        status["in_sync"] = True
    return status
