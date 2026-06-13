"""
端對端測試：起一個本機 aiosmtpd 假 SMTP，呼叫 mcp_email 的 email_send 真寄信，驗收件人 / 內容 / 附件正確。
"""
from __future__ import annotations

import asyncio
import base64
import email
import sys
import tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).parent))

from aiosmtpd.controller import Controller
import mcp_email as srv


class CaptureHandler:
    """攔截所有送來的 SMTP message。"""

    def __init__(self) -> None:
        self.messages: list[email.message.Message] = []
        self.envelopes: list[dict] = []

    async def handle_DATA(self, server_, session, envelope):
        import email.policy
        msg = email.message_from_bytes(envelope.content, policy=email.policy.default)
        self.messages.append(msg)
        self.envelopes.append({
            "mail_from": envelope.mail_from,
            "rcpt_tos": envelope.rcpt_tos,
        })
        return "250 OK"


def _start_smtpd(port: int) -> tuple[Controller, CaptureHandler]:
    handler = CaptureHandler()
    controller = Controller(handler, hostname="127.0.0.1", port=port)
    controller.start()
    return controller, handler


async def _configure(port: int) -> None:
    await srv._dispatch("email_configure", {
        "smtp": {
            "host": "127.0.0.1",
            "port": port,
            "username": "",  # 本地測試不 auth
            "password": "",
            "use_tls": False,
            "use_ssl": False,
            "timeout": 10,
        },
        "email_from": "sender@example.com",
        "retry_max": 1,
    })


async def test_simple_text():
    controller, handler = _start_smtpd(8025)
    try:
        await _configure(8025)
        result = await srv._dispatch("email_send", {
            "to": "alice@example.com",
            "subject": "Hello",
            "text": "你好，這是純文字測試。",
        })
        assert result["ok"]
        assert handler.messages, "沒收到信"
        msg = handler.messages[0]
        assert msg["Subject"] == "Hello"
        assert msg["From"] == "sender@example.com"
        body = msg.get_body(preferencelist=("plain",))
        assert body and "純文字測試" in body.get_content()
        print("✅ test_simple_text PASS")
    finally:
        controller.stop()


async def test_html_and_multi_recipients():
    controller, handler = _start_smtpd(8026)
    try:
        await _configure(8026)
        result = await srv._dispatch("email_send", {
            "to": ["alice@example.com", "bob@example.com"],
            "cc": "carol@example.com, dave@example.com",
            "bcc": ["eve@example.com"],
            "subject": "HTML test",
            "text": "純文字 fallback",
            "html": "<h1>Hello</h1><p>HTML 內容</p>",
            "reply_to": "support@example.com",
        })
        assert result["ok"]
        assert len(result["to"]) == 2
        assert len(result["cc"]) == 2
        assert len(result["bcc"]) == 1

        env = handler.envelopes[0]
        # bcc 不該出現在 header 但要進 recipient list
        assert "eve@example.com" in env["rcpt_tos"]
        assert "carol@example.com" in env["rcpt_tos"]
        assert "alice@example.com" in env["rcpt_tos"]
        assert "bob@example.com" in env["rcpt_tos"]
        assert len(env["rcpt_tos"]) == 5

        msg = handler.messages[0]
        assert "Bcc" not in msg  # bcc 不該外漏
        assert msg["Reply-To"] == "support@example.com"
        assert msg["Cc"] == "carol@example.com, dave@example.com"

        # 找 HTML part
        html_part = msg.get_body(preferencelist=("html",))
        assert html_part and "<h1>Hello</h1>" in html_part.get_content()
        plain_part = msg.get_body(preferencelist=("plain",))
        assert plain_part and "純文字 fallback" in plain_part.get_content()
        print("✅ test_html_and_multi_recipients PASS")
    finally:
        controller.stop()


async def test_attachments_file_and_base64():
    controller, handler = _start_smtpd(8027)
    try:
        await _configure(8027)

        with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
            tmp.write("檔案附件內容\n".encode("utf-8"))
            file_path = tmp.name

        base64_content = base64.b64encode("base64 內容".encode("utf-8")).decode()

        result = await srv._dispatch("email_send", {
            "to": "alice@example.com",
            "subject": "with attachments",
            "text": "請收附件",
            "attachments": [
                {"path": file_path, "filename": "備註.txt"},
                {
                    "content_base64": base64_content,
                    "filename": "data.txt",
                    "mime_type": "text/plain",
                },
            ],
        })
        assert result["ok"]
        msg = handler.messages[0]

        atts = list(msg.iter_attachments())
        assert len(atts) == 2, f"附件數錯誤，預期 2，實際 {len(atts)}"
        names = {a.get_filename() for a in atts}
        assert "備註.txt" in names
        assert "data.txt" in names

        b64_part = next(a for a in atts if a.get_filename() == "data.txt")
        assert "base64 內容" in b64_part.get_content()
        print("✅ test_attachments_file_and_base64 PASS")
    finally:
        controller.stop()


async def test_retry_on_failure():
    """關掉 SMTP server 後 send 應該 retry，最終 fail with 明確 error。"""
    await _configure(8028)  # port 8028 沒人 listen
    try:
        await srv._dispatch("email_send", {
            "to": "alice@example.com",
            "subject": "fail",
            "text": "x",
        })
        assert False, "應該要 raise"
    except (OSError, ConnectionRefusedError, TimeoutError) as exc:
        print(f"✅ test_retry_on_failure PASS（捕到預期 error: {type(exc).__name__})")


async def test_configure_summary_no_password_leak():
    summary = await srv._dispatch("email_configure", {
        "smtp": {"host": "smtp.example.com", "port": 587, "username": "u", "password": "secret123"},
    })
    assert summary["smtp"]["host"] == "smtp.example.com"
    assert summary["smtp"]["password_set"] is True
    assert "password" not in summary["smtp"]  # 密碼絕對不能回傳
    print("✅ test_configure_summary_no_password_leak PASS")


async def test_env_alias_names():
    """IMAP_SERVER/USERNAME/PASSWORD 等別名應被 load_config_from_env 正確讀到。"""
    import os
    keys = ["IMAP_SERVER", "IMAP_USERNAME", "IMAP_PASSWORD", "IMAP_PORT",
            "SMTP_SERVER", "SMTP_USERNAME", "SMTP_PASSWORD"]
    saved = {k: os.environ.get(k) for k in keys}
    try:
        os.environ["IMAP_SERVER"] = "mail.example.com"
        os.environ["IMAP_USERNAME"] = "alias_user"
        os.environ["IMAP_PASSWORD"] = "alias_pass"
        os.environ["IMAP_PORT"] = "993"
        os.environ["SMTP_SERVER"] = "smtp.example.com"
        os.environ["SMTP_USERNAME"] = "smtp_user"
        os.environ["SMTP_PASSWORD"] = "smtp_pass"
        cfg = srv.load_config_from_env()
        assert cfg.imap.host == "mail.example.com", cfg.imap.host
        assert cfg.imap.username == "alias_user"
        assert cfg.imap.password == "alias_pass"
        assert cfg.smtp.host == "smtp.example.com"
        assert cfg.smtp.username == "smtp_user"
        assert cfg.smtp.password == "smtp_pass"
        print("✅ test_env_alias_names PASS")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def test_shared_credentials_one_set():
    """只設一組帳密（IMAP_*）時，SMTP 應自動沿用同一組；EMAIL_* 共用也該兩邊都吃。"""
    import os
    keys = ["IMAP_USERNAME", "IMAP_PASSWORD", "IMAP_SERVER", "SMTP_SERVER",
            "SMTP_USER", "SMTP_USERNAME", "SMTP_PASS", "SMTP_PASSWORD",
            "EMAIL_USER", "EMAIL_PASS", "IMAP_USER", "IMAP_PASS", "MAIL_USER", "MAIL_PASS"]
    saved = {k: os.environ.get(k) for k in keys}
    try:
        for k in keys:
            os.environ.pop(k, None)
        # 只設 IMAP 帳密 + 兩邊 host（帳密刻意不重複設）
        os.environ["IMAP_SERVER"] = "mail.example.com"
        os.environ["SMTP_SERVER"] = "mail.example.com"
        os.environ["IMAP_USERNAME"] = "wilber"
        os.environ["IMAP_PASSWORD"] = "pw"
        cfg = srv.load_config_from_env()
        assert cfg.imap.username == "wilber" and cfg.imap.password == "pw"
        assert cfg.smtp.username == "wilber", f"SMTP 沒沿用 IMAP 帳號：{cfg.smtp.username}"
        assert cfg.smtp.password == "pw", "SMTP 沒沿用 IMAP 密碼"

        # 改用共用 EMAIL_* 一次設定，兩邊都該吃到
        for k in ("IMAP_USERNAME", "IMAP_PASSWORD"):
            os.environ.pop(k, None)
        os.environ["EMAIL_USER"] = "shared@example.com"
        os.environ["EMAIL_PASS"] = "shared_pw"
        cfg = srv.load_config_from_env()
        assert cfg.imap.username == "shared@example.com" and cfg.smtp.username == "shared@example.com"
        assert cfg.imap.password == "shared_pw" and cfg.smtp.password == "shared_pw"
        print("✅ test_shared_credentials_one_set PASS")
    finally:
        for k, v in saved.items():
            if v is None:
                os.environ.pop(k, None)
            else:
                os.environ[k] = v


async def test_test_connection_smtp_ok():
    controller, _ = _start_smtpd(8029)
    try:
        await _configure(8029)
        out = await srv._dispatch("email_test_connection", {"smtp": True, "imap": False})
        assert out["smtp"]["ok"]
        print("✅ test_test_connection_smtp_ok PASS")
    finally:
        controller.stop()


# ─── 錯誤路徑測試（不需任何 server）────────────────────────────────────────
async def test_send_no_sender_error():
    """沒有寄件人時應 raise RuntimeError。"""
    saved_from = srv.CONFIG.email_from
    saved_user = srv.CONFIG.smtp.username
    try:
        srv.CONFIG.email_from = ""
        srv.CONFIG.smtp.username = ""
        try:
            await srv._dispatch("email_send", {"to": "a@example.com", "subject": "x"})
            assert False, "應該要 raise"
        except RuntimeError as exc:
            assert "寄件人" in str(exc)
            print("✅ test_send_no_sender_error PASS")
    finally:
        srv.CONFIG.email_from = saved_from
        srv.CONFIG.smtp.username = saved_user


async def test_send_no_recipient_error():
    """to 解析後為空應 raise ValueError。"""
    srv.CONFIG.email_from = "sender@example.com"
    try:
        await srv._dispatch("email_send", {"to": "", "subject": "x", "text": "y"})
        assert False, "應該要 raise"
    except ValueError as exc:
        assert "收件人" in str(exc)
        print("✅ test_send_no_recipient_error PASS")


async def test_attachment_not_found_error():
    """附件路徑不存在應 raise FileNotFoundError（在 SMTP 連線前）。"""
    try:
        srv._build_message(
            sender="sender@example.com",
            to=["a@example.com"], cc=[], bcc=[],
            subject="x", text="y", html=None, reply_to=None, headers=None,
            attachments=[{"path": "/no/such/file/definitely_missing.txt"}],
        )
        assert False, "應該要 raise"
    except FileNotFoundError:
        print("✅ test_attachment_not_found_error PASS")


async def test_bad_base64_error():
    """壞掉的 base64 應 raise ValueError。"""
    try:
        srv._build_message(
            sender="sender@example.com",
            to=["a@example.com"], cc=[], bcc=[],
            subject="x", text="y", html=None, reply_to=None, headers=None,
            attachments=[{"content_base64": "!!!not-valid-base64!!!", "filename": "x.bin"}],
        )
        assert False, "應該要 raise"
    except ValueError as exc:
        assert "base64" in str(exc)
        print("✅ test_bad_base64_error PASS")


async def test_html_only_adds_text_fallback():
    """只給 html 時應自動補純文字 fallback。"""
    msg = srv._build_message(
        sender="sender@example.com",
        to=["a@example.com"], cc=[], bcc=[],
        subject="x", text=None, html="<h1>hi</h1>", reply_to=None, headers=None,
        attachments=None,
    )
    assert msg.get_body(preferencelist=("plain",)) is not None
    assert msg.get_body(preferencelist=("html",)) is not None
    print("✅ test_html_only_adds_text_fallback PASS")


# ─── 附件白名單測試 ─────────────────────────────────────────────────────────
async def test_attachment_whitelist_blocks_outside():
    """白名單啟用後，名單外的檔案應被 PermissionError 擋下。"""
    import os
    allowed_dir = tempfile.mkdtemp()
    with tempfile.NamedTemporaryFile(suffix=".txt", delete=False) as tmp:
        tmp.write(b"secret")
        outside_path = tmp.name  # 在系統 tmp，不在 allowed_dir
    saved = srv.CONFIG.attachment_allowed_dirs
    try:
        srv.CONFIG.attachment_allowed_dirs = [str(Path(allowed_dir).resolve())]
        try:
            srv._build_message(
                sender="sender@example.com",
                to=["a@example.com"], cc=[], bcc=[],
                subject="x", text="y", html=None, reply_to=None, headers=None,
                attachments=[{"path": outside_path}],
            )
            assert False, "應該要 raise PermissionError"
        except PermissionError:
            print("✅ test_attachment_whitelist_blocks_outside PASS")
    finally:
        srv.CONFIG.attachment_allowed_dirs = saved
        os.unlink(outside_path)


async def test_attachment_whitelist_allows_inside():
    """白名單內的檔案應正常附加。"""
    allowed_dir = tempfile.mkdtemp()
    inside_path = Path(allowed_dir) / "ok.txt"
    inside_path.write_text("allowed content", encoding="utf-8")
    saved = srv.CONFIG.attachment_allowed_dirs
    try:
        srv.CONFIG.attachment_allowed_dirs = [str(Path(allowed_dir).resolve())]
        msg = srv._build_message(
            sender="sender@example.com",
            to=["a@example.com"], cc=[], bcc=[],
            subject="x", text="y", html=None, reply_to=None, headers=None,
            attachments=[{"path": str(inside_path)}],
        )
        atts = list(msg.iter_attachments())
        assert len(atts) == 1 and atts[0].get_filename() == "ok.txt"
        print("✅ test_attachment_whitelist_allows_inside PASS")
    finally:
        srv.CONFIG.attachment_allowed_dirs = saved


# ─── IMAP 測試（用 FakeIMAP 取代真連線）─────────────────────────────────────
def _unq(s):
    """模擬 server 端解開 IMAP quoted-string（去引號 + 反跳脫）。"""
    if isinstance(s, str) and len(s) >= 2 and s[0] == '"' and s[-1] == '"':
        return s[1:-1].replace('\\"', '"').replace("\\\\", "\\")
    return s


class _FakeIMAPConn:
    """模擬 imaplib.IMAP4 的最小子集，回傳 imaplib 風格的 tuple。

    可參數化：
    - capabilities：模擬 server 能力（含/不含 UIDPLUS）。
    - meta_order："leading" 把 UID/FLAGS 放在 BODY literal 之前（多數 server）；
                  "trailing" 放在 literal「之後」的獨立 bytes 元素裡（合法但較少見），
                  用來驗證 parser 不依賴回應項目順序。
    """

    def __init__(self, store, capabilities=("IMAP4REV1", "UIDPLUS"), meta_order="leading"):
        self._store = store  # dict: uid(str) -> raw bytes
        self.flags = {uid: set() for uid in store}
        self.expunged = []
        self.capabilities = capabilities
        self.meta_order = meta_order
        self.folders = {"INBOX", "Sent"}  # server 上已存在的 mailbox（encoded 名稱）
        self.moved = []                    # [(uid, dest_encoded)] 給測試驗證

    def select(self, folder, readonly=True):
        return "OK", [str(len(self._store)).encode()]

    def search(self, charset, *criteria):
        # 回傳 sequence numbers 1..N
        seqs = " ".join(str(i + 1) for i in range(len(self._store)))
        return "OK", [seqs.encode()]

    def fetch(self, mid, spec):
        # mid 是 sequence number；對應到第 mid 封（1-based）
        idx = int(mid) - 1
        uid = list(self._store.keys())[idx]
        raw = self._store[uid]
        # 只回 header 區塊給 list_messages
        header = raw.split(b"\r\n\r\n", 1)[0] + b"\r\n\r\n"
        flagstr = " ".join(self.flags[uid]) or "\\Seen"
        if self.meta_order == "trailing":
            # UID / FLAGS 出現在 literal 之後的獨立元素
            lead = f"{mid} (BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)] {{{len(header)}}}".encode()
            trail = f" FLAGS ({flagstr}) UID {uid})".encode()
            return "OK", [(lead, header), trail]
        lead = f'{mid} (UID {uid} FLAGS ({flagstr}) BODY[HEADER.FIELDS (FROM TO SUBJECT DATE)] {{{len(header)}}}'.encode()
        return "OK", [(lead, header), b")"]

    def uid(self, command, uid, *rest):
        cmd = command.lower()
        if cmd == "fetch":
            raw = self._store[uid]
            meta = f"1 (UID {uid} BODY[] {{{len(raw)}}}".encode()
            return "OK", [(meta, raw), b")"]
        if cmd == "store":
            action, flagspec = rest[0], rest[1]
            flag = flagspec.strip("()")
            if action.startswith("+"):
                self.flags.setdefault(uid, set()).add(flag)
            else:
                self.flags.setdefault(uid, set()).discard(flag)
            return "OK", [b"stored"]
        if cmd == "expunge":
            # UID EXPUNGE：只清掉 uidset 內、且已標 \Deleted 的訊息
            target = set(uid.split(","))
            done = [u for u in target if "\\Deleted" in self.flags.get(u, set())]
            self.expunged.extend(done)
            return "OK", [str(len(done)).encode()]
        if cmd == "move":
            if uid not in self._store:
                return "NO", [b"no such uid"]
            self.moved.append((uid, _unq(rest[0])))
            self._store.pop(uid, None)
            self.flags.pop(uid, None)
            return "OK", [b"moved"]
        if cmd == "copy":
            if uid not in self._store:
                return "NO", [b"no such uid"]
            self.moved.append((uid, _unq(rest[0])))
            return "OK", [b"copied"]
        return "NO", [b"unknown"]

    def list(self, directory=None, pattern=None):
        names = sorted(self.folders)
        if pattern is not None:  # 存在性查詢（精確比對 encoded 名稱）
            pat = _unq(pattern)
            names = [n for n in names if n == pat]
        return "OK", [f'(\\HasNoChildren) "/" "{n}"'.encode() for n in names]

    def create(self, name):
        name = _unq(name)
        if name in self.folders:
            return "NO", [b"[ALREADYEXISTS] Mailbox already exists"]
        self.folders.add(name)
        return "OK", [b"created"]

    def subscribe(self, name):
        return "OK", [b"subscribed"]

    def expunge(self):
        deleted = [u for u, fl in self.flags.items() if "\\Deleted" in fl]
        self.expunged.extend(deleted)
        return "OK", [str(len(deleted)).encode()]

    def noop(self):
        return "OK", [b"NOOP completed"]

    def logout(self):
        return "OK", [b"BYE"]


class _FakeIMAPClient:
    """取代 srv.IMAPClient 的 context manager。

    _factory 決定每次 with 建立的 fake conn（測試可換掉以模擬不同 server）。
    """
    _last = None
    _factory = staticmethod(lambda: _build_fake_store())

    def __init__(self, cfg):
        self.conn = _FakeIMAPClient._factory()
        _FakeIMAPClient._last = self.conn

    def __enter__(self):
        return self.conn

    def __exit__(self, *exc):
        return False


def _build_fake_store(capabilities=("IMAP4REV1", "UIDPLUS"), meta_order="leading"):
    import email.policy
    from email.message import EmailMessage

    # msg uid=101：multipart text+html+附件
    m1 = EmailMessage(policy=email.policy.SMTP)
    m1["From"] = "Alice <alice@example.com>"
    m1["To"] = "me@example.com"
    m1["Subject"] = "測試主旨"
    m1["Date"] = "Mon, 12 Jun 2026 10:00:00 +0800"
    m1["Message-ID"] = "<msg101@example.com>"
    m1.set_content("純文字 body")
    m1.add_alternative("<p>HTML body</p>", subtype="html")
    m1.add_attachment(b"file data", maintype="application", subtype="octet-stream", filename="doc.bin")

    m2 = EmailMessage(policy=email.policy.SMTP)
    m2["From"] = "Bob <bob@example.com>"
    m2["To"] = "me@example.com"
    m2["Subject"] = "第二封"
    m2["Date"] = "Tue, 13 Jun 2026 11:00:00 +0800"
    m2.set_content("只有純文字")

    store = {
        "101": m1.as_bytes(policy=email.policy.SMTP),
        "102": m2.as_bytes(policy=email.policy.SMTP),
    }
    return _FakeIMAPConn(store, capabilities=capabilities, meta_order=meta_order)


async def _with_fake_imap(coro_fn, factory=None):
    saved = srv.IMAPClient
    saved_factory = _FakeIMAPClient._factory
    srv.IMAPClient = _FakeIMAPClient
    if factory is not None:
        _FakeIMAPClient._factory = staticmethod(factory)
    try:
        return await coro_fn()
    finally:
        srv.IMAPClient = saved
        _FakeIMAPClient._factory = saved_factory


async def test_imap_list_folders():
    async def run():
        out = await srv._dispatch("email_list_folders", {})
        assert out["count"] == 2
        assert "INBOX" in out["folders"]
        assert "Sent" in out["folders"]
    await _with_fake_imap(run)
    print("✅ test_imap_list_folders PASS")


async def test_imap_list_messages():
    async def run():
        out = await srv._dispatch("email_list_messages", {"folder": "INBOX", "limit": 10})
        assert out["count"] == 2, out
        msgs = out["messages"]
        # 最新優先（reversed），uid 應被正確解析出來
        uids = {m["uid"] for m in msgs}
        assert uids == {"101", "102"}, uids
        subjects = {m["subject"] for m in msgs}
        assert "測試主旨" in subjects
        assert all(m["uid"] is not None for m in msgs)
    await _with_fake_imap(run)
    print("✅ test_imap_list_messages PASS")


async def test_imap_get_message():
    async def run():
        out = await srv._dispatch("email_get_message", {"folder": "INBOX", "uid": "101"})
        assert out["uid"] == "101"
        assert "alice@example.com" in out["from"]
        assert out["subject"] == "測試主旨"
        assert "純文字 body" in out["body_text"]
        assert "HTML body" in out["body_html"]
        assert len(out["attachments"]) == 1
        assert out["attachments"][0]["filename"] == "doc.bin"
    await _with_fake_imap(run)
    print("✅ test_imap_get_message PASS")


async def test_imap_mark():
    async def run():
        out = await srv._dispatch("email_mark", {
            "folder": "INBOX", "uids": ["101", "102"], "flag": "\\Flagged", "add": True,
        })
        assert out["updated"] == 2
        assert out["failed"] == []
        # 確認 flag 真的進去了
        assert "\\Flagged" in _FakeIMAPClient._last.flags["101"]
    await _with_fake_imap(run)
    print("✅ test_imap_mark PASS")


async def test_imap_delete():
    """支援 UIDPLUS 的 server 應走 UID EXPUNGE，只清掉指定 uid。"""
    async def run():
        out = await srv._dispatch("email_delete", {"folder": "INBOX", "uids": ["102"]})
        assert out["deleted"] == 1
        assert out["failed"] == []
        assert out["method"] == "UID EXPUNGE", out
        assert _FakeIMAPClient._last.expunged == ["102"], _FakeIMAPClient._last.expunged
    await _with_fake_imap(run)
    print("✅ test_imap_delete PASS")


async def test_imap_delete_fallback_without_uidplus():
    """server 不支援 UIDPLUS 時，應 fallback 到一般 EXPUNGE。"""
    async def run():
        out = await srv._dispatch("email_delete", {"folder": "INBOX", "uids": ["102"]})
        assert out["deleted"] == 1
        assert out["method"] == "EXPUNGE", out
        assert "102" in _FakeIMAPClient._last.expunged
    await _with_fake_imap(run, factory=lambda: _build_fake_store(capabilities=("IMAP4REV1",)))
    print("✅ test_imap_delete_fallback_without_uidplus PASS")


async def test_imap_list_messages_trailing_meta():
    """FLAGS/UID 出現在 BODY literal 之後時，parser 仍能正確取出 uid。"""
    async def run():
        out = await srv._dispatch("email_list_messages", {"folder": "INBOX", "limit": 10})
        uids = {m["uid"] for m in out["messages"]}
        assert uids == {"101", "102"}, uids
        assert all(m["uid"] is not None for m in out["messages"])
    await _with_fake_imap(run, factory=lambda: _build_fake_store(meta_order="trailing"))
    print("✅ test_imap_list_messages_trailing_meta PASS")


async def test_imap_test_connection_ok():
    async def run():
        out = await srv._dispatch("email_test_connection", {"smtp": False, "imap": True})
        assert out["imap"]["ok"], out
    await _with_fake_imap(run)
    print("✅ test_imap_test_connection_ok PASS")


# ─── modified UTF-7（資料夾名稱編碼）───────────────────────────────────────
async def test_utf7_folder_name():
    assert srv._utf7_encode("日本語") == "&ZeVnLIqe-"      # 已知向量
    for s in ["INBOX", "BizForm Testing", "測試機錯誤信", "a&b", "混合 mix"]:
        assert srv._utf7_decode(srv._utf7_encode(s)) == s, s
    print("✅ test_utf7_folder_name PASS")


async def test_imap_mailbox_quoting():
    """含空格 / 引號 / 中文的資料夾名稱都要正確包成 quoted-string（imaplib 不會自動加）。"""
    assert srv._imap_mailbox("BizForm Testing") == '"BizForm Testing"'   # 空格需引號
    # 中文：引號包住 UTF-7 編碼後的字串
    assert srv._imap_mailbox("測試機錯誤信") == '"' + srv._utf7_encode("測試機錯誤信") + '"'
    assert "&" in srv._imap_mailbox("測試機錯誤信")                       # 確實有編碼
    assert srv._imap_mailbox('a"b\\c') == '"a\\"b\\\\c"'                 # 跳脫 " 與 \
    print("✅ test_imap_mailbox_quoting PASS")


# ─── 新 tools：create_folder / move_messages / apply_rules ─────────────────
async def test_create_folder():
    async def run():
        out = await srv._dispatch("email_create_folder", {"folder": "BizForm Testing"})
        assert out["created"] is True and out["already_exists"] is False, out
        # 已存在時不報錯
        out2 = await srv._dispatch("email_create_folder", {"folder": "INBOX"})
        assert out2["already_exists"] is True, out2
    await _with_fake_imap(run)
    print("✅ test_create_folder PASS")


async def test_move_messages_uidplus_fallback():
    """無 MOVE capability → COPY + UID EXPUNGE fallback。"""
    async def run():
        await srv._dispatch("email_create_folder", {"folder": "BizForm Testing"})
        out = await srv._dispatch("email_move_messages", {
            "source_folder": "INBOX", "uids": ["101"], "destination_folder": "BizForm Testing",
        })
        assert out["moved"] == 1 and out["failed"] == [], out
        assert out["method"].startswith("COPY"), out
        assert "101" in _FakeIMAPClient._last.expunged
    conn = _build_fake_store(capabilities=("IMAP4REV1", "UIDPLUS"))
    await _with_fake_imap(run, factory=lambda: conn)  # 同測試內共用一個 conn（保留資料夾狀態）
    print("✅ test_move_messages_uidplus_fallback PASS")


async def test_move_messages_uid_move():
    """有 MOVE capability → 直接 UID MOVE。"""
    async def run():
        await srv._dispatch("email_create_folder", {"folder": "Archive"})
        out = await srv._dispatch("email_move_messages", {
            "source_folder": "INBOX", "uids": ["101", "102"], "destination_folder": "Archive",
        })
        assert out["moved"] == 2 and out["method"] == "UID MOVE", out
        assert len(_FakeIMAPClient._last.moved) == 2
    conn = _build_fake_store(capabilities=("IMAP4REV1", "MOVE", "UIDPLUS"))
    await _with_fake_imap(run, factory=lambda: conn)
    print("✅ test_move_messages_uid_move PASS")


async def test_move_to_missing_folder_errors():
    """目的地不存在應 raise（不自動建立）。"""
    async def run():
        try:
            await srv._dispatch("email_move_messages", {
                "source_folder": "INBOX", "uids": ["101"], "destination_folder": "Nope 不存在",
            })
            assert False, "應該要 raise"
        except RuntimeError as exc:
            assert "不存在" in str(exc)
    await _with_fake_imap(run)
    print("✅ test_move_to_missing_folder_errors PASS")


async def test_apply_rules_dry_run_and_execute():
    """dry_run 預覽命中、不動作；正式執行才搬信。"""
    async def run():
        await srv._dispatch("email_create_folder", {"folder": "BizForm Testing"})
        rule = {
            "name": "alice→BizForm",
            "from_contains": "ALICE@example.com",   # 大小寫不敏感
            "subject_contains_all": ["測試主旨"],
            "action": {"move_to": "BizForm Testing"},
        }
        # dry_run：命中 uid=101，但不搬
        dry = await srv._dispatch("email_apply_rules", {
            "folder": "INBOX", "rules": [rule], "dry_run": True,
        })
        assert dry["matched"] == 1 and dry["dry_run"] is True, dry
        assert dry["preview"][0]["uid"] == "101"
        assert "executed" not in dry
        assert _FakeIMAPClient._last.moved == []  # 還沒搬

        # 正式執行
        real = await srv._dispatch("email_apply_rules", {
            "folder": "INBOX", "rules": [rule], "dry_run": False,
        })
        assert real["executed"]["moved"] == 1, real
        assert any(uid == "101" for uid, _ in _FakeIMAPClient._last.moved)
    conn = _build_fake_store()
    await _with_fake_imap(run, factory=lambda: conn)
    print("✅ test_apply_rules_dry_run_and_execute PASS")


async def test_apply_rules_empty_rule_no_match():
    """無條件的空規則不應命中任何信（安全）。"""
    async def run():
        out = await srv._dispatch("email_apply_rules", {
            "folder": "INBOX", "rules": [{"name": "empty", "action": {"delete": True}}],
            "dry_run": True,
        })
        assert out["matched"] == 0, out
    await _with_fake_imap(run)
    print("✅ test_apply_rules_empty_rule_no_match PASS")


async def test_apply_rules_regex_and_case_sensitive():
    """match=regex 比對；case_sensitive=True 時大小寫需吻合。"""
    async def run():
        # regex：subject 結尾為「主旨」的（uid 101 = 測試主旨）
        out = await srv._dispatch("email_apply_rules", {
            "folder": "INBOX", "dry_run": True,
            "rules": [{"name": "re", "match": "regex", "subject_contains_all": ["主旨$"],
                       "action": {"delete": True}}],
        })
        assert out["matched"] == 1 and out["preview"][0]["uid"] == "101", out

        # case_sensitive：大寫 ALICE 不該命中小寫的 from
        out2 = await srv._dispatch("email_apply_rules", {
            "folder": "INBOX", "dry_run": True, "case_sensitive": True,
            "rules": [{"name": "cs", "from_contains": "ALICE", "action": {"delete": True}}],
        })
        assert out2["matched"] == 0, out2
    await _with_fake_imap(run)
    print("✅ test_apply_rules_regex_and_case_sensitive PASS")


async def test_apply_rules_exact_and_match_mode_all():
    """match=exact 完全相等；match_mode=all 一封信可被多條規則命中。"""
    async def run():
        # exact：subject 完全等於「第二封」(uid 102)
        out = await srv._dispatch("email_apply_rules", {
            "folder": "INBOX", "dry_run": True,
            "rules": [{"name": "ex", "match": "exact", "subject_contains_all": ["第二封"],
                       "action": {"mark": "\\Flagged"}}],
        })
        assert out["matched"] == 1 and out["preview"][0]["uid"] == "102", out

        # match_mode=all：兩條都命中 uid 101 → matched(hit 數)=2、但 matched_messages=1
        allout = await srv._dispatch("email_apply_rules", {
            "folder": "INBOX", "dry_run": True, "match_mode": "all",
            "rules": [
                {"name": "byfrom", "from_contains": "alice", "action": {"mark": "\\Flagged"}},
                {"name": "bysubj", "subject_contains_all": ["測試主旨"], "action": {"delete": True}},
            ],
        })
        assert allout["matched"] == 2 and allout["matched_messages"] == 1, allout
    await _with_fake_imap(run)
    print("✅ test_apply_rules_exact_and_match_mode_all PASS")


async def test_apply_rules_execute_mark():
    """執行 mark 動作：flag 應真的寫入。"""
    async def run():
        out = await srv._dispatch("email_apply_rules", {
            "folder": "INBOX", "dry_run": False,
            "rules": [{"name": "flag-bob", "from_contains": "bob",
                       "action": {"mark": "\\Flagged", "add": True}}],
        })
        assert out["executed"]["marked"] == 1, out
        assert "\\Flagged" in _FakeIMAPClient._last.flags.get("102", set())
    conn = _build_fake_store()
    await _with_fake_imap(run, factory=lambda: conn)
    print("✅ test_apply_rules_execute_mark PASS")


async def test_apply_rules_execute_delete():
    """執行 delete 動作：應標 \\Deleted 並 expunge。"""
    async def run():
        out = await srv._dispatch("email_apply_rules", {
            "folder": "INBOX", "dry_run": False,
            "rules": [{"name": "del-bob", "from_contains": "bob", "action": {"delete": True}}],
        })
        assert out["executed"]["deleted"] == 1, out
        assert "102" in _FakeIMAPClient._last.expunged
    conn = _build_fake_store()
    await _with_fake_imap(run, factory=lambda: conn)
    print("✅ test_apply_rules_execute_delete PASS")


async def test_apply_rules_all_execute_precedence():
    """match_mode=all 執行：同一封同時命中 move 與 delete 規則 → move 優先，不刪。"""
    async def run():
        await srv._dispatch("email_create_folder", {"folder": "Archive"})
        out = await srv._dispatch("email_apply_rules", {
            "folder": "INBOX", "dry_run": False, "match_mode": "all",
            "rules": [
                {"name": "mv", "from_contains": "alice", "action": {"move_to": "Archive"}},
                {"name": "del", "subject_contains_all": ["測試主旨"], "action": {"delete": True}},
            ],
        })
        assert out["executed"]["moved"] == 1, out
        assert out["executed"]["deleted"] == 0, out  # move 優先，不該刪
        assert any(uid == "101" for uid, _ in _FakeIMAPClient._last.moved)
    conn = _build_fake_store(capabilities=("IMAP4REV1", "MOVE", "UIDPLUS"))
    await _with_fake_imap(run, factory=lambda: conn)
    print("✅ test_apply_rules_all_execute_precedence PASS")


async def test_apply_rules_subject_contains_any():
    async def run():
        out = await srv._dispatch("email_apply_rules", {
            "folder": "INBOX", "dry_run": True,
            "rules": [{"name": "any", "subject_contains_any": ["不存在", "測試主旨"],
                       "action": {"delete": True}}],
        })
        assert out["matched"] == 1 and out["preview"][0]["uid"] == "101", out
    await _with_fake_imap(run)
    print("✅ test_apply_rules_subject_contains_any PASS")


async def test_move_partial_failure():
    """部分 uid 不存在 → 進 failed 清單，不影響其他。"""
    async def run():
        await srv._dispatch("email_create_folder", {"folder": "Archive"})
        out = await srv._dispatch("email_move_messages", {
            "source_folder": "INBOX", "uids": ["101", "999"], "destination_folder": "Archive",
        })
        assert out["moved"] == 1 and out["failed"] == ["999"], out
    conn = _build_fake_store(capabilities=("IMAP4REV1", "UIDPLUS"))
    await _with_fake_imap(run, factory=lambda: conn)
    print("✅ test_move_partial_failure PASS")


async def test_create_folder_chinese():
    """中文資料夾名稱：建立後存在性檢查（modified UTF-7）應一致。"""
    async def run():
        out = await srv._dispatch("email_create_folder", {"folder": "測試資料夾"})
        assert out["created"] is True, out
        out2 = await srv._dispatch("email_create_folder", {"folder": "測試資料夾"})
        assert out2["already_exists"] is True, out2
    conn = _build_fake_store()
    await _with_fake_imap(run, factory=lambda: conn)
    print("✅ test_create_folder_chinese PASS")


async def main():
    tests = [
        test_simple_text,
        test_html_and_multi_recipients,
        test_attachments_file_and_base64,
        test_retry_on_failure,
        test_configure_summary_no_password_leak,
        test_env_alias_names,
        test_shared_credentials_one_set,
        test_test_connection_smtp_ok,
        # 錯誤路徑
        test_send_no_sender_error,
        test_send_no_recipient_error,
        test_attachment_not_found_error,
        test_bad_base64_error,
        test_html_only_adds_text_fallback,
        # 附件白名單
        test_attachment_whitelist_blocks_outside,
        test_attachment_whitelist_allows_inside,
        # IMAP（讀信路徑）
        test_imap_list_folders,
        test_imap_list_messages,
        test_imap_get_message,
        test_imap_mark,
        test_imap_delete,
        test_imap_delete_fallback_without_uidplus,
        test_imap_list_messages_trailing_meta,
        test_imap_test_connection_ok,
        # 資料夾名稱編碼 + 新 tools
        test_utf7_folder_name,
        test_imap_mailbox_quoting,
        test_create_folder,
        test_move_messages_uidplus_fallback,
        test_move_messages_uid_move,
        test_move_to_missing_folder_errors,
        test_apply_rules_dry_run_and_execute,
        test_apply_rules_empty_rule_no_match,
        test_apply_rules_regex_and_case_sensitive,
        test_apply_rules_exact_and_match_mode_all,
        test_apply_rules_execute_mark,
        test_apply_rules_execute_delete,
        test_apply_rules_all_execute_precedence,
        test_apply_rules_subject_contains_any,
        test_move_partial_failure,
        test_create_folder_chinese,
    ]
    passed = 0
    for t in tests:
        try:
            await t()
            passed += 1
        except AssertionError as e:
            print(f"❌ {t.__name__} FAIL: {e}")
        except Exception as e:
            import traceback; traceback.print_exc()
            print(f"❌ {t.__name__} ERROR: {e}")
    print()
    print(f"=== {passed}/{len(tests)} passed ===")
    sys.exit(0 if passed == len(tests) else 1)


if __name__ == "__main__":
    asyncio.run(main())
