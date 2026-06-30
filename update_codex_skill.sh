#!/usr/bin/env bash
# 把仓库 agent/ 的工具与 SKILL.md 同步进本地全局 Codex 技能。
# 每次发布新版本后跑一次,保证本地 codex 用到的技能是最新的。
set -euo pipefail
REPO="$(cd "$(dirname "$0")" && pwd)"
SKILL="$HOME/.codex/skills/paper-trading"

mkdir -p "$SKILL/scripts"
cp "$REPO"/agent/cli.py "$REPO"/agent/paper_trading_client.py "$REPO"/agent/review.py "$SKILL/scripts/"

# SKILL.md = 仓库版 + 在前面插入 Codex 专用 Setup 段 + 把命令路径改成技能内 scripts。
python3 - "$REPO/agent/SKILL.md" "$SKILL/SKILL.md" "$REPO" <<'PY'
import sys
src, dst, repo = sys.argv[1], sys.argv[2], sys.argv[3]
text = open(src, encoding="utf-8").read()
setup = f"""## Setup (read first)

The CLI/SDK in this skill's `scripts/` are **standalone HTTP clients** (stdlib only) — they talk to a running paper-trading server over HTTP. Before using them:

1. **Make sure the server is running.** Open the **PaperTrading desktop app** on *this* machine (it auto-starts the server; the port it uses is shown in its window / via `/api/meta`). If instead you have the source repo, run `python3 -m backend.server` from it (default `http://127.0.0.1:8000`). The skill is machine-independent — it just needs a reachable server, do **not** assume any fixed repo path.
2. **Point the tools at it.** Default base url `http://127.0.0.1:8000`; if the app picked another port, pass `--base-url http://127.0.0.1:<port>` or set `PAPER_TRADING_URL` (find the port via the app window or by trying `http://127.0.0.1:8000/api/meta`).
3. **Call tools by their path in this skill**, e.g. `python3 /path/to/paper-trading/scripts/cli.py meta` (replace `/path/to/paper-trading` with this skill's dir). First call should be `meta` to handshake + check version.

> The server needs the full source repo or the desktop app; this skill bundles only the thin client. Never connects to a real broker — all simulated.

## Core Facts"""
text = text.replace("## Core Facts", setup, 1)
text = text.replace("python3 agent/cli.py", "python3 /path/to/paper-trading/scripts/cli.py")
open(dst, "w", encoding="utf-8").write(text)
PY

echo "✅ Codex 技能已同步: $SKILL"
echo "   scripts: $(ls "$SKILL/scripts" | tr '\n' ' ')"
