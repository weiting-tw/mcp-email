# mcp-email — 本機 IMAP / SMTP 信箱 MCP server

[![tests](https://github.com/weiting-tw/mcp-email/actions/workflows/test.yml/badge.svg)](https://github.com/weiting-tw/mcp-email/actions/workflows/test.yml)

純 Python 單模組實作（`mcp_email.py`），把任意支援 IMAP / SMTP 的標準信箱（Gmail / Outlook / Yahoo / iCloud / Zoho / 公司 Exchange / 自架 mail server …）包成 MCP tools 給 Claude / Claude Code / Cowork / 任何 MCP host 用。

## 能做什麼（8 個 tools）

| Tool | 用途 |
|---|---|
| `email_configure` | Runtime 動態切換 SMTP / IMAP 帳密、port、TLS |
| `email_test_connection` | 一鍵測 SMTP + IMAP 是否能登入 |
| `email_send` | 寄信：HTML + 純文字、to/cc/bcc、檔案/Base64 附件、Reply-To、自訂 headers、retry |
| `email_list_folders` | IMAP 列出所有 mailbox 名稱 |
| `email_list_messages` | 列出 folder 內訊息 header（支援 IMAP search syntax） |
| `email_get_message` | 抓單封信完整內容（body text/html、附件 metadata、選擇是否標 SEEN） |
| `email_mark` | 加/移除 IMAP flag（`\Seen` / `\Flagged` 等） |
| `email_delete` | 標記 `\Deleted` 並 expunge |

對應使用者需求：
- 📤 寄信 HTML + 純文字 ✅（multipart/alternative，純文字 fallback 自動）
- 👥 多收件人 to / cc / bcc ✅（bcc **不會** 出現在 header）
- 📎 附件 — 本機檔案路徑 + Base64 ✅（text/* 自動 charset=utf-8）
- 🔧 Runtime 動態設定 ✅（`email_configure` tool，任一欄位可單獨更新）
- 🔍 連線測試 ✅（SMTP `NOOP` + IMAP `NOOP` 各別測）
- ⚡ 高效能 ✅（timeout 可調、SMTP send 失敗 exponential backoff 重試）

## 安裝

### 最快：uvx / pip（從 PyPI）

已發佈到 PyPI，用 [uv](https://docs.astral.sh/uv/) 免手動建環境，首次執行自動抓套件：

```bash
uvx mcp-email          # 直接跑（uv 會自動安裝到隔離環境）
# 或
pipx install mcp-email
pip install mcp-email   # 裝進現有環境，提供 `mcp-email` 指令
```

MCP host 設定（Claude Desktop / Code / Cursor / Cline … 通用），帳密走 env：

```json
{
  "mcpServers": {
    "email": {
      "command": "uvx",
      "args": ["mcp-email"],
      "env": {
        "IMAP_SERVER": "imap.gmail.com", "IMAP_PORT": "993",
        "SMTP_SERVER": "smtp.gmail.com", "SMTP_PORT": "465", "SMTP_USE_SSL": "true",
        "EMAIL_USER": "you@gmail.com", "EMAIL_PASS": "<app password>"
      }
    }
  }
}
```

> Claude Code 使用者：repo 內附 `.mcp.json`，用 `claude` 開這個資料夾會自動提示啟用。

### 從原始碼（開發 / 自己改）

建議用獨立的 virtualenv，避免污染系統 Python（也不要把 `.venv/` 提交到 git）：

```bash
cd ~/Documents/workspace/mcp-email

# 建立並啟用虛擬環境（請用你實際的 Python，例如 Homebrew 的 python3）
python3 -m venv .venv
source .venv/bin/activate          # Windows: .venv\Scripts\activate

# 執行時依賴
pip install -r requirements.txt

# 開發 / 測試依賴（含 aiosmtpd 假 SMTP）
pip install -r requirements-dev.txt

# 跑端到端測試（in-process 假 SMTP + 假 IMAP，驗 19 個情境）
python test_e2e.py
# 預期：=== 19/19 passed ===
```

> ⚠️ macOS 注意：別用 `/usr/bin/python3`（CommandLineTools 內建的 stub）建 venv，
> 它可能跳出 `xcode-select` 安裝提示。請改用 Homebrew 的 `python3`
> （`/opt/homebrew/bin/python3`）或 pyenv。`.venv/` 已列入 `.gitignore`，
> 不會、也不該進版控；clone 後請各自重建。

## 註冊到 Claude Desktop / Cowork / Code

打開 Claude 的 MCP 設定檔（通常在 `~/Library/Application Support/Claude/claude_desktop_config.json` 或對應路徑），加上：

```json
{
  "mcpServers": {
    "email": {
      "command": "python3",
      "args": ["/Users/weiting/Documents/workspace/mcp-email/mcp_email.py"],
      "env": {
        "SMTP_HOST": "smtp.gmail.com",
        "SMTP_PORT": "587",
        "SMTP_USER": "you@gmail.com",
        "SMTP_PASS": "<gmail app password>",
        "SMTP_USE_TLS": "true",
        "IMAP_HOST": "imap.gmail.com",
        "IMAP_PORT": "993",
        "IMAP_USER": "you@gmail.com",
        "IMAP_PASS": "<gmail app password>",
        "IMAP_USE_SSL": "true",
        "EMAIL_FROM": "Your Name <you@gmail.com>",
        "EMAIL_TIMEOUT_SEC": "30",
        "EMAIL_RETRY_MAX": "3"
      }
    }
  }
}
```

重啟 Claude 後就會看到 `email_*` 8 個 tool 出現。

不設環境變數也可以，啟動後第一次用之前先呼叫 `email_configure` 設定帳密（runtime 設定不會落地，重啟會清空）。

### 用環境變數 + wrapper（推薦：設定檔零密碼）

如果你的帳密放在 `~/.secrets`（會被 shell source 的機密檔），可以用附帶的 `run_server.sh`：
它會先 `source ~/.secrets` 再啟動 server，所以連 GUI 啟動的 Claude Desktop 也吃得到 env、
而設定檔裡完全不用寫密碼。設定改成：

```json
{
  "mcpServers": {
    "email": { "command": "/絕對路徑/mcp-email/run_server.sh" }
  }
}
```

**環境變數別名**：server 同時接受兩種命名，`~/.secrets` 不必改名 —

| 標準名 | 也接受的別名 |
|---|---|
| `SMTP_HOST` / `IMAP_HOST` | `SMTP_SERVER` / `IMAP_SERVER` |
| `SMTP_USER` / `IMAP_USER` | `SMTP_USERNAME` / `IMAP_USERNAME` |
| `SMTP_PASS` / `IMAP_PASS` | `SMTP_PASSWORD` / `IMAP_PASSWORD` |

**帳密只需設一組**：SMTP 與 IMAP 通常是同一個信箱帳號，所以帳密不必重複設 —
- 用共用的 `EMAIL_USER` / `EMAIL_PASS`（或 `MAIL_*`），SMTP / IMAP 兩邊都吃；
- 或只設一邊（例如只有 `IMAP_USERNAME` / `IMAP_PASSWORD`），另一邊會自動沿用同一組帳密。
- 只有 `host` / `port` 因協定不同需各自設（如 `smtp.x.com:587` vs `imap.x.com:993`；自架常是同 host 不同 port）。

**只設一個協定也可以**：IMAP 與 SMTP 功能完全獨立 —— 只設 IMAP 就能用全部讀信工具
（`list_folders` / `list_messages` / `get_message` / `mark` / `delete`）；
只設 SMTP 就能用 `email_send`。要兩種都用才需要兩邊的 host 都設。

## 常見信箱設定

| 服務 | SMTP host | port | TLS | IMAP host | port | SSL | 備註 |
|---|---|---|---|---|---|---|---|
| Gmail | `smtp.gmail.com` | 587 | TLS | `imap.gmail.com` | 993 | SSL | 需 [App Password](https://myaccount.google.com/apppasswords) |
| Outlook / Microsoft 365 | `smtp.office365.com` | 587 | TLS | `outlook.office365.com` | 993 | SSL | 帳號需開 SMTP / IMAP 開關 |
| Outlook.com / Hotmail | `smtp-mail.outlook.com` | 587 | TLS | `outlook.office365.com` | 993 | SSL | 同上 |
| Yahoo Mail | `smtp.mail.yahoo.com` | 587 | TLS | `imap.mail.yahoo.com` | 993 | SSL | 需開 App Password |
| iCloud Mail | `smtp.mail.me.com` | 587 | TLS | `imap.mail.me.com` | 993 | SSL | 需開 App-specific Password |
| Zoho Mail | `smtp.zoho.com` | 587 | TLS | `imap.zoho.com` | 993 | SSL | |
| 自架（Postfix + Dovecot） | 視設定 | 587/465 | TLS / SSL | 視設定 | 143/993 | STARTTLS / SSL | |

**安全提醒**：別把純密碼塞進 config 檔案明文。建議：
- Gmail / Yahoo / iCloud / Outlook：用 App Password（兩階段驗證 → 應用程式密碼）
- 公司 Exchange：問 IT 拿 SMTP/IMAP credential，或用 OAuth（這個版本暫未支援 OAuth flow）
- 密碼存 macOS Keychain 並用 `security find-generic-password -w` 動態注入

**TLS / SSL 憑證驗證**：預設兩邊都 **驗** 憑證（`verify_cert: true`）。自架信箱或公司內部用自簽憑證會在連線時 `CERTIFICATE_VERIFY_FAILED` 報錯。要關掉驗證：

```jsonc
// 環境變數
"SMTP_VERIFY_CERT": "false",
"IMAP_VERIFY_CERT": "false"
```

或 runtime 用 `email_configure`：
```json
{ "smtp": { "verify_cert": false }, "imap": { "verify_cert": false } }
```

關掉後會喪失 MITM 防護，**只在自簽 / 測試環境用**，連 Gmail / Outlook 之類公網信箱絕對不要關。

**附件路徑白名單**：因為 `email_send` 的 `path` 附件受模型/host 控制，預設不設限時，
理論上可被誘導把任意本機檔案（如 `~/.ssh/id_rsa`）夾帶寄出。若要防護，設定允許目錄：

```jsonc
// 環境變數（os.pathsep 分隔，macOS/Linux 用 : ）
"EMAIL_ATTACHMENT_DIRS": "/Users/me/Documents:/Users/me/Pictures"
```

或 runtime 用 `email_configure`：

```json
{ "attachment_allowed_dirs": ["/Users/me/Documents", "/Users/me/Pictures"] }
```

設定後，`path` 附件只能來自這些目錄底下（會解析 symlink 防繞過），名單外一律 `PermissionError`。
空白（預設）= 不限制。`content_base64` 附件不受此限（內容由呼叫端直接提供）。

## Tool 呼叫範例

### email_configure

```json
{
  "smtp": {
    "host": "smtp.gmail.com",
    "port": 587,
    "username": "you@gmail.com",
    "password": "xxxx xxxx xxxx xxxx",
    "use_tls": true
  },
  "imap": {
    "host": "imap.gmail.com",
    "port": 993,
    "username": "you@gmail.com",
    "password": "xxxx xxxx xxxx xxxx",
    "use_ssl": true
  },
  "email_from": "Your Name <you@gmail.com>",
  "retry_max": 3
}
```

### email_send

```json
{
  "to": ["alice@example.com", "bob@example.com"],
  "cc": "carol@example.com, dave@example.com",
  "bcc": ["eve@example.com"],
  "subject": "週報 2026-06-12",
  "text": "純文字版本內容",
  "html": "<h1>週報</h1><p>本週進度：…</p>",
  "reply_to": "support@example.com",
  "headers": {
    "X-Report-Week": "2026-W24"
  },
  "attachments": [
    {"path": "/Users/me/Documents/report.pdf"},
    {"path": "/Users/me/Pictures/chart.png", "filename": "週報圖表.png"},
    {
      "content_base64": "SGVsbG8gV29ybGQh",
      "filename": "data.txt",
      "mime_type": "text/plain"
    }
  ]
}
```

### email_test_connection

```json
{ "smtp": true, "imap": true }
```

回傳：
```json
{
  "smtp": {"ok": true, "host": "smtp.gmail.com", "port": 587},
  "imap": {"ok": true, "host": "imap.gmail.com", "port": 993}
}
```

### email_list_messages

```json
{
  "folder": "INBOX",
  "limit": 20,
  "search": "UNSEEN"
}
```

IMAP search syntax 常用：
- `ALL`、`UNSEEN`、`SEEN`、`FLAGGED`、`UNFLAGGED`、`ANSWERED`、`UNANSWERED`、`DELETED`、`DRAFT`、`RECENT`
- `FROM "alice@example.com"`、`TO "..."`、`CC "..."`、`SUBJECT "週報"`
- `SINCE 1-Jan-2026`、`BEFORE 31-Dec-2026`、`ON 12-Jun-2026`
- 邏輯：`UNSEEN FROM "alice"`、`OR FROM "alice" FROM "bob"`、`NOT SEEN`

### email_get_message

```json
{ "folder": "INBOX", "uid": "12345", "mark_read": false }
```

### email_mark / email_delete

```json
{"folder": "INBOX", "uids": ["12345", "12346"], "flag": "\\Seen", "add": true}
{"folder": "INBOX", "uids": ["12345"]}
```

## 驗證測試結果

```
=== 21/21 passed ===
```

另有 `test_mcp_stdio.py`：用真正的 MCP client 把 `mcp_email.py` 以 stdio 子行程啟動，
跑完整 `initialize` → `list_tools` → 呼叫 tool 的 handshake，驗證能被任何 MCP host 載入。

每次 push / PR 會由 GitHub Actions 在 Python 3.10–3.13 上自動跑兩支測試（見 `.github/workflows/test.yml`）。

涵蓋三類情境：

**SMTP 寄信（in-process aiosmtpd 假 SMTP，真的把信送過去比對）**
- header（From / To / Cc / Reply-To / Subject）
- bcc 不外漏（rcpt_tos 有 bcc 但 header 沒）
- multipart：純文字 + HTML 兩個 part 都存在
- 附件：檔案 + base64 兩種來源，filename 含中文（`備註.txt`）也對
- retry：port 沒人 listen 時 SMTP 會 raise `ConnectionRefusedError`
- 密碼不會出現在 `email_configure` 回傳

**錯誤路徑 / 安全**
- 沒寄件人、沒收件人、附件不存在、壞 base64 各自 raise 對應 error
- html-only 自動補純文字 fallback
- 附件白名單：名單外檔案被 `PermissionError` 擋下、名單內正常附加

**IMAP 讀信（FakeIMAP 取代真連線）**
- `list_folders` / `list_messages`（UID 正確解析）/ `get_message`（text+html+附件 metadata）
- `mark`（flag 真的寫入）/ `delete`（UID EXPUNGE，及不支援 UIDPLUS 時 fallback）/ `test_connection`（IMAP NOOP）
- parser 健壯性：FLAGS/UID 出現在 BODY literal「之後」的回應排序也能正確解析

## 設計選擇 / 已知限制

- **無 OAuth flow**：目前只支援帳密。Gmail / 365 OAuth2 比較複雜，要看後續是否真實需要再加。
- **不維護長連線**：每次 IMAP 呼叫都重開連線。簡單可靠，效能上對「偶爾讀信」場景夠用；如果你要做「常駐 polling」可能要改長連線。
- **IMAP search 直接傳給伺服器**：自由度高但要懂 RFC 3501 syntax；含空格的字串記得加雙引號（如 `FROM "alice@x.com"`）。
- **刪信用 UID EXPUNGE**：`email_delete` 在 server 支援 UIDPLUS（RFC 4315）時只清掉你指定的 uid，不會誤刪資料夾內其他已標 `\Deleted` 的信；不支援時才 fallback 到一般 `EXPUNGE`（回傳的 `method` 欄位會標明用了哪種）。
- **附件大小**：受 SMTP 伺服器限制（Gmail 25MB、Outlook 20MB、Exchange 視設定）。本工具不設上限，超過會直接 SMTP error。
- **僅 stdio 協定**：給本機 Claude Desktop / Cowork 用。不支援 HTTP MCP server transport。
- **single-process**：跑一個 user。多 tenant / 多帳號要靠 host 多開幾個 instance（每個給不同 env）。

## License

MIT。隨便用。
