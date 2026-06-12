"""MCP stdio 整合煙霧測試。

真的用 MCP client 把 server.py 以 stdio 子行程啟動，跑完整 protocol handshake：
  initialize -> list_tools -> 呼叫幾個不需網路的 tool。
驗證這個 server 能被任何 MCP host（Claude Desktop / Code / Cowork）正常載入。

執行：python test_mcp_stdio.py
"""
from __future__ import annotations

import asyncio
import os
import sys
from pathlib import Path

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

HERE = Path(__file__).resolve().parent
SERVER = str(HERE / "server.py")


async def main() -> int:
    params = StdioServerParameters(
        command=sys.executable,
        args=[SERVER],
        env={**os.environ, "MCP_EMAIL_LOG": "WARNING"},
    )
    async with stdio_client(params) as (read, write):
        async with ClientSession(read, write) as session:
            init = await session.initialize()
            print(f"✅ initialize OK — server: {init.serverInfo.name} v{init.serverInfo.version}")

            tools = await session.list_tools()
            names = [t.name for t in tools.tools]
            print(f"✅ list_tools — {len(names)} tools: {names}")
            assert len(names) == 8, f"預期 8 個 tool，實際 {len(names)}"

            # email_configure（不需網路）：驗證不回傳明文密碼
            r = await session.call_tool("email_configure", {
                "smtp": {"host": "smtp.example.com", "port": 587, "username": "u", "password": "secret"},
                "email_from": "me@example.com",
                "attachment_allowed_dirs": ["/tmp"],
            })
            text = r.content[0].text
            assert "secret" not in text, "明文密碼外洩！"
            assert '"password_set": true' in text.lower()
            print("✅ call email_configure — 回傳含 password_set、不含明文密碼")

            # email_test_connection：對假 host 應 graceful 回 ok:false，不 crash
            r = await session.call_tool("email_test_connection", {"smtp": True, "imap": False})
            assert '"ok": false' in r.content[0].text.lower()
            print("✅ call email_test_connection — 對假 host graceful 回 ok:false")

            # email_send 缺收件人：應被包成錯誤字串回傳，不讓 server crash
            r = await session.call_tool("email_send", {"to": "", "subject": "x", "text": "y"})
            assert "❌" in r.content[0].text or "收件人" in r.content[0].text
            print("✅ call email_send（空收件人）— graceful error")

    print("\n=== MCP stdio 整合測試全部通過 ===")
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
