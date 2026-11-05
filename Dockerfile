# Lunar X 运行时镜像。WebUI 与 Bot 共用同一镜像，靠 command 区分：
#   WebUI: gunicorn -c deploy/gunicorn.conf.py wsgi:application
#   Bot:   python main.py
# 持久化状态统一放在 /app/state（见 deploy/docker-entrypoint.sh）。
FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    UV_LINK_MODE=copy \
    UV_COMPILE_BYTECODE=1 \
    PATH="/app/.venv/bin:$PATH"

RUN apt-get update \
    && apt-get install -y --no-install-recommends ca-certificates \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /uvx /bin/

WORKDIR /app

# 先装依赖，利用 Docker 层缓存
COPY pyproject.toml uv.lock ./
RUN uv sync --frozen --no-dev --extra serve --no-install-project

COPY . .
RUN uv sync --frozen --no-dev --extra serve \
    && chmod +x /app/deploy/docker-entrypoint.sh \
    && useradd --create-home --uid 10001 lunarx \
    && mkdir -p /app/state \
    && chown -R lunarx:lunarx /app

USER lunarx
EXPOSE 5000

HEALTHCHECK --interval=15s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:5000/healthz', timeout=3)"

ENTRYPOINT ["/app/deploy/docker-entrypoint.sh"]
CMD ["gunicorn", "-c", "deploy/gunicorn.conf.py", "wsgi:application"]
