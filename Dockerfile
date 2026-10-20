# GoFile.io Telegram File Downloader Bot
FROM python:3.11-slim

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    MALLOC_ARENA_MAX=2 \
    DOWNLOAD_DIR=/app/downloads \
    DATA_DIR=/app/data \
    PIP_NO_CACHE_DIR=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PORT=10000

WORKDIR /app

# Install dependencies first for better layer caching
COPY requirements.txt ./
RUN pip install -r requirements.txt

# Application code (no tests, no secrets)
COPY gofile_bot/ ./gofile_bot/

# Run as a non-root user
RUN mkdir -p /app/downloads /app/data \
    && useradd --create-home --uid 10001 botuser \
    && chown -R botuser:botuser /app
USER botuser

EXPOSE 10000

# Platform-independent health check (Render uses healthCheckPath; this is
# for plain Docker / docker-compose environments).
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD ["python", "-c", "import os, urllib.request; urllib.request.urlopen('http://127.0.0.1:' + os.environ.get('PORT', '10000') + '/health', timeout=4)"]

CMD ["python", "-m", "gofile_bot"]
