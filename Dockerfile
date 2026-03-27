FROM python:3.12-slim

WORKDIR /app

# 系统依赖（playwright CDP 连接不需要本地 Chromium，但需要基础 lib）
RUN apt-get update && apt-get install -y --no-install-recommends \
    curl \
    ca-certificates \
    && rm -rf /var/lib/apt/lists/*

# Python 依赖
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# 安装 playwright 浏览器驱动依赖（connect_over_cdp 不需要本地浏览器，但需要 lib）
RUN playwright install-deps chromium 2>/dev/null || true

# 应用代码
COPY agent-run-jobs.py .
COPY static/ static/

ENV SERVER_MODE=true
ENV SERVER_PORT=8080

EXPOSE 8080

CMD ["python", "agent-run-jobs.py"]
