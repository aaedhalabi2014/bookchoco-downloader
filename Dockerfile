FROM python:3.13-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    DENO_DIR=/tmp/deno-cache \
    DENO_NO_PROMPT=1 \
    DENO_NO_UPDATE_CHECK=1

RUN apt-get update \
    && apt-get install -y --no-install-recommends ffmpeg ca-certificates curl unzip git \
    && rm -rf /var/lib/apt/lists/*

# JavaScript runtime required by current YouTube extraction/challenge solving.
RUN curl -fsSL https://deno.land/install.sh | DENO_INSTALL=/usr/local sh -s -- --yes --no-modify-path \
    && deno --version

# Local PO-token provider. It stays bound to 127.0.0.1 inside this container;
# it is never exposed as a public Render port.
RUN git clone --depth 1 --branch 2.0.0 https://github.com/Brainicism/bgutil-ytdlp-pot-provider.git /opt/bgutil-ytdlp-pot-provider \
    && cd /opt/bgutil-ytdlp-pot-provider/server \
    && deno install --allow-scripts=npm:canvas --frozen \
    && chmod -R a+rX /opt/bgutil-ytdlp-pot-provider

WORKDIR /app
COPY requirements.txt ./
RUN pip install --upgrade pip && pip install --pre -U -r requirements.txt

COPY app ./app
COPY start.sh ./start.sh

RUN useradd --create-home --uid 10001 appuser \
    && mkdir -p /app/runtime/downloads \
    && chown -R appuser:appuser /app \
    && chmod +x /app/start.sh

USER appuser

EXPOSE 10000
HEALTHCHECK --interval=30s --timeout=5s --start-period=35s --retries=3 CMD python -c "import os,urllib.request; p=os.getenv('PORT','10000'); urllib.request.urlopen('http://127.0.0.1:'+p+'/healthz', timeout=3)"

CMD ["/app/start.sh"]
