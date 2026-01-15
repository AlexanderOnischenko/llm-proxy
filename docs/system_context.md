# System Context

## Overview
LLM Proxy — FastAPI-сервис, предоставляющий OpenAI-совместимый API поверх разных upstream LLM-провайдеров с контролем конкурентности, кэшированием, приоритизацией и общей транспортной политикой (включая управляемый SOCKS-proxy) без внедрения прокси-логики в провайдеры.

## Key Scenarios
- Chat completion: клиент вызывает `POST /v1/chat/completions`, сервис выбирает провайдера и транспорт (direct/SOCKS), применяет очередь/кэш и возвращает ответ.
- Cache hit / inflight dedup: повторяющиеся запросы получают ответ из TTL-кэша или ожидают inflight-результат без повторного вызова upstream.
- SOCKS strict / fallback: при включённом SOCKS политика решает использовать SOCKS; при ошибках connect/auth — либо fast-fail (strict), либо единичный fallback в direct (non-strict).
- Diagnostics: оператор/SRE вызывает `GET /status` и `GET /doctor`, чтобы увидеть конфигурацию/рантайм SOCKS и результаты проверок доступности.
- CLI observability: `llm-proxy status` и `llm-proxy doctor` показывают runtime-состояние (docker compose) и сетевую диагностику через HTTP API сервиса.

## Core Components
| Component | Responsibility |
|----------|----------------|
| FastAPI App (llm.main) | Инициализация приложения, роутеров, lifecycle (startup/shutdown), вывод startup-логов конфигурации (без секретов). |
| Routers (chat/health/metrics/status/doctor) | Экспонирование `POST /v1/chat/completions`, `GET /health`, `GET /metrics`, `GET /status`, `GET /doctor`. |
| Settings (llm.config.settings.Settings) | Загрузка конфигурации (в т.ч. SOCKS global + provider overrides), derived-поля, overrides для тестов. |
| DI / deps (llm.deps) | Создание singleton-зависимостей: очередь, кэш, provider factory, transport/http client factory; best-effort закрытие клиентов на shutdown. |
| ChatProvider abstraction (llm.services.chat_provider) | Единый интерфейс вызова upstream; провайдер получает готовый HTTP-клиент/транспорт и не содержит SOCKS-ветвлений. |
| Transport/HttpClientFactory | Построение/кэширование long-lived httpx клиентов (direct и socks) и выдача их по резолвнутой политике. |
| SocksPolicyResolver | Резолвинг политики SOCKS per-provider: disable/inherit/force на основе global settings и overrides. |
| Cache (llm.services.cache.SQLiteCache) | TTL-кэш, pickle storage, inflight-dedup, метрики hits/misses/ошибки. |
| PriorityChatQueue (llm.services.priority_queue) | Ограничение конкурентности, приоритизация, max_queue, метрики inflight/latency/outcomes. |
| Client factory (llm.services.client.create_custom_llm) | Создание LangChain ChatOpenAI/обёртки с инъекцией готового httpx async client (когда поддержано). |
| Wrapper (llm.services.wrapper.CachedQueuedChatOpenAI) | Асинхронные вызовы через очередь + кэш; блок синхронных методов для сохранения инвариантов. |
| Metrics (llm.metrics.prometheus + service metrics) | `/metrics`, метрики очереди/кэша и метрики транспорта/ошибок SOCKS. |
| CLI (llm-proxy) | Управление runtime (up/down/logs/status) и диагностика сети (`doctor`) через HTTP API сервиса. |

## Core Entities
| Entity | Description |
|--------|-------------|
| Settings | Единый источник конфигурации сервиса, включая `socks.*` (enabled/type/endpoint/auth/strict/timeouts) и overrides по провайдерам. |
| ProviderName | Ключ провайдера (например `openai`, `gigachat`, `openrouter`), используемый для выбора реализации и политики SOCKS. |
| SocksPolicy | Результат резолвинга per-provider: `disable` / `inherit` / `force` → итоговый transport `direct` или `socks`. |
| HttpClient | Long-lived httpx.AsyncClient, созданный фабрикой для `direct` или `socks` транспорта. |
| SocksConfigView | Безопасное представление конфигурации SOCKS для `/status` (без username/password, только `username_set`). |
| SocksRuntimeStatus | Рантайм-состояние SOCKS (OK/ERROR/DISABLED), error без секретов, last_checked_at. |
| DoctorReport | Отчёт `/doctor`: список проверок и краткий impact (какие режимы/провайдеры затронуты). |
| DoctorCheck | Одна проверка doctor (tcp_connect/auth/outbound_test) с ok/details. |
| ChatCompletionRequest/Response | Доменные модели запроса/ответа OpenAI-style, используемые роутером и провайдером. |
| CacheKey | Стабильный ключ запроса для TTL-кэша и inflight-dedup. |
| QueueTask | Единица работы в PriorityChatQueue (callable + priority + метаданные endpoint/provider). |

## System Data Flow
1. Клиент отправляет `POST /v1/chat/completions`.
2. Роутер валидирует вход и получает зависимости: Settings, Cache, Queue, Provider, Transport/HttpClientFactory.
3. Определяется `provider_name` и резолвится SocksPolicy (override→global→default).
4. Transport/HttpClientFactory выдаёт httpx.AsyncClient для `direct` или `socks` (lazy singleton, pool reuse).
5. Запрос проходит через кэш: вычисляется CacheKey → hit возвращается сразу; miss идёт дальше.
6. Miss ставится в PriorityChatQueue; выполняется callable, вызывающий ChatProvider с готовым HttpClient.
7. При SOCKS-ошибках connect/auth/connect-timeout:
   - strict=true: запрос завершается ошибкой SOCKS без fallback;
   - strict=false: выполняется ровно один fallback в direct (только до handshake), логируется warning и инкрементируются метрики ошибок.
8. Успешный ответ сохраняется в кэш и возвращается клиенту.
9. Метрики фиксируют транспорт (`direct|socks`) на запрос и ошибки SOCKS по причинам; debug-логи содержат `provider` и `transport`.
10. `GET /status` возвращает агрегированный статус, включая socks config/runtime; `GET /doctor` выполняет проверки (парсинг конфига, tcp_connect, auth при наличии, outbound_test) и возвращает DoctorReport.

## Notes
- Совместимость: при `LLM_SOCKS_ENABLED=false` все исходящие запросы идентичны direct-поведению.
- Provider-agnostic: ChatProvider не читает SOCKS env/settings и не содержит SOCKS-ветвлений; получает готовый HttpClient от фабрики.
- Политика per-provider: применяется ровно одна политика `disable|inherit|force` (default=inherit).
- Strict/fallback: fallback разрешён только для ошибок до установления SOCKS-канала; read-timeout и ошибки чтения ответа не должны автоматически триггерить fallback из-за риска двойного POST.
- Секреты: username/password не логируются и не возвращаются из `/status`/`/doctor`; допускается только `username_set`.
- Ресурсы: direct/socks httpx clients — long-lived singletons на процесс, создаются лениво, закрываются best-effort на shutdown.
- Наблюдаемость: каждый запрос фиксирует ровно один итоговый transport; при fallback учитывается ошибка SOCKS и итоговый транспорт успешного вызова.
- Рантайм-инвариант: сервис рассчитан на один процесс/воркер для корректности очереди, кэша и метрик.

