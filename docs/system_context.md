# System Context

## Overview

LLM Proxy — промежуточный слой между клиентами и LLM‑провайдерами, обеспечивающий предсказуемость выполнения запросов за счёт нормализации политики, приоритезации и управляемой конкурентности в однопроцессном режиме.

## Key Scenarios

- Обработка чат‑запроса с нормализацией параметров, приоритезацией и ограничением конкурентности перед вызовом апстрима.
- Повторное использование ответов через кэш и дедупликацию одновременных идентичных запросов.
- Наблюдаемость за глубиной очереди, задержками и исходами через метрики и идентификаторы запросов.
- Динамическое изменение лимитов конкурентности по приоритетам без рестарта по сигналу control plane.

## System Phases & Lifecycle

Ingress → Normalization/Policy → Admission/Scheduling → Execution → Egress → Observability (read-only, пересекает фазы)

Control Plane (поправки политики конкурентности) действует асинхронно, влияя на Admission/Scheduling.

## Canonical Concepts

| Concept                | Description                                                                                                      |
| ---------------------- | ---------------------------------------------------------------------------------------------------------------- |
| Canonical Chat Request | Нормализованное представление пользовательского запроса на чат‑комплишн, свободное от двусмысленностей политики. |
| Priority Claim         | Явное или выведенное значение приоритета, совместимое с допустимым набором системы.                              |
| Admission Decision     | Результат выбора: принять запрос в обработку или отклонить из‑за переполнения/некорректности.                    |
| Concurrency Budget     | Текущее распределение слотов исполнения по приоритетам, применяемое планировщиком.                               |
| Request Identity       | Уникальный идентификатор запроса, связывающий обработку, метрики и ответ.                                        |
| Execution Outcome      | Результат вызова провайдера (успех/ошибка) с сопутствующими задержками.                                          |
| Observability Snapshot | Читаемое состояние очереди и метрики, не изменяющее выполнение.                                                  |

## Feature Map by Phase

| Feature                                                  | Primary Phase                        | Produces                                                | Consumes                                                   |
| -------------------------------------------------------- | ------------------------------------ | ------------------------------------------------------- | ---------------------------------------------------------- |
| Request Policy & Normalization                           | Normalization/Policy                 | Canonical Chat Request                                  | Raw client payload, Settings defaults/overrides            |
| Client-Defined Priorities                                | Admission/Scheduling                 | Priority Claim                                          | Client priority hint, Concurrency Budget (valid set)       |
| LLM Proxy Core Priority Admission and Execution Tracking | Admission/Scheduling                 | Admission Decision, Request Identity, Execution Outcome | Canonical Chat Request, Priority Claim, Concurrency Budget |
| Dynamic Concurrency Control                              | Admission/Scheduling (control plane) | Updated Concurrency Budget                              | Operator/admin intent, Observability Snapshot              |
| Queue Observability                                      | Observability                        | Observability Snapshot, Metrics with Request Identity   | Queue events, Execution Outcome                            |

## Core Components

| Component                | Responsibility                                                                        |
| ------------------------ | ------------------------------------------------------------------------------------- |
| FastAPI Application      | Приём HTTP запросов, маршрутизация по публичным и административным эндпоинтам.        |
| Settings Loader          | Централизованная конфигурация и overrides для окружений.                              |
| Canonical Request Models | Нормализация и валидация чат‑payload перед планированием.                             |
| Priority Planner         | Применение приоритета, лимитов и принятие решений о постановке/отказе.                |
| Provider Abstraction     | Унифицированный вызов LLM‑апстрима по нормализованному запросу.                       |
| Cache/Dedup Layer        | Повторное использование результатов и дедупликация одновременных идентичных запросов. |
| Observability Exporter   | Экспорт метрик и снимков состояния для мониторинга.                                   |
| Control Plane Interface  | Применение изменений Concurrency Budget без рестарта.                                 |


## Core Entties

| Entity                   | Description                                                                    |
| ------------------------ | ------------------------------------------------------------------------------ |
| Chat Request             | Пользовательский запрос на чат‑комплишн с сообщениями и параметрами генерации. |
| Priority                 | Поддерживаемое системой относительное значение важности запроса.               |
| Concurrency Budget State | Карта слотов исполнения по приоритетам с версией и источником изменения.       |
| Request ID               | Уникальный идентификатор, сквозной для обработки и наблюдаемости.              |
| Queue State Snapshot     | Текущее количество ожидающих и выполняющихся запросов по приоритетам.          |
| Provider Response        | Результат вызова апстрима, включая метаданные задержек и исход.                |


## System Data Flow

1. **Ingress**: Приём HTTP запроса, извлечение сырого тела и метаданных.
2. **Normalization/Policy**: Валидация и канонизация параметров (модель, сэмплинг, запрет stream), формирование Canonical Chat Request.
3. **Admission/Scheduling**: Нормализация Priority Claim, сопоставление с Concurrency Budget, принятие или отказ; назначение Request ID.
4. **Execution**: Вызов провайдера с учётом кэша/дедупликации; фиксация Execution Outcome.
5. **Egress**: Возврат ответа с Request ID и метаданными задержек.
6. **Observability**: Экспорт метрик и Queue State Snapshot; доступно без изменения выполнения.

7. **Control Plane (asynchronous)**: Обновление Concurrency Budget, влияющее на будущие решения Admission/Scheduling.


## Global Rules & Invariants

- Система работает в однопроцессном режиме; многопроцессный запуск нарушает корректность лимитов и метрик.
- Только приоритеты, присутствующие в текущем Concurrency Budget с положительной вместимостью, могут быть приняты.
- Concurrency Budget может обновляться без рестарта; новое состояние применяется атомарно для последующих Admission решений.
- Каждый принятый запрос получает уникальный Request ID, отражённый в ответах и метриках.
- Observability пути (метрики, snapshots) не изменяют состояние исполнения или бюджетов.
- Нормализация запроса завершается до попытки планирования; невалидные или запрещённые параметры не поступают в Admission.
- Переполнение очереди или отсутствие слотов приводит к отказу без нарушения уже выполняющихся запросов.


## Notes

- Кэш и дедупликация рассматриваются как оптимизации Execution и не изменяют фазовую модель или канонические концепты.
- Расширение приоритетов или изменение их вместимости требует согласованности между Control Plane, Admission и Observability.
