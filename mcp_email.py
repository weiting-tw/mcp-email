#!/usr/bin/env python3
"""
本地 IMAP/SMTP 信箱 MCP server。

特色：
  📤 SMTP 寄信：HTML / 純文字、可混合 multipart
  👥 多收件人：to / cc / bcc
  📎 附件：本機檔案路徑 或 base64 直餵內容
  🔧 動態設定：runtime 透過 email_configure 換 SMTP / IMAP 帳號
  🔍 連線測試：email_test_connection 一鍵測 SMTP + IMAP
  ⚡ 高效能：可調 timeout + 自動 retry（exponential backoff）
  📬 IMAP 讀信：list folders / list messages / get message / search / mark / delete
  🗂️ IMAP 整理：create folder（中文名 UTF-7）/ move messages（MOVE→COPY fallback）/ apply rules（dry_run）

啟動：python -m mcp_email（或安裝後直接 `mcp-email`）
協定：stdio (MCP standard)

依賴：
  pip install "mcp[cli]>=0.9.0"

可選環境變數預設值（runtime 也能用 email_configure 覆蓋）：
  SMTP_HOST, SMTP_PORT, SMTP_USER, SMTP_PASS, SMTP_USE_TLS
  IMAP_HOST, IMAP_PORT, IMAP_USER, IMAP_PASS, IMAP_USE_SSL
  # 別名：HOST 可用 *_SERVER、USER 可用 *_USERNAME、PASS 可用 *_PASSWORD
  #       例如 IMAP_SERVER / IMAP_USERNAME / IMAP_PASSWORD 也會被讀到
  # 帳密只需設一次：SMTP / IMAP 同帳號時可用共用的 EMAIL_USER / EMAIL_PASS；
  #       或只設一邊（如只有 IMAP_*），另一邊會自動沿用同一組帳密（host/port 仍各自設）
  EMAIL_FROM           # 寄件人預設 From（沒設就用 SMTP_USER）
  EMAIL_TIMEOUT_SEC    # 預設 30
  EMAIL_RETRY_MAX      # 預設 3
  EMAIL_ATTACHMENT_DIRS  # 附件路徑白名單，os.pathsep 分隔；設了才限制，否則不限
"""

from __future__ import annotations

import asyncio
import base64
import email
import email.message
import email.policy
import imaplib
import logging
import mimetypes
import os
import re
import smtplib
import ssl
import time
from dataclasses import dataclass, field
from email.message import EmailMessage
from email.utils import formataddr, getaddresses, parseaddr
from pathlib import Path
from typing import Any, Optional

from mcp.server import Server
from mcp.server.stdio import stdio_server
from mcp.types import TextContent, Tool

# ─── logging（寫 stderr，stdout 留給 MCP 協定）─────────────────────────────
logging.basicConfig(
    level=os.environ.get("MCP_EMAIL_LOG", "INFO"),
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("mcp-email")


# ─── 設定模型 ──────────────────────────────────────────────────────────────
@dataclass
class SMTPConfig:
    host: str = ""
    port: int = 587
    username: str = ""
    password: str = ""
    use_tls: bool = True       # STARTTLS（587）
    use_ssl: bool = False      # SMTPS（465）
    verify_cert: bool = True   # 驗證 TLS/SSL 憑證；自架 / 自簽伺服器可設 False
    timeout: float = 30.0


@dataclass
class IMAPConfig:
    host: str = ""
    port: int = 993
    username: str = ""
    password: str = ""
    use_ssl: bool = True
    verify_cert: bool = True   # 驗證 SSL 憑證；自架 / 自簽伺服器可設 False
    timeout: float = 30.0


@dataclass
class GlobalConfig:
    smtp: SMTPConfig = field(default_factory=SMTPConfig)
    imap: IMAPConfig = field(default_factory=IMAPConfig)
    email_from: str = ""       # default From
    retry_max: int = 3
    retry_base_delay: float = 1.0  # exponential backoff base sec
    # 附件路徑白名單：非空時，attachments[].path 只允許落在這些目錄下（防任意檔案外洩）。
    # 空 list = 不限制（向後相容）。可用 EMAIL_ATTACHMENT_DIRS 或 email_configure 設定。
    attachment_allowed_dirs: list[str] = field(default_factory=list)


def _bool_env(name: str, default: bool) -> bool:
    raw = os.environ.get(name)
    if raw is None:
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


def _parse_dir_list(raw: Optional[str]) -> list[str]:
    """把 os.pathsep（: 或 ;）分隔的目錄字串拆成 list，並各自 expanduser + resolve。"""
    if not raw:
        return []
    out: list[str] = []
    for part in raw.split(os.pathsep):
        part = part.strip()
        if part:
            out.append(str(Path(part).expanduser().resolve()))
    return out


def _env_any(*names: str, default: str = "") -> str:
    """回傳第一個有設且非空的環境變數值（支援多種別名命名）。"""
    for n in names:
        v = os.environ.get(n)
        if v is not None and v != "":
            return v
    return default


def load_config_from_env() -> GlobalConfig:
    timeout = float(os.environ.get("EMAIL_TIMEOUT_SEC", "30"))
    retry = int(os.environ.get("EMAIL_RETRY_MAX", "3"))

    # SMTP 與 IMAP 通常是同一組帳號密碼，因此帳密只需設一次：
    #   1. 共用：EMAIL_USER / EMAIL_PASS（或 MAIL_*）兩邊都吃
    #   2. 個別：SMTP_* / IMAP_*（含 *_USERNAME / *_PASSWORD 別名）會覆蓋共用值
    #   3. 跨協定 fallback：只設了一邊（例如只有 IMAP_*）時，另一邊自動沿用同一組帳密
    shared_user = _env_any("EMAIL_USER", "EMAIL_USERNAME", "MAIL_USER", "MAIL_USERNAME")
    shared_pass = _env_any("EMAIL_PASS", "EMAIL_PASSWORD", "MAIL_PASS", "MAIL_PASSWORD")
    smtp_user = _env_any("SMTP_USER", "SMTP_USERNAME", default=shared_user)
    smtp_pass = _env_any("SMTP_PASS", "SMTP_PASSWORD", default=shared_pass)
    imap_user = _env_any("IMAP_USER", "IMAP_USERNAME", default=shared_user)
    imap_pass = _env_any("IMAP_PASS", "IMAP_PASSWORD", default=shared_pass)
    # 跨協定沿用（同一信箱帳號）
    smtp_user = smtp_user or imap_user
    smtp_pass = smtp_pass or imap_pass
    imap_user = imap_user or smtp_user
    imap_pass = imap_pass or smtp_pass

    return GlobalConfig(
        smtp=SMTPConfig(
            # host 同時接受 SMTP_HOST / SMTP_SERVER
            host=_env_any("SMTP_HOST", "SMTP_SERVER"),
            port=int(_env_any("SMTP_PORT", default="587")),
            username=smtp_user,
            password=smtp_pass,
            use_tls=_bool_env("SMTP_USE_TLS", True),
            use_ssl=_bool_env("SMTP_USE_SSL", False),
            verify_cert=_bool_env("SMTP_VERIFY_CERT", True),
            timeout=timeout,
        ),
        imap=IMAPConfig(
            # host 同時接受 IMAP_HOST / IMAP_SERVER
            host=_env_any("IMAP_HOST", "IMAP_SERVER"),
            port=int(_env_any("IMAP_PORT", default="993")),
            username=imap_user,
            password=imap_pass,
            use_ssl=_bool_env("IMAP_USE_SSL", True),
            verify_cert=_bool_env("IMAP_VERIFY_CERT", True),
            timeout=timeout,
        ),
        email_from=os.environ.get("EMAIL_FROM", ""),
        retry_max=retry,
        retry_base_delay=float(os.environ.get("EMAIL_RETRY_DELAY", "1")),
        attachment_allowed_dirs=_parse_dir_list(os.environ.get("EMAIL_ATTACHMENT_DIRS")),
    )


# ─── 全域可變 config（runtime 透過 tool 覆蓋）──────────────────────────────
CONFIG: GlobalConfig = load_config_from_env()


# ─── SMTP 工具 ─────────────────────────────────────────────────────────────
def _make_ssl_context(verify: bool) -> ssl.SSLContext:
    ctx = ssl.create_default_context()
    if not verify:
        # 自架 / 自簽伺服器需要時關閉驗證；正式環境千萬不要設 False
        ctx.check_hostname = False
        ctx.verify_mode = ssl.CERT_NONE
        log.warning("SSL/TLS 憑證驗證已關閉（verify_cert=False）— 僅建議測試或自簽環境")
    return ctx


def _build_smtp_client(cfg: SMTPConfig) -> smtplib.SMTP | smtplib.SMTP_SSL:
    if cfg.use_ssl:
        ctx = _make_ssl_context(cfg.verify_cert)
        client = smtplib.SMTP_SSL(cfg.host, cfg.port, timeout=cfg.timeout, context=ctx)
    else:
        client = smtplib.SMTP(cfg.host, cfg.port, timeout=cfg.timeout)
        if cfg.use_tls:
            ctx = _make_ssl_context(cfg.verify_cert)
            client.starttls(context=ctx)
    if cfg.username:
        client.login(cfg.username, cfg.password)
    return client


def _parse_addresses(value: Any) -> list[str]:
    """接受 str（逗號分隔）或 list[str]，回傳 normalized email list。"""
    if not value:
        return []
    if isinstance(value, str):
        addrs = [a for _name, a in getaddresses([value]) if a]
    elif isinstance(value, list):
        addrs = []
        for item in value:
            if not item:
                continue
            for _name, addr in getaddresses([str(item)]):
                if addr:
                    addrs.append(addr)
    else:
        raise ValueError(f"address field must be str or list, got {type(value).__name__}")
    return addrs


def _resolve_attachment_path(path: str) -> Path:
    """解析附件路徑並套用白名單檢查。

    - expanduser + resolve（會解 symlink，避免用連結繞過白名單）。
    - CONFIG.attachment_allowed_dirs 非空時，路徑必須落在其中一個目錄下，否則 raise。
    - 路徑必須是存在的一般檔案。
    """
    p = Path(path).expanduser().resolve()
    allowed = CONFIG.attachment_allowed_dirs
    if allowed:
        ok = False
        for base in allowed:
            try:
                p.relative_to(base)
                ok = True
                break
            except ValueError:
                continue
        if not ok:
            raise PermissionError(
                f"附件路徑不在允許的目錄白名單內：{path}（允許：{allowed}）"
            )
    if not p.is_file():
        raise FileNotFoundError(f"附件路徑不存在或不是檔案：{path}")
    return p


def _attach_file(msg: EmailMessage, path: str, filename: Optional[str] = None) -> None:
    p = _resolve_attachment_path(path)
    ctype, _ = mimetypes.guess_type(p.name)
    if ctype is None:
        ctype = "application/octet-stream"
    maintype, subtype = ctype.split("/", 1)
    with p.open("rb") as fp:
        msg.add_attachment(
            fp.read(),
            maintype=maintype,
            subtype=subtype,
            filename=filename or p.name,
        )


def _attach_base64(
    msg: EmailMessage,
    filename: str,
    content_b64: str,
    mime_type: Optional[str] = None,
) -> None:
    try:
        data = base64.b64decode(content_b64, validate=True)
    except Exception as exc:
        raise ValueError(f"base64 解碼失敗：{exc}") from exc
    if mime_type is None:
        mime_type, _ = mimetypes.guess_type(filename)
        if mime_type is None:
            mime_type = "application/octet-stream"
    maintype, subtype = mime_type.split("/", 1)
    if maintype == "text":
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError:
            text = data.decode("utf-8", errors="replace")
        msg.add_attachment(text, subtype=subtype, filename=filename, charset="utf-8")
    else:
        msg.add_attachment(data, maintype=maintype, subtype=subtype, filename=filename)


def _build_message(
    *,
    sender: str,
    to: list[str],
    cc: list[str],
    bcc: list[str],
    subject: str,
    text: Optional[str],
    html: Optional[str],
    reply_to: Optional[str],
    headers: Optional[dict[str, str]],
    attachments: Optional[list[dict[str, Any]]],
) -> EmailMessage:
    msg = EmailMessage(policy=email.policy.SMTP)
    msg["From"] = sender
    msg["To"] = ", ".join(to)
    if cc:
        msg["Cc"] = ", ".join(cc)
    msg["Subject"] = subject
    if reply_to:
        msg["Reply-To"] = reply_to
    if headers:
        for key, value in headers.items():
            msg[key] = value

    # body 處理：純文字 + HTML
    if text and html:
        msg.set_content(text)
        msg.add_alternative(html, subtype="html")
    elif html:
        msg.set_content("此郵件含 HTML 內容，請使用支援 HTML 的客戶端閱讀。")
        msg.add_alternative(html, subtype="html")
    elif text:
        msg.set_content(text)
    else:
        msg.set_content("")  # 空 body 也允許

    # 附件
    for att in attachments or []:
        if "path" in att:
            _attach_file(msg, att["path"], att.get("filename"))
        elif "content_base64" in att:
            _attach_base64(
                msg,
                filename=att.get("filename") or "attachment.bin",
                content_b64=att["content_base64"],
                mime_type=att.get("mime_type"),
            )
        else:
            raise ValueError("attachment 必須含 path 或 content_base64")

    return msg


async def _retry(action, *, max_retries: int, base_delay: float, what: str) -> Any:
    """同步函式包成 retry-with-backoff。透過 asyncio.to_thread 跑。"""
    last_exc: Optional[BaseException] = None
    for attempt in range(1, max_retries + 1):
        try:
            return await asyncio.to_thread(action)
        except (smtplib.SMTPException, imaplib.IMAP4.error, OSError, TimeoutError) as exc:
            last_exc = exc
            if attempt >= max_retries:
                break
            delay = base_delay * (2 ** (attempt - 1))
            log.warning("%s 第 %d 次失敗：%s（%.1fs 後重試）", what, attempt, exc, delay)
            await asyncio.sleep(delay)
    assert last_exc is not None
    raise last_exc


def _send_via_smtp(cfg: SMTPConfig, msg: EmailMessage, recipients: list[str]) -> dict[str, Any]:
    with _build_smtp_client(cfg) as client:
        # send_message 會自己處理 Bcc（不會出現在 header）
        refused = client.send_message(msg, to_addrs=recipients)
    return {"refused": refused or {}}


# ─── IMAP 工具 ─────────────────────────────────────────────────────────────
class IMAPClient:
    """thin wrapper，每次 use 開新連線（簡單可靠，不維護長連線）。"""

    def __init__(self, cfg: IMAPConfig):
        if not cfg.host:
            raise RuntimeError("IMAP host 未設定，請先呼叫 email_configure")
        self.cfg = cfg

    def __enter__(self) -> imaplib.IMAP4:
        if self.cfg.use_ssl:
            ctx = _make_ssl_context(self.cfg.verify_cert)
            self._conn = imaplib.IMAP4_SSL(
                self.cfg.host, self.cfg.port, timeout=self.cfg.timeout, ssl_context=ctx,
            )
        else:
            self._conn = imaplib.IMAP4(self.cfg.host, self.cfg.port, timeout=self.cfg.timeout)
        if self.cfg.username:
            self._conn.login(self.cfg.username, self.cfg.password)
        return self._conn

    def __exit__(self, *_exc):
        try:
            self._conn.logout()
        except Exception:
            pass


def _decode_header(raw: str) -> str:
    if raw is None:
        return ""
    parts = email.header.decode_header(raw)
    decoded = []
    for value, charset in parts:
        if isinstance(value, bytes):
            try:
                decoded.append(value.decode(charset or "utf-8", errors="replace"))
            except LookupError:
                decoded.append(value.decode("utf-8", errors="replace"))
        else:
            decoded.append(value)
    return "".join(decoded)


def _utf7_encode(name: str) -> str:
    """純文字資料夾名稱 → IMAP modified UTF-7（RFC 3501 §5.1.3）。

    例：'日本語' → '&ZeVnLIqe-'；ASCII（含空格）原樣保留；'&' → '&-'。
    """
    out: list[str] = []
    i, n = 0, len(name)
    while i < n:
        ch = name[i]
        if 0x20 <= ord(ch) <= 0x7e:
            out.append("&-" if ch == "&" else ch)
            i += 1
        else:
            j = i
            while j < n and not (0x20 <= ord(name[j]) <= 0x7e):
                j += 1
            b64 = base64.b64encode(name[i:j].encode("utf-16-be")).decode("ascii")
            out.append("&" + b64.rstrip("=").replace("/", ",") + "-")
            i = j
    return "".join(out)


def _utf7_decode(name: Any) -> str:
    """IMAP modified UTF-7 → 純文字資料夾名稱（_utf7_encode 的逆運算）。"""
    if isinstance(name, (bytes, bytearray)):
        name = bytes(name).decode("ascii", "replace")
    out: list[str] = []
    i, n = 0, len(name)
    while i < n:
        ch = name[i]
        if ch == "&":
            j = name.find("-", i)
            if j == -1:
                j = n
            chunk = name[i + 1:j]
            if chunk == "":
                out.append("&")
            else:
                b64 = chunk.replace(",", "/")
                b64 += "=" * ((-len(b64)) % 4)
                try:
                    out.append(base64.b64decode(b64).decode("utf-16-be"))
                except Exception:
                    out.append("&" + chunk + "-")
            i = j + 1
        else:
            out.append(ch)
            i += 1
    return "".join(out)


def _imap_mailbox(name: str) -> str:
    """把資料夾名稱轉成可安全傳給 IMAP 的 quoted-string（UTF-7 編碼 + 加引號 + 跳脫）。

    imaplib 不會自動為含空格的 mailbox 名稱加引號（如 'BizForm Testing' 會被當兩個參數），
    所以一律包成 quoted string，並跳脫 \\ 與 "。
    """
    enc = _utf7_encode(name)
    escaped = enc.replace("\\", "\\\\").replace('"', '\\"')
    return '"' + escaped + '"'


def _imap_select(conn: imaplib.IMAP4, folder: str, readonly: bool = True) -> int:
    status, data = conn.select(_imap_mailbox(folder), readonly=readonly)
    if status != "OK":
        raise RuntimeError(f"select folder {folder} 失敗：{data!r}")
    count = int(data[0]) if data and data[0] else 0
    return count


def _imap_list_messages(folder: str, limit: int, search: str = "ALL") -> list[dict[str, Any]]:
    with IMAPClient(CONFIG.imap) as conn:
        _imap_select(conn, folder, readonly=True)
        status, data = conn.search(None, search)
        if status != "OK":
            raise RuntimeError(f"search 失敗：{data!r}")
        ids = data[0].split() if data and data[0] else []
        ids = ids[-limit:] if limit > 0 else ids
        results: list[dict[str, Any]] = []
        for mid in reversed(ids):
            status, fetched = conn.fetch(mid, "(BODY.PEEK[HEADER.FIELDS (FROM TO SUBJECT DATE)] FLAGS UID)")
            if status != "OK" or not fetched:
                continue
            # 從整個 fetch 回應彙整 metadata：UID / FLAGS 可能出現在 BODY literal「之後」的
            # 獨立 bytes 元素裡（server 排序自由），所以把所有非 literal 片段都納入比對。
            header_blob = b""
            meta_parts: list[str] = []
            for piece in fetched:
                if isinstance(piece, tuple):
                    # (含 literal 標記的回應字串, literal 資料)
                    header_blob = piece[1] or header_blob
                    if piece[0]:
                        meta_parts.append(piece[0].decode("utf-8", errors="replace"))
                elif isinstance(piece, (bytes, bytearray)):
                    meta_parts.append(bytes(piece).decode("utf-8", errors="replace"))
            meta = " ".join(meta_parts)
            m = re.search(r"\bUID\s+(\d+)", meta)
            uid_val: Optional[str] = m.group(1) if m else None
            m = re.search(r"\bFLAGS\s+\(([^)]*)\)", meta)
            flags_str = m.group(1).strip() if m else ""
            msg = email.message_from_bytes(header_blob)
            results.append({
                "imap_id": mid.decode(),
                "uid": uid_val,
                "from": _decode_header(msg.get("From", "")),
                "to": _decode_header(msg.get("To", "")),
                "subject": _decode_header(msg.get("Subject", "")),
                "date": msg.get("Date", ""),
                "flags": flags_str,
            })
        return results


def _imap_get_message(folder: str, uid: str, peek: bool = True) -> dict[str, Any]:
    with IMAPClient(CONFIG.imap) as conn:
        _imap_select(conn, folder, readonly=peek)
        section = "BODY.PEEK[]" if peek else "BODY[]"
        status, data = conn.uid("fetch", uid, f"({section})")
        if status != "OK" or not data or data[0] is None:
            raise RuntimeError(f"fetch uid={uid} 失敗：{data!r}")
        raw = data[0][1]
        msg = email.message_from_bytes(raw, policy=email.policy.default)
        body_text = ""
        body_html = ""
        atts = []
        if msg.is_multipart():
            for part in msg.walk():
                ctype = part.get_content_type()
                disp = part.get("Content-Disposition", "")
                filename = part.get_filename()
                if "attachment" in (disp or "").lower() or filename:
                    atts.append({
                        "filename": _decode_header(filename or "unknown"),
                        "mime_type": ctype,
                        "size": len(part.get_payload(decode=True) or b""),
                    })
                elif ctype == "text/plain" and not body_text:
                    body_text = part.get_content()
                elif ctype == "text/html" and not body_html:
                    body_html = part.get_content()
        else:
            if msg.get_content_type() == "text/html":
                body_html = msg.get_content()
            else:
                body_text = msg.get_content()
        return {
            "uid": uid,
            "from": _decode_header(msg.get("From", "")),
            "to": _decode_header(msg.get("To", "")),
            "cc": _decode_header(msg.get("Cc", "")),
            "subject": _decode_header(msg.get("Subject", "")),
            "date": msg.get("Date", ""),
            "message_id": msg.get("Message-ID", ""),
            "body_text": body_text,
            "body_html": body_html,
            "attachments": atts,
        }


def _imap_list_folders() -> list[str]:
    with IMAPClient(CONFIG.imap) as conn:
        status, folders = conn.list()
        if status != "OK":
            raise RuntimeError(f"list folders 失敗：{folders!r}")
        results = []
        for raw in folders or []:
            if not raw:
                continue
            line = raw.decode("utf-8", errors="replace")
            # 格式 e.g.: (\HasNoChildren) "/" "INBOX"
            m = re.search(r'"(?P<name>[^"]*)"\s*$', line)
            # 解 modified UTF-7，回傳可讀的中文資料夾名稱
            results.append(_utf7_decode(m.group("name")) if m else line)
        return results


def _imap_mark(folder: str, uids: list[str], flag: str, add: bool) -> dict[str, Any]:
    with IMAPClient(CONFIG.imap) as conn:
        _imap_select(conn, folder, readonly=False)
        action = "+FLAGS.SILENT" if add else "-FLAGS.SILENT"
        ok = 0
        fail = []
        for uid in uids:
            status, _ = conn.uid("store", uid, action, f"({flag})")
            if status == "OK":
                ok += 1
            else:
                fail.append(uid)
        return {"updated": ok, "failed": fail}


def _imap_delete(folder: str, uids: list[str]) -> dict[str, Any]:
    with IMAPClient(CONFIG.imap) as conn:
        _imap_select(conn, folder, readonly=False)
        ok = 0
        fail = []
        for uid in uids:
            status, _ = conn.uid("store", uid, "+FLAGS.SILENT", "(\\Deleted)")
            if status == "OK":
                ok += 1
            else:
                fail.append(uid)
        marked = [u for u in uids if u not in fail]
        # 優先用 UID EXPUNGE（RFC 4315 UIDPLUS）：只清掉這次標記的 uid，
        # 避免一般 EXPUNGE 把資料夾內其他「本來就標 \Deleted」的信一併清掉。
        caps = getattr(conn, "capabilities", ()) or ()
        method = "EXPUNGE"
        if marked and "UIDPLUS" in caps:
            try:
                status, _ = conn.uid("EXPUNGE", ",".join(marked))
                if status == "OK":
                    method = "UID EXPUNGE"
                else:
                    conn.expunge()
                    method = "EXPUNGE (fallback)"
            except imaplib.IMAP4.error:
                conn.expunge()
                method = "EXPUNGE (fallback)"
        else:
            conn.expunge()
        return {"deleted": ok, "failed": fail, "method": method}


def _imap_folder_exists(conn: imaplib.IMAP4, folder: str) -> bool:
    """以 LIST 精確比對 folder 是否存在（folder 為純文字名稱）。"""
    enc = _utf7_encode(folder)
    # reference 要用 quoted 空字串 '""'，傳真正的空字串 imaplib 會漏掉參數
    status, data = conn.list('""', _imap_mailbox(folder))
    if status != "OK":
        return False
    for raw in data or []:
        if not raw:
            continue
        line = raw.decode("utf-8", errors="replace") if isinstance(raw, (bytes, bytearray)) else str(raw)
        m = re.search(r'"(?P<name>[^"]*)"\s*$', line)
        if m and m.group("name") == enc:
            return True
    return False


def _imap_create_folder(folder: str) -> dict[str, Any]:
    with IMAPClient(CONFIG.imap) as conn:
        mbox = _imap_mailbox(folder)
        if _imap_folder_exists(conn, folder):
            return {"folder": folder, "created": False, "already_exists": True}
        status, data = conn.create(mbox)
        if status != "OK":
            detail = (data[0].decode("utf-8", errors="replace") if data and data[0] else "").lower()
            if "exist" in detail:
                return {"folder": folder, "created": False, "already_exists": True}
            raise RuntimeError(f"create folder {folder} 失敗：{data!r}")
        # best-effort 訂閱，讓 Webmail / 多數客戶端看得到
        try:
            conn.subscribe(mbox)
        except Exception:
            pass
        return {"folder": folder, "created": True, "already_exists": False}


def _imap_move_messages(source: str, uids: list[str], dest: str) -> dict[str, Any]:
    with IMAPClient(CONFIG.imap) as conn:
        # 目的地必須先存在（不自動建立，請先呼叫 email_create_folder）
        if not _imap_folder_exists(conn, dest):
            raise RuntimeError(f"目的地資料夾不存在：{dest}（請先呼叫 email_create_folder）")
        _imap_select(conn, source, readonly=False)
        dest_enc = _imap_mailbox(dest)
        caps = getattr(conn, "capabilities", ()) or ()
        moved: list[str] = []
        failed: list[str] = []

        if "MOVE" in caps:
            method = "UID MOVE"
            for uid in uids:
                status, _ = conn.uid("MOVE", uid, dest_enc)
                (moved if status == "OK" else failed).append(uid)
        else:
            # fallback：UID COPY → 標 \Deleted → UID EXPUNGE（避免誤刪其他 \Deleted 信）
            method = "COPY+EXPUNGE"
            for uid in uids:
                status, _ = conn.uid("COPY", uid, dest_enc)
                if status != "OK":
                    failed.append(uid)
                    continue
                conn.uid("STORE", uid, "+FLAGS.SILENT", "(\\Deleted)")
                moved.append(uid)
            if moved:
                if "UIDPLUS" in caps:
                    try:
                        conn.uid("EXPUNGE", ",".join(moved))
                    except imaplib.IMAP4.error:
                        conn.expunge()
                        method = "COPY+EXPUNGE (full)"
                else:
                    conn.expunge()
                    method = "COPY+EXPUNGE (full)"
        return {"moved": len(moved), "failed": failed, "method": method,
                "source": source, "destination": dest}


def _make_matcher(match_type: str, case_sensitive: bool):
    """回傳 contains(haystack, needle) -> bool。

    match_type: 'substring'（預設）/ 'regex' / 'exact'。
    case_sensitive=False 時：substring/exact 兩邊轉小寫；regex 加 IGNORECASE。
    """
    if match_type == "regex":
        flags = 0 if case_sensitive else re.IGNORECASE
        def _re(haystack: str, needle: str) -> bool:
            try:
                return re.search(needle, haystack, flags) is not None
            except re.error:
                return False
        return _re
    if match_type == "exact":
        if case_sensitive:
            return lambda h, n: h == n
        return lambda h, n: h.lower() == n.lower()
    # substring（預設）
    if case_sensitive:
        return lambda h, n: n in h
    return lambda h, n: n.lower() in h.lower()


def _rule_matches(rule: dict[str, Any], frm: str, subj: str, case_sensitive: bool) -> bool:
    """依規則比對。空規則（無任何條件）一律不命中（安全）。

    每條規則可用 rule['match'] 指定 'substring'(預設) / 'regex' / 'exact'。
    """
    contains = _make_matcher(rule.get("match", "substring"), case_sensitive)
    has_cond = False
    fc = rule.get("from_contains")
    if fc:
        has_cond = True
        if not contains(frm, fc):
            return False
    all_subs = rule.get("subject_contains_all") or []
    if all_subs:
        has_cond = True
        if not all(contains(subj, s) for s in all_subs):
            return False
    any_subs = rule.get("subject_contains_any") or []
    if any_subs:
        has_cond = True
        if not any(contains(subj, s) for s in any_subs):
            return False
    return has_cond


def _imap_apply_rules(folder: str, limit: int, search: str,
                      rules: list[dict[str, Any]], dry_run: bool,
                      case_sensitive: bool = False,
                      match_mode: str = "first") -> dict[str, Any]:
    msgs = _imap_list_messages(folder, limit, search)
    hits: list[dict[str, Any]] = []          # 命中明細（給 dry_run / by_rule）
    per_uid: dict[str, list[dict[str, Any]]] = {}  # uid -> 命中的 rules（依序）
    for m in msgs:
        frm = m.get("from") or ""
        subj = m.get("subject") or ""
        uid = m.get("uid")
        for rule in rules:
            if _rule_matches(rule, frm, subj, case_sensitive):
                hits.append({"uid": uid, "rule": rule.get("name"),
                             "from": m.get("from"), "subject": m.get("subject"),
                             "action": rule.get("action") or {}})
                if uid:
                    per_uid.setdefault(uid, []).append(rule)
                if match_mode == "first":
                    break  # 一封信只套第一條命中的規則

    by_rule: dict[str, int] = {}
    for h in hits:
        by_rule[h["rule"] or "?"] = by_rule.get(h["rule"] or "?", 0) + 1

    summary = {
        "folder": folder, "scanned": len(msgs),
        "matched": len(hits), "matched_messages": len(per_uid),
        "match_mode": match_mode, "case_sensitive": case_sensitive,
        "by_rule": by_rule, "dry_run": dry_run,
        "preview": [{"uid": h["uid"], "rule": h["rule"],
                     "from": h["from"], "subject": h["subject"],
                     "action": h["action"]} for h in hits[:10]],
    }
    if dry_run:
        return summary

    # 每封信解析出單一有效動作（優先序：move > delete > mark）
    # —— move 後信已離開原 folder，後續用 UID 標記/刪除會失效，故以此優先序避免衝突。
    executed = {"moved": 0, "marked": 0, "deleted": 0, "failed": []}
    moves: dict[str, list[str]] = {}
    marks: list[tuple[str, str, bool]] = []
    deletes: list[str] = []
    for uid, matched_rules in per_uid.items():
        actions = [r.get("action") or {} for r in matched_rules]
        move_dest = next((a["move_to"] for a in actions if a.get("move_to")), None)
        if move_dest:
            moves.setdefault(move_dest, []).append(uid)
        elif any(a.get("delete") for a in actions):
            deletes.append(uid)
        else:
            for a in actions:
                if a.get("mark"):
                    marks.append((uid, a["mark"], a.get("add", True)))

    for dest, uids in moves.items():
        r = _imap_move_messages(folder, uids, dest)
        executed["moved"] += r["moved"]
        executed["failed"] += r["failed"]
    if marks:
        with IMAPClient(CONFIG.imap) as conn:
            _imap_select(conn, folder, readonly=False)
            for uid, flag, add in marks:
                action = "+FLAGS.SILENT" if add else "-FLAGS.SILENT"
                status, _ = conn.uid("store", uid, action, f"({flag})")
                if status == "OK":
                    executed["marked"] += 1
                else:
                    executed["failed"].append(uid)
    if deletes:
        r = _imap_delete(folder, deletes)
        executed["deleted"] += r["deleted"]
        executed["failed"] += r["failed"]

    summary["executed"] = executed
    return summary


# ─── MCP server ───────────────────────────────────────────────────────────
server = Server("mcp-email")


@server.list_tools()
async def list_tools() -> list[Tool]:
    return [
        Tool(
            name="email_configure",
            description=(
                "Runtime 動態設定 SMTP / IMAP 帳號設定。"
                "任一欄位可單獨更新，未提供的欄位維持現值。回傳更新後的非敏感設定摘要。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "smtp": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string"},
                            "port": {"type": "integer"},
                            "username": {"type": "string"},
                            "password": {"type": "string"},
                            "use_tls": {"type": "boolean"},
                            "use_ssl": {"type": "boolean"},
                            "verify_cert": {"type": "boolean", "description": "驗證 TLS/SSL 憑證；自簽憑證可設 false"},
                            "timeout": {"type": "number"},
                        },
                    },
                    "imap": {
                        "type": "object",
                        "properties": {
                            "host": {"type": "string"},
                            "port": {"type": "integer"},
                            "username": {"type": "string"},
                            "password": {"type": "string"},
                            "use_ssl": {"type": "boolean"},
                            "verify_cert": {"type": "boolean", "description": "驗證 TLS/SSL 憑證；自簽憑證可設 false"},
                            "timeout": {"type": "number"},
                        },
                    },
                    "email_from": {"type": "string", "description": "預設寄件人"},
                    "retry_max": {"type": "integer"},
                    "retry_base_delay": {"type": "number"},
                    "attachment_allowed_dirs": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "附件路徑白名單目錄。設定後 email_send 的 path 附件只能來自這些目錄底下，"
                            "用來防止任意本機檔案外洩。空 list 代表不限制。"
                        ),
                    },
                },
            },
        ),
        Tool(
            name="email_test_connection",
            description="測試 SMTP 與/或 IMAP 連線是否能登入。回傳兩邊狀態。",
            inputSchema={
                "type": "object",
                "properties": {
                    "smtp": {"type": "boolean", "default": True},
                    "imap": {"type": "boolean", "default": True},
                },
            },
        ),
        Tool(
            name="email_send",
            description=(
                "寄送郵件。支援 HTML + 純文字、to/cc/bcc 多收件人、檔案/Base64 附件、"
                "自訂 headers、Reply-To。失敗自動 retry（exponential backoff）。"
            ),
            inputSchema={
                "type": "object",
                "required": ["to", "subject"],
                "properties": {
                    "to": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                        "description": "收件人，可逗號分隔字串或 list",
                    },
                    "cc": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                    },
                    "bcc": {
                        "oneOf": [
                            {"type": "string"},
                            {"type": "array", "items": {"type": "string"}},
                        ],
                    },
                    "subject": {"type": "string"},
                    "text": {"type": "string", "description": "純文字內容"},
                    "html": {"type": "string", "description": "HTML 內容"},
                    "from": {"type": "string", "description": "覆蓋預設 From"},
                    "reply_to": {"type": "string"},
                    "headers": {
                        "type": "object",
                        "additionalProperties": {"type": "string"},
                    },
                    "attachments": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "path": {"type": "string", "description": "本機絕對路徑"},
                                "content_base64": {"type": "string", "description": "base64 編碼內容"},
                                "filename": {"type": "string"},
                                "mime_type": {"type": "string"},
                            },
                        },
                    },
                },
            },
        ),
        Tool(
            name="email_list_folders",
            description="列出 IMAP 上所有 mailbox / folder 名稱。",
            inputSchema={"type": "object", "properties": {}},
        ),
        Tool(
            name="email_list_messages",
            description=(
                "列出指定 folder 中的訊息 header（最新優先）。"
                "可用 IMAP search syntax 篩選（預設 ALL）。"
            ),
            inputSchema={
                "type": "object",
                "properties": {
                    "folder": {"type": "string", "default": "INBOX"},
                    "limit": {"type": "integer", "default": 20, "minimum": 1, "maximum": 200},
                    "search": {
                        "type": "string",
                        "default": "ALL",
                        "description": "IMAP search 條件，如 UNSEEN / FROM \"xxx@y.com\" / SINCE 1-Jan-2026",
                    },
                },
            },
        ),
        Tool(
            name="email_get_message",
            description="抓取單封郵件完整內容（含 body text/html、附件 metadata）。預設 peek 不改 SEEN flag。",
            inputSchema={
                "type": "object",
                "required": ["uid"],
                "properties": {
                    "folder": {"type": "string", "default": "INBOX"},
                    "uid": {"type": "string"},
                    "mark_read": {"type": "boolean", "default": False},
                },
            },
        ),
        Tool(
            name="email_mark",
            description="加 / 移除 IMAP flag（\\Seen / \\Flagged 等）。",
            inputSchema={
                "type": "object",
                "required": ["uids", "flag"],
                "properties": {
                    "folder": {"type": "string", "default": "INBOX"},
                    "uids": {"type": "array", "items": {"type": "string"}},
                    "flag": {"type": "string", "default": "\\Seen"},
                    "add": {"type": "boolean", "default": True},
                },
            },
        ),
        Tool(
            name="email_delete",
            description="標記 \\Deleted 並 expunge。注意：無法復原。",
            inputSchema={
                "type": "object",
                "required": ["uids"],
                "properties": {
                    "folder": {"type": "string", "default": "INBOX"},
                    "uids": {"type": "array", "items": {"type": "string"}},
                },
            },
        ),
        Tool(
            name="email_create_folder",
            description=(
                "建立 IMAP folder / mailbox（支援中文名稱，自動 modified UTF-7 編碼）。"
                "已存在時不報錯，回傳 already_exists=true。"
            ),
            inputSchema={
                "type": "object",
                "required": ["folder"],
                "properties": {
                    "folder": {"type": "string", "description": "資料夾名稱，如 'BizForm Testing'"},
                },
            },
        ),
        Tool(
            name="email_move_messages",
            description=(
                "把指定 UID 的信從 source_folder 搬到 destination_folder。"
                "優先用 UID MOVE，server 不支援則 fallback 為 COPY + UID EXPUNGE。"
                "目的地需先存在（請先呼叫 email_create_folder）。"
            ),
            inputSchema={
                "type": "object",
                "required": ["source_folder", "uids", "destination_folder"],
                "properties": {
                    "source_folder": {"type": "string"},
                    "uids": {"type": "array", "items": {"type": "string"}},
                    "destination_folder": {"type": "string"},
                },
            },
        ),
        Tool(
            name="email_apply_rules",
            description=(
                "掃描 folder 後，依規則對信件做 move / mark / delete。"
                "預設：子字串、大小寫不敏感、first-match-wins（一封信只套第一條命中的規則）。"
                "可用 case_sensitive / match_mode 與每條規則的 match(substring|regex|exact) 調整。"
                "強烈建議先 dry_run=true 預覽命中數與前幾封，確認無誤再執行。"
            ),
            inputSchema={
                "type": "object",
                "required": ["rules"],
                "properties": {
                    "folder": {"type": "string", "default": "INBOX"},
                    "limit": {"type": "integer", "default": 200, "minimum": 1, "maximum": 2000},
                    "search": {
                        "type": "string",
                        "default": "ALL",
                        "description": "可選 IMAP search 先在 server 端縮小掃描範圍（大信箱建議用）",
                    },
                    "dry_run": {"type": "boolean", "default": True},
                    "case_sensitive": {
                        "type": "boolean", "default": False,
                        "description": "比對是否區分大小寫（預設否）",
                    },
                    "match_mode": {
                        "type": "string", "enum": ["first", "all"], "default": "first",
                        "description": "first=一封只套第一條命中規則；all=套用所有命中規則（執行優先序 move>delete>mark）",
                    },
                    "rules": {
                        "type": "array",
                        "items": {
                            "type": "object",
                            "properties": {
                                "name": {"type": "string"},
                                "match": {
                                    "type": "string", "enum": ["substring", "regex", "exact"],
                                    "default": "substring",
                                    "description": "此規則的比對方式",
                                },
                                "from_contains": {"type": "string"},
                                "subject_contains_all": {"type": "array", "items": {"type": "string"}},
                                "subject_contains_any": {"type": "array", "items": {"type": "string"}},
                                "action": {
                                    "type": "object",
                                    "description": "{move_to: 資料夾} 或 {mark: '\\\\Seen', add: true} 或 {delete: true}",
                                    "properties": {
                                        "move_to": {"type": "string"},
                                        "mark": {"type": "string"},
                                        "add": {"type": "boolean"},
                                        "delete": {"type": "boolean"},
                                    },
                                },
                            },
                        },
                    },
                },
            },
        ),
    ]


# ─── tool 實作 dispatch ───────────────────────────────────────────────────
@server.call_tool()
async def call_tool(name: str, arguments: dict[str, Any]) -> list[TextContent]:
    try:
        result = await _dispatch(name, arguments or {})
    except Exception as exc:
        log.exception("tool %s 執行失敗", name)
        return [TextContent(type="text", text=f"❌ {name} 失敗：{exc}")]
    if isinstance(result, str):
        return [TextContent(type="text", text=result)]
    import json
    return [TextContent(type="text", text=json.dumps(result, ensure_ascii=False, indent=2))]


async def _dispatch(name: str, args: dict[str, Any]) -> Any:
    global CONFIG

    if name == "email_configure":
        if "smtp" in args:
            for k, v in (args["smtp"] or {}).items():
                if hasattr(CONFIG.smtp, k):
                    setattr(CONFIG.smtp, k, v)
        if "imap" in args:
            for k, v in (args["imap"] or {}).items():
                if hasattr(CONFIG.imap, k):
                    setattr(CONFIG.imap, k, v)
        for k in ("email_from", "retry_max", "retry_base_delay"):
            if k in args:
                setattr(CONFIG, k, args[k])
        if "attachment_allowed_dirs" in args:
            dirs = args["attachment_allowed_dirs"] or []
            CONFIG.attachment_allowed_dirs = [
                str(Path(d).expanduser().resolve()) for d in dirs if d
            ]
        return _config_summary()

    if name == "email_test_connection":
        do_smtp = args.get("smtp", True)
        do_imap = args.get("imap", True)
        out: dict[str, Any] = {}
        if do_smtp:
            try:
                def _test_smtp():
                    with _build_smtp_client(CONFIG.smtp) as client:
                        client.noop()
                await asyncio.to_thread(_test_smtp)
                out["smtp"] = {"ok": True, "host": CONFIG.smtp.host, "port": CONFIG.smtp.port}
            except Exception as exc:
                out["smtp"] = {"ok": False, "error": str(exc)}
        if do_imap:
            try:
                def _test_imap():
                    with IMAPClient(CONFIG.imap) as conn:
                        conn.noop()
                await asyncio.to_thread(_test_imap)
                out["imap"] = {"ok": True, "host": CONFIG.imap.host, "port": CONFIG.imap.port}
            except Exception as exc:
                out["imap"] = {"ok": False, "error": str(exc)}
        return out

    if name == "email_send":
        sender = args.get("from") or CONFIG.email_from or CONFIG.smtp.username
        if not sender:
            raise RuntimeError("找不到寄件人：請設定 EMAIL_FROM 或 email_configure({email_from:...})")
        to = _parse_addresses(args.get("to"))
        cc = _parse_addresses(args.get("cc"))
        bcc = _parse_addresses(args.get("bcc"))
        if not to:
            raise ValueError("至少要有一個收件人 (to)")
        msg = _build_message(
            sender=sender,
            to=to,
            cc=cc,
            bcc=bcc,
            subject=args.get("subject", ""),
            text=args.get("text"),
            html=args.get("html"),
            reply_to=args.get("reply_to"),
            headers=args.get("headers"),
            attachments=args.get("attachments"),
        )
        recipients = list(dict.fromkeys(to + cc + bcc))  # dedupe 保序

        started = time.time()
        result = await _retry(
            lambda: _send_via_smtp(CONFIG.smtp, msg, recipients),
            max_retries=CONFIG.retry_max,
            base_delay=CONFIG.retry_base_delay,
            what="SMTP send",
        )
        return {
            "ok": True,
            "to": to,
            "cc": cc,
            "bcc": bcc,
            "subject": msg["Subject"],
            "refused_addresses": result.get("refused", {}),
            "elapsed_sec": round(time.time() - started, 3),
        }

    if name == "email_list_folders":
        folders = await asyncio.to_thread(_imap_list_folders)
        return {"folders": folders, "count": len(folders)}

    if name == "email_list_messages":
        folder = args.get("folder", "INBOX")
        limit = int(args.get("limit", 20))
        search = args.get("search", "ALL")
        messages = await asyncio.to_thread(_imap_list_messages, folder, limit, search)
        return {"folder": folder, "count": len(messages), "messages": messages}

    if name == "email_get_message":
        folder = args.get("folder", "INBOX")
        uid = str(args["uid"])
        peek = not args.get("mark_read", False)
        msg = await asyncio.to_thread(_imap_get_message, folder, uid, peek)
        return msg

    if name == "email_mark":
        folder = args.get("folder", "INBOX")
        uids = [str(u) for u in args["uids"]]
        flag = args.get("flag", "\\Seen")
        add = args.get("add", True)
        return await asyncio.to_thread(_imap_mark, folder, uids, flag, add)

    if name == "email_delete":
        folder = args.get("folder", "INBOX")
        uids = [str(u) for u in args["uids"]]
        return await asyncio.to_thread(_imap_delete, folder, uids)

    if name == "email_create_folder":
        folder = args["folder"]
        return await asyncio.to_thread(_imap_create_folder, folder)

    if name == "email_move_messages":
        source = args["source_folder"]
        dest = args["destination_folder"]
        uids = [str(u) for u in args["uids"]]
        return await asyncio.to_thread(_imap_move_messages, source, uids, dest)

    if name == "email_apply_rules":
        folder = args.get("folder", "INBOX")
        limit = int(args.get("limit", 200))
        search = args.get("search", "ALL")
        dry_run = args.get("dry_run", True)
        case_sensitive = args.get("case_sensitive", False)
        match_mode = args.get("match_mode", "first")
        rules = args.get("rules") or []
        if not rules:
            raise ValueError("rules 不可為空")
        return await asyncio.to_thread(
            _imap_apply_rules, folder, limit, search, rules, dry_run,
            case_sensitive, match_mode,
        )

    raise ValueError(f"未知 tool: {name}")


def _config_summary() -> dict[str, Any]:
    """不曝光密碼，回傳當前 config 摘要。"""
    return {
        "smtp": {
            "host": CONFIG.smtp.host,
            "port": CONFIG.smtp.port,
            "username": CONFIG.smtp.username,
            "password_set": bool(CONFIG.smtp.password),
            "use_tls": CONFIG.smtp.use_tls,
            "use_ssl": CONFIG.smtp.use_ssl,
            "verify_cert": CONFIG.smtp.verify_cert,
            "timeout": CONFIG.smtp.timeout,
        },
        "imap": {
            "host": CONFIG.imap.host,
            "port": CONFIG.imap.port,
            "username": CONFIG.imap.username,
            "password_set": bool(CONFIG.imap.password),
            "use_ssl": CONFIG.imap.use_ssl,
            "verify_cert": CONFIG.imap.verify_cert,
            "timeout": CONFIG.imap.timeout,
        },
        "email_from": CONFIG.email_from,
        "retry_max": CONFIG.retry_max,
        "retry_base_delay": CONFIG.retry_base_delay,
        "attachment_allowed_dirs": CONFIG.attachment_allowed_dirs,
    }


# ─── entry ────────────────────────────────────────────────────────────────
async def main() -> None:
    log.info("mcp-email server starting (stdio)")
    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options())


def cli() -> None:
    """console_scripts / `python -m mcp_email` 的同步進入點。"""
    asyncio.run(main())


if __name__ == "__main__":
    cli()
