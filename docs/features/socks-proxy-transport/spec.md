# Feature: SOCKS Proxy Transport for Chat Providers

## Problem Statement
Добавить поддержку SOCKS5‑прокси на транспортном уровне для всех chat‑провайдеров, сохраняя обратную совместимость и прозрачность маршрутизации запросов при прямом соединении или через прокси.

## Users / Actors
- Операторы/SRE, управляющие глобальными настройками прокси и проверяющие его здоровье.
- Разработчики интеграций/провайдеров, использующие фабрику HTTP‑клиентов без необходимости знать о наличии SOCKS.
- Клиентские приложения косвенно, через надёжное выполнение запросов и диагностируемые ошибки транспорта.

## Use Cases
- Оператор включает SOCKS5 и задаёт endpoint/аутентификацию для всего трафика, сохраняя OpenAI‑совместимые API без изменений.
- Провайдер, настроенный с `socks: inherit`, автоматически использует прокси при его доступности либо падает в строгом режиме.
- Провайдер с `socks: disable` всегда ходит напрямую, даже если глобально SOCKS включён.
- Провайдер с `socks: force` должен полностью заваливаться на ошибках прокси, без фолбека на прямое соединение.
- CLI `llm-proxy status` и `llm-proxy doctor` показывают состояние SOCKS, соединения и аутентификации для диагностики инцидентов.

## Feature Boundary & Responsibility
Фича отвечает за транспортный выбор и конфигурацию SOCKS‑прокси (вкл/выкл, strict/fallback, аутентификация, таймауты), за построение HTTP‑клиента для провайдеров и за наблюдаемость/диагностику статуса прокси. Она не меняет логику провайдеров, формирование payload или политику очереди.

## Execution Phase
Transport / Connectivity (выбор и построение транспорта перед вызовом апстрим‑провайдера).

## Conceptual Inputs & Outputs
### Inputs (conceptual)
- Глобальная конфигурация SOCKS (`LLM_SOCKS_*`: enabled/type/host/port/auth/strict/connect/read timeouts).
- Политика провайдера: `socks` ∈ {inherit, disable, force}.
- Запрос провайдера к апстриму (endpoint/URL) и контекст логирования.

### Outputs (conceptual)
- Сконфигурированный HTTP‑клиент/транспорт: direct или socks5.
- Решение о фолбеке или аварийной остановке в зависимости от strict/force.
- Диагностические записи (INFO/WARN/ERROR/DEBUG) и результаты health‑проверок для CLI/status/doctor.
- Ошибки транспорта, различающие `SOCKS_CONNECT_ERROR`, `SOCKS_AUTH_ERROR`, `SOCKS_TIMEOUT` без подмены на ошибки провайдера.

## Proposed Solution

### Affected Components
- Новый конфиг‑слой: `SocksSettings` (отражает `LLM_SOCKS_*`) + глобальный флаг enable/strict/таймауты/аутентификация/endpoint.
- Фабрика транспорта/клиента (`http_client_factory`): строит direct или socks5 транспорт в зависимости от политики провайдера и глобальных настроек; проксирует таймауты/ретраи.
- Провайдеры (например, `OpenAIChatProvider`): получают готовый HTTP‑клиент и не содержат логики SOCKS.
- CLI/UX (`llm-proxy status`, `llm-proxy doctor`): добавляют секцию SOCKS с конфигурацией, результатом tcp connect/auth/outbound теста и строгим/нестрогим статусом.
- Логирование/метрики: записи INFO/WARN/ERROR/DEBUG, новые счётчики `llm_socks_requests_total`, `llm_socks_errors_total`, `llm_requests_transport`.

### Data Models
- Требуется: `SocksSettings` — enabled/type/host/port/username/password/strict/connect_timeout/read_timeout.
- Требуется: `SocksPolicy` для провайдеров — enum {inherit, disable, force} с дефолтом inherit.
- Требуется: `SocksDiagnostics` — результирующие проверки (config parsed, tcp connect, auth, outbound test) с кодом статуса/ошибки.
- Используется: существующие провайдерские клиенты/фабрика HTTP транспорта.

### Interfaces
- Конфигурация окружения: `LLM_SOCKS_ENABLED`, `LLM_SOCKS_TYPE`, `LLM_SOCKS_HOST`, `LLM_SOCKS_PORT`, `LLM_SOCKS_USERNAME`, `LLM_SOCKS_PASSWORD`, `LLM_SOCKS_STRICT`, `LLM_SOCKS_CONNECT_TIMEOUT`, `LLM_SOCKS_READ_TIMEOUT`.
- Provider overrides (config/YAML): `providers.<name>.socks` = inherit|disable|force.
- Internal API: `http_client_factory.for_provider(provider)` возвращает клиент с учётом политики и глобальных настроек; ошибки транспорта маппятся в SOCKS‑специфичные коды.
- CLI: `llm-proxy status` (секция Socks Proxy), `llm-proxy doctor` (блок `[SOCKS PROXY]` с чек‑листом).
- Observability: метрики `llm_socks_requests_total{provider}`, `llm_socks_errors_total{reason=connect|auth|timeout}`, `llm_requests_transport{transport=socks|direct}`.

### Integration Notes
- Transport‑слой не должен менять payload или политику приоритетов; он предоставляется провайдерам как готовый клиент.
- При `LLM_SOCKS_ENABLED=false` поведение идентично текущему direct‑транспорту; `inherit` в провайдерах выбирает direct.
- Strict‑режим влияет на fallback: если SOCKS недоступен и strict=true либо провайдер force — запрос завершается ошибкой без direct‑ретрая.
- Фолбек на direct сопровождается WARN‑логом с провайдером; strict/force выдаёт ERROR и прерывает запрос.
- Ошибки SOCKS отделяются от ошибок апстрима для корректной диагностики и метрик.

### Feature Interfaces
- Потребляет гарантии от: глобальных Settings (env), фабрики HTTP‑клиентов, роутеров, которые не вмешиваются в транспорт.
- Поставляет гарантии: единообразное применение SOCKS по политике провайдера, фолбек/strict семантика, диагностируемые ошибки транспорта, метрики и CLI‑диагностика.

## Rules and Invariants
- Глобально SOCKS отключён по умолчанию; `inherit` провайдера следует глобальному enable/disable.
- `disable` всегда выбирает direct; `force` требует SOCKS даже если `enabled=false`.
- Strict=true приводит к немедленному отказу при ошибках соединения/аутентификации/таймаута; strict=false допускает fallback на direct с WARN.
- Тип поддерживается `socks5`; другие типы не должны silently деградировать (ошибка конфигурации).
- Таймауты connect/read применяются к SOCKS‑транспорту; превышение генерирует `SOCKS_TIMEOUT`.
- Логи INFO на старте фиксируют включённость/endpoint/strict; DEBUG логирует transport=... и target апстрима per request.
- Диагностика doctor/status не должна менять состояние; outbound тест использует фиксированный HTTPS endpoint (напр. `https://api.openai.com`).

## ADRs
- SOCKS трактуется как capability транспорта, а не логика провайдеров; провайдеры получают уже сконфигурированный клиент.
- Фолбек на direct допускается только при `strict=false` и политиках inherit/disable; force/strict комбинируются для fail‑fast.
- Диагностические команды CLI расширяются секцией SOCKS вместо отдельных API‑эндпоинтов.

## Non-Goals / Out of Scope
- HTTP‑proxy или комбинированный режим (будущее расширение).
- Пер‑запросные overrides SOCKS; политика задаётся глобально или на уровне провайдера.
- Специфическая логика TOR; поддержка ограничена общим SOCKS5.
- Многопроцессная синхронизация настроек; остаётся однопроцессный контур.

## Open Questions
- Нужен ли ретрай или backoff при временных ошибках SOCKS в strict=false, или достаточно однократного фолбека?
- Как выбирать outbound тест для doctor в приватных инсталляциях (конфигурируемый URL vs. дефолт `api.openai.com`)?
- Требуется ли экспортировать статус SOCKS в `/settings` или другом API для UI/observability?
