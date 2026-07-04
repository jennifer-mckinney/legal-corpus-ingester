# Docker + compose

## Purpose

Give the ingester a reproducible container image and a compose file that stands up LocalAI as a sibling service. The image is the deployable unit for CI, self-hosted runners, and any future container platform. Compose is the local convenience wrapper that removes the "where is LocalAI running" question during development.

## Image layout

The `Dockerfile` is multi-stage.

**Stage 1 (`builder`)** is a full `python:3.12-slim` image. It creates a virtualenv at `/venv` and, if `pyproject.toml` is present, installs the project in editable mode with the `dev` extras into that venv. If `pyproject.toml` is not yet present (Phase 0.0 grace), it still creates an empty venv so the runtime stage has something to copy. This mirrors the pre-commit hook's Phase 0.0 grace behavior and keeps the image buildable before Task 1 lands.

**Stage 2 (`runtime`)** is a fresh `python:3.12-slim`. It creates a non-root `ingester` user, copies the venv from the builder stage, then copies the source tree with `--chown=ingester:ingester`. `PATH` is prepended with `/venv/bin` so `ingester` (the console script from `pyproject.toml`) resolves without any activation step. Environment variables `INGESTER_OUT_DIR`, `INGESTER_STATE_DIR`, and `INGESTER_CONFIG_DIR` point at the three declared `VOLUME`s.

`ENTRYPOINT` is a small wrapper script (`docker-entrypoint.sh`) that checks whether the `ingester` console script actually exists in the venv. If it does, it `exec`s `ingester "$@"` — so `docker run legal-corpus-ingester:dev` prints usage by default and `docker run ... run --config config/us.yaml` passes through to the CLI unchanged. If the script is missing (Phase 0.0 grace-path build, no `pyproject.toml`), the wrapper prints a clear diagnostic and exits 1 rather than the opaque `exec: "ingester": executable file not found` that plain `ENTRYPOINT ["ingester"]` would emit. This is the G7 fix.

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

## Base image digest pins

Both base images are pinned by SHA256 digest rather than floating tag. This closes the SecF4 / G6 supply-chain risk: a compromised or silently-rotated upstream tag cannot land in a rebuild without a matching source-tree change. Digests refreshed 2026-07-03:

| Image | Tag when pulled | Digest |
|-------|-----------------|--------|
| `python:3.12-slim` | `3.12-slim` | `sha256:423ed6ab25b1921a477529254bfeeabf5855151dc2c3141699a1bfc852199fbf` |
| `localai/localai` | `latest-aio-cpu` | `sha256:4cbc20c59558ed6c1cae9bc3f6ae34d75d390b370aa8ac62a59327089fa56cec` (multi-arch index) |

To refresh:

```
docker pull python:3.12-slim
docker inspect --format '{{index .RepoDigests 0}}' python:3.12-slim
docker buildx imagetools inspect localai/localai:latest-aio-cpu | head
```

Update the `FROM` lines in `Dockerfile` and the `image:` field in `docker-compose.yml`, then update the table above with the new digest and the pull date.

## Model directory and path traversal

`./models` is a bind mount owned by LocalAI, not by the ingester service. The ingester does not mount `./models` at all — LocalAI is the only writer. A `models/.gitkeep` is tracked so the directory exists with the correct perms on a fresh checkout. If a future service needs to read model metadata, mount it read-only (`- ./models:/models:ro`) so writes remain a LocalAI-only capability (SecF5).

Model filenames referenced in configs MUST match `^[a-zA-Z0-9_.-]+$` to prevent path traversal. Enforcement is added at config load time in Task 7 (P7). This constraint is surfaced here so config authors know the shape before the enforcement code lands.

## Known Phase 0.0 gaps

- **Runtime image ships `dev` extras.** The builder stage runs `pip install -e '.[dev]'` which pulls pytest, mypy, and ruff into `/venv`. Because the runtime stage copies `/venv` verbatim, those tools ride along into production images. This is fine for now because `pyproject.toml` does not yet exist and the `.[dev]` branch is not exercised. Tracked as SecF8. Task 2 will land `pyproject.toml`; at that point split extras so `runtime` installs only the runtime deps and a separate `test` stage installs `.[dev]` for CI. TODO comment lives in `Dockerfile`.
- **Empty-venv builder branch.** Without `pyproject.toml` the builder falls through to `python -m venv /venv` and produces an empty venv. The image builds green but has no `ingester` console script. `docker-entrypoint.sh` guards this by printing a clear diagnostic and exiting 1 (G7).
