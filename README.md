# llm-proxy (Docker-first)

LLM Proxy сервис, рассчитанный на запуск **только в Docker**.  
Управление окружениями/профилями и запуском — через `cli/llm-proxy` (docker compose under the hood).

---

## TL;DR

```bash
./cli/llm-proxy init default
./cli/llm-proxy up
./cli/llm-proxy logs
```

---

## Требования

- Docker + Docker Compose v2 (`docker compose ...`)
- bash (macOS/Linux)

Проверка:
```bash
docker --version
docker compose version
```

---

## Быстрый старт

### 1) Инициализировать профиль (создаст env-файлы и каталоги)
```bash
./cli/llm-proxy init default
```

Это создаст два файла профиля (в конфиг-директории пользователя) и откроет редактор для `runtime env`:

- `<profile>.env` — переменные **для подстановки в compose** (порты/volumes/image/build/etc)
- `<profile>.llm.env` — **runtime-конфиг приложения** (LLM_API_BASE, LLM_MODEL_NAME, таймауты, кэш, очередь...)

Также будут созданы директории на хосте для bind mounts (кэш/логи/дампы), принадлежащие профилю.

### 2) Запустить сервис
```bash
./cli/llm-proxy up
```

### 3) Посмотреть логи
```bash
./cli/llm-proxy logs
```

### 4) Остановить
```bash
./cli/llm-proxy down
```

---

## Как это устроено

### Профили
Профиль = набор настроек и “свои” каталоги данных. Это удобно для:
- разных upstream провайдеров/ключей
- разных портов
- разных политик кэша/очереди
- переключения между контекстами (dev/stage/etc)

Основные команды:

```bash
./cli/llm-proxy list
./cli/llm-proxy use <name>
./cli/llm-proxy active
./cli/llm-proxy edit            # edit runtime env for active profile
./cli/llm-proxy edit-compose    # edit compose env for active profile
./cli/llm-proxy delete <name>
```

### Где лежат файлы профилей
CLI хранит профили в XDG-путях (или в legacy `~/.llm-proxy`, если он уже есть):

- Linux:
  - `~/.config/llm-proxy/profiles/*.env`
  - `~/.config/llm-proxy/profiles/*.llm.env`
- macOS:
  - аналогично (через `$XDG_CONFIG_HOME`, если задан)

Профильные данные (bind mounts):
- кэш: `~/.cache/llm-proxy/profiles/<name>/`
- логи: `~/.local/share/llm-proxy/logs/<name>/`
- дампы: `~/.local/share/llm-proxy/profiles/<name>/crash_dumps/`

> При `llm-proxy delete <name>` эти каталоги удаляются.

---

## Конфигурация (env)

### 1) Compose env (`<profile>.env`)
Это переменные **для docker compose**, например:

- `LLM_IMAGE`, `LLM_BUILD_CONTEXT`, `LLM_DOCKERFILE`
- `LLM_PORT`
- `CACHE_HOST_DIR`, `LOGS_HOST_DIR`, `DUMP_HOST_DIR`
- `CACHE_CONTAINER_DIR`, `LOGS_CONTAINER_DIR`, `DUMP_CONTAINER_DIR`
- `LLM_ENV_FILE` — путь к `<profile>.llm.env`

### 2) Runtime env (`<profile>.llm.env`)
Это переменные приложения (из `src/llm/config/settings.py`), например:

- `LLM_API_BASE`, `LLM_API_KEY`, `LLM_MODEL_NAME`
- `LLM_TIMEOUT`, `LLM_MAX_CONCURRENT`
- `LLM_PRIORITY_SLOTS` (JSON строка)
- `LLM_CACHE_ENABLED`, `CACHE_CONTAINER_DIR`, `LLM_CACHE_FILE`, `LLM_CACHE_TTL`
- `ENV`, `TESTING`, `LOG_LEVEL`

Пример `LLM_PRIORITY_SLOTS`:
```dotenv
LLM_PRIORITY_SLOTS={"p1":2,"p2":1,"p3":1}
```

---

## Запуск не из репозитория

Если CLI установлен в PATH (см. ниже), то чтобы запускать из любого места,
нужно указать путь к репозиторию (где лежит `compose.yaml`):

```bash
export LLM_PROXY_PROJECT_DIR=/path/to/llm-proxy-repo
llm-proxy up
```

---

## Установка CLI (опционально)

Можно установить скрипт в `~/.local/bin`:

```bash
./cli/llm-proxy install
```

Проверка:
```bash
which llm-proxy
llm-proxy help
```

Удаление:
```bash
llm-proxy uninstall
```

---

## Полезные команды

Статус контейнеров (для активного профиля):
```bash
./cli/llm-proxy status
```

Переключить профиль (остановит старый best-effort, активирует новый):
```bash
./cli/llm-proxy use staging
./cli/llm-proxy up
```

---

## Endpoints

Реальный список зависит от роутеров в `src/llm/routers/`, но обычно:
- `GET /health` — healthcheck (если включён)
- `GET /metrics` — Prometheus метрики
- `POST /llm` или `/v1/chat/...` — LLM proxy endpoints

---

## Разработка

Установка dev-зависимостей:
```bash
python -m pip install -r requirements.txt -r requirements-dev.txt
```

Линт/типы/тесты (если есть `Makefile`):
```bash
make lint
make test
```

---

## Troubleshooting

### “compose.yaml not found”
Запускай CLI:
- из корня репозитория (где `compose.yaml`), **или**
- установи `LLM_PROXY_PROJECT_DIR`:
  ```bash
  export LLM_PROXY_PROJECT_DIR=/path/to/repo
  ```

### Порт занят
Поменяй `LLM_PORT` в `<profile>.env` (compose env) или в `.env` если используешь его напрямую.

### Нужно быстро посмотреть/изменить env
```bash
./cli/llm-proxy edit
./cli/llm-proxy edit-compose
```

---
