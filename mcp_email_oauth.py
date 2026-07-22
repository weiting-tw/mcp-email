#!/usr/bin/env python3
"""
mcp-email OAuth bridge — 讓 claude.ai Connectors（手機/網頁版）能連 mcp-email server。

設計：無狀態加密 token（伺服器不保存任何憑證）
  1. 用戶端走標準 OAuth 2.1（動態註冊 + PKCE，由 mcp SDK 處理端點）。
  2. /authorize 會把使用者導到本模組的 /login 頁，輸入信箱帳號＋密碼
     （建議用應用程式專用密碼）；bridge 先以 IMAP 登入驗證一次。
  3. 驗證通過後，把憑證用伺服器金鑰 AES-GCM 加密封進 access/refresh
     token。之後每個 MCP 請求由 token 解密取回憑證，pass-through 給
     IMAP/SMTP 登入。伺服器端沒有憑證資料庫；撤銷＝使用者改密碼或
     撤掉應用程式專用密碼。

金鑰：環境變數 EMAIL_BRIDGE_KEY（urlsafe base64 的 32 bytes）優先；
     否則用 EMAIL_BRIDGE_KEY_FILE（預設專案根目錄 .bridge-key，
     首次啟動自動產生，chmod 600）。換金鑰＝所有已發 token 立即失效。

需要：pip install cryptography（其餘同 MCP server）。
"""
import html
import json
import os
import secrets
import sys
import time
from urllib.parse import urlencode, urlparse, urlunparse

import anyio
from cryptography.hazmat.primitives.ciphers.aead import AESGCM
from pydantic import AnyHttpUrl
from starlette.requests import Request
from starlette.responses import HTMLResponse, RedirectResponse, Response

from mcp.server.auth.provider import (
    AccessToken,
    AuthorizationCode,
    AuthorizationParams,
    RefreshToken,
    TokenError,
)
from mcp.server.auth.settings import AuthSettings, ClientRegistrationOptions
from mcp.shared.auth import OAuthClientInformationFull, OAuthToken

import mcp_email  # 重用 IMAPClient 與伺服器端 IMAP 主機設定

SOURCE_URL = "https://github.com/weiting-tw/mcp-email"
SCOPE = "email"
ACCESS_TTL = 3600            # access token 1 小時
REFRESH_TTL = 30 * 24 * 3600  # refresh token 30 天
TXN_TTL = 600                # /authorize → /login 完成的時限
CODE_TTL = 300               # 授權碼時限
DONE_TTL = 600               # 記住「已完成授權」的 txn，讓舊分頁看到正確的成功訊息
MAX_LOGIN_TRIES = 5          # 同一授權交易的密碼錯誤上限（防透過 /login 暴力猜測）
MAX_CLIENTS = 200            # DCR 開放註冊的上限：超過就淘汰最舊的 client
                             # （防匿名灌爆 clients 檔；被淘汰的 client 重新註冊即可）
# 失敗節流：不少信箱服務會把「短時間多次錯誤密碼」的來源 IP 整個封鎖。
# bridge 對上游而言是單一 IP，被封＝全部使用者斷線，
# 所以寧可先在這裡拒絕，也不能把失敗流量透傳給上游。
FAIL_WINDOW = 900            # 失敗計數的滑動視窗（秒）
FAIL_LIMIT_IP = 8            # 同一來源 IP 視窗內的失敗上限
FAIL_LIMIT_GLOBAL = 20       # 全 server 視窗內的失敗上限（斷路器）
AUTH_LOG_MAX = 5 * 1024 * 1024  # auth.log 自我輪替門檻：超過換檔（保留一份 .1），
                                # 否則攻擊流量會拿日誌灌爆磁碟
DEFAULT_DOMAIN = os.environ.get("EMAIL_DOMAIN", "")  # 設了才在帳號沒打 @ 時自動補

_SEC_HEADERS = {             # /login 頁安全標頭：防點擊劫持、不快取憑證頁
    "X-Frame-Options": "DENY",
    # 不能設 form-action：Chrome 會拿它檢查「表單 POST 後的 302 重導目標」，
    # 而登入成功後必須 302 到 client 的 redirect_uri（不同 origin），鎖 'self' 會整個擋掉。
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; "
                               "script-src 'unsafe-inline'; "
                               "frame-ancestors 'none'",
    "Cache-Control": "no-store",
    "Referrer-Policy": "no-referrer",
}

# 三個網頁（login / completed / index）共用同一套樣式，避免各頁重複維護 CSS。
_PAGE_STYLE = """
body{font-family:-apple-system,"PingFang TC",sans-serif;background:#f1f5f9;
display:flex;justify-content:center;padding-top:8vh;margin:0}
.box{background:#fff;padding:28px;border-radius:12px;box-shadow:0 4px 16px rgba(0,0,0,.08);
width:340px;box-sizing:border-box}
h1{font-size:17px;margin:0 0 6px}
p,li{font-size:13px;color:#475569;margin:6px 0}
ol{padding-left:18px;margin:6px 0}
input{width:100%;box-sizing:border-box;padding:8px;margin:6px 0;border:1px solid #cbd5e1;
border-radius:6px;font-size:14px}
button{width:100%;padding:10px;margin-top:10px;background:#2563eb;color:#fff;border:none;
border-radius:8px;font-size:14px;cursor:pointer}
button:disabled{background:#94a3b8;cursor:progress}
code{background:#f1f5f9;border-radius:4px;padding:1px 5px;font-size:12px;word-break:break-all}
.err{color:#dc2626;font-size:13px;margin:6px 0}
.note{font-size:11px;color:#94a3b8}
.consent{background:#f8fafc;border:1px solid #e2e8f0;border-radius:8px;padding:12px;margin:10px 0}
.consent .dest{margin:4px 0}
.consent .warn{color:#b45309;font-size:12px;margin:6px 0 0}
.center{text-align:center}
"""

ROOT = os.path.dirname(os.path.abspath(__file__))
DEFAULT_KEY_PATH = os.environ.get("EMAIL_BRIDGE_KEY_FILE") or os.path.join(ROOT, ".bridge-key")
DEFAULT_CLIENTS_PATH = os.environ.get("EMAIL_OAUTH_CLIENTS") or os.path.join(ROOT, ".oauth-clients.json")


def _verify_imap_login(user: str, pwd: str) -> None:
    """以 IMAP 登入驗證帳密（用伺服器端設定的主機）；失敗丟例外。"""
    import dataclasses
    cfg = dataclasses.replace(mcp_email.CONFIG.imap, username=user, password=pwd)
    with mcp_email.IMAPClient(cfg) as conn:
        conn.noop()


# ---------- token 加解密（憑證只存在於 token 密文內） ----------
class TokenCrypto:
    _AAD = b"mcp-email-bridge-v1"
    _PREFIX = "me1."

    def __init__(self, key: bytes):
        self._aead = AESGCM(key)

    @staticmethod
    def load_key(path=DEFAULT_KEY_PATH) -> bytes:
        import base64
        env = os.environ.get("EMAIL_BRIDGE_KEY")
        if env:
            return base64.urlsafe_b64decode(env)
        if os.path.isfile(path):
            with open(path) as f:
                return base64.urlsafe_b64decode(f.read().strip())
        key = AESGCM.generate_key(bit_length=256)
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w") as f:
            f.write(base64.urlsafe_b64encode(key).decode())
        return key

    def seal(self, payload: dict) -> str:
        import base64
        nonce = secrets.token_bytes(12)
        ct = self._aead.encrypt(nonce, json.dumps(payload).encode("utf-8"), self._AAD)
        return self._PREFIX + base64.urlsafe_b64encode(nonce + ct).decode().rstrip("=")

    def open(self, token: str) -> dict | None:
        """解不開/被竄改/格式錯 → None（不丟例外，交由呼叫端視為無效 token）。"""
        import base64
        if not isinstance(token, str) or not token.startswith(self._PREFIX):
            return None
        body = token[len(self._PREFIX):]
        try:
            raw = base64.urlsafe_b64decode(body + "=" * (-len(body) % 4))
            data = self._aead.decrypt(raw[:12], raw[12:], self._AAD)
            return json.loads(data)
        except Exception:
            return None


# ---------- 帶憑證的 token 模型（只在伺服器行程內，不外露） ----------
class CredAuthorizationCode(AuthorizationCode):
    email_user: str = ""
    email_pass: str = ""


class CredRefreshToken(RefreshToken):
    email_user: str = ""
    email_pass: str = ""


class CredAccessToken(AccessToken):
    email_user: str = ""
    email_pass: str = ""


def _merge_query(url: str, extra: dict) -> str:
    parts = urlparse(url)
    q = parts.query + ("&" if parts.query else "") + urlencode(extra)
    return urlunparse(parts._replace(query=q))


class EmailOAuthProvider:
    """OAuthAuthorizationServerProvider 實作。
    狀態僅有：已註冊 client（存 JSON 檔，重啟保留）、進行中的授權交易
    與授權碼（記憶體、短效）。token 本身無狀態。"""

    def __init__(self, crypto: TokenCrypto, clients_path=DEFAULT_CLIENTS_PATH):
        self.crypto = crypto
        self.clients_path = clients_path
        self._clients: dict[str, dict] = {}
        if os.path.isfile(clients_path):
            try:
                with open(clients_path) as f:
                    self._clients = json.load(f)
            except Exception:
                self._clients = {}
        # txn → {cid, params, exp, tries}；tries 達上限即作廢該授權交易
        self._pending: dict[str, dict] = {}
        self._codes: dict[str, CredAuthorizationCode] = {}
        self._done: dict[str, float] = {}  # 已成功完成的 txn → 到期時間
        self._fails: dict[str, list[float]] = {}  # 來源 IP（"*"=全域）→ 失敗時間戳

    # --- client 註冊（DCR） ---
    async def get_client(self, client_id: str) -> OAuthClientInformationFull | None:
        data = self._clients.get(client_id)
        return OAuthClientInformationFull.model_validate(data) if data else None

    async def register_client(self, client_info: OAuthClientInformationFull) -> None:
        self._clients[client_info.client_id] = client_info.model_dump(mode="json")
        if len(self._clients) > MAX_CLIENTS:
            for cid in sorted(self._clients,
                              key=lambda c: self._clients[c].get("client_id_issued_at") or 0
                              )[:len(self._clients) - MAX_CLIENTS]:
                del self._clients[cid]
        tmp = self.clients_path + ".tmp"
        with open(tmp, "w") as f:
            json.dump(self._clients, f, ensure_ascii=False, indent=1)
        os.replace(tmp, self.clients_path)

    # --- 授權流程 ---
    async def authorize(self, client: OAuthClientInformationFull,
                        params: AuthorizationParams) -> str:
        self._prune()
        txn = secrets.token_urlsafe(24)
        self._pending[txn] = {"cid": client.client_id, "params": params,
                              "exp": time.time() + TXN_TTL, "tries": 0}
        return "/login?" + urlencode({"txn": txn})

    def _prune(self):
        now = time.time()
        self._pending = {k: v for k, v in self._pending.items() if v["exp"] > now}
        self._codes = {k: v for k, v in self._codes.items() if v.expires_at > now}
        self._done = {k: v for k, v in self._done.items() if v > now}
        self._fails = {k: ts for k, ts in
                       ((k, [t for t in v if now - t < FAIL_WINDOW])
                        for k, v in self._fails.items()) if ts}

    @staticmethod
    def _client_ip(request: Request) -> str:
        xff = request.headers.get("x-forwarded-for", "")
        if xff:
            return xff.split(",")[0].strip()
        return request.client.host if request.client else "?"

    def _throttled(self, ip: str) -> bool:
        now = time.time()
        for k in (ip, "*"):
            self._fails[k] = [t for t in self._fails.get(k, []) if now - t < FAIL_WINDOW]
        return (len(self._fails.get(ip, [])) >= FAIL_LIMIT_IP
                or len(self._fails.get("*", [])) >= FAIL_LIMIT_GLOBAL)

    def _record_fail(self, ip: str) -> None:
        now = time.time()
        self._fails.setdefault(ip, []).append(now)
        self._fails.setdefault("*", []).append(now)
        # 固定格式寫入失敗日誌，供主機層 fail2ban 監看並在防火牆 ban 掉來源
        path = os.environ.get("EMAIL_AUTH_LOG")
        if path:
            try:
                try:
                    if os.path.getsize(path) > AUTH_LOG_MAX:
                        os.replace(path, path + ".1")  # 自我輪替，fail2ban 會照檔名重新跟
                except FileNotFoundError:
                    pass
                with open(path, "a") as f:
                    f.write(time.strftime("%Y-%m-%d %H:%M:%S")
                            + f" mcp-email-login-fail ip={ip}\n")
            except OSError as e:
                if not getattr(self, "_authlog_warned", False):
                    self._authlog_warned = True
                    print(f"警告：無法寫入 EMAIL_AUTH_LOG（{path}）：{e}；"
                          "fail2ban 日誌功能停用（檢查掛載目錄對執行 UID 的寫入權限）",
                          file=sys.stderr)

    def _document(self, body: str, title: str) -> str:
        """把頁面內容包進共用的 doctype/head（含 _PAGE_STYLE）外殼。"""
        return ('<!doctype html><html lang="zh-Hant"><head><meta charset="utf-8">'
                '<meta name="viewport" content="width=device-width,initial-scale=1">'
                f"<title>{html.escape(title)}</title><style>{_PAGE_STYLE}</style>"
                f"</head><body>{body}</body></html>")

    def _footer(self) -> str:
        return (f'<p class="note">mcp-email v{mcp_email.__version__} · '
                f'<a href="{SOURCE_URL}" target="_blank" rel="noopener" '
                'style="color:#94a3b8">原始碼</a></p>')

    def _client_display(self, entry: dict) -> tuple[str, str]:
        """回 (應用程式名稱, 授權碼將送達的網域)，供登入頁的同意橫幅顯示。
        名稱是 client 註冊時自填的（不可盡信）；真正的信任訊號是「送達網域」
        ——釣魚攻擊會把 redirect_uri 指向自己的網域，使用者認不得就該中止。"""
        info = self._clients.get(entry.get("cid", ""), {})
        name = info.get("client_name") or info.get("client_uri") or entry.get("cid", "")
        params = entry.get("params")
        redirect = str(getattr(params, "redirect_uri", "") or "")
        return name, (urlparse(redirect).netloc or redirect)

    def _login_html(self, txn: str, error: str = "",
                    client_name: str = "", redirect_host: str = "") -> str:
        err = f'<p class="err">{html.escape(error)}</p>' if error else ""
        hint = f"（可省略 @{html.escape(DEFAULT_DOMAIN)}）" if DEFAULT_DOMAIN else ""
        # 同意橫幅：client_name / redirect_host 皆為註冊時的外部輸入，一律 escape 防 XSS
        consent = ""
        if client_name or redirect_host:
            consent = (
                '<div class="consent">'
                f'<p>應用程式 <b>{html.escape(client_name) or "（未提供名稱）"}</b> '
                '要求連接你的信箱。</p>'
                f'<p class="dest">授權後存取權將送往：<b>{html.escape(redirect_host) or "（未知）"}</b></p>'
                '<p class="warn">只有在你認得這個應用程式與目的地時才繼續；'
                '若不是你主動發起的連接，請直接關閉此頁。</p></div>'
            )
        body = (
            '<form class="box" method="post" action="/login">'
            '<h1>連接你的信箱</h1>'
            f'{consent}'
            '<p>請輸入信箱帳號與密碼（信箱服務若支援，<b>建議使用應用程式專用密碼</b>）。</p>'
            f'{err}'
            f'<input type="hidden" name="txn" value="{html.escape(txn)}">'
            f'<input name="user" placeholder="帳號{hint}" autocomplete="username" required>'
            '<input name="password" type="password" placeholder="密碼（建議應用程式專用密碼）" '
            'autocomplete="current-password" required>'
            '<button id="sb" type="submit">驗證並授權</button>'
            '<p class="note">憑證只用來即時以 IMAP 驗證並加密封入你的存取權杖，伺服器不儲存。'
            '撤銷方式：更改密碼或撤銷該應用程式專用密碼。</p>'
            f'{self._footer()}</form>'
            '<script>document.querySelector("form").addEventListener("submit",function(){'
            'var b=document.getElementById("sb");b.disabled=true;'
            'b.textContent="驗證中，請稍候…（正在連線信箱伺服器）";});</script>'
        )
        return self._document(body, "mcp-email 信箱授權")

    def _completed_html(self) -> str:
        body = ('<div class="box center"><h1>✅ 授權完成</h1>'
                '<p>信箱已成功連接，可以關閉此視窗並回到應用程式。</p></div>')
        return self._document(body, "授權完成")

    def _index_html(self) -> str:
        mcp_url = html.escape(getattr(self, "issuer", "") + "/mcp")
        body = (
            '<div class="box"><h1>📮 mcp-email MCP server</h1>'
            '<p>讓 Claude 等 AI 助理收發與整理你的信箱（IMAP/SMTP）。</p>'
            '<p><b>連接方式（claude.ai）：</b></p><ol>'
            '<li>設定 → 連接器（Connectors）→ 新增自訂連接器</li>'
            f'<li>貼上 <code>{mcp_url}</code></li>'
            '<li>授權時輸入信箱帳號＋密碼（建議使用應用程式專用密碼）</li></ol>'
            '<p class="note">憑證只用來即時以 IMAP 驗證並加密封入你的存取權杖，伺服器不儲存；'
            '撤銷方式：更改密碼或撤銷該應用程式專用密碼。</p>'
            f'{self._footer()}</div>'
        )
        return self._document(body, "mcp-email MCP server")

    async def index_page(self, request: Request) -> Response:
        return self._page(self._index_html())

    def _page(self, html: str, status: int = 200) -> HTMLResponse:
        return HTMLResponse(html, status_code=status, headers=_SEC_HEADERS)

    async def login_page(self, request: Request) -> Response:
        txn = request.query_params.get("txn", "")
        if txn in self._done and self._done[txn] > time.time():
            return self._page(self._completed_html())
        if txn not in self._pending:
            return self._page(self._login_html(
                "", "此授權連結無效或已過期。若尚未連上，請回到用戶端重新連接。"), 400)
        name, host = self._client_display(self._pending[txn])
        return self._page(self._login_html(txn, client_name=name, redirect_host=host))

    async def login_submit(self, request: Request) -> Response:
        form = await request.form()
        txn = str(form.get("txn", ""))
        user = str(form.get("user", "")).strip()
        # 密碼不含前後空白；strip 掉複製貼上常見的殘留空白/換行
        pwd = str(form.get("password", "")).strip()
        if DEFAULT_DOMAIN and "@" not in user:
            user += "@" + DEFAULT_DOMAIN
        if txn in self._done and self._done[txn] > time.time():
            return self._page(self._completed_html())  # 舊分頁重送：其實已完成
        entry = self._pending.get(txn)
        if not entry or entry["exp"] < time.time():
            return self._page(self._login_html(
                "", "此授權階段已失效或已過期。若尚未連上，請回到用戶端重新連接一次。"), 400)
        client_id, params = entry["cid"], entry["params"]
        name, host = self._client_display(entry)  # 重繪登入頁時保留同意橫幅

        # 失敗節流：上游可能封鎖「多次錯誤密碼」的來源 IP（即本 server 的 IP），
        # 達門檻就先在這裡擋下，不透傳給上游
        ip = self._client_ip(request)
        if self._throttled(ip):
            return self._page(self._login_html(
                txn, "驗證嘗試過於頻繁，請稍後再試。",
                client_name=name, redirect_host=host), 429)

        # 打一次 IMAP 登入驗證憑證（blocking → 丟 worker thread）
        try:
            await anyio.to_thread.run_sync(lambda: _verify_imap_login(user, pwd))
        except Exception:
            self._record_fail(ip)
            entry["tries"] += 1
            if entry["tries"] >= MAX_LOGIN_TRIES:
                del self._pending[txn]
                return self._page(self._login_html(
                    "", "嘗試次數過多，此授權交易已作廢，請回到用戶端重新連接。"), 429)
            return self._page(self._login_html(
                txn, "驗證失敗：帳號或密碼不正確。",
                client_name=name, redirect_host=host), 401)

        del self._pending[txn]
        code = secrets.token_urlsafe(32)
        self._codes[code] = CredAuthorizationCode(
            code=code,
            scopes=params.scopes or [SCOPE],
            expires_at=time.time() + CODE_TTL,
            client_id=client_id,
            code_challenge=params.code_challenge,
            redirect_uri=params.redirect_uri,
            redirect_uri_provided_explicitly=params.redirect_uri_provided_explicitly,
            resource=params.resource,
            subject=user,
            email_user=user,
            email_pass=pwd,
        )
        self._done[txn] = time.time() + DONE_TTL  # 記住已完成，舊分頁重送時給正向頁
        extra = {"code": code}
        if params.state:
            extra["state"] = params.state
        return RedirectResponse(_merge_query(str(params.redirect_uri), extra), status_code=302)

    async def load_authorization_code(self, client, authorization_code: str):
        code = self._codes.get(authorization_code)
        if not code or code.client_id != client.client_id or code.expires_at < time.time():
            return None
        return code

    async def exchange_authorization_code(self, client, authorization_code) -> OAuthToken:
        self._codes.pop(authorization_code.code, None)  # 一次性
        return self._mint(client.client_id, authorization_code.scopes,
                          authorization_code.email_user, authorization_code.email_pass)

    # --- token 發行 / 驗證（無狀態） ---
    def _mint(self, client_id: str, scopes: list[str], user: str, pwd: str) -> OAuthToken:
        now = int(time.time())
        base = {"c": client_id, "s": scopes, "u": user, "p": pwd}
        access = self.crypto.seal({**base, "t": "a", "e": now + ACCESS_TTL})
        refresh = self.crypto.seal({**base, "t": "r", "e": now + REFRESH_TTL})
        return OAuthToken(access_token=access, token_type="Bearer", expires_in=ACCESS_TTL,
                          scope=" ".join(scopes), refresh_token=refresh)

    def _open_typed(self, token: str, typ: str) -> dict | None:
        d = self.crypto.open(token)
        if not d or d.get("t") != typ or d.get("e", 0) < time.time():
            return None
        return d

    async def load_refresh_token(self, client, refresh_token: str):
        d = self._open_typed(refresh_token, "r")
        if not d or d["c"] != client.client_id:
            return None
        return CredRefreshToken(token=refresh_token, client_id=d["c"], scopes=d["s"],
                                expires_at=d["e"], subject=d["u"],
                                email_user=d["u"], email_pass=d["p"])

    async def exchange_refresh_token(self, client, refresh_token, scopes: list[str]) -> OAuthToken:
        if scopes and not set(scopes) <= set(refresh_token.scopes):
            raise TokenError("invalid_scope", "要求的 scope 超出原授權範圍")
        return self._mint(client.client_id, scopes or refresh_token.scopes,
                          refresh_token.email_user, refresh_token.email_pass)

    async def load_access_token(self, token: str):
        d = self._open_typed(token, "a")
        if not d:
            return None
        return CredAccessToken(token=token, client_id=d["c"], scopes=d["s"],
                               expires_at=d["e"], subject=d["u"],
                               email_user=d["u"], email_pass=d["p"])


def create(issuer: str, key_path=DEFAULT_KEY_PATH,
           clients_path=DEFAULT_CLIENTS_PATH) -> tuple[EmailOAuthProvider, AuthSettings]:
    """建立 provider 與 AuthSettings。issuer 需為用戶端可達的對外網址。"""
    provider = EmailOAuthProvider(TokenCrypto(TokenCrypto.load_key(key_path)), clients_path)
    issuer = issuer.rstrip("/")
    provider.issuer = issuer  # 首頁介紹用（組 /mcp 連接網址）
    settings = AuthSettings(
        issuer_url=AnyHttpUrl(issuer),
        resource_server_url=AnyHttpUrl(issuer + "/mcp"),
        client_registration_options=ClientRegistrationOptions(
            enabled=True, valid_scopes=[SCOPE], default_scopes=[SCOPE]),
        required_scopes=[SCOPE],
    )
    return provider, settings


def add_login_routes(server, provider: EmailOAuthProvider) -> None:
    server.custom_route("/", methods=["GET"])(provider.index_page)
    server.custom_route("/login", methods=["GET"])(provider.login_page)
    server.custom_route("/login", methods=["POST"])(provider.login_submit)
