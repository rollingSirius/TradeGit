"""Configuration and path resolution.

Layout on disk::

    ~/.tradegit/                 TRADEGIT_HOME
      config.json                storage mode, repo, default account, sync prefs
      repo/                      GitHub clone or local-only git repo
      cache/index.sqlite         derived index, rebuilt when HEAD moves
      cache/state.json           last-seen local/remote SHAs
      imports/                   copies of raw broker files that were imported
"""

from __future__ import annotations

import json
import os
from dataclasses import dataclass, field, asdict
from pathlib import Path
from typing import Any

DEFAULT_HOME = Path.home() / ".tradegit"
CONFIG_NAME = "config.json"


def home() -> Path:
    return Path(os.environ.get("TRADEGIT_HOME", str(DEFAULT_HOME))).expanduser()


@dataclass
class Config:
    """Persisted user configuration."""

    repo_slug: str = ""          # "owner/name" on GitHub, or "local" in local-only mode
    repo_url: str = ""           # remote URL actually used by git
    storage_mode: str = "github" # "github" or "local"
    default_account: str = "main"
    default_currency: str = "USD"
    auto_push: bool = True       # push right after every write
    check_remote_on_write: bool = True
    accounts: dict[str, dict[str, Any]] = field(default_factory=dict)
    schema_version: int = 1

    # --- paths ------------------------------------------------------------
    @property
    def home(self) -> Path:
        return home()

    @property
    def repo_dir(self) -> Path:
        return self.home / "repo"

    @property
    def journal_dir(self) -> Path:
        return self.repo_dir / "journal"

    @property
    def cache_dir(self) -> Path:
        return self.home / "cache"

    @property
    def index_path(self) -> Path:
        return self.cache_dir / "index.sqlite"

    @property
    def state_path(self) -> Path:
        return self.cache_dir / "state.json"

    @property
    def imports_dir(self) -> Path:
        return self.home / "imports"

    @property
    def config_path(self) -> Path:
        return self.home / CONFIG_NAME

    # --- io ---------------------------------------------------------------
    @classmethod
    def load(cls) -> "Config":
        path = home() / CONFIG_NAME
        if not path.exists():
            return cls()
        with path.open(encoding="utf-8") as fh:
            raw = json.load(fh)
        known = {f for f in cls.__dataclass_fields__}
        return cls(**{k: v for k, v in raw.items() if k in known})

    def save(self) -> None:
        self.home.mkdir(parents=True, exist_ok=True)
        tmp = self.config_path.with_suffix(".json.tmp")
        with tmp.open("w", encoding="utf-8") as fh:
            json.dump(asdict(self), fh, indent=2, ensure_ascii=False)
            fh.write("\n")
        tmp.replace(self.config_path)

    def ensure_dirs(self) -> None:
        # 0700: the journal is financial data, and on a shared machine the
        # default umask would leave it world-readable.
        self.home.mkdir(parents=True, exist_ok=True, mode=0o700)
        for d in (self.cache_dir, self.imports_dir):
            d.mkdir(parents=True, exist_ok=True, mode=0o700)
        try:
            self.home.chmod(0o700)
        except OSError:
            pass

    @property
    def initialized(self) -> bool:
        return (self.repo_dir / ".git").exists() or (self.local_only and self.journal_dir.exists())

    @property
    def git_initialized(self) -> bool:
        return (self.repo_dir / ".git").exists()

    @property
    def local_only(self) -> bool:
        return self.storage_mode == "local"


def read_state(cfg: Config) -> dict[str, Any]:
    if not cfg.state_path.exists():
        return {}
    try:
        with cfg.state_path.open(encoding="utf-8") as fh:
            return json.load(fh)
    except (json.JSONDecodeError, OSError):
        return {}


def write_state(cfg: Config, state: dict[str, Any]) -> None:
    cfg.cache_dir.mkdir(parents=True, exist_ok=True)
    tmp = cfg.state_path.with_suffix(".json.tmp")
    with tmp.open("w", encoding="utf-8") as fh:
        json.dump(state, fh, indent=2, ensure_ascii=False)
    tmp.replace(cfg.state_path)
