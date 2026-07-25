"""Files written into the private journal repo when it is first created."""

from __future__ import annotations

from pathlib import Path

from . import SCHEMA_VERSION
from .sync import GITATTRIBUTES

README = """# 交易日志 / Trading Journal

私有仓库，由 [TradeGit](https://github.com/) skill 维护。**请保持 private。**

## 目录结构

```
journal/<YYYY>/<YYYY-MM>.jsonl   每月一个分片，一行一条记录（append-only）
manifest.json                    记录数、时间范围、账户列表（自动生成）
schema/trade.schema.json         记录结构定义
```

## 为什么是 JSONL

每笔交易占一行，追加写只产生一行 diff：git 历史干净，两台机器同时记录
也能靠 `.gitattributes` 里的 `merge=union` 自动合并。

## 修改历史记录

记录是只追加的。更正时写入一条新记录并带上 `supersedes: <原 id>`，
删除时写入 `{"kind": "void", "voids": "<原 id>"}`。原始记录留在 git 历史里。

## 常用命令

```
tradegit status              # 本地与远端是否一致
tradegit log --symbol AAPL --side BUY --qty 100 --price 213.45 --why "..."
tradegit import --file ~/Downloads/statement.csv --broker ibkr
tradegit analyze --since 90d
tradegit roundtrips --sort pnl --limit 10
```
"""

GITIGNORE = """.DS_Store
*.tmp
*.swp
.cache/
"""

SCHEMA_JSON = """{
  "$schema": "https://json-schema.org/draft/2020-12/schema",
  "title": "TradeGit journal record",
  "type": "object",
  "required": ["id", "schema_version", "kind", "ts", "account", "symbol"],
  "properties": {
    "id": {"type": "string"},
    "schema_version": {"type": "integer", "const": %(version)d},
    "kind": {"enum": ["trade", "cash", "void"]},
    "ts": {"type": "string", "description": "UTC, YYYY-MM-DDTHH:MM:SSZ"},
    "account": {"type": "string"},
    "broker": {"type": "string"},
    "symbol": {"type": "string", "description": "ticker, or OSI symbol for options"},
    "asset_class": {"enum": ["STK", "ETF", "OPT", "FUT", "FX", "CRYPTO", "BOND", "FUND", "OTHER"]},
    "currency": {"type": "string"},
    "side": {"enum": ["BUY", "SELL", "SHORT", "COVER"]},
    "quantity": {"type": "number", "exclusiveMinimum": 0},
    "price": {"type": "number", "minimum": 0},
    "multiplier": {"type": "number", "default": 1},
    "fees": {"type": "object", "additionalProperties": {"type": "number"}},
    "fees_total": {"type": "number"},
    "gross_amount": {"type": "number"},
    "net_amount": {"type": "number", "description": "cash impact; negative when buying"},
    "signed_quantity": {"type": "number", "description": "+ increases position, - decreases"},
    "option": {
      "type": "object",
      "properties": {
        "underlying": {"type": "string"},
        "expiry": {"type": "string"},
        "strike": {"type": "number"},
        "right": {"enum": ["C", "P"]}
      }
    },
    "cash_type": {"enum": ["DIVIDEND", "INTEREST", "FEE", "TAX", "DEPOSIT",
                            "WITHDRAWAL", "REBATE", "ADJUSTMENT", "OTHER"]},
    "amount": {"type": "number"},
    "thesis": {"type": "string", "description": "交易理由 — why this trade, at entry"},
    "strategy": {"type": "string"},
    "setup": {"type": "string"},
    "exit_plan": {"type": "string"},
    "review": {"type": "string", "description": "written after the fact"},
    "mistake": {"type": "string"},
    "emotions": {"type": "string"},
    "market_context": {"type": "string"},
    "conviction": {"type": "integer", "minimum": 1, "maximum": 5},
    "tags": {"type": "array", "items": {"type": "string"}},
    "risk": {
      "type": "object",
      "properties": {
        "stop": {"type": "number"},
        "target": {"type": "number"},
        "risk_amount": {"type": "number"},
        "planned_r": {"type": "number"}
      }
    },
    "source": {
      "type": "object",
      "properties": {
        "kind": {"enum": ["manual", "import"]},
        "importer": {"type": "string"},
        "external_id": {"type": "string"},
        "file": {"type": "string"}
      }
    },
    "supersedes": {"type": "string"},
    "voids": {"type": "string"},
    "dedup_key": {"type": "string"},
    "created_at": {"type": "string"},
    "updated_at": {"type": "string"},
    "hash": {"type": "string"}
  }
}
""" % {"version": SCHEMA_VERSION}


def write_repo_scaffold(repo_dir: Path) -> list[str]:
    """Create the initial file set. Existing files are never overwritten."""
    files = {
        "README.md": README,
        ".gitignore": GITIGNORE,
        ".gitattributes": GITATTRIBUTES,
        "schema/trade.schema.json": SCHEMA_JSON,
        "journal/.gitkeep": "",
    }
    written = []
    for name, content in files.items():
        path = repo_dir / name
        if path.exists():
            continue
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
        written.append(name)
    return written
