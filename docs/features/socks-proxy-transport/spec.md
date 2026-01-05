# Feature: Deterministic SOCKS5 Transport for Provider Traffic

## Problem Statement
SOCKS5‑транспорт должен применяться как детерминированная политика по каждому провайдеру: вся исходящая связь к апстримам либо гарантированно идёт через заданный SOCKS5 (с учётом per‑provider правил), либо явно отклоняется без неочевидных фолбеков. Система обязана различать ошибки SOCKS и ошибки провайдера, предоставлять наблюдаемость и диагностику без утечек секрета аутентификации и без деградации TLS/mTLS семантики.

## Users / Actors
- Операторы/SRE: включают или запрещают SOCKS, настраивают DNS/NO_PROXY, контролируют строгий режим, читают статус/doctor, следят за метриками фолбеков.
- Разработчики провайдеров: используют фабрику HTTP‑клиентов с прозрачной политикой SOCKS и предсказуемыми ошибками транспорта.
- Клиентские приложения/пользователи: получают корректный ответ или предсказуемую ошибку транспорта без смешения с ошибками апстрима.

## Use Cases
- Глобальное включение SOCKS5 с учётом аутентификации; провайдеры по умолчанию наследуют политику.
- Провайдер `socks=force` требует маршрут только через SOCKS, даже если глобально выключено, и падает на любой недоступности прокси.
- Провайдер `socks=disable` всегда идёт напрямую, игнорируя глобальную настройку.
- Конфигурация NO_PROXY для локальных/внутренних доменов и подсетей: «через SOCKS всё, кроме перечисленного».
- Управляемое разрешение DNS: локальное или через SOCKS (remote DNS) для избежания утечек приватных доменов.
- Диагностика `status` показывает кэшированный результат и итоговую («effective») политику провайдера; `doctor` выполняет активные проверки соединения, аутентификации и egress.
- Метрики отражают использование SOCKS, ошибки и количество фолбеков с классификацией причин.

## Feature Boundary & Responsibility
- **Принимает решения**: какой транспорт применить (SOCKS vs direct) per‑provider с учётом глобальной политики, strict/force семантики, NO_PROXY, DNS‑режима и таймаутов; как классифицировать ошибки и что отдавать в диагностике; когда выполнять фолбек.
- **Не принимает решения**: не меняет payload/маршруты провайдера, не подменяет TLS/mTLS, не выбирает приоритеты/очереди, не исполняет контроль плоскость конкурентности.
- Фича — **решатель транспорта**: предоставляет готовый HTTP‑клиент и диагностические гарантии; провайдеры остаются потребителями готового транспорта.

## Execution Phase
Primary: **Execution / Transport setup** — перед установкой исходящего соединения к апстриму. Наблюдаемость (status/doctor) работает read‑only и не меняет рабочих клиентов.

## Conceptual Inputs & Outputs
### Inputs
- Глобальные переменные окружения `LLM_SOCKS_*` (включая enable/host/port/auth/strict/таймауты, DNS режим, NO_PROXY, тестовый endpoint).
- Провайдерская политика `socks ∈ {inherit, disable, force}`.
- Целевой хост/URL апстрима для маршрутизации и проверки NO_PROXY.
- Режим выполнения запроса (обычный/streaming) для выбора таймаутов чтения.

### Outputs
- Решение о транспорте: direct или SOCKS5, с выбранным DNS‑режимом и применёнными таймаутами.
- Классифицированный исход (success | unavailable | reachable_no_egress | auth_failed | timeout | blocked_by_no_proxy) для применения фолбека/ошибки и для метрик.
- Диагностический снапшот (конфигурация без секретов, статус последней проверки, timestamp, effective‑policy, результат активного теста doctor).
- Метрики: requests/errors/transport usage + `llm_socks_fallback_total{provider,reason}`.

## Proposed Solution
- **Settings/SocksSettings**: расширить загрузку окружения `LLM_SOCKS_*` новыми полями `NO_PROXY`, `REMOTE_DNS`, `TEST_URL`, `STRICT`, таймаутами (`CONNECT_TIMEOUT`, `READ_TIMEOUT`, `STREAM_READ_TIMEOUT`) и auth. Хранить флаг enabled и вычислять effective политику per provider (force overrides global disable).
- **Transport factory**: создаёт HTTP‑клиенты с SOCKS5 либо direct. Учитывает NO_PROXY (bypass на уровне резолва цели), применяет remote DNS флаг, подключает таймауты/ретраи, различает streaming таймаут чтения. Возвращает причину недоступности в явных кодах.
- **Diagnostics**: `status` использует кэш последней проверки с timestamp, `doctor` проводит активные проверки (resolve, TCP connect, SOCKS handshake/auth, egress HEAD/GET на TEST_URL с коротким таймаутом). Пароли никогда не логируются/не выводятся; username опционально маскируется.
- **Observability**: обновить счётчики `llm_socks_requests_total`, `llm_socks_errors_total{reason}`, `llm_socks_fallback_total{provider,reason}` и `llm_requests_transport{transport}`. Логирование WARN при фолбеке, ERROR при fail‑fast.
- **CLI/UX**: `status` и `doctor` показывают effective политику per provider (inherit/disable/force + strict/fallback), remote DNS режим, NO_PROXY список (если есть), результат проверки доступности и egress. Отдельно отображать различие «reachable, but no egress».
- **TLS/mTLS integrity**: транспорт не трогает сертификаты/ключи; ошибки TLS handshake репортятся отдельно от SOCKS‑доступности.

## Feature Interfaces
- **Environment**:
  - `LLM_SOCKS_ENABLED` (bool, default=false)
  - `LLM_SOCKS_HOST` / `LLM_SOCKS_PORT`
  - `LLM_SOCKS_USERNAME` / `LLM_SOCKS_PASSWORD` (пароль никогда не выводится)
  - `LLM_SOCKS_STRICT` (bool) — fail‑fast при ошибках транспорта
  - `LLM_SOCKS_NO_PROXY` — список хостов/доменов/подсетей для обхода (пример: `localhost,127.0.0.1,.corp.local,10.0.0.0/8`)
  - `LLM_SOCKS_REMOTE_DNS` (bool, default=true) — резолв через SOCKS vs локально
  - `LLM_SOCKS_CONNECT_TIMEOUT`, `LLM_SOCKS_READ_TIMEOUT`, `LLM_SOCKS_STREAM_READ_TIMEOUT`
  - `LLM_SOCKS_TEST_URL` — endpoint для outbound проверки (default: малый публичный HTTPS ресурс, без auth)
- **Provider config**: `providers.<name>.socks` ∈ {inherit (default), disable, force}; force всегда требует SOCKS, даже при глобальном disabled.
- **Internal API**: `http_client_factory.for_provider(provider, mode)` возвращает транспорт и причину ошибки; различает unavailable/blocked/no-egress/auth/timeout.
- **CLI**: `llm-proxy status` выводит кэшированный результат и effective policy; `llm-proxy doctor` выполняет активную проверку connect/auth/egress (HEAD/GET) с краткими таймаутами, с маскировкой секрета.
- **Metrics**: счётчики использования, ошибок, фолбеков; лейблы provider, reason, transport.

## Rules and Invariants
- По умолчанию SOCKS отключён; наследование (`inherit`) следует глобальному флагу. `force` всегда требует SOCKS, даже при `enabled=false`.
- При `strict=true` или `force` фолбек на direct запрещён; ошибка классифицируется и отдаётся как транспортная. При `strict=false` допускается одноразовый фолбек с WARN и инкрементом `llm_socks_fallback_total`.
- NO_PROXY применяется до попытки соединения: если цель матчится, используется direct без попытки SOCKS; doctor/status фиксируют это в effective policy (blocked_by_no_proxy).
- Remote DNS поведение фиксируется флагом: `true` — резолв через SOCKS, `false` — локальный резолв; doctor должен явно проверять и выводить используемый режим.
- TLS/mTLS цепочка не модифицируется транспортом; ошибки TLS отражаются отдельно от SOCKS недоступности.
- Секреты не логируются: пароль никогда не выводится; username опционально маскируется или отображается как `auth: enabled`.
- `SOCKS unavailable` определяется как отказ TCP connect, таймаут соединения/чтения, сбой аутентификации; кейс «handshake успешен, egress заблокирован» фиксируется отдельным статусом reachable_no_egress.
- Тестовый outbound запрос использует `LLM_SOCKS_TEST_URL`, HEAD/GET малый ресурс с коротким таймаутом и без auth. Таймаут для streaming запросов не должен обрывать длительное чтение (используется STREAM_READ_TIMEOUT либо отсутствие таймаута для idle‑периодов SSE).
- Status кэширует последний результат диагностики с timestamp, doctor всегда выполняет активные проверки и обновляет кэш; частота doctor не должна приводить к DoS прокси.

## ADRs
- SOCKS политика реализована как часть транспортной фабрики, а не в провайдерах; провайдеры остаются транспарантными к выбору канала.
- Force политика имеет приоритет над глобальным выключением, чтобы обеспечить явный отказ вместо тихого обхода.
- Remote DNS включён по умолчанию для предотвращения утечек приватных доменов.
- Диагностика разделяет состояния unavailable vs reachable_no_egress, чтобы корректно выбирать фолбек и алерты.
- Status использует кэш, doctor — активный прогон; это снижает нагрузку на SOCKS и улучшает UX.

## Non-Goals / Out of Scope
- Поддержка HTTP/HTTPS прокси, TOR или смешанных маршрутов.
- Пер‑запросные overrides SOCKS и runtime переключение для отдельного вызова.
- Управление TLS/mTLS материалами (сертификаты, ключи) или их хранение — остаётся обязанностью провайдеров/настроек вне SOCKS.
- Автоматический бэкофф/ретраи транспортного слоя вне одного фолбека в нестрогом режиме.
- Изменение логики приоритезации, очереди или control plane конкурентности.

## Open Questions
- Нужен ли тонкий контроль для streaming таймаутов (например, отсутствие таймаута на период без данных) или достаточно отдельного STREAM_READ_TIMEOUT?
- Требуется ли UI/API экспонирование кэшированного статуса SOCKS вне CLI (например, в `/settings`)?
- Следует ли логировать маскированный username или полностью скрывать его для анонимности?
