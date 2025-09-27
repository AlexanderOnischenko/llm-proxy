# Dockerfile for LLM mock (place in ./llm-mock/Dockerfile)
# -------------------------------------------------------------
FROM python:3.13-slim

# 1) Аргументы по умолчанию
ARG USER=appuser
ARG GROUP=appgroup
ARG LLM_PORT=8002
ARG CACHE_CONTAINER_DIR=/home/${USER}/app/cache

# 2) Переменные среды
ENV PYTHONPATH=/home/${USER}/app:/home/${USER}/app/shared_pkg/src
ENV CACHE_CONTAINER_DIR=${CACHE_CONTAINER_DIR}
ENV LLM_PORT=${LLM_PORT}

# 3) Создаём пользователя
RUN groupadd -r ${GROUP} \
    && useradd -r -g ${GROUP} ${USER}

# 4) Устанавливаем зависимости
WORKDIR /tmp
COPY ./llm/requirements.txt                 .
COPY ./shared_pkg/                          /tmp/shared_pkg/
RUN pip install --no-cache-dir -r requirements.txt \
 && pip install --no-cache-dir -e /tmp/shared_pkg

# 5) Создаем папки и выставляем права одним слоем
RUN mkdir -p /home/${USER}/app/logs \
             ${CACHE_CONTAINER_DIR} \
    && chown -R ${USER}:${GROUP} /home/${USER}

# 6) Переключаемся на пользователя
USER ${USER}
WORKDIR /home/${USER}/app

COPY --chown=${USER}:${GROUP} llm/src/.             .
COPY --chown=${USER}:${GROUP} shared_pkg/           ./shared_pkg/
COPY --chown=${USER}:${GROUP} src/domain/           ./domain/


# DEBUG
# RUN pip install debugpy
# RUN apt-get update && apt-get install -y net-tools
# END DEBUG



# Copy mock server code
# COPY config/                    ./config/
# COPY src/utils/                 ./utils/


# Expose LLM
EXPOSE ${LLM_PORT}
CMD uvicorn llm.main:app --host 0.0.0.0 --port ${LLM_PORT}