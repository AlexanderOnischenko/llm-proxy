# Feature: Queue Observability

## Problem Statement
Обеспечить прозрачную, неизменяющую систему наблюдаемости за приоритетной очередью и конкурентностью, чтобы операторы видели глубину очереди, inflight по приоритетам и исходы запросов без вмешательства в выполнение.

## Users / Actors
- Операторы/SRE, следящие за состоянием сервиса и реагирующие на деградации.
- DORA/инфраструктурные системы мониторинга, собирающие метрики Prometheus.
- Инженеры фич приоритезации/динамической конкурентности, использующие наблюдаемость для верификации настроек.

## Use Cases
- Оператор хочет увидеть текущее состояние очереди и inflight по приоритетам, чтобы понять узкое место.
- Мониторинг собирает метрики `queue_wait`/`provider_latency`/`overflow` и строит алерты.
- Инженер проверяет, что изменение лимитов конкурентности отражается в метриках без рестарта.

## Feature Boundary & Responsibility
Фича предоставляет read-only наблюдаемость за состоянием очереди и исходами запросов: метрики, снимки состояния и идентификаторы запросов. Она не меняет планирование, приоритеты, лимиты или приём запросов.

## Execution Phase
Observability

## Conceptual Inputs & Outputs
### Inputs (conceptual)
- События очереди: enqueue, dequeue, overflow, завершение провайдера.
- Текущее состояние очереди и inflight по приоритетам (`PriorityChatQueue.snapshot()`).
- Уникальные `request_id` и приоритеты из admission слоя.

### Outputs (conceptual)
- Метрики Prometheus: queue depth по приоритетам, inflight, очередь ожидания, латентность провайдера, счетчики overflow/исходов.
- Read-only состояние очереди (queue_len/in_flight) для админ‑просмотра.
- Метаданные выполнения в ответах (`request_id`, очередь/латентность в заголовках и теле `/invoke`).

## Proposed Solution

### Affected Components
- `PriorityChatQueue` — продолжить эмитировать метрики inflight/queue/latency/overflow и предоставлять `snapshot()` для чтения.
- `llm.metrics.prometheus.router` — экспорт метрик `/metrics` (Prometheus pull).
- `llm.routers.chat` — возвращать `request_id` и метрики очереди в заголовках (и в теле для `/v1/chat/invoke`).
- Новый admin router (read-only): `/admin/queue` — вернуть `snapshot()` очереди для операторов.

### Data Models
- Используется: `QueueMetrics`/`QueueResult` — внутренняя фиксация задержек и приоритета.
- Используется: Prometheus метрики `_QUEUE_LENGTH`, `_INFLIGHT`, `_QUEUE_WAIT_SECONDS`, `_PROVIDER_LATENCY_SECONDS`, `_QUEUE_OVERFLOW`, `_REQUESTS_TOTAL`.
- Требуется новая сущность: `QueueStateView` — сериализуемый снимок `queue_len`/`in_flight` по приоритетам для admin API.

### Interfaces
- Public observability endpoints: `GET /metrics` (Prometheus), `GET /admin/queue` (JSON snapshot, read-only).
- Response headers: `x-llm-proxy-request-id`, `x-llm-proxy-queue-wait-ms`, `x-llm-proxy-provider-latency-ms`, `x-llm-proxy-priority`.
- Internal call: `PriorityChatQueue.snapshot()` используется admin router’ом.

### Integration Notes
- Граничит с `LLM Proxy Core Priority Admission`: использует его `request_id` и приоритеты, но не меняет их.
- Граничит с `Dynamic Concurrency Control`: метрики и snapshot отражают актуальные лимиты и inflight, но не управляют ими.
- Работает в однопроцессном режиме; при многопроцессном запуске метрики Prometheus некорректны (см. системный инвариант).
- Admin snapshot должен быть лёгким и не блокировать диспетчер: вызов `snapshot()` под локом, без модификаций.

### Feature Interfaces
- Потребляет гарантии от: приём/планирование (уникальные `request_id`, известные приоритеты), однопроцессного режима метрик.
- Поставляет гарантии: наблюдаемое состояние очереди и задержек для алертов и ручных действий; идентифицируемость каждого ответа через `request_id`.

## Rules and Invariants
- Метрики Prometheus обновляются на каждом enqueue/overflow/завершении провайдера; отсутствует молчаливый пропуск событий.
- `snapshot()` возвращает непротиворечивое состояние (`queue_len`, `in_flight`) в момент захвата и не изменяет очередь.
- Read-only endpoints (Prometheus, `/admin/queue`) не изменяют состояние очереди или лимитов.
- Все HTTP ответы с очереди содержат `request_id`; `/v1/chat/invoke` дополнительно возвращает метрики в теле.
- Приоритеты в метриках соответствуют конфигурации очереди; отключённые приоритеты не появляются в экспорте.

## ADRs
- Использование Prometheus как единого канала наблюдаемости очереди и провайдера; отказ от дублирующих форматов.
- Поддержка read-only admin snapshot вместо изменения состояния в рамках этой фичи.

## Non-Goals / Out of Scope
- Управление лимитами, приоритетами или очередью (это ответственность control plane).
- UI/дашборды; построение графиков остаётся на внешних системах мониторинга.
- Изменение политики приёма или планирования запросов.

## Open Questions
- Нужна ли пагинация/агрегация для `snapshot()`, если приоритетов станет много, или достаточно текущей структуры?
- Требуется ли ограничение доступа/аутентификация для `/admin/queue` (сейчас предполагается внутренний доступ)? 
