#!/bin/bash
# Claude Desktop / MCP host 啟動用 wrapper。
#
# 為什麼需要它：GUI 啟動的 Claude Desktop 不會 source 你的 shell profile，
# 所以拿不到 ~/.secrets 裡的環境變數。這個 wrapper 先載入 ~/.secrets，
# 再用同目錄的 .venv 啟動 server.py —— 帳密全部走 env，設定檔裡零密碼。
#
# server.py 已支援別名：IMAP_SERVER/IMAP_USERNAME/IMAP_PASSWORD（或 IMAP_HOST/USER/PASS）
# 與對應的 SMTP_*，所以 ~/.secrets 不必改名。
set -euo pipefail

# 載入機密環境變數（自動 export 期間 source）
set -a
[ -f "$HOME/.secrets" ] && source "$HOME/.secrets"
set +a

DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$DIR/.venv/bin/python" "$DIR/server.py"
