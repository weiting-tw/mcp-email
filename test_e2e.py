"""
端對端測試：起一個本機 aiosmtpd 假 SMTP，呼叫 server.py 的 email_send 真寄信，驗收件人 / 內容 / 附件正確。
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
import server as srv


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
        return "NO", [b"unknown"]

    def list(self):
        return "OK", [
            b'(\\HasNoChildren) "/" "INBOX"',
            b'(\\HasNoChildren) "/" "Sent"',
        ]

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


async def main():
    tests = [
        test_simple_text,
        test_html_and_multi_recipients,
        test_attachments_file_and_base64,
        test_retry_on_failure,
        test_configure_summary_no_password_leak,
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
