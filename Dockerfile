# syntax=docker/dockerfile:1.7
FROM python:3.12-slim AS builder
WORKDIR /app
COPY pyproject.toml* ./
RUN if [ -f pyproject.toml ]; then \
      python -m venv /venv && \
      /venv/bin/pip install --upgrade pip && \
      /venv/bin/pip install -e '.[dev]' ; \
    else \
      python -m venv /venv ; \
    fi

FROM python:3.12-slim AS runtime
RUN useradd --create-home --shell /bin/bash ingester
WORKDIR /app
COPY --from=builder /venv /venv
COPY --chown=ingester:ingester . .
USER ingester
ENV PATH="/venv/bin:${PATH}" \
    INGESTER_OUT_DIR=/app/out \
    INGESTER_STATE_DIR=/app/state \
    INGESTER_CONFIG_DIR=/app/config \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
VOLUME ["/app/out", "/app/state", "/app/config"]
ENTRYPOINT ["ingester"]
CMD ["--help"]
