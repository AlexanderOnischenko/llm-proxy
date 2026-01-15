# Feature: SOCKS Proxy support for Chat Providers
## Problem Statement
Нужно добавить управляемую поддержку исходящих запросов к upstream LLM через SOCKS-proxy (в первую очередь SOCKS5), не внедряя SOCKS-логику в сами провайдеры и не ломая текущую OpenAI-совместимость. Дополнительно требуется прозрачная диагностика (status/doctor) и наблюдаемость (логи/метрики), чтобы было понятно кто (какой провайдер) куда ходит и как (direct vs socks).

## Users / Actors
* Оператор/DevOps — включает/настраивает SOCKS, включает strict/fallback, запускает диагностику.
* Сервис LLM Proxy — резолвит политику, строит транспорт/клиент, выполняет запросы.
* ChatProvider implementations (OpenAIChatProvider, GigaChatProvider, будущие) — потребляют готовый HTTP-транспорт, не знают про SOCKS.
* Разработчик провайдера — добавляет новый provider без прокси-кода.
* SRE/дежурный — читает status/doctor, логи и метрики для дебага сетевых проблем.

## Use Cases
* DevOps хочет включить SOCKS5 для всех исходящих запросов, чтобы проксировать трафик из изолированной сети.
(при этом используется strict-mode, чтобы при недоступности SOCKS запросы падали быстро и предсказуемо).
* Разработчик хочет добавить нового провайдера, чтобы он автоматически получил поддержку SOCKS без изменений внутри провайдера.
* SRE хочет увидеть статус SOCKS и проверить соединение/аутентификацию/внешний доступ, чтобы быстро локализовать проблему.

## Proposed Solution
### Affected Components
* settings (llm.config.settings.Settings)
    * добавить глобальные параметры SOCKS (enabled/type/endpoint/auth/strict/timeouts)
    * добавить опциональные overrides политики SOCKS на уровне провайдера
* provider (llm.services.chat_provider)
    * отделить “строительство HTTP-клиента/транспорта” в отдельный слой (transport factory)
    * резолвить socks policy (provider → global), получать готовый httpx.AsyncClient
    * классифицировать ошибки SOCKS отдельно от provider errors
    * реализовать strict поведение (без изменения контракта /v1/chat/completions)
* client_factory (llm.services.client.create_custom_llm)
    * при создании LangChain ChatOpenAI передавать готовый HTTP async client (если доступно в интеграции) через http_async_client (и при необходимости http_client для sync). 
* deps (llm.deps)
    * создать singleton HttpClientFactory/TransportFactory и отдавать его провайдеру и client_factory
    * обеспечить корректное закрытие клиентов на shutdown (best-effort)
* app (llm.main)
    * добавить startup-логи конфигурации SOCKS (без утечек пароля)
    * добавить API для диагностики: /status и /doctor (или эквивалентные маршруты)
* metrics (llm.metrics.prometheus + новые метрики в сервисном слое)
    * добавить метрики по транспорту и ошибкам SOCKS (см. ниже)
* cli (llm-proxy)
    * добавить команду llm-proxy doctor
    * расширить llm-proxy status выводом SOCKS-секции (через HTTP API сервиса), сохранив текущий runtime-статус docker compose

### Data Models
* Используется: Settings — расширяется новыми полями SOCKS.
* Требуются новые сущности (в виде Pydantic моделей для API ответов):
    * SocksConfigView:
        * enabled: bool
        * type: str (ожидаемо socks5)
        * endpoint: str (host:port)
        * strict: bool = true
        * connect_timeout_s: float
        * read_timeout_s: float
        * username_set: bool (пароль/логин не возвращать)
    * SocksRuntimeStatus:
        * status: Literal["OK","ERROR","DISABLED"]
        * error: str | None (класс/код без секретов)
        * last_checked_at: datetime | None
    * DoctorReport:
        * checks: list[DoctorCheck]
        * impact: list[str] (кратко: какие режимы/провайдеры пострадают)
    * DoctorCheck:
        * name: str (например tcp_connect, auth, outbound_test)
        * ok: bool
        * details: str | None

### Interfaces
Configuration (ENV)
Глобальная конфигурация SOCKS (в Settings):
* LLM_SOCKS_ENABLED (bool, default false)
* LLM_SOCKS_TYPE (string, default socks5)
* LLM_SOCKS_HOST (string, default 127.0.0.1)
* LLM_SOCKS_PORT (int, default 1080)
* LLM_SOCKS_USERNAME (string, optional)
* LLM_SOCKS_PASSWORD (string, optional)
* LLM_SOCKS_STRICT (bool, default false)
* LLM_SOCKS_CONNECT_TIMEOUT (float seconds, default 3)
* LLM_SOCKS_READ_TIMEOUT (float seconds, default 30)
Overrides на уровне провайдера (опционально, расширяемо на будущие провайдеры):
* LLM_SOCKS_PROVIDER_OVERRIDES (JSON string)
    * пример: {"openai":"inherit","openrouter":"disable","custom":"force"}
    * поддерживаемые значения: inherit (default), disable, force

### Transport selection (internal)
Алгоритм выбора транспорта на запрос:
1. определить provider_name (ключ провайдера, например openai, gigachat, …)
2. resolve_socks_policy(provider_name):
    * если override=disable → direct
    * если override=force → socks (игнорируя global disable)
    * иначе (inherit) → socks если LLM_SOCKS_ENABLED=true, иначе direct
3. построить/получить из factory HTTP transport:
    * direct: обычный httpx.AsyncClient
    * socks: httpx.AsyncClient с SOCKS transport (например через httpx-socks). 
4. выполнить provider request
Public HTTP API
* GET /status
    * возвращает агрегированный статус, включая секцию socks:{
    *   "socks": {
    *     "config": { "...": "..." },
    *     "runtime": { "...": "..." }
    *   }
    * }
    * 
* GET /doctor
    * выполняет серию проверок (парсинг конфига, tcp connect, auth (если задано), outbound test на целевой URL)
    * возвращает структурированный отчёт (DoctorReport)
Существующие API (например POST /v1/chat/completions) не меняются.

### Observability
Логи:
* Startup:
    * [INFO] SOCKS proxy enabled: socks5://<host>:<port>
    * [INFO] SOCKS strict mode: <bool>
* Runtime:
    * [WARN] SOCKS unavailable, falling back to direct (provider=<name>, reason=<code>)
    * [ERROR] SOCKS strict mode enabled, aborting request (provider=<name>, reason=<code>)
* Request-level debug:
    * [DEBUG] provider=<name> transport=<socks5|direct> target=<upstream_host>

### Метрики (новые):
* llm_socks_requests_total{provider,transport="socks|direct"}
* llm_socks_errors_total{provider,reason="connect|auth|timeout|other"}
* llm_requests_transport_total{provider,transport="socks|direct"}

### Integration Notes
* OpenAI SDK уже допускает инъекцию кастомного HTTP клиента в AsyncOpenAI через параметр http_client (backend по умолчанию — httpx). 
* Для LangChain ChatOpenAI поддерживается передача http_async_client (и http_client для sync). 
* SOCKS реализуется как transport-capability (общий слой), а не как ветка логики в ChatProvider.
* Для strict/fallback требуется корректная классификация ошибок “на уровне SOCKS/соединения”, чтобы:
    * не маскировать реальные upstream/provider ошибки,
    * не делать опасных повторов POST после частично выполненного запроса.

### Правила и инварианты
1. Backward compatibility: при LLM_SOCKS_ENABLED=false поведение исходящих запросов идентично текущему (direct), без требований к наличию прокси.
2. Provider-agnostic: реализация ChatProvider не читает SOCKS env/settings напрямую и не содержит SOCKS-ветвлений; провайдер получает готовый HTTP-клиент/transport из factory.
3. Policy resolution: для каждого provider применяется ровно одна политика disable|inherit|force; значение по умолчанию — inherit.
4. Strict mode: если итоговая политика для провайдера требует SOCKS (inherit+enabled или force) и LLM_SOCKS_STRICT=true, то при любой ошибке установления SOCKS-канала запрос не должен выполнять direct fallback и должен завершаться ошибкой с кодом SOCKS.
5. Fallback mode (default): если итоговая политика требует SOCKS и LLM_SOCKS_STRICT=false, то при ошибках SOCKS класса:
    * SOCKS_CONNECT_ERROR, SOCKS_AUTH_ERROR, SOCKS_TIMEOUTвыполняется ровно одна попытка direct fallback, и в логах фиксируется warning.
6. Безопасность повторов: fallback разрешён только для ошибок, возникающих до установления соединения/прокси-handshake (connect/auth/connect-timeout). Ошибки чтения ответа/таймаут чтения (read timeout) не триггерят fallback по умолчанию (иначе риск двойного POST).
7. Коды ошибок как контракт: классы/коды ошибок SOCKS должны быть стабильными и совпадать с именами, используемыми в метриках/логах (например SOCKS_CONNECT_ERROR), чтобы по ним можно было строить алерты и дашборды.
8. Секреты: username/password никогда не логируются и не возвращаются из /status//doctor; в лучшем случае возвращается флаг username_set.
9. Ресурсы: HTTP clients (direct/socks) являются long-lived singletons (на процесс) и переиспользуют connection pool; создаются лениво и закрываются best-effort на shutdown.
10. Наблюдаемость: для каждого запроса фиксируется ровно один transport (direct|socks) в debug-логах, и инкрементируется соответствующая метрика транспорта; при fallback должен инкрементироваться транспорт итогового успешного вызова и отдельно метрика ошибки SOCKS.

### ADRs
* ADR: “SOCKS as transport-layer capability”
    * Решение: вынести SOCKS из провайдеров в общий TransportFactory/HttpClientFactory.
* ADR: “httpx + httpx-socks for SOCKS5 transport”
    * Решение: использовать transport-подход для httpx (вместо пер-провайдер прокси-логики). 
* ADR: “Diagnostics via service HTTP endpoints”
    * Решение: предоставить /status и /doctor, чтобы CLI мог показывать состояние без docker-специфичных хаков.

### Non-Goals / Out of Scope
* HTTP proxy (CONNECT/forward proxy) — не в рамках этой фичи.
* Per-request SOCKS override — не поддерживается (слишком усложняет UX и тестирование).
* TOR-specific логика — не добавляется (TOR как SOCKS5 должен работать как частный случай).
* Гарантированная поддержка SOCKS для провайдеров/SDK, которые не используют httpx (если появятся) — только best-effort и отдельная задача адаптации.

### Open Questions
1. Нужен ли LLM_SOCKS_READ_TIMEOUT по умолчанию 30s, или корректнее наследовать/ограничивать общим LLM_TIMEOUT, чтобы не ломать долгие ответы?
2. Какой “outbound test” должен выполнять doctor:
    * всегда https://api.openai.com (как в черновике),
    * или settings.llm_api_base,
    * или оба (с флагами)?
3. Требуется ли circuit-breaker/кэширование “SOCKS down” статуса (cooldown), чтобы не множить connect timeouts при массовых запросах в режиме fallback?
4. Какой формат overrides удобнее для env/профилей: JSON (LLM_SOCKS_PROVIDER_OVERRIDES) vs отдельные переменные на провайдера?
5. Должен ли GigaChatProvider получить SOCKS поддержку (если SDK позволяет прокинуть httpx client/transport), или на первом этапе ограничиться OpenAI-совместимыми провайдерами?
