# mcp-email — IMAP / SMTP 信箱 MCP server（本機 stdio ＋ 遠端 HTTP / OAuth）

[![tests](https://github.com/weiting-tw/mcp-email/actions/workflows/test.yml/badge.svg)](https://github.com/weiting-tw/mcp-email/actions/workflows/test.yml)

純 Python 實作（`mcp_email.py`），把任意支援 IMAP / SMTP 的標準信箱（Gmail / Outlook / Yahoo / iCloud / Zoho / 公司 Exchange / 自架 mail server …）包成 MCP tools 給 Claude / Claude Code / Cowork / 任何 MCP host 用。

三種啟動模式（參考 [m2k-calendar-tools](https://github.com/weiting-tw/m2k-calendar-tools) 的部署架構）：

| 模式 | 指令 | 適用場景 | 認證 |
|---|---|---|---|
| **stdio**（預設） | `mcp-email` | 本機單人（Claude Desktop / Code） | 環境變數 / `.env` / `email_configure`（原有方式，完全不變） |
| **HTTP** | `mcp-email --http` | 內網多人共用（Claude Code / Desktop 遠端連線） | 每請求 `Authorization: Basic`，伺服器不存帳密 |
| **OAuth** | `mcp-email --oauth --issuer …` | claude.ai Connectors（手機 app / 網頁版） | OAuth 2.1 + 無狀態加密 token，伺服器不存帳密 |

## 能做什麼（11 個 tools）

| Tool | 用途 |
|---|---|
| `email_configure` | Runtime 動態切換 SMTP / IMAP 帳密、port、TLS |
| `email_test_connection` | 一鍵測 SMTP + IMAP 是否能登入 |
| `email_send` | 寄信：HTML + 純文字、to/cc/bcc、檔案/Base64 附件、Reply-To、自訂 headers、retry |
| `email_list_folders` | IMAP 列出所有 mailbox 名稱（中文名稱自動解碼） |
| `email_list_messages` | 列出 folder 內訊息 header（支援 IMAP search syntax） |
| `email_get_message` | 抓單封信完整內容（body text/html、附件 metadata、選擇是否標 SEEN） |
| `email_mark` | 加/移除 IMAP flag（`\Seen` / `\Flagged` 等） |
| `email_delete` | 標記 `\Deleted` 並 expunge（UID EXPUNGE） |
| `email_create_folder` | 建立 folder（支援中文，自動 modified UTF-7；已存在不報錯） |
| `email_move_messages` | 搬信：UID MOVE，server 不支援則 COPY + UID EXPUNGE fallback |
| `email_apply_rules` | 規則整理：掃描後依條件 move/mark/delete，`dry_run` 預覽；比對方式可調（`match`: substring/regex/exact、`case_sensitive`、`match_mode`: first/all） |

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

# 跑端到端測試（in-process 假 SMTP + 假 IMAP）
python test_e2e.py
# 預期：=== 39/39 passed ===

# 遠端（--http/--oauth）模式測試
python -m pytest test_remote.py -q
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

## 遠端使用（本地以外）

以下兩種模式讓同一份 server 部署在內網主機 / NAS / 雲端 VM，多人共用。
共同原則：**伺服器只設定主機資訊（`IMAP_HOST` / `SMTP_HOST` / port / TLS），
不設定任何人的帳密**；每位使用者的帳密隨請求帶入、原樣 pass-through 給
IMAP/SMTP 登入，撤銷 = 使用者自己改密碼或撤掉應用程式專用密碼。

兩種模式都是明文帳密等級的傳輸，**正式部署必須放在 HTTPS 反向代理後面**
（nginx / Caddy / Synology 反向代理皆可）。放在反向代理後面時，記得設
`EMAIL_ALLOWED_HOSTS=你的對外網域`，否則 SDK 的 DNS-rebinding 防護會把
請求擋成 `421 Invalid Host header`。

遠端模式與 stdio 的行為差異（多人共用的安全考量）：
- `email_configure` 停用並自 tool 列表隱藏（全域設定不容任一使用者改動）
- `path` 附件預設停用（那是「伺服器」的檔案系統）；請改用 `content_base64`，
  或由管理員設 `EMAIL_ATTACHMENT_DIRS` 白名單開放
- 預設 `From` = 該請求登入的帳號（不吃伺服器端 `EMAIL_FROM`）

### HTTP 模式（Basic pass-through，適合內網共用）

```bash
# 已安裝（pip / pipx）或直接 uvx，一行起服務：
IMAP_HOST=mail.example.com SMTP_HOST=mail.example.com \
uvx mcp-email --http --host 0.0.0.0 --port 8765

# 從原始碼跑也一樣：python3 mcp_email.py --http --host 0.0.0.0 --port 8765
```

主機設定也可以放在工作目錄的 `.env` 檔（`KEY=VALUE` 每行一組，環境變數優先），
不必每次打在指令前。

每位使用者在自己的 Claude Code 註冊（帳密只存在自己機器上）：

```bash
claude mcp add --transport http email https://主機/mcp \
  --header "Authorization: Basic $(printf '%s' '帳號:密碼' | base64)"
```

伺服器不保存任何帳密、也**絕不回退到環境變數憑證**：沒帶 `Authorization`
標頭的工具呼叫一律被拒絕。

### OAuth 模式（claude.ai Connectors：手機 app / 網頁版）

> 注意：OAuth 模式為**單行程**設計（進行中的授權交易存在記憶體），
> 不支援多副本負載平衡；token 本身無狀態，重啟不影響已發 token。

claude.ai 的連線來自 Anthropic 雲端，issuer 必須是公網可達的 HTTPS 網址：

```bash
# uvx（自動帶上 OAuth 模式的額外依賴 cryptography）：
IMAP_HOST=mail.example.com SMTP_HOST=mail.example.com \
uvx --with cryptography mcp-email --oauth --issuer https://mail-mcp.example.com \
  --host 0.0.0.0 --port 8765

# 或既有環境：pip install "mcp-email[oauth]"（= pip install cryptography）
```

流程：標準 OAuth 2.1（動態註冊 + PKCE）。使用者第一次連接會被導到
`/login` 輸入信箱帳密（建議應用程式專用密碼），bridge 先以 IMAP 登入驗證，
通過後把憑證用伺服器金鑰 AES-GCM 加密封進 token —— **無狀態設計，
伺服器沒有憑證資料庫**。access token 1 小時、refresh token 30 天自動輪替。

claude.ai 端：設定 → 連接器（Connectors）→ 新增自訂連接器 → 貼上
`https://mail-mcp.example.com/mcp`。

相關環境變數（皆選配）：

| 變數 | 用途 |
|---|---|
| `EMAIL_BRIDGE_KEY` | token 加密金鑰（urlsafe base64 的 32 bytes）；沒設就自動產生 `.bridge-key` 檔（chmod 600）。換金鑰＝所有已發 token 立即失效 |
| `EMAIL_BRIDGE_KEY_FILE` / `EMAIL_OAUTH_CLIENTS` | 金鑰檔 / DCR client 註冊檔路徑（Docker 部署指向 `/data`） |
| `EMAIL_DOMAIN` | 設了之後，登入帳號沒打 `@` 會自動補 `@此網域`（如 `EMAIL_DOMAIN=example.com` 時輸入 `alice` → `alice@example.com`） |
| `EMAIL_AUTH_LOG` | 登入失敗日誌（固定格式，供 fail2ban 監看封鎖來源）|
| `EMAIL_ALLOWED_HOSTS` | 反向代理後的對外網域白名單（逗號分隔） |

⚠️ **金鑰是整個 OAuth 模式的單點要害**：拿到 bridge 金鑰的人可以解開所有
已發 token 內的使用者憑證。金鑰檔 / `EMAIL_BRIDGE_KEY` 務必限縮讀取權限、
不進版控（repo 的 `.gitignore` 已排除 `.bridge-key`），有疑慮就換金鑰
（代價只是所有人重新授權一次）。

內建防護：登入頁**同意畫面**（顯示發起授權的應用程式名稱＋授權碼送達的
網域，讓使用者辨識並攔截釣魚連結）、登入失敗節流（同 IP / 全域滑動視窗，
避免上游信箱服務封鎖 bridge 的 IP）、同授權交易密碼錯誤 5 次作廢、
登入頁安全標頭、auth log 自我輪替。搭配 fail2ban 的 filter 範例：

```ini
# /etc/fail2ban/filter.d/mcp-email.conf
[Definition]
failregex = mcp-email-login-fail ip=<HOST>
# jail：logpath 指向 EMAIL_AUTH_LOG 的路徑，maxretry/bantime 視需求
```

### Docker 部署

```bash
docker build -t mcp-email .

# HTTP 模式
docker run -d -p 8765:8765 \
  -e IMAP_HOST=mail.example.com -e SMTP_HOST=mail.example.com \
  mcp-email

# OAuth 模式（掛 /data volume 保留金鑰與 client 註冊）
docker run -d -p 8765:8765 -v mcp-email-data:/data \
  -e IMAP_HOST=mail.example.com -e SMTP_HOST=mail.example.com \
  mcp-email --oauth --issuer https://對外網址 --host 0.0.0.0 --port 8765
```

容器以非 root（uid 10001）執行。OAuth 模式為單行程設計（授權交易存在
記憶體），不支援多副本負載平衡。

### HTTPS 反向代理範例（Caddy，兩行搞定自動憑證）

```
mail-mcp.example.com {
    reverse_proxy 127.0.0.1:8765
}
```

nginx 使用者：`proxy_pass http://127.0.0.1:8765;` 並保留
`proxy_set_header Host $host;`；SSE 緩衝不用另外關（server 回應已帶
`X-Accel-Buffering: no`）。設好代理後記得配 `EMAIL_ALLOWED_HOSTS=mail-mcp.example.com`。

### 遠端部署上線前檢查清單

上線 `--http` / `--oauth` 前逐項確認，尤其打 ✅ 的三項是實務上最常害人踩雷的：

- [ ] **HTTPS 反向代理**：帳密/token 是明文等級傳輸，一定要放在 TLS 後面，絕不裸跑對外。
- [ ] **`EMAIL_ALLOWED_HOSTS`**：設成你的對外網域，否則 SDK 的 DNS-rebinding 防護會回 `421 Invalid Host header`。
- [ ] ✅ **OAuth 金鑰要持久化**：`--oauth` 一定要掛 `-v mcp-email-data:/data`（或設固定的 `EMAIL_BRIDGE_KEY`）。**沒掛 volume→容器每次重啟都重新產生金鑰→所有人 token 全失效、要重新授權**，這是最常見的災情。
- [ ] ✅ **金鑰檔權限**：`.bridge-key`（或 `/data`）只給執行帳號讀；拿到金鑰的人可解開所有已發 token 內的使用者憑證。已在 `.gitignore` 排除，切勿進版控。
- [ ] ✅ **用應用程式專用密碼**：宣導使用者在登入頁輸入 App Password，不要用信箱主密碼——外洩時只波及收發信、可單獨撤銷。
- [ ] **同意畫面**：登入頁會顯示「哪個應用程式要連你的信箱」＋「授權碼送達的網域」；提醒使用者只在認得目的地時才繼續（防釣魚）。
- [ ] **失敗節流 / fail2ban**：設 `EMAIL_AUTH_LOG` 並掛 fail2ban（filter 範例見上），擋帳密暴力猜測。
- [ ] **`path` 附件**：遠端模式預設停用；若確有需求才用 `EMAIL_ATTACHMENT_DIRS` 白名單開放，範圍越小越好。
- [ ] **token 生命週期已知**：access 1 小時（自動續）、refresh 30 天（活躍使用者滾動不過期）；使用者改密碼＝所有舊 token 失效（需重新授權），這是刻意的撤銷機制。

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

**中文關鍵字**：支援。非 ASCII 關鍵字會自動改用 `SEARCH CHARSET UTF-8` + literal
（RFC 3501 標準做法）；一次一個中文詞，且含 `OR`/`NOT` 時要放在最後一個條件。
若伺服器接受語法但比對不到（實測 Mail2000 不解 RFC2047 編碼的中文標頭），會自動
fallback 成「以剩餘 ASCII 條件縮小範圍 → 抓 header 在客戶端比對」（掃描上限 200 封，
回傳會附 `note` 說明）。`email_apply_rules` 的規則比對本來就在客戶端做，中文不受限。

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
=== 39/39 passed ===
```

另有 `test_mcp_stdio.py`：用真正的 MCP client 把 `mcp_email.py` 以 stdio 子行程啟動，
跑完整 `initialize` → `list_tools` → 呼叫 tool 的 handshake，驗證能被任何 MCP host 載入。

以及 `test_remote.py`（29 tests）：遠端模式的 Basic 標頭解析、每請求憑證覆蓋、
遠端限制（`email_configure` / `path` 附件停用、白名單 symlink 繞過）、
OAuth token 加解密與 provider 流程（含失敗節流、狀態清理、DCR 上限淘汰）、
in-process uvicorn 起真正 streamable-http server 的端對端測試——包括
「HTTP Basic 帳密穿透到 IMAP/SMTP 登入」與「DCR→PKCE→/login→token→Bearer
呼叫工具」的完整 OAuth 授權流程。

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
- **遠端模式的多帳號**：`--http` / `--oauth` 天生多人（每請求帶各自憑證、彼此隔離）；stdio 模式仍是一個 instance 一個帳號，多帳號要靠 host 多開幾個 instance（每個給不同 env）。
- **OAuth 模式單行程**：授權交易存在記憶體，不支援多副本負載平衡；token 本身無狀態，重啟不影響已發 token（金鑰不變的前提下）。

## 發佈到 PyPI（維護者用）

推 `v*` tag 會由 GitHub Actions 自動 build + 發佈（`.github/workflows/publish.yml`，
走 PyPI [Trusted Publishing](https://docs.pypi.org/trusted-publishers/)，不需要 API token）：

```bash
# 1. bump pyproject.toml 的 version 並 commit
# 2. 上 tag 並推出去，Actions 就會發佈
git tag v0.3.0 && git push origin v0.3.0
```

一次性設定：PyPI 的 mcp-email 專案 → Settings → Publishing → 新增 GitHub publisher
（owner `weiting-tw`、repository `mcp-email`、workflow `publish.yml`、environment `pypi`），
並在 GitHub repo 的 Settings → Environments 建立名為 `pypi` 的 environment。

## License

MIT。隨便用。
