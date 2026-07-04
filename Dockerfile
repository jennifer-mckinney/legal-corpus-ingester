# syntax=docker/dockerfile:1.7
# Base image digest-pinned (SecF4/G6). Pulled 2026-07-03.
FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS builder
WORKDIR /app
COPY pyproject.toml* ./
COPY src/ ./src/
RUN python -m venv /venv && \
    /venv/bin/pip install --upgrade pip && \
    /venv/bin/pip install -e .

FROM builder AS test
RUN /venv/bin/pip install -e '.[dev]'
COPY . .

FROM python:3.12-slim@sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf AS runtime
RUN useradd --create-home --shell /bin/bash ingester
WORKDIR /app
COPY --from=builder /venv /venv
COPY --chown=ingester:ingester . .
# Entrypoint wrapper fails fast if `ingester` is missing (G7 safety net)
COPY --chown=root:root docker-entrypoint.sh /usr/local/bin/docker-entrypoint.sh
RUN chmod +x /usr/local/bin/docker-entrypoint.sh
USER ingester
ENV PATH="/venv/bin:${PATH}" \
    INGESTER_OUT_DIR=/app/out \
    INGESTER_STATE_DIR=/app/state \
    INGESTER_CONFIG_DIR=/app/config \
    PYTHONUNBUFFERED=1 \
    PYTHONDONTWRITEBYTECODE=1
VOLUME ["/app/out", "/app/state", "/app/config"]
ENTRYPOINT ["/usr/local/bin/docker-entrypoint.sh"]
CMD ["--help"]
