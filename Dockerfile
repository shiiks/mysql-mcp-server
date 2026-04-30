# syntax=docker/dockerfile:1

FROM registry.access.redhat.com/ubi8/python-312

# curl is only needed for HEALTHCHECK; everything else is pure Python.
RUN apt-get update \
    && apt-get install -y --no-install-recommends curl \
    && rm -rf /var/lib/apt/lists/*

WORKDIR /app

COPY requirements.txt ./
RUN pip install --no-cache-dir -r requirements.txt

COPY server.py auth.py ./

# Defaults aimed at remote deployment. Override via -e at run time.
ENV MCP_TRANSPORT=sse \
    MCP_HTTP_HOST=0.0.0.0 \
    MCP_HTTP_PORT=8080 \
    IDE_AUTH_REQUIRED=true \
    PYTHONUNBUFFERED=1

EXPOSE 8080

# Health endpoint is unauthenticated by design; auth would block the orchestrator's probes.
HEALTHCHECK --interval=30s --timeout=5s --start-period=15s --retries=3 \
    CMD curl -fsS http://127.0.0.1:8080/healthz || exit 1

# Drop privileges. The app never needs to write to the filesystem.
RUN useradd --create-home --shell /usr/sbin/nologin app
USER app

CMD ["python", "server.py"]
