# Docker + compose

## Purpose

Give the ingester a reproducible container image and a compose file that stands up LocalAI as a sibling service. The image is the deployable unit for CI, self-hosted runners, and any future container platform. Compose is the local convenience wrapper that removes the "where is LocalAI running" question during development.

## Image layout

The `Dockerfile` is multi-stage.

**Stage 1 (`builder`)** is a full `python:3.12-slim` image. It creates a virtualenv at `/venv` and, if `pyproject.toml` is present, installs the project in editable mode with the `dev` extras into that venv. If `pyproject.toml` is not yet present (Phase 0.0 grace), it still creates an empty venv so the runtime stage has something to copy. This mirrors the pre-commit hook's Phase 0.0 grace behavior and keeps the image buildable before Task 1 lands.

**Stage 2 (`runtime`)** is a fresh `python:3.12-slim`. It creates a non-root `ingester` user, copies the venv from the builder stage, then copies the source tree with `--chown=ingester:ingester`. `PATH` is prepended with `/venv/bin` so `ingester` (the console script from `pyproject.toml`) resolves without any activation step. Environment variables `INGESTER_OUT_DIR`, `INGESTER_STATE_DIR`, and `INGESTER_CONFIG_DIR` point at the three declared `VOLUME`s.

`ENTRYPOINT ["ingester"]` with `CMD ["--help"]` means `docker run legal-corpus-ingester:dev` prints usage by default. Overriding the command (`docker run ... run --config config/us.yaml`) passes through to the CLI.

## Why non-root

The `_AUTOMATION` hub convention is least-privilege by default. Running the ingester as a non-root user reduces the blast radius if the container is ever compromised. It also flags any accidental writes to system paths at build time rather than at runtime. The `--chown` on the source copy avoids the "files owned by root, process running as ingester" trap that surfaces later as opaque permission errors.

## Volume mounts

Three declared mounts, all writable by the `ingester` user:

| Mount | Purpose |
|-------|---------|
| `/app/out` | Bundle artifacts. Final `.jsonl` / `.parquet` / manifest bundles land here. |
| `/app/state` | `manifest.json`, checkpoints, `license-hashes.json`. Persists across runs so resumability works. |
| `/app/config` | Source YAMLs (`us.yaml`, `eu.yaml`, etc.). Read-only in practice, but declared writable to keep the compose file symmetric. |

Compose binds each of these to the equivalent host directory under the repo root, so local runs land artifacts where the developer can inspect them without `docker cp`.

## LocalAI sibling service

`docker-compose.yml` adds a `localai` service using the `localai/localai:latest-aio-cpu` image on port 8080. The ingester service sets `INGESTER_LOCALAI_URL=http://localai:8080/v1`, which is the value Task P7 expects the embedder to read. This means the embedder has a target from day one; there is no window where the ingester is built but has nowhere to embed against.

The `depends_on: [localai]` ordering does not wait for LocalAI to be *ready*, only *started*. The embedder is expected to handle connection-refused retries on its own, which matches how it will behave against a remote LocalAI in production.

## `docker compose` vs `docker run`

Use `docker compose up` when you want the ingester and LocalAI together. This is the typical developer workflow because it avoids the "did I remember to start LocalAI" step.

Use `docker run legal-corpus-ingester:dev` (with `-v` flags mirroring the compose mounts) when LocalAI is already running elsewhere. Common cases are: LocalAI is running natively on the host, LocalAI is on a shared dev server, or CI provides its own model-serving endpoint. Point `INGESTER_LOCALAI_URL` at the external endpoint via `-e INGESTER_LOCALAI_URL=...`.

## Build cache invalidation

The Dockerfile copies `pyproject.toml*` before the rest of the source. This means:

- A change to `pyproject.toml` re-invalidates the venv-install layer and pip re-resolves. Slow but rare.
- A change to source code under `src/` (with `pyproject.toml` unchanged) skips the venv layer entirely and only re-runs the final `COPY . .`. Fast, on the order of a second or two.

`.dockerignore` excludes `.git`, `.venv`, `__pycache__`, `out/`, `state/`, and other local scratch so the build context stays small and code-only changes stay cache-friendly.

## Image size

Expect the runtime image to land around 150 to 220 MB. Slim base is roughly 45 MB, the venv adds the rest, dominated by whatever the `dev` extras pull in (pytest, mypy, ruff). If the image drifts materially past 250 MB, that is a signal to audit the `dev` extras or move some of them to a separate `test` extras group so they do not ship in the runtime.

LocalAI's `latest-aio-cpu` image is much larger, roughly 2 GB, because it bundles model runtimes. That is expected. It is pulled once and cached locally.

## When Docker Desktop is not running

On macOS the daemon runs inside Docker Desktop, not as a system service. If `docker info` reports "Cannot connect to the Docker daemon", start it with:

```
open -a Docker
```

The daemon typically takes 20 to 60 seconds to come up. A short polling loop is the reliable pattern:

```
for i in $(seq 1 36); do
    docker info >/dev/null 2>&1 && break
    sleep 5
done
```

If it does not come up within a few minutes, check Docker Desktop's own UI. The CLI has no useful further signal beyond "not ready".

## Common commands

| Task | Command |
|------|---------|
| Build the image | `docker build -t legal-corpus-ingester:dev .` |
| Print CLI help | `docker run --rm legal-corpus-ingester:dev` |
| Run against a config | `docker run --rm -v $PWD/out:/app/out -v $PWD/state:/app/state -v $PWD/config:/app/config legal-corpus-ingester:dev run --config config/us.yaml` |
| Stand up ingester + LocalAI | `docker compose up` |
| Tear down | `docker compose down` |
| Inspect image size | `docker images legal-corpus-ingester:dev` |
