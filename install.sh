#!/usr/bin/env bash
# TradeGit installer — registers the skill with Claude and/or Codex.
#
#   ./install.sh                 install everywhere it detects a host tool
#   ./install.sh --claude        Claude Code / Claude Desktop / claude.ai skills dir
#   ./install.sh --codex         Codex (AGENTS.md + custom prompt)
#   ./install.sh --project DIR   install into DIR/.claude/skills (per-project)
#   ./install.sh --copy          copy files instead of symlinking
#   ./install.sh --uninstall     remove everything this script installed
#
# Idempotent. Never touches your journal data in ~/.tradegit.

set -euo pipefail

SRC="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
NAME="tradegit"
BEGIN="<!-- BEGIN TRADEGIT (managed by install.sh) -->"
END="<!-- END TRADEGIT -->"

CLAUDE_DIR="${CLAUDE_SKILLS_DIR:-$HOME/.claude/skills}"
CODEX_DIR="${CODEX_HOME:-$HOME/.codex}"
BIN_DIR="${TRADEGIT_BIN_DIR:-$HOME/.local/bin}"

do_claude=0; do_codex=0; do_bin=1; uninstall=0; copy=0; project=""
explicit=0

while [ $# -gt 0 ]; do
  case "$1" in
    --claude)    do_claude=1; explicit=1 ;;
    --codex)     do_codex=1; explicit=1 ;;
    --project)   project="${2:?--project needs a directory}"; explicit=1; shift ;;
    --copy)      copy=1 ;;
    --no-bin)    do_bin=0 ;;
    --uninstall) uninstall=1 ;;
    -h|--help)   sed -n '2,12p' "$0" | sed 's/^# \{0,1\}//'; exit 0 ;;
    *) echo "unknown option: $1" >&2; exit 2 ;;
  esac
  shift
done

say()  { printf '%s\n' "$*"; }
ok()   { printf '  \033[32m✓\033[0m %s\n' "$*"; }
warn() { printf '  \033[33m!\033[0m %s\n' "$*"; }

link_or_copy() {  # src dst
  local src="$1" dst="$2"
  rm -rf "$dst"
  mkdir -p "$(dirname "$dst")"
  if [ "$copy" = 1 ]; then
    cp -R "$src" "$dst"
  else
    ln -s "$src" "$dst" 2>/dev/null || cp -R "$src" "$dst"
  fi
}

strip_block() {  # file
  local file="$1"
  [ -f "$file" ] || return 0
  awk -v b="$BEGIN" -v e="$END" '
    index($0, b) { skip = 1 } !skip { print } index($0, e) { skip = 0 }
  ' "$file" > "$file.tmp" && mv "$file.tmp" "$file"
}

# ---------------------------------------------------------------- detection --
if [ "$explicit" = 0 ]; then
  [ -d "$HOME/.claude" ] && do_claude=1
  [ -d "$CODEX_DIR" ]    && do_codex=1
  if [ "$do_claude" = 0 ] && [ "$do_codex" = 0 ]; then
    warn "没有检测到 ~/.claude 或 ~/.codex，默认按 Claude 安装。"
    do_claude=1
  fi
fi

# ---------------------------------------------------------------- uninstall --
if [ "$uninstall" = 1 ]; then
  say "卸载 TradeGit skill（不会删除 ~/.tradegit 里的交易记录）："
  rm -rf "$CLAUDE_DIR/$NAME" && ok "removed $CLAUDE_DIR/$NAME"
  [ -n "$project" ] && rm -rf "$project/.claude/skills/$NAME" \
    && ok "removed $project/.claude/skills/$NAME"
  rm -f "$CODEX_DIR/prompts/$NAME.md" && ok "removed $CODEX_DIR/prompts/$NAME.md"
  strip_block "$CODEX_DIR/AGENTS.md" && ok "cleaned $CODEX_DIR/AGENTS.md"
  rm -f "$BIN_DIR/$NAME" && ok "removed $BIN_DIR/$NAME"
  say ""
  say "交易记录仍在 ~/.tradegit 和你的 GitHub 私有仓库里，未受影响。"
  exit 0
fi

# ---------------------------------------------------------------- preflight --
say "TradeGit 安装"
say "  源目录: $SRC"
command -v python3 >/dev/null 2>&1 || { echo "需要 python3" >&2; exit 1; }
command -v git     >/dev/null 2>&1 || { echo "需要 git" >&2; exit 1; }
python3 - <<'PY' || { echo "需要 Python 3.9+" >&2; exit 1; }
import sys
raise SystemExit(0 if sys.version_info >= (3, 9) else 1)
PY

# ------------------------------------------------------------------ install --
if [ "$do_claude" = 1 ]; then
  link_or_copy "$SRC" "$CLAUDE_DIR/$NAME"
  ok "Claude skill → $CLAUDE_DIR/$NAME"
fi

if [ -n "$project" ]; then
  link_or_copy "$SRC" "$project/.claude/skills/$NAME"
  ok "项目内 skill → $project/.claude/skills/$NAME"
fi

if [ "$do_codex" = 1 ]; then
  mkdir -p "$CODEX_DIR/prompts"
  # Codex custom prompt: /tradegit
  cat > "$CODEX_DIR/prompts/$NAME.md" <<EOF
Use the TradeGit trading journal at $SRC.
Read $SRC/AGENTS.md first, then carry out the user's request:

\$ARGUMENTS
EOF
  ok "Codex prompt → $CODEX_DIR/prompts/$NAME.md  (用 /tradegit 调用)"

  strip_block "$CODEX_DIR/AGENTS.md"
  {
    [ -s "$CODEX_DIR/AGENTS.md" ] && printf '\n'
    printf '%s\n' "$BEGIN"
    printf '## 交易日志 (TradeGit)\n\n'
    printf '用户要求**记录交易**、**导入券商流水（IBKR / 嘉信 / Schwab）**、\n'
    printf '**分析或复盘历史盈亏**、**查看持仓**时，使用 TradeGit。\n\n'
    printf '先读 `%s/AGENTS.md` 了解完整流程，CLI 入口：\n\n' "$SRC"
    printf '```bash\n%s/scripts/tradegit <command> --json\n```\n\n' "$SRC"
    printf '所有命令都支持 `--json`，请始终加上并解析结果。\n'
    printf '%s\n' "$END"
  } >> "$CODEX_DIR/AGENTS.md"
  ok "Codex 指令 → $CODEX_DIR/AGENTS.md"
fi

if [ "$do_bin" = 1 ]; then
  mkdir -p "$BIN_DIR"
  ln -sf "$SRC/scripts/tradegit" "$BIN_DIR/$NAME"
  ok "CLI → $BIN_DIR/$NAME"
  case ":$PATH:" in
    *":$BIN_DIR:"*) ;;
    *) warn "$BIN_DIR 不在 PATH 上。加一行到 shell 配置："
       say  "      export PATH=\"\$PATH:$BIN_DIR\"" ;;
  esac
fi

# ------------------------------------------------------------------ verify --
say ""
say "自检："
"$SRC/scripts/tradegit" doctor || true

say ""
say "下一步："
say "  1. 连接 GitHub：  gh auth login        （或 export GITHUB_TOKEN=...）"
say "  2. 创建私有仓库：  tradegit init"
say "  3. 在 Claude / Codex 里直接说「记一笔交易」「导入我的嘉信流水」「复盘上季度」"
