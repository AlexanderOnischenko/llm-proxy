# llm-proxy (Docker-first)

`llm-proxy` is an LLM proxy service with queueing, priorities, and caching, designed **exclusively to run in Docker**.
Environments and profiles are managed through the `llm-proxy` CLI, which operates on top of `docker compose`.

The project is designed for:

* proxying OpenAI-compatible LLM APIs,
* limiting and balancing load,
* isolating configurations through profiles,
* use as a shared LLM backend.

---

## TL;DR

```bash
./cli/llm-proxy init default
./cli/llm-proxy up
./cli/llm-proxy logs
```

By default, the service will be available at:

```text
http://localhost:8002
```

---

## Requirements

* Docker
* Docker Compose v2 (`docker compose ...`)
* bash (Linux / macOS)

Check your installation:

```bash
docker --version
docker compose version
```

---

## Architectural Principles (Important)

* ❗ **Single-worker only**
* ❌ Gunicorn is not supported
* ❌ Multiprocess Prometheus is not supported
* ❌ `WEB_CONCURRENCY` / `UVICORN_WORKERS > 1` are prohibited

These constraints are validated when the container starts.
If any of them are violated, the container exits with an error.

---

## Quick Start

### 1. Initialize a Profile

```bash
./cli/llm-proxy init default
```

This creates a `default` profile containing:

* `<profile>.env` — **docker compose** environment variables
* `<profile>.llm.env` — **application runtime configuration**
* bind-mount directories for cache, logs, and crash dumps

The profile files are automatically opened in your editor.

---

### 2. Start

```bash
./cli/llm-proxy up
```

### 3. View Logs

```bash
./cli/llm-proxy logs
```

### 4. Stop

```bash
./cli/llm-proxy down
```

---

## CLI

### Main Commands

```bash
llm-proxy init <name>      # create a profile
llm-proxy list             # list profiles
llm-proxy use <name>       # make a profile active
llm-proxy active           # show the active profile
llm-proxy up [name]        # start the service
llm-proxy down             # stop the service
llm-proxy logs             # show logs
llm-proxy status           # show container status
llm-proxy delete <name>    # delete a profile and its data
```

### Editing Configuration

```bash
llm-proxy edit             # runtime env
llm-proxy edit-compose     # compose env
```

---

## Profile Storage

XDG paths are used by default, with legacy `~/.llm-proxy` paths supported if they already exist.

### Configuration

```text
~/.config/llm-proxy/profiles/
  ├── default.env
  ├── default.llm.env
  └── staging.env
```

### Profile Data (Bind Mounts)

```text
~/.cache/llm-proxy/profiles/<profile>/
~/.local/share/llm-proxy/logs/<profile>/
~/.local/share/llm-proxy/profiles/<profile>/crash_dumps/
```

Running `llm-proxy delete <profile>` removes all of these directories.

---

## Makefile (When Working from the Repository)

### CLI

```bash
make cli-install
make cli-reinstall
make cli-uninstall
```

### Service Management

```bash
make up PROFILE=default
make down PROFILE=default
make logs PROFILE=default
make status PROFILE=default
```

### Development

```bash
make lint
make test
make help
```

---

## Runtime Configuration (`<profile>.llm.env`)

### Upstream LLM

```env
LLM_PROVIDER=openai
LLM_API_BASE=https://api.llm7.io/v1
LLM_API_KEY=...
LLM_DEFAULT_MODEL_NAME=deepseek-v3-0324
# optional hard override (forces this model for all requests)
# LLM_MODEL_NAME=gemini-2.0-flash
LLM_TEMPERATURE=0.7
LLM_TIMEOUT=300
```

Any OpenAI-compatible API is supported.

---

## Queue and Priorities

The following priorities are supported:

* `p1` — high priority
* `p2` — default
* `p3` — background

### Basic Settings

```env
LLM_MAX_CONCURRENT=3
LLM_PRIORITY_DEFAULT=p2
LLM_QUEUE_MAX_LEN=100
```

### Slot Limits (Recommended)

```env
LLM_PRIORITY_SLOTS={"p1":2,"p2":1,"p3":1}
```

### Legacy Compatibility

```env
LLM_PRIORITY_SLOTS_P1=2
LLM_PRIORITY_SLOTS_P2=1
LLM_PRIORITY_SLOTS_P3=1
```

If `LLM_PRIORITY_SLOTS` is set, the legacy fields are ignored.

---

## Caching

A **single shared SQLite cache** is used across all chat endpoints.

### Settings

```env
LLM_CACHE_ENABLED=true
LLM_CACHE_TTL=600000
```

### Paths

```env
LLM_CACHE_PATH=/tmp/llm_cache.db
```

Or, for legacy configuration:

```env
CACHE_CONTAINER_DIR=/cache
LLM_CACHE_FILE=llm_cache.sqlite
```

Path resolution order:

1. Use `LLM_CACHE_PATH` if it is set.
2. Otherwise use `CACHE_CONTAINER_DIR/LLM_CACHE_FILE`.

---

## HTTP API

### Main Endpoints

* `POST /llm`
* `POST /v1/chat/completions`

The request format is compatible with the OpenAI Chat Completions API.

---

## Running Outside the Repository Directory

If the CLI is installed in `$PATH`, but you are not currently inside the repository directory:

```bash
export LLM_PROXY_PROJECT_DIR=/path/to/llm-proxy
llm-proxy up
```

It is recommended to add this variable to your shell profile.

---

## Installing the CLI

```bash
./cli/llm-proxy install
```

Verify the installation:

```bash
llm-proxy help
```

Uninstall:

```bash
llm-proxy uninstall
```

---

## Troubleshooting

### compose.yaml not found

Either:

* you are not in the repository root, or
* `LLM_PROXY_PROJECT_DIR` is not set.

### Port Already in Use

Change `LLM_PORT` in `<profile>.env`.

### Container Exits Immediately

Check that none of the following are set:

* `WEB_CONCURRENCY`
* `UVICORN_WORKERS`
* `PROMETHEUS_MULTIPROC_DIR`

---

## License

MIT
