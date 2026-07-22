"""遠端（--http / --oauth）模式測試：憑證 pass-through、遠端限制、OAuth token、HTTP 傳輸。

不需要真的 IMAP/SMTP 伺服器；HTTP 傳輸測試用 in-process uvicorn。
"""
from __future__ import annotations

import asyncio
import base64
import json
import socket
import sys
import threading
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import mcp_email as srv
import mcp_email_oauth as oa


@pytest.fixture(autouse=True)
def _reset_mode():
    """每個測試後還原模式與每請求憑證，避免測試間互相污染。"""
    old = srv.MODE
    yield
    srv.MODE = old
    srv._REQ_CREDS.set(None)


def _basic(user: str, pwd: str) -> str:
    return "Basic " + base64.b64encode(f"{user}:{pwd}".encode()).decode()


# ─── Basic auth 解析 ────────────────────────────────────────────────────────
def test_parse_basic_auth_ok():
    assert srv._parse_basic_auth(_basic("alice@x.tw", "p:w:d")) == ("alice@x.tw", "p:w:d")


@pytest.mark.parametrize("header", [
    "", "Bearer abc",                                    # 非 Basic
    "Basic !!!!not-base64!!!!",                          # 非法 base64
    "Basic " + base64.b64encode(b"no-colon").decode(),   # 少冒號
    "Basic " + base64.b64encode(b":pwd-only").decode(),  # 空帳號
])
def test_parse_basic_auth_rejects(header):
    with pytest.raises(PermissionError):
        srv._parse_basic_auth(header)


# ─── 每請求憑證覆蓋設定 ─────────────────────────────────────────────────────
def test_cfg_override_with_request_creds():
    srv._REQ_CREDS.set(("alice@x.tw", "pw"))
    smtp, imap = srv._smtp_cfg(), srv._imap_cfg()
    assert smtp.username == "alice@x.tw" and smtp.password == "pw"
    assert imap.username == "alice@x.tw" and imap.password == "pw"
    # 是複本，不污染全域設定
    assert smtp is not srv.CONFIG.smtp and imap is not srv.CONFIG.imap
    assert srv.CONFIG.smtp.username != "alice@x.tw"
    # 主機/埠/TLS 仍沿用伺服器端設定
    assert smtp.host == srv.CONFIG.smtp.host and imap.port == srv.CONFIG.imap.port


def test_cfg_passthrough_without_creds():
    srv._REQ_CREDS.set(None)
    assert srv._smtp_cfg() is srv.CONFIG.smtp
    assert srv._imap_cfg() is srv.CONFIG.imap


def test_request_creds_stdio_is_none():
    srv.MODE = "stdio"
    assert srv._request_creds() is None


def test_request_creds_http_from_header():
    from mcp.server.lowlevel.server import request_ctx
    srv.MODE = "http"
    fake = SimpleNamespace(request=SimpleNamespace(
        headers={"authorization": _basic("bob@corp.tw", "secret")}))
    tok = request_ctx.set(fake)
    try:
        assert srv._request_creds() == ("bob@corp.tw", "secret")
    finally:
        request_ctx.reset(tok)


def test_request_creds_http_requires_header():
    """HTTP 模式絕不回退到環境變數憑證：拿不到請求/標頭就拒絕。"""
    srv.MODE = "http"
    with pytest.raises(PermissionError):
        srv._request_creds()


def test_request_creds_oauth_from_token(monkeypatch):
    """OAuth 模式：憑證來自 Bearer token 解密出的 email_user/email_pass。"""
    srv.MODE = "oauth"
    tok = oa.CredAccessToken(token="t", client_id="c", scopes=[oa.SCOPE],
                             expires_at=int(time.time()) + 60,
                             email_user="carol@x.tw", email_pass="pw2")
    monkeypatch.setattr(srv, "get_access_token", lambda: tok)
    assert srv._request_creds() == ("carol@x.tw", "pw2")


def test_email_send_remote_refuses_without_creds():
    """防禦性：遠端模式下沒有請求憑證時，email_send 不得落回伺服器身分。"""
    srv.MODE = "http"
    srv._REQ_CREDS.set(None)
    with pytest.raises(PermissionError):
        asyncio.run(srv._dispatch("email_send", {"to": "x@y.z", "subject": "s"}))


# ─── 遠端模式限制 ───────────────────────────────────────────────────────────
def test_email_configure_blocked_in_remote_mode():
    srv.MODE = "http"
    with pytest.raises(PermissionError):
        asyncio.run(srv._dispatch("email_configure", {"email_from": "x@y.z"}))


def test_list_tools_hides_configure_in_remote_mode():
    srv.MODE = "http"
    assert "email_configure" not in [t.name for t in asyncio.run(srv.list_tools())]
    srv.MODE = "stdio"
    assert "email_configure" in [t.name for t in asyncio.run(srv.list_tools())]


def test_path_attachment_blocked_in_remote_mode(tmp_path):
    f = tmp_path / "a.txt"
    f.write_text("hi")
    srv.MODE = "http"
    old = srv.CONFIG.attachment_allowed_dirs
    try:
        srv.CONFIG.attachment_allowed_dirs = []
        with pytest.raises(PermissionError):
            srv._resolve_attachment_path(str(f))
        # 管理員以白名單開放後才可用
        srv.CONFIG.attachment_allowed_dirs = [str(tmp_path.resolve())]
        assert srv._resolve_attachment_path(str(f)) == f.resolve()
    finally:
        srv.CONFIG.attachment_allowed_dirs = old


def test_attachment_whitelist_symlink_escape(tmp_path):
    """白名單目錄內的 symlink 指向名單外檔案：resolve 後必須被擋下。"""
    allowed = tmp_path / "allowed"
    outside = tmp_path / "outside"
    allowed.mkdir()
    outside.mkdir()
    secret = outside / "secret.txt"
    secret.write_text("leak")
    link = allowed / "innocent.txt"
    link.symlink_to(secret)
    srv.MODE = "http"
    old = srv.CONFIG.attachment_allowed_dirs
    try:
        srv.CONFIG.attachment_allowed_dirs = [str(allowed.resolve())]
        with pytest.raises(PermissionError):
            srv._resolve_attachment_path(str(link))
    finally:
        srv.CONFIG.attachment_allowed_dirs = old


def test_email_send_remote_uses_request_creds(monkeypatch):
    """遠端模式：SMTP 登入帳密與預設 From 都來自該請求的憑證。"""
    sent = {}

    def fake_send(cfg, msg, recipients):
        sent["cfg"], sent["msg"], sent["rcpt"] = cfg, msg, recipients
        return {"refused": {}}

    monkeypatch.setattr(srv, "_send_via_smtp", fake_send)
    srv.MODE = "http"
    srv._REQ_CREDS.set(("bob@corp.tw", "secret"))
    out = asyncio.run(srv._dispatch(
        "email_send", {"to": "x@y.z", "subject": "hi", "text": "t"}))
    assert out["ok"]
    assert sent["cfg"].username == "bob@corp.tw" and sent["cfg"].password == "secret"
    assert sent["msg"]["From"] == "bob@corp.tw"
    assert sent["rcpt"] == ["x@y.z"]


# ─── OAuth bridge：token 加解密與 provider ──────────────────────────────────
def test_token_crypto_roundtrip(tmp_path):
    key = oa.TokenCrypto.load_key(str(tmp_path / "k"))
    tc = oa.TokenCrypto(key)
    token = tc.seal({"u": "a", "p": "b"})
    assert token.startswith("me1.")
    assert tc.open(token) == {"u": "a", "p": "b"}
    assert tc.open(token[:-2] + "zz") is None   # 竄改 → 無效
    assert tc.open("bogus") is None             # 格式錯 → 無效
    # 金鑰持久化：同路徑再載入是同一把
    assert oa.TokenCrypto.load_key(str(tmp_path / "k")) == key


def test_provider_mint_and_load(tmp_path):
    provider, settings = oa.create(
        "https://mail-mcp.example.com/",
        key_path=str(tmp_path / "k"), clients_path=str(tmp_path / "c.json"))
    t = provider._mint("cid", [oa.SCOPE], "user@x.tw", "pw")

    at = asyncio.run(provider.load_access_token(t.access_token))
    assert at is not None
    assert (at.email_user, at.email_pass, at.client_id) == ("user@x.tw", "pw", "cid")
    # refresh token 不能當 access token 用
    assert asyncio.run(provider.load_access_token(t.refresh_token)) is None
    # 過期 token 失效
    expired = provider.crypto.seal(
        {"c": "cid", "s": [oa.SCOPE], "u": "u", "p": "p", "t": "a",
         "e": int(time.time()) - 10})
    assert asyncio.run(provider.load_access_token(expired)) is None
    # issuer 正規化（去尾斜線）
    assert str(settings.issuer_url).rstrip("/") == "https://mail-mcp.example.com"


def test_provider_login_throttle_and_fail_log(tmp_path, monkeypatch):
    """失敗節流：同 IP 達上限即擋；失敗會寫入 EMAIL_AUTH_LOG 固定格式。"""
    provider, _ = oa.create(
        "https://mail-mcp.example.com",
        key_path=str(tmp_path / "k"), clients_path=str(tmp_path / "c.json"))
    log_path = tmp_path / "auth.log"
    monkeypatch.setenv("EMAIL_AUTH_LOG", str(log_path))
    ip = "203.0.113.9"
    assert provider._throttled(ip) is False
    for _ in range(oa.FAIL_LIMIT_IP):
        provider._record_fail(ip)
    assert provider._throttled(ip) is True
    # 別的 IP 不受單一 IP 節流影響（未達全域上限時）
    assert provider._throttled("203.0.113.10") is False
    content = log_path.read_text()
    assert f"mcp-email-login-fail ip={ip}" in content
    assert content.count("\n") == oa.FAIL_LIMIT_IP


def test_provider_prune_expired_state(tmp_path):
    """_prune：過期的授權交易 / 授權碼 / done 記錄 / 失敗計數都要被清掉。"""
    provider, _ = oa.create(
        "https://mail-mcp.example.com",
        key_path=str(tmp_path / "k"), clients_path=str(tmp_path / "c.json"))
    past = time.time() - 10
    provider._pending["old"] = {"cid": "c", "params": None, "exp": past, "tries": 0}
    provider._codes["old"] = oa.CredAuthorizationCode(
        code="old", scopes=[oa.SCOPE], expires_at=past, client_id="c",
        code_challenge="x", redirect_uri="http://x/cb",
        redirect_uri_provided_explicitly=True)
    provider._done["old"] = past
    provider._fails["1.2.3.4"] = [past - oa.FAIL_WINDOW]
    provider._prune()
    assert not provider._pending and not provider._codes
    assert not provider._done and not provider._fails


def test_provider_max_clients_eviction(tmp_path, monkeypatch):
    """DCR 註冊超過 MAX_CLIENTS：淘汰最舊的 client，檔案持久化。"""
    from mcp.shared.auth import OAuthClientInformationFull
    monkeypatch.setattr(oa, "MAX_CLIENTS", 3)
    provider, _ = oa.create(
        "https://mail-mcp.example.com",
        key_path=str(tmp_path / "k"), clients_path=str(tmp_path / "c.json"))
    for i in range(4):
        info = OAuthClientInformationFull.model_validate(
            {"client_id": f"c{i}", "redirect_uris": ["http://x/cb"],
             "client_id_issued_at": 1000 + i})
        asyncio.run(provider.register_client(info))
    assert "c0" not in provider._clients          # 最舊的被淘汰
    assert set(provider._clients) == {"c1", "c2", "c3"}
    saved = json.loads((tmp_path / "c.json").read_text())
    assert set(saved) == {"c1", "c2", "c3"}


def test_provider_refresh_flow(tmp_path):
    provider, _ = oa.create(
        "https://mail-mcp.example.com",
        key_path=str(tmp_path / "k"), clients_path=str(tmp_path / "c.json"))
    t = provider._mint("cid", [oa.SCOPE], "user@x.tw", "pw")
    client = SimpleNamespace(client_id="cid")
    rt = asyncio.run(provider.load_refresh_token(client, t.refresh_token))
    assert rt is not None and rt.email_user == "user@x.tw"
    # 別的 client 拿不到
    other = SimpleNamespace(client_id="other")
    assert asyncio.run(provider.load_refresh_token(other, t.refresh_token)) is None
    # 換新 token 後仍帶原憑證
    t2 = asyncio.run(provider.exchange_refresh_token(client, rt, []))
    at2 = asyncio.run(provider.load_access_token(t2.access_token))
    assert at2.email_user == "user@x.tw" and at2.email_pass == "pw"


# ─── 同意畫面（consent screen）與 XSS escaping ──────────────────────────────
def _provider(tmp_path):
    provider, _ = oa.create(
        "https://mail-mcp.example.com",
        key_path=str(tmp_path / "k"), clients_path=str(tmp_path / "c.json"))
    return provider


def test_client_display_prefers_name_and_redirect_host(tmp_path):
    provider = _provider(tmp_path)
    provider._clients["cid1"] = {"client_id": "cid1", "client_name": "Claude"}
    entry = {"cid": "cid1",
             "params": SimpleNamespace(redirect_uri="https://claude.ai/api/mcp/cb")}
    name, host = provider._client_display(entry)
    assert name == "Claude" and host == "claude.ai"


def test_client_display_falls_back_to_client_id(tmp_path):
    provider = _provider(tmp_path)
    entry = {"cid": "unknown-cid", "params": SimpleNamespace(redirect_uri="")}
    name, host = provider._client_display(entry)
    assert name == "unknown-cid"


def test_login_html_renders_consent_banner(tmp_path):
    provider = _provider(tmp_path)
    page = provider._login_html("tok", client_name="Claude", redirect_host="claude.ai")
    assert "要求連接你的信箱" in page
    assert "Claude" in page and "claude.ai" in page
    assert "只有在你認得這個應用程式與目的地時才繼續" in page


def test_login_html_escapes_client_name_xss(tmp_path):
    """client_name 是註冊時外部輸入，必須 escape，不能形成 <script>。"""
    provider = _provider(tmp_path)
    payload = '<script>alert(1)</script>'
    page = provider._login_html("tok", client_name=payload,
                                redirect_host='evil"><img src=x onerror=alert(1)>')
    assert "<script>alert(1)</script>" not in page
    assert "&lt;script&gt;" in page
    assert "onerror=alert(1)>" not in page  # 屬性突破也被 escape


def test_login_html_no_consent_when_no_client_info(tmp_path):
    provider = _provider(tmp_path)
    page = provider._login_html("tok")
    assert 'class="consent"' not in page


def test_all_pages_share_style(tmp_path):
    """三頁都套用共用 _PAGE_STYLE（同一份 CSS，不再各自複製）。"""
    provider = _provider(tmp_path)
    provider.issuer = "https://mail-mcp.example.com"
    for page in (provider._login_html("t"), provider._completed_html(),
                 provider._index_html()):
        assert ".consent" in page and ".box" in page  # 共用樣式標記
        assert page.count("<style>") == 1


# ─── MCP prompts ────────────────────────────────────────────────────────────
def test_list_prompts():
    names = [p.name for p in asyncio.run(srv.list_prompts())]
    assert set(names) == {"triage_inbox", "weekly_cleanup", "draft_reply"}


def test_get_prompt_renders_arguments():
    r = asyncio.run(srv.get_prompt("triage_inbox", {"folder": "工作", "limit": "5"}))
    text = r.messages[0].content.text
    assert "工作" in text and "limit=5" in text
    assert r.messages[0].role == "user"


def test_get_prompt_defaults_when_arg_omitted():
    r = asyncio.run(srv.get_prompt("triage_inbox", {}))
    assert "INBOX" in r.messages[0].content.text  # 用預設值


def test_get_prompt_required_arg_enforced():
    with pytest.raises(ValueError):
        asyncio.run(srv.get_prompt("draft_reply", {}))  # 缺 uid


def test_get_prompt_unknown_name():
    with pytest.raises(ValueError):
        asyncio.run(srv.get_prompt("nope", {}))


# ─── HTTP 傳輸端對端（in-process uvicorn + mcp client）────────────────────
def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture()
def http_server():
    import uvicorn
    srv.MODE = "http"
    port = _free_port()
    fm = srv.build_remote_server("127.0.0.1", port)
    config = uvicorn.Config(fm.streamable_http_app(), host="127.0.0.1", port=port,
                            log_level="warning")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("uvicorn 啟動逾時")
    yield f"http://127.0.0.1:{port}/mcp"
    server.should_exit = True
    th.join(timeout=5)


async def _http_call(url: str, headers: dict, tool: str, args: dict):
    from mcp import ClientSession
    from mcp.client.streamable_http import streamablehttp_client

    async with streamablehttp_client(url, headers=headers) as (read, write, _):
        async with ClientSession(read, write) as sess:
            await sess.initialize()
            tools = await sess.list_tools()
            result = await sess.call_tool(tool, args)
            return [t.name for t in tools.tools], result


def test_http_transport_end_to_end(http_server):
    """帶 Basic 標頭連線：工具列表正確（無 email_configure）、呼叫可通。"""
    names, result = asyncio.run(_http_call(
        http_server, {"Authorization": _basic("alice@x.tw", "pw")},
        "email_test_connection", {"smtp": False, "imap": False}))
    assert "email_send" in names and "email_configure" not in names
    assert result.content[0].text == "{}"  # smtp/imap 都跳過 → 空結果，代表憑證解析沒炸


def test_http_transport_rejects_missing_auth(http_server):
    """不帶 Basic 標頭：工具呼叫應回錯誤（絕不回退到環境變數憑證）。"""
    _, result = asyncio.run(_http_call(
        http_server, {}, "email_test_connection", {"smtp": False, "imap": False}))
    assert result.content[0].text.startswith("❌")
    assert "Authorization" in result.content[0].text


def test_http_transport_exposes_prompts(http_server):
    """遠端模式也要透過 MCP client 曝露 prompts 並能 get_prompt。"""
    async def flow():
        from mcp import ClientSession
        from mcp.client.streamable_http import streamablehttp_client
        hdr = {"Authorization": _basic("alice@x.tw", "pw")}
        async with streamablehttp_client(http_server, headers=hdr) as (read, write, _):
            async with ClientSession(read, write) as sess:
                await sess.initialize()
                prompts = await sess.list_prompts()
                got = await sess.get_prompt("draft_reply", {"uid": "9", "folder": "INBOX"})
                return [p.name for p in prompts.prompts], got

    names, got = asyncio.run(flow())
    assert set(names) == {"triage_inbox", "weekly_cleanup", "draft_reply"}
    assert "uid=9" in got.messages[0].content.text


def test_http_passthrough_reaches_imap_login(http_server, monkeypatch):
    """全鏈路驗證：HTTP Basic 標頭的帳密要一路穿過 transport → contextvar
    → to_thread → _imap_cfg()，成為 IMAP 登入實際使用的憑證。"""
    import json as _json
    seen = {}

    class RecordingIMAP:
        def __init__(self, cfg):
            self.cfg = cfg

        def __enter__(self):
            seen["user"], seen["pwd"] = self.cfg.username, self.cfg.password

            class _Conn:
                def noop(self):
                    return ("OK", [b""])
            return _Conn()

        def __exit__(self, *exc):
            pass

    monkeypatch.setattr(srv, "IMAPClient", RecordingIMAP)
    _, result = asyncio.run(_http_call(
        http_server, {"Authorization": _basic("alice@x.tw", "s3cret")},
        "email_test_connection", {"smtp": False, "imap": True}))
    out = _json.loads(result.content[0].text)
    assert out["imap"]["ok"] is True
    assert seen == {"user": "alice@x.tw", "pwd": "s3cret"}


def test_http_send_e2e_with_smtp_auth(http_server):
    """全鏈路驗證（SMTP）：透過 HTTP 呼叫 email_send，帶 AUTH 的 in-process
    aiosmtpd 要收到 Basic 標頭那組帳密的登入，且信件內容正確、From 預設為該帳號。"""
    import dataclasses as _dc
    import json as _json
    from aiosmtpd.controller import Controller
    from aiosmtpd.smtp import AuthResult, LoginPassword

    envelopes = []
    seen_auth = {}

    class Handler:
        async def handle_DATA(self, server_, session, envelope):
            envelopes.append(envelope)
            return "250 Message accepted"

    def authenticator(server_, session, envelope, mechanism, auth_data):
        if isinstance(auth_data, LoginPassword):
            seen_auth["user"] = auth_data.login.decode()
            seen_auth["pwd"] = auth_data.password.decode()
        return AuthResult(success=True)

    port = _free_port()
    ctl = Controller(Handler(), hostname="127.0.0.1", port=port,
                     authenticator=authenticator, auth_require_tls=False)
    ctl.start()
    old_smtp = _dc.replace(srv.CONFIG.smtp)
    srv.CONFIG.smtp = _dc.replace(
        srv.CONFIG.smtp, host="127.0.0.1", port=port,
        use_tls=False, use_ssl=False)
    try:
        _, result = asyncio.run(_http_call(
            http_server, {"Authorization": _basic("bob@corp.tw", "topsecret")},
            "email_send", {"to": "x@y.z", "subject": "遠端測試", "text": "hi"}))
        out = _json.loads(result.content[0].text)
    finally:
        srv.CONFIG.smtp = old_smtp
        ctl.stop()
    assert out["ok"] is True
    assert seen_auth == {"user": "bob@corp.tw", "pwd": "topsecret"}
    assert len(envelopes) == 1
    assert envelopes[0].rcpt_tos == ["x@y.z"]
    assert "From: bob@corp.tw" in envelopes[0].content.decode()


# ─── OAuth 完整授權流程端對端（in-process uvicorn，IMAP 驗證 mock）────────
@pytest.fixture()
def oauth_server(tmp_path, monkeypatch):
    """--oauth 模式 server：金鑰/註冊檔在 tmp，IMAP 驗證 mock（密碼 'good' 才過）。"""
    import uvicorn

    real_create = oa.create
    monkeypatch.setattr(oa, "create", lambda issuer: real_create(
        issuer, key_path=str(tmp_path / "k"), clients_path=str(tmp_path / "c.json")))

    def fake_verify(user, pwd):
        if pwd != "good":
            raise RuntimeError("bad credentials")
    monkeypatch.setattr(oa, "_verify_imap_login", fake_verify)

    srv.MODE = "oauth"
    port = _free_port()
    issuer = f"http://127.0.0.1:{port}"
    fm = srv.build_remote_server("127.0.0.1", port, oauth=True, issuer=issuer)
    config = uvicorn.Config(fm.streamable_http_app(), host="127.0.0.1", port=port,
                            log_level="warning")
    server = uvicorn.Server(config)
    th = threading.Thread(target=server.run, daemon=True)
    th.start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.05)
    else:
        raise RuntimeError("uvicorn 啟動逾時")
    yield issuer
    server.should_exit = True
    th.join(timeout=5)


def test_oauth_full_flow_end_to_end(oauth_server, monkeypatch):
    """DCR → /authorize(PKCE) → /login（錯密碼 401、對密碼 302+code）→
    /token 換 token → Bearer 打 /mcp，且 token 解出的憑證真的成為 IMAP 登入帳密。"""
    import hashlib
    import secrets as _secrets
    from urllib.parse import parse_qs, urlparse

    import httpx

    base = oauth_server
    redirect = "http://localhost:19999/callback"

    def b64url(b: bytes) -> str:
        return base64.urlsafe_b64encode(b).decode().rstrip("=")

    async def flow():
        async with httpx.AsyncClient(follow_redirects=False, timeout=30) as c:
            meta = (await c.get(base + "/.well-known/oauth-authorization-server")).json()
            assert meta["issuer"].rstrip("/") == base

            reg = (await c.post(base + "/register", json={
                "client_name": "t", "redirect_uris": [redirect],
                "grant_types": ["authorization_code", "refresh_token"],
                "response_types": ["code"],
                "token_endpoint_auth_method": "client_secret_post"})).json()
            cid, csec = reg["client_id"], reg.get("client_secret", "")

            verifier = b64url(_secrets.token_bytes(32))
            challenge = b64url(hashlib.sha256(verifier.encode()).digest())
            r = await c.get(base + "/authorize", params={
                "client_id": cid, "response_type": "code", "redirect_uri": redirect,
                "code_challenge": challenge, "code_challenge_method": "S256",
                "state": "st8", "scope": oa.SCOPE})
            assert r.status_code in (302, 307)
            txn = parse_qs(urlparse(r.headers["location"]).query)["txn"][0]

            page = await c.get(base + "/login", params={"txn": txn})
            assert page.status_code == 200 and 'name="txn"' in page.text
            # 同意橫幅要顯示本次授權的 client 名稱與送達網域（防釣魚）
            assert "要求連接你的信箱" in page.text
            assert "localhost:19999" in page.text  # redirect_uri 的 host

            r = await c.post(base + "/login",
                             data={"txn": txn, "user": "dave@x.tw", "password": "bad"})
            assert r.status_code == 401  # 錯密碼：留在登入頁

            r = await c.post(base + "/login",
                             data={"txn": txn, "user": "dave@x.tw", "password": "good"})
            assert r.status_code == 302
            q = parse_qs(urlparse(r.headers["location"]).query)
            assert q["state"][0] == "st8"

            r = await c.post(base + "/token", data={
                "grant_type": "authorization_code", "code": q["code"][0],
                "redirect_uri": redirect, "client_id": cid,
                "client_secret": csec, "code_verifier": verifier})
            assert r.status_code == 200, r.text
            tok = r.json()

            # 亂 token 打 /mcp 應 401
            r = await c.post(base + "/mcp", headers={
                "Authorization": "Bearer me1.bogus",
                "content-type": "application/json",
                "accept": "application/json, text/event-stream"}, content=b"{}")
            assert r.status_code == 401
            return tok

    tok = asyncio.run(flow())

    # Bearer token 解出的憑證要成為 IMAP 登入實際使用的帳密（全鏈路）
    seen = {}

    class RecordingIMAP:
        def __init__(self, cfg):
            self.cfg = cfg

        def __enter__(self):
            seen["user"], seen["pwd"] = self.cfg.username, self.cfg.password

            class _Conn:
                def noop(self):
                    return ("OK", [b""])
            return _Conn()

        def __exit__(self, *exc):
            pass

    monkeypatch.setattr(srv, "IMAPClient", RecordingIMAP)
    _, result = asyncio.run(_http_call(
        base + "/mcp", {"Authorization": f"Bearer {tok['access_token']}"},
        "email_test_connection", {"smtp": False, "imap": True}))
    out = json.loads(result.content[0].text)
    assert out["imap"]["ok"] is True
    assert seen == {"user": "dave@x.tw", "pwd": "good"}
