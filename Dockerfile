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
WORKDIR /tmp/build
COPY requirements.txt ./requirements.txt
COPY ./llm/requirements.txt                 ./llm/requirements.txt
RUN pip install --no-cache-dir -r llm/requirements.txt
WORKDIR /tmp

COPY ./shared_pkg/                          /tmp/shared_pkg/
RUN pip install --no-cache-dir -e /tmp/shared_pkg

# GIGACHAT specials. Корневой и выпускающий (intermediate) УЦ Минцифры
# Debian/Ubuntu base
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates curl \
 && mkdir -p /usr/local/share/ca-certificates/russian-trusted \
 && curl -kfsSL https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt \
      -o /usr/local/share/ca-certificates/russian-trusted/russian_trusted_root_ca.crt \
 && curl -kfsSL https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt  \
      -o /usr/local/share/ca-certificates/russian-trusted/russian_trusted_sub_ca.crt \
 && update-ca-certificates -v \
 # (опционально) собрать отдельный бандл именно для клиента LLM
 && cat /usr/local/share/ca-certificates/russian-trusted/russian_trusted_root_ca.crt \
        /usr/local/share/ca-certificates/russian-trusted/russian_trusted_sub_ca.crt \
      > /etc/ssl/certs/mincifry_bundle.pem

# Минцифра: кладём root+sub в системное хранилище и собираем отдельный bundle
RUN set -eux; \
    apt-get update && apt-get install -y --no-install-recommends ca-certificates curl; \
    mkdir -p /usr/local/share/ca-certificates/russian-trusted; \
    curl -kfsSL https://gu-st.ru/content/lending/russian_trusted_root_ca_pem.crt \
      | awk '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/{print}' \
      | tr -d "\r" \
      > /usr/local/share/ca-certificates/russian-trusted/mincifry_root.crt; \
    curl -kfsSL https://gu-st.ru/content/lending/russian_trusted_sub_ca_pem.crt \
      | awk '/-----BEGIN CERTIFICATE-----/,/-----END CERTIFICATE-----/{print}' \
      | tr -d "\r" \
      > /usr/local/share/ca-certificates/russian-trusted/mincifry_sub.crt; \
    update-ca-certificates; \
    cat /usr/local/share/ca-certificates/russian-trusted/mincifry_root.crt \
        /usr/local/share/ca-certificates/russian-trusted/mincifry_sub.crt \
      > /etc/ssl/certs/mincifry_bundle.pem; \
    chmod 0644 /etc/ssl/certs/mincifry_bundle.pem

ENV GIGACHAT_CA_BUNDLE=/etc/ssl/certs/mincifry_bundle.pem \
    GIGACHAT_VERIFY_SSL_CERTS=true \
    SSL_CERT_FILE=/etc/ssl/certs/mincifry_bundle.pem \
    REQUESTS_CA_BUNDLE=/etc/ssl/certs/mincifry_bundle.pem


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