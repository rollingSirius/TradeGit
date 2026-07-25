"""TradeGit — a GitHub-backed trading journal.

Local-first: the working copy is a git clone of a private GitHub repo.
Journal records are month-partitioned JSONL; a derived SQLite index makes
analysis fast without adding any third-party dependency.
"""

__version__ = "0.1.0"
SCHEMA_VERSION = 1
