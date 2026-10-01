FROM python:3.12-slim-bookworm

ARG TARGETARCH

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    DEBIAN_FRONTEND=noninteractive \
    TZ=Asia/Singapore \
    PATH=/app/.venv/bin:$PATH \
    XDG_CACHE_HOME=/opt/browser-cache \
    PLAYWRIGHT_BROWSERS_PATH=/opt/browser-cache/ms-playwright \
    GROK_REGISTER_DATA_DIR=/data \
    GROK_REGISTER_CONFIG_FILE=/data/config.json \
    ACCOUNT_LOGIN_STATE_FILE=/data/accounts/imported_credentials.json \
    MONITOR_MAX_REQUEST_BODY=16777216 \
    NEXT_ACTION_CACHE_FILE=/data/.next_action_id.cache \
    MONITOR_HOST=0.0.0.0 \
    MONITOR_PORT=8787 \
    PANEL_INCLUDE_TAIL=0

WORKDIR /app

RUN apt-get update \
    && apt-get install -y --no-install-recommends \
        bash \
        ca-certificates \
        curl \
        fonts-liberation \
        fonts-noto-color-emoji \
        libasound2 \
        libatk-bridge2.0-0 \
        libatk1.0-0 \
        libcups2 \
        libdbus-1-3 \
        libdbus-glib-1-2 \
        libdrm2 \
        libfontconfig1 \
        libgbm1 \
        libgtk-3-0 \
        libnspr4 \
        libnss3 \
        libpango-1.0-0 \
        libx11-6 \
        libx11-xcb1 \
        libxcb1 \
        libxcomposite1 \
        libxdamage1 \
        libxext6 \
        libxfixes3 \
        libxkbcommon0 \
        libxrandr2 \
        libxrender1 \
        libxshmfence1 \
        libxss1 \
        libxtst6 \
        libtk8.6 \
        nodejs \
        tini \
        tzdata \
        xauth \
        xvfb \
    && ln -snf /usr/share/zoneinfo/${TZ} /etc/localtime \
    && echo "${TZ}" > /etc/timezone \
    && rm -rf /var/lib/apt/lists/*

COPY requirements.txt requirements.lock.txt /app/
RUN python -m venv /app/.venv \
    && python -m pip install --no-cache-dir --upgrade pip setuptools wheel \
    && python -m pip install --no-cache-dir -r /app/requirements.txt \
    && python -m pip check \
    && python -c "import tkinter; print('Tk', tkinter.Tcl().eval('info patchlevel'))" \
    && rm -rf /root/.cache/pip

# Bake the Camoufox build selected by the installed package into the image.
RUN mkdir -p "${XDG_CACHE_HOME}" "${PLAYWRIGHT_BROWSERS_PATH}" \
    && python -m camoufox fetch \
    && python -c "from camoufox.pkgman import installed_verstr; print('Camoufox', installed_verstr())" \
    && echo "browser baked for target architecture: ${TARGETARCH:-native}"

COPY . /app
RUN chmod +x \
        /app/docker-entrypoint.sh \
        /app/scripts/fetch_browsers.sh \
        /app/scripts/publish_docker.sh \
        /app/scripts/resolve_browser_cache.sh \
    && rm -rf /app/accounts /app/cpa_auth /app/grok2api_auth /app/log \
    && find /app -type d -name __pycache__ -prune -exec rm -rf {} + \
    && find /app -type f -name '*.pyc' -delete

VOLUME ["/data"]
EXPOSE 8787

HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=5 \
  CMD curl -fsS "http://127.0.0.1:${MONITOR_PORT:-8787}/api/health" >/dev/null || exit 1

ENTRYPOINT ["tini", "-g", "--", "/app/docker-entrypoint.sh"]
CMD ["python", "-u", "webui/monitor.py"]
