FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip \
    && rm -rf /var/lib/apt/lists/*

# Modern YouTube extraction needs a JavaScript runtime for challenge solving.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- --yes --no-modify-path \
    && deno --version

WORKDIR /app
COPY requirements.txt ./
# yt-dlp recommends the nightly/pre-release channel when stable breaks on sites.
RUN pip install --upgrade pip && pip install --pre -U -r requirements.txt
COPY app ./app

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/runtime/downloads \
    && chown -R appuser:appuser /app
USER appuser

EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 CMD python -c "import os,urllib.request; p=os.getenv('PORT','10000'); urllib.request.urlopen('http://127.0.0.1:'+p+'/healthz', timeout=3)"
CMD ["sh", "-c", "uvicorn app.main:app --host 0.0.0.0 --port ${PORT:-10000} --workers 1 --proxy-headers --forwarded-allow-ips '*'"]
