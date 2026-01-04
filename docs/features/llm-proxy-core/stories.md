## LLMPROXY-1 — Enforce single-worker startup and initialize priority resources
**Description**
При старте сервиса выполняется проверка однопроцессного режима, загрузка политик приоритетов из @Component:Settings Loader и инициализация одиночных экземпляров @Component:PriorityChatQueue, @Component:OpenAIChatProvider и кэша, чтобы очередь и метрики работали консистентно.

**Input State**
Настройки приоритетов и лимитов присутствуют, но отсутствует проверка многопроцессного режима и не создан единичный экземпляр @DataModel:QueuePolicy/@DataModel:PriorityClass.

**Output State**
Процесс завершается ошибкой при обнаружении многопроцессного контекста, либо успешно поднимает одинокие инстансы очереди, провайдера и кэша с валидированной @DataModel:QueuePolicy/@DataModel:PriorityClass, готовые для использования эндпоинтами.

## LLMPROXY-2 — Accept chat completions with priority admission and queue metrics
**Description**
Эндпоинт /v1/chat/completions принимает OpenAI-совместимый @DataModel:LLMRequest, валидирует тело и приоритет, присваивает request_id, применяет лимиты inflight и max_queue, ставит запрос в @Component:PriorityChatQueue, выполняет через @Component:OpenAIChatProvider с кэшем и возвращает ответ с заголовками x-llm-proxy-* (request_id, задержки, приоритет), обрабатывая ошибки 400/429/500 согласно правилам.

**Input State**
Клиент отправляет ChatCompletionRequestDTO; очередь и провайдер инициализированы, но приём не проверяет приоритеты/лимиты и не добавляет метрики в ответ.

**Output State**
Запрос либо отклонён с кодом и reason (invalid_priority, queue_overflow, streaming), либо выполнен с учётом лимитов; в ответе присутствуют request_id и метрики очереди/провайдера, а слоты/очередь актуализированы.

## LLMPROXY-3 — Return inline metrics for invoke requests
**Description**
Эндпоинт /v1/chat/invoke обрабатывает канонический @DataModel:LLMRequest как в chat, включая приоритетное планирование и кэш, но дополняет тело ответа InvokeResponseWithMetricsDTO метриками очереди и провайдера вместе с request_id, сохраняя заголовки и все ветки ошибок 400/429/500.

**Input State**
Invoke-запросы принимаются, но не выполняют приоритетную очередь и не возвращают inline метрики в теле.

**Output State**
Invoke-запросы проходят ту же валидацию и admission, получают request_id, выполняются или отклоняются по тем же правилам; успешные ответы содержат output и метрики в теле, а слоты/метрики обновлены.

## LLMPROXY-4 — Adapt LangChain /llm requests into prioritized chat executions
**Description**
Маршрут /llm принимает LangChainLLMRequestDTO, нормализует его в ChatCompletionRequestDTO с применением дефолтной модели/параметров и приоритета, выполняет admission в @Component:PriorityChatQueue, вызывает @Component:OpenAIChatProvider и возвращает LangChain-совместимый ответ с request_id и очередными метриками.

**Input State**
Внутренние LangChain-запросы не конвертируются в канонический чат и не учитывают очередь/метрики.

**Output State**
/llm-запросы приводятся к каноническому виду, проходят те же проверки приоритета и лимитов, исполняются через очередь/провайдер и получают LangChain-совместимый ответ с метаданными и актуализированными слотами.

## LLMPROXY-5 — Expose Prometheus metrics for queue, inflight, and latencies
**Description**
Эндпоинт /metrics возвращает MetricsExpositionDTO со снимком метрик очереди, inflight и задержек по приоритетам, используя обновления из @Component:PriorityChatQueue и событий выполнения, чтобы операторы получали актуальные показатели.

**Input State**
Метрики очереди и задержек недоступны или не экспонируются через /metrics.

**Output State**
Запрос GET /metrics выдаёт корректный Prometheus payload с текущей глубиной очереди, inflight, задержками и счётчиками overflow/provider, поддерживая монотонность в однопроцессном режиме.

