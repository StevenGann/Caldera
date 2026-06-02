FROM python:3.12-slim AS base

# git is required by the GitHub source (GitPython shells out to it).
RUN apt-get update \
    && apt-get install -y --no-install-recommends git \
    && rm -rf /var/lib/apt/lists/*

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    CALDERA_VAULT_PATH=/vault

WORKDIR /app

# Install dependencies first for better layer caching.
COPY pyproject.toml README.md ./
RUN pip install --no-cache-dir .

# Then the application code.
COPY caldera ./caldera

# Vault working tree (clone target / mount point).
RUN mkdir -p /vault
VOLUME ["/vault"]

EXPOSE 8000

HEALTHCHECK --interval=30s --timeout=5s --start-period=20s --retries=3 \
    CMD python -c "import urllib.request,sys; sys.exit(0 if urllib.request.urlopen('http://localhost:8000/healthz').status==200 else 1)"

CMD ["caldera"]
