# ── Stage 1: install dependencies ──────────────────────────────────────────
FROM python:3.12-slim-bookworm AS builder

WORKDIR /app

RUN apt-get update && apt-get install -y --no-install-recommends \
    gcc \
    libpq-dev \
    git \
    && rm -rf /var/lib/apt/lists/*

COPY --from=ghcr.io/astral-sh/uv:latest /uv /usr/local/bin/uv

COPY pyproject.toml uv.lock ./

# invoice-models is a private GitHub dependency — requires a token at build time.
# The secret is mounted read-only and never written into any image layer.
RUN --mount=type=secret,id=github_token \
    export GITHUB_TOKEN=$(cat /run/secrets/github_token) \
    && git config --global url."https://x-access-token:${GITHUB_TOKEN}@github.com/".insteadOf "https://github.com/" \
    && uv sync --no-dev --frozen \
    && git config --global --remove-section "url.https://x-access-token:${GITHUB_TOKEN}@github.com/"


# ── Stage 2: runtime ───────────────────────────────────────────────────────
FROM python:3.12-slim-bookworm AS runtime

WORKDIR /app

RUN groupadd -g 1001 appgroup \
    && useradd -u 1001 -g appgroup -s /bin/sh -m appuser

COPY --from=builder /app/.venv /app/.venv
COPY app/ /app/app/

ENV PATH="/app/.venv/bin:$PATH"
ENV PYTHONPATH="/app"

USER 1001

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
