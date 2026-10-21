# Образ сервера «Каркас». Python 3.10 — как на ВМ хакатона. Подробно: docs/deploy.md, «Способ 2 — Docker».
#
#   GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
#
# Запуск без compose (с томами для сессий, кеша и журналов) — там же, в docs/deploy.md.
# Ключи шлюза (OPENAI_BASE_URL, OPENAI_API_KEY) в образ не попадают: только из окружения при запуске.
# Без них сервер работает в режиме mock (заготовленные ответы модели).
FROM python:3.10-slim

ENV PYTHONDONTWRITEBYTECODE=1 \
    PYTHONUNBUFFERED=1 \
    PIP_DISABLE_PIP_VERSION_CHECK=1 \
    PIP_ROOT_USER_ACTION=ignore

WORKDIR /app

# Зависимости — отдельным слоем: правка кода не переустанавливает пакеты
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt

# Непривилегированный пользователь, от которого работает сервер
RUN useradd --create-home --uid 1000 --shell /usr/sbin/nologin karkas

# Код и данные: владелец root, пользователю karkas — только чтение.
# tests/fixtures/llm — заготовки ответов модели для режима mock.
COPY app ./app
COPY data ./data
COPY tests/fixtures/llm ./tests/fixtures/llm

# COPY переносит права из папки сборщика (файл 0600 у него — 0600 в образе), поэтому права на чтение
# выставляются явно. Писать сервер может только туда, где хранит данные: сессии, кеш ответов модели,
# журнал расходов (в docker-compose.yml это тома).
RUN chmod -R a+rX app data tests \
 && mkdir -p data/sessions cache/llm logs \
 && chown -R karkas:karkas data/sessions cache logs

# Коммит для «version» в /health (в образе нет .git)
ARG GIT_COMMIT=""
ENV APP_COMMIT=${GIT_COMMIT}

USER karkas
EXPOSE 8000

# Без прокси: HTTP(S)_PROXY из окружения не должен перехватывать запрос к самому себе
HEALTHCHECK --interval=30s --timeout=5s --start-period=30s --retries=3 \
  CMD ["python", "-c", "import urllib.request as u; u.build_opener(u.ProxyHandler({})).open('http://127.0.0.1:8000/health', timeout=4)"]

CMD ["uvicorn", "app.main:app", "--host", "0.0.0.0", "--port", "8000"]
