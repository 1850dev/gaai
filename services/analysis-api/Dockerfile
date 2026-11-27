FROM python:3.12.16-slim-trixie AS builder

ENV PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_NO_CACHE_DIR=1

WORKDIR /build
COPY pyproject.toml requirements.lock ./
RUN python -m pip wheel --require-hashes --wheel-dir /wheels -r requirements.lock
COPY src ./src
RUN python -m pip wheel --no-deps --wheel-dir /wheels .

FROM python:3.12.16-slim-trixie AS runtime

ENV PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1 \
    PORT=8080

# Debian's security release fixes CVE-2026-124851.
RUN apt-get update \
    && apt-get install --yes --no-install-recommends libzstd1=1.5.7+dfsg-1+deb13u1 \
    && rm -rf /var/lib/apt/lists/* \
    && addgroup --system app \
    && adduser --system --ingroup app app
COPY --from=builder /wheels /wheels
RUN python -m pip install --no-cache-dir /wheels/* && rm -rf /wheels

USER app
WORKDIR /app
EXPOSE 8080
CMD ["sh", "-c", "uvicorn analytics_ai.api.app:app --host 0.0.0.0 --port ${PORT}"]
