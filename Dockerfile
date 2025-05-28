# Dockerfile for LLM mock (place in ./llm-mock/Dockerfile)
# -------------------------------------------------------------
FROM python:3.13-slim

ARG CACHE_CONTAINER_DIR=/app/cache
ENV CACHE_CONTAINER_DIR=${CACHE_CONTAINER_DIR}

WORKDIR /app

# If you have a simple mock_requirements.txt, copy & install
COPY ./llm/requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# DEBUG
# RUN pip install debugpy
# RUN apt-get update && apt-get install -y net-tools
# END DEBUG

# 2) Создадим папку кеша на всякий случай (volume её затрет, но для локального RUN полезно)
RUN mkdir -p $CACHE_CONTAINER_DIR

# Copy mock server code
COPY config/ ./config/
COPY utils/ ./utils/
COPY llm/. .

# Expose LLM
EXPOSE ${LLM_PORT}
CMD uvicorn llm_server:app --host 0.0.0.0 --port ${LLM_PORT}