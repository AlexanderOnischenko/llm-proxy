# Клиенты для LLM Proxy

Папка содержит минимальные клиенты для работы с `POST /v1/chat/completions`.
Оба клиента поддерживают приоритеты `p1/p2/p3` (через `X-Priority`) и позволяют
передавать любые дополнительные заголовки, которые принимает прокси.

## Python

Файл: `clients/python/llm_proxy_client.py`

### Конфигурация

- `base_url` — базовый URL прокси (например, `http://localhost:8000`).
- `api_key` — токен, если прокси ожидает `Authorization: Bearer ...`.
- `timeout_s` — таймаут запроса.
- `default_headers` — заголовки по умолчанию (например, `X-Org`, `X-Trace-Id`).
- `priority_in_body` — при `True` добавляет `priority` в тело запроса,
  если там его нет (в дополнение к `X-Priority`).
- `verify_ssl` — контроль проверки сертификата для HTTPS.

### Пример

```python
from clients.python.llm_proxy_client import LlmProxyClient, ProxyClientConfig

client = LlmProxyClient(
    ProxyClientConfig(
        base_url="http://localhost:8000",
        api_key="token",
        default_headers={"X-Client-Name": "demo"},
        timeout_s=30,
    )
)

response = client.chat_completions(
    {
        "model": "gpt-4.1-mini",
        "messages": [{"role": "user", "content": "Привет!"}],
    },
    priority="p2",
    headers={"X-Trace-Id": "trace-123"},
)
print(response)
```

## Java

Файл: `clients/java/LlmProxyClient.java`

### Конфигурация

- `baseUrl` — базовый URL прокси (например, `http://localhost:8000`).
- `apiKey` — токен для `Authorization: Bearer ...`.
- `timeout` — таймаут запроса.
- `defaultHeader` — заголовки по умолчанию.

### Пример

```java
LlmProxyClient.Config config = LlmProxyClient.Config.builder()
        .baseUrl("http://localhost:8000")
        .apiKey("token")
        .defaultHeader("X-Client-Name", "demo")
        .build();

String payload = "{" +
        "\"model\": \"gpt-4.1-mini\"," +
        "\"messages\": [{\"role\": \"user\", \"content\": \"Привет!\"}]" +
        "}";

LlmProxyClient client = new LlmProxyClient(config);
String response = client.chatCompletions(payload, "p3", Map.of("X-Trace-Id", "trace-123"));
System.out.println(response);
```

> Примечание: сериализацию JSON лучше делать через библиотеку вроде Jackson/Gson,
> чтобы удобно формировать payload. Приоритет можно передавать и в теле запроса
> (поле `priority`), если это требуется в вашей интеграции.
