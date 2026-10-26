FROM python:3.12-slim

LABEL org.opencontainers.image.title="posthouse" \
      org.opencontainers.image.description="Posthouse: shared, versioned memory for all your AI agents (MCP + REST)" \
      org.opencontainers.image.source="https://github.com/conscienceli/posthouse" \
      org.opencontainers.image.licenses="MIT"

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_NO_CACHE_DIR=1 \
    HOME=/tmp \
    POSTHOUSE_DATA_DIR=/data \
    POSTHOUSE_HOST=0.0.0.0 \
    POSTHOUSE_PORT=8080

RUN apt-get update \
    && apt-get install -y --no-install-recommends git tini \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app
COPY requirements.txt .
RUN pip install -r requirements.txt

COPY posthouse ./posthouse
RUN mkdir -p /data && chmod 0777 /data

# Run as an unprivileged user by default; docker-compose overrides the uid/gid so the
# bind-mounted ./data directory stays owned by the host user.
USER 1000:1000
VOLUME ["/data"]
EXPOSE 8080

HEALTHCHECK --interval=30s --timeout=5s --start-period=10s --retries=3 \
  CMD python -c "import urllib.request; urllib.request.urlopen('http://127.0.0.1:8080/healthz', timeout=4)" || exit 1

ENTRYPOINT ["/usr/bin/tini", "--"]
CMD ["python", "-m", "posthouse"]
