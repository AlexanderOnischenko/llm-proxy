import java.io.IOException;
import java.net.URI;
import java.net.http.HttpClient;
import java.net.http.HttpRequest;
import java.net.http.HttpResponse;
import java.time.Duration;
import java.util.HashMap;
import java.util.Map;

public final class LlmProxyClient {
    private final HttpClient httpClient;
    private final Config config;

    public LlmProxyClient(Config config) {
        this.config = config;
        this.httpClient = HttpClient.newBuilder()
                .connectTimeout(config.timeout)
                .build();
    }

    public String chatCompletions(String jsonPayload, String priority, Map<String, String> headers)
            throws IOException, InterruptedException {
        Map<String, String> requestHeaders = new HashMap<>(config.defaultHeaders);
        requestHeaders.putIfAbsent("Content-Type", "application/json");
        if (config.apiKey != null && !config.apiKey.isBlank()) {
            requestHeaders.putIfAbsent("Authorization", "Bearer " + config.apiKey);
        }
        if (priority != null && !priority.isBlank()) {
            requestHeaders.put("X-Priority", priority);
        }
        if (headers != null) {
            requestHeaders.putAll(headers);
        }

        HttpRequest.Builder requestBuilder = HttpRequest.newBuilder()
                .uri(URI.create(config.baseUrl + "/v1/chat/completions"))
                .timeout(config.timeout)
                .POST(HttpRequest.BodyPublishers.ofString(jsonPayload));

        for (Map.Entry<String, String> entry : requestHeaders.entrySet()) {
            requestBuilder.header(entry.getKey(), entry.getValue());
        }

        HttpResponse<String> response = httpClient.send(requestBuilder.build(),
                HttpResponse.BodyHandlers.ofString());
        if (response.statusCode() >= 400) {
            throw new IOException("Proxy request failed (" + response.statusCode() + "): " + response.body());
        }
        return response.body();
    }

    public static final class Config {
        private final String baseUrl;
        private final String apiKey;
        private final Duration timeout;
        private final Map<String, String> defaultHeaders;

        private Config(Builder builder) {
            this.baseUrl = builder.baseUrl;
            this.apiKey = builder.apiKey;
            this.timeout = builder.timeout;
            this.defaultHeaders = Map.copyOf(builder.defaultHeaders);
        }

        public static Builder builder() {
            return new Builder();
        }
    }

    public static final class Builder {
        private String baseUrl = "http://localhost:8000";
        private String apiKey;
        private Duration timeout = Duration.ofSeconds(60);
        private final Map<String, String> defaultHeaders = new HashMap<>();

        public Builder baseUrl(String baseUrl) {
            this.baseUrl = baseUrl.endsWith("/") ? baseUrl.substring(0, baseUrl.length() - 1) : baseUrl;
            return this;
        }

        public Builder apiKey(String apiKey) {
            this.apiKey = apiKey;
            return this;
        }

        public Builder timeout(Duration timeout) {
            this.timeout = timeout;
            return this;
        }

        public Builder defaultHeader(String key, String value) {
            this.defaultHeaders.put(key, value);
            return this;
        }

        public Config build() {
            return new Config(this);
        }
    }

    public static void main(String[] args) throws Exception {
        Config config = Config.builder()
                .baseUrl("http://localhost:8000")
                .defaultHeader("X-Custom-Header", "value")
                .build();

        String payload = "{" +
                "\"model\": \"gpt-4.1-mini\"," +
                "\"messages\": [{\"role\": \"user\", \"content\": \"Привет!\"}]" +
                "}";

        LlmProxyClient client = new LlmProxyClient(config);
        String response = client.chatCompletions(payload, "p1", Map.of("X-Trace-Id", "example"));
        System.out.println(response);
    }
}
