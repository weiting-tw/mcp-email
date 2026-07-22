# mcp-email MCP server（HTTP / OAuth 公用部署用；本機 stdio 模式不需要 Docker）
#
# 本機建置：docker build -t mcp-email .
# HTTP 模式（Basic pass-through；IMAP/SMTP 主機由環境變數指定）：
#   docker run -d -p 8765:8765 \
#     -e IMAP_HOST=mail.example.com -e SMTP_HOST=mail.example.com \
#     mcp-email
# OAuth 模式（claude.ai Connectors）——掛 /data volume 保留金鑰與 client 註冊：
#   docker run -d -p 8765:8765 -v mcp-email-data:/data \
#     -e IMAP_HOST=mail.example.com -e SMTP_HOST=mail.example.com \
#     mcp-email --oauth --issuer https://對外網址 --host 0.0.0.0 --port 8765
# 兩種模式都必須放在 HTTPS 反向代理後面。
FROM python:3.12-slim

WORKDIR /app
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt "cryptography>=42"

COPY mcp_email.py mcp_email_oauth.py ./

# 非 root 執行；OAuth 執行期檔案（金鑰、client 註冊）collect 在 /data
RUN useradd -r -u 10001 mcpemail && mkdir /data && chown mcpemail /data
USER mcpemail
ENV EMAIL_BRIDGE_KEY_FILE=/data/bridge-key \
    EMAIL_OAUTH_CLIENTS=/data/oauth-clients.json \
    EMAIL_AUTH_LOG=/data/auth.log

EXPOSE 8765
ENTRYPOINT ["python", "mcp_email.py"]
CMD ["--http", "--host", "0.0.0.0", "--port", "8765"]
