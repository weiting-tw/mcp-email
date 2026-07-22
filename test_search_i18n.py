"""中文（非 ASCII）IMAP 搜尋測試：_nonascii_search_plan 的重排/錯誤，
與 _imap_search_ids 的 CHARSET UTF-8 + literal 呼叫方式。"""
from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).parent))

import mcp_email as srv


# ─── _nonascii_search_plan ──────────────────────────────────────────────────
def test_plan_simple_subject():
    crit, lit = srv._nonascii_search_plan('SUBJECT "週報"')
    assert crit == ["SUBJECT"]
    assert lit == "週報".encode("utf-8")


def test_plan_keeps_leading_ascii_conditions():
    crit, lit = srv._nonascii_search_plan('UNSEEN SUBJECT "週報"')
    assert crit == ["UNSEEN", "SUBJECT"]
    assert lit == "週報".encode("utf-8")


def test_plan_reorders_to_tail_for_and_sequence():
    crit, lit = srv._nonascii_search_plan('SUBJECT "週報" UNSEEN SINCE 1-Jan-2026')
    assert crit == ["UNSEEN", "SINCE", "1-Jan-2026", "SUBJECT"]
    assert lit == "週報".encode("utf-8")


def test_plan_unquoted_chinese_token():
    crit, lit = srv._nonascii_search_plan("FROM 王小明")
    assert crit == ["FROM"]
    assert lit == "王小明".encode("utf-8")


def test_plan_header_key_moves_three_tokens():
    crit, lit = srv._nonascii_search_plan('UNSEEN HEADER X-Tag "中文標記"')
    assert crit == ["UNSEEN", "HEADER", "X-Tag"]
    assert lit == "中文標記".encode("utf-8")


def test_plan_requotes_remaining_ascii_strings():
    crit, lit = srv._nonascii_search_plan('SUBJECT "中" FROM "alice smith"')
    assert crit == ["FROM", '"alice smith"', "SUBJECT"]
    assert lit == "中".encode("utf-8")


def test_plan_or_with_chinese_at_tail_is_ok():
    crit, lit = srv._nonascii_search_plan('OR FROM "a@b.c" SUBJECT "中"')
    assert crit == ["OR", "FROM", '"a@b.c"', "SUBJECT"]
    assert lit == "中".encode("utf-8")


def test_plan_or_with_chinese_in_middle_rejected():
    with pytest.raises(ValueError):
        srv._nonascii_search_plan('OR SUBJECT "中" FROM "a@b.c"')


def test_plan_multiple_chinese_terms_rejected():
    with pytest.raises(ValueError):
        srv._nonascii_search_plan('SUBJECT "週報" FROM "王"')


def test_plan_chinese_without_string_key_rejected():
    with pytest.raises(ValueError):
        srv._nonascii_search_plan('SINCE 週一')  # SINCE 不是字串搜尋 key


# ─── _imap_search_ids ───────────────────────────────────────────────────────
class _FakeConn:
    """記錄 search 呼叫方式的假連線。"""

    def __init__(self, result=(b"1 2 3",)):
        self.calls = []
        self.literal = None
        self.literal_at_call = None
        self.result = result
        self.status = "OK"

    def search(self, charset, *criteria):
        self.calls.append((charset, criteria))
        self.literal_at_call = self.literal
        return self.status, list(self.result)


def test_search_ids_ascii_passthrough():
    conn = _FakeConn()
    ids = srv._imap_search_ids(conn, 'UNSEEN FROM "alice"')
    assert ids == [b"1", b"2", b"3"]
    assert conn.calls == [(None, ('UNSEEN FROM "alice"',))]
    assert conn.literal_at_call is None  # ASCII 路徑不用 literal


def test_search_ids_chinese_uses_charset_and_literal():
    conn = _FakeConn()
    ids = srv._imap_search_ids(conn, 'UNSEEN SUBJECT "週報"')
    assert ids == [b"1", b"2", b"3"]
    charset, criteria = conn.calls[0]
    assert charset == "UTF-8"
    assert criteria == ("UNSEEN", "SUBJECT")
    assert conn.literal_at_call == "週報".encode("utf-8")


def test_search_ids_server_rejection_message():
    conn = _FakeConn()
    conn.status = "NO"
    with pytest.raises(RuntimeError, match="CHARSET UTF-8"):
        srv._imap_search_ids(conn, 'SUBJECT "週報"')


def test_search_ids_empty_result():
    conn = _FakeConn(result=(b"",))
    assert srv._imap_search_ids(conn, "ALL") == []


# ─── 客戶端過濾 fallback（伺服器接受 CHARSET UTF-8 語法但比對不到中文時）───
def test_client_filter_plan():
    assert srv._client_filter_plan('UNSEEN SUBJECT "週報"') == ("UNSEEN", "subject", "週報")
    assert srv._client_filter_plan('SUBJECT "週報"') == ("ALL", "subject", "週報")
    assert srv._client_filter_plan("FROM 王") == ("ALL", "from", "王")
    assert srv._client_filter_plan('TEXT "中文"') is None      # body 沒抓，做不到
    assert srv._client_filter_plan('HEADER X-Tag "中"') is None  # 自訂 header 不在 fetch 欄位
    assert srv._client_filter_plan('SUBJECT "週報" FROM "王"') is None  # 兩個中文詞


class _FallbackConn:
    """模擬 Mail2000 行為：CHARSET UTF-8 搜尋回 OK 但 0 筆；ASCII 搜尋有結果。"""

    def __init__(self, subjects: dict[bytes, str]):
        self.literal = None
        self.subjects = subjects  # mid -> subject（明文，fetch 時做 RFC2047 編碼）

    def search(self, charset, *criteria):
        if charset == "UTF-8":
            return "OK", [b""]
        return "OK", [b" ".join(sorted(self.subjects))]

    def fetch(self, mid, _spec):
        import base64 as b64
        subj = "=?utf-8?b?" + b64.b64encode(
            self.subjects[mid].encode("utf-8")).decode() + "?="
        blob = (f"From: sender@x.tw\r\nTo: me@x.tw\r\nSubject: {subj}\r\n"
                f"Date: Mon, 1 Jan 2026 00:00:00 +0800\r\n\r\n").encode()
        meta = b"%s (UID %s FLAGS (\\Seen) BODY[HEADER] {%d}" % (mid, mid, len(blob))
        return "OK", [(meta, blob), b")"]


def test_list_messages_chinese_fallback(monkeypatch):
    conn = _FallbackConn({b"1": "週報 2026-W01", b"2": "系統通知", b"3": "meeting notes"})

    class _FakeClient:
        def __init__(self, cfg):
            pass

        def __enter__(self):
            return conn

        def __exit__(self, *exc):
            pass

    monkeypatch.setattr(srv, "IMAPClient", _FakeClient)
    monkeypatch.setattr(srv, "_imap_select", lambda *a, **k: 3)

    msgs, note = srv._imap_list_messages("INBOX", 10, 'SUBJECT "通知"')
    assert note is not None and "客戶端過濾" in note
    assert [m["subject"] for m in msgs] == ["系統通知"]
    assert msgs[0]["uid"] == "2"

    # ASCII 搜尋不觸發 fallback，note 為 None
    msgs, note = srv._imap_list_messages("INBOX", 10, "ALL")
    assert note is None and len(msgs) == 3
