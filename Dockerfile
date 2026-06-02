FROM python:3.12-slim AS base

# git is required by the GitHub source (GitPython shells out to it).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CALDERA_VAULT_PATH=/vault

WORKDIR /app

# Project metadata + source, then install (with the MCP extra so /mcp works).
COPY pyproject.toml README.md ./
COPY caldera ./caldera
RUN pip install --no-cache-dir '.[mcp]'

# Vault working tree (clone target / mount point).
RUN mkdir -p /vault
VOLUME ["/vault"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

CMD ["caldera"]
