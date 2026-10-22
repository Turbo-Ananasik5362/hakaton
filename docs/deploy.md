# Развёртывание сервера «Каркас»

Сервер — одно приложение FastAPI, его запускает uvicorn на порту **8000**. Команда запуска на ВМ и в Docker
одна и та же:

```bash
uvicorn app.main:app --host 0.0.0.0 --port 8000
```

Есть три способа запуска:

| Способ | Когда | Команда |
|---|---|---|
| 1. ВМ хакатона | основной: на нём идут автопрогоны лидерборда и демо | `bash scripts/vm_update.sh` на ВМ |
| 2. Docker | любой сервер или компьютер с Docker | `docker compose up -d --build` |
| 3. Локально | разработка и ручная проверка | `.venv/bin/uvicorn app.main:app --port 8000` |

После любого способа выполните [проверку после выкладки](#проверка-после-выкладки).

## Перед выкладкой

- `bash scripts/check.sh` выдаёт «ВСЁ ЗЕЛЁНОЕ»: ruff, тесты и смоук API, без сети и без расходов.
- Код запушен в `main` репозитория Gitea. Автопрогон и стоп-код смотрят **последний коммит `main`**.
- **Не выкладывайте с 06:00 до 10:00 МСК**: в это время идёт автопрогон, и перезапуск сервера его сорвёт.
- Ключи шлюза (`OPENAI_BASE_URL`, `OPENAI_API_KEY`) не попадают ни в git, ни в образ Docker. `.env` указан
  в `.gitignore` и `.dockerignore`.
- Лимит GigaChat 300 ₽ в сутки общий для всей команды: ноутбуков, ВМ и Docker. Предохранитель
  `LLM_DAILY_BUDGET_RUB` по умолчанию — 280 ₽, с запасом на траты коллег. Комплект на 72 часа стоит
  около 11 ₽.

## Способ 1 — ВМ хакатона

На ВМ проект лежит в `~/project`, используется Python 3.10 и своё `.venv`. Сервер работает в фоне
на `0.0.0.0:8000`.

**Прямо на ВМ** (в терминале ВМ):

```bash
cd ~/project && bash scripts/vm_update.sh
```

**С ноутбука** (нужен ssh-доступ через бастион, ssh спросит пароли):

```bash
bash scripts/deploy_vm.sh          # push в Gitea → на ВМ scripts/vm_update.sh
bash scripts/deploy_vm.sh --rsync  # если Gitea с ноутбука недоступен: файлы копируются по ssh, коммит делается с ВМ
```

Перед `--rsync` убедитесь, что в рабочей папке нет чужого или неготового: на ВМ выполняется `git add -A` и push
в `main`.

`scripts/vm_update.sh` делает следующее:

1. Выполняет `git pull --ff-only origin main`.
2. Создаёт `.venv`, если его нет, и выполняет `pip install -r requirements.txt`.
3. Останавливает старый uvicorn.
4. Запускает `LLM_MODE=live nohup .venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000` с выводом
   в `logs/server.log`.
5. Печатает ответ `/health` и коммит. Если сервер не ответил, печатает хвост лога и завершается с ошибкой.

| Режим | Что делает |
|---|---|
| `--pull` (по умолчанию) | обновление из `main`, установка и перезапуск |
| `--commit` | код уже скопирован через rsync: коммит и push в `main` с ВМ, затем установка и перезапуск |
| `--restart` | без git: только установка зависимостей и перезапуск (нужен при [откате](#откат-на-прошлый-коммит)) |

Ключи шлюза на ВМ берутся из окружения или из `~/project/.env`. Сервер на ВМ всегда запускается
с `LLM_MODE=live`. Если ключей нет, `vm_update.sh` выведет «ВНИМАНИЕ: llm_ready=false», и автопрогон получит
ошибки модели.

## Способ 2 — Docker

Нужны Docker и Docker Compose 2.24 или новее: в более старых версиях не поддерживается необязательный `.env`.

```bash
GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
docker compose ps                     # через 30–60 с в STATUS: healthy
curl -s http://127.0.0.1:8000/health
```

`GIT_COMMIT` нужен только для поля `version` в `/health`, потому что в образе нет папки `.git`. Без него
`version` будет `0.5.0` без коммита, а всё остальное работает так же.

Как устроен образ (`Dockerfile`):

- базовый образ `python:3.10-slim`, как на ВМ;
- сервер работает от пользователя `karkas` (uid 1000), не от root. Код и данные в `/app` доступны ему только
  на чтение (права выставляются при сборке, какими бы они ни были у файлов на машине сборщика), писать он может
  в сессии, кеш и журналы;
- в образ копируются `requirements.txt`, `app/`, `data/` (реестр, шаблоны, шапка организации) и `tests/fixtures/llm/`
  (заготовленные ответы модели для режима mock). Что не попадает в сборку, перечислено в `.dockerignore`;
- `HEALTHCHECK` каждые 30 с запрашивает `http://127.0.0.1:8000/health`.

**Порт.** `docker-compose.yml` открывает порт 8000 для всей сети. Если на компьютере в `.env` лежат ключи шлюза,
замените `"8000:8000"` на `"127.0.0.1:8000:8000"`, иначе любой в сети сможет тратить общий бюджет.

**Настройки.** Создайте `.env` в корне проекта, как в README, раздел «Работа с настоящей моделью».
`docker-compose.yml` подхватывает его через `env_file`. Без `.env` сервер работает в режиме mock. После правки
`.env` выполните `docker compose up -d`, и контейнер пересоздастся с новыми значениями.

**Тома.** Данные сервера лежат в томах Docker и не пропадают при пересборке образа. Имя проекта compose задано
явно (`karkas`), поэтому в `docker volume ls` тома называются `karkas_sessions`, `karkas_llm-cache` и `karkas_logs`
независимо от имени папки:

| Том | Путь в контейнере | Что там |
|---|---|---|
| `sessions` | `/app/data/sessions` | сессии: состояние, комплект, выгрузки |
| `llm-cache` | `/app/cache` | кеш ответов модели (повторный такой же вызов бесплатен) |
| `logs` | `/app/logs` | журнал расходов `llm_cost.jsonl` |

`docker compose down` останавливает сервер, тома при этом сохраняются. `docker compose down -v` **удаляет тома**
вместе с сессиями и кешем.

**Обновление:**

```bash
git pull --ff-only origin main
GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
```

**Без compose:**

```bash
docker build --build-arg GIT_COMMIT=$(git rev-parse --short HEAD) -t karkas .
docker run -d --name karkas -p 8000:8000 --env-file .env \
  -v karkas-sessions:/app/data/sessions -v karkas-cache:/app/cache -v karkas-logs:/app/logs karkas
```

`--env-file` требует, чтобы файл существовал (без ключей уберите этот параметр), и не снимает кавычки: пишите
значения в `.env` без кавычек, иначе адрес шлюза придёт в сервер вместе с ними. Compose кавычки снимает сам.

Если вместо томов подключаете папки хоста (`-v $PWD/data/sessions:/app/data/sessions`), они должны быть доступны
на запись пользователю с uid 1000. Иначе сервер не сможет сохранить сессию.

## Способ 3 — локально

Подробно описано в README, раздел «Быстрый старт».

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
.venv/bin/uvicorn app.main:app --port 8000                  # доступно только с этого компьютера
.venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000   # доступно из локальной сети
```

Если в `.env` лежат ключи, локальный сервер работает с настоящей моделью и тратит общий бюджет. Для работы
без расходов добавьте в `.env` строку `LLM_MODE=mock`.

## Проверка после выкладки

```bash
curl -s http://<адрес>:8000/health
# {"status":"ok","llm_mode":"live","llm_ready":true,"version":"0.5.0+9ebdd5e"}
```

| Поле | Нормально | Если не так |
|---|---|---|
| `status` | `"ok"` | Нет ответа — сервер не запущен. Смотрите лог (раздел ниже). |
| `llm_mode` | `"live"` на ВМ | `"mock"`: ответы модели заготовленные (локально и в Docker — нет ключей при `LLM_MODE=auto` или задан `LLM_MODE=mock`). `"replay"`: ответы только из кеша. `vm_update.sh` принудительно задаёт `live`, независимо от режима в `.env`. |
| `llm_ready` | `true` на ВМ | `false` в mock/replay или если в live нет `OPENAI_BASE_URL` либо `OPENAI_API_KEY`. Для live задайте обе переменные и перезапустите сервер. Значения ключей сервер не показывает. |
| `version` | `0.5.0+<коммит>` | Коммит должен совпадать с развёрнутым `HEAD` (`git rev-parse --short HEAD`), в Docker — с переданным при сборке `GIT_COMMIT`. При обычном обновлении из main это актуальный `origin/main`, при откате — выбранный коммит. |

Что ещё можно проверить:

- `GET /`: если ответы модели не настоящие, там будет поле `warning` с объяснением.
- `http://<адрес>:8000/docs`: интерактивная документация API.
- Сквозную проверку кода без сети и без расходов: в папке проекта на ВМ или локально выполните
  `.venv/bin/python scripts/smoke.py`. Скрипт проверяет код текущей папки в режиме mock, а не запущенный сервер.
- Полный комплект на настоящей модели (`POST /api/v1/run`, пример в README) стоит около 11 ₽. Не запускайте его
  с 06:00 до 10:00 МСК.

## Где логи и сессии

| Что | ВМ (`~/project`) | Docker | Локально |
|---|---|---|---|
| Лог сервера | `logs/server.log` (перезаписывается при каждом перезапуске) | `docker compose logs -f karkas` | вывод в терминале uvicorn |
| Расход модели | `logs/llm_cost.jsonl`; сводка за сегодня — `.venv/bin/python scripts/budget.py` | `docker compose exec karkas tail -n 20 logs/llm_cost.jsonl` | `logs/llm_cost.jsonl` |
| Сессии | `data/sessions/<id>/` | `docker compose exec karkas ls data/sessions` | `data/sessions/<id>/` |
| Кеш ответов модели | `cache/llm/` | том `llm-cache` | `cache/llm/` |

В папке сессии `data/sessions/<id>/` лежат:

- `state.json`: состояние, шаги, история правок;
- `kit.json`: комплект;
- `inputs/`: вложения запроса;
- `files/`: выгрузки (.docx, .pptx, GIFT, Moodle XML, CSV, отчёт);
- `log.jsonl`: журнал шагов и ошибок, у ошибок программы — с трассировкой.

Скопировать сессию из контейнера: `docker compose cp karkas:/app/data/sessions/<id> ./<id>`.

`logs/llm_cost.jsonl` показывает расход только той машины, на которой лежит. Траты коллег и других серверов
в нём не видны, а лимит 300 ₽ общий. Локальный предохранитель `LLM_DAILY_BUDGET_RUB` по умолчанию — 280 ₽.

## Откат на прошлый коммит

История `main` не переписывается: без force-push и `git reset` опубликованных коммитов. Есть два шага:

- **быстро** запустить на сервере прошлую версию;
- **насовсем** откатить изменения в `main` новым коммитом `git revert`.

Сессии и кеш при откате сохраняются. Прошлая версия, как правило, читает сессии, созданные более новой:
незнакомые поля она пропускает. Новые сессии автопрогона создаются заново, поэтому на них откат не влияет.

**ВМ, быстро** (например, если до автопрогона мало времени):

```bash
cd ~/project
git log --oneline -10                 # найти последний рабочий коммит
git checkout --detach <коммит>
bash scripts/vm_update.sh --restart   # без git pull: зависимости и перезапуск
curl -s http://127.0.0.1:8000/health  # в version — <коммит>
```

Пока ВМ стоит на прошлом коммите, `vm_update.sh` без `--restart` и `deploy_vm.sh` вернут ВМ на ветку `main`
и выложат свежий код. `deploy_vm.sh --rsync` в этом состоянии откажется работать и попросит сначала выполнить
`git checkout main`. Вернуться на `main` вручную: `bash scripts/vm_update.sh`.

**Насовсем, в репозитории** (на своём компьютере, в `main` без своих незапушенных коммитов):

```bash
git fetch origin && git pull --rebase origin main
git revert --no-edit <плохой_коммит>     # несколько подряд: git revert --no-edit <последний_рабочий>..HEAD
bash scripts/check.sh
git push origin main
```

Затем на ВМ: `cd ~/project && bash scripts/vm_update.sh`.

**Docker.** Перед каждым обновлением сохраняйте прошлый образ: `docker tag karkas:latest karkas:prev`.
Тогда откат не требует пересборки:

```bash
docker tag karkas:prev karkas:latest && docker compose up -d --no-build --force-recreate
```

Можно и пересобрать образ из прошлого коммита, но только из коммита, где `Dockerfile` уже есть:

```bash
git checkout --detach <коммит>
GIT_COMMIT=$(git rev-parse --short HEAD) docker compose up -d --build
```

## Если что-то не так

- **`/health` не отвечает.** На ВМ: `tail -n 50 ~/project/logs/server.log`. В Docker: `docker compose ps`
  (статус `unhealthy` или `restarting`) и `docker compose logs --tail 50 karkas`. `restart: unless-stopped`
  перезапускает упавший контейнер, но не «unhealthy»: такой перезапустите сами (`docker compose restart karkas`).
  Время в логе контейнера — UTC (на 3 часа меньше московского).
- **Порт 8000 занят.** На ВМ `vm_update.sh` сам останавливает старый uvicorn. Локально остановите свой uvicorn
  или запустите с `--port 8001` (адрес — `http://127.0.0.1:8001`). В Docker поменяйте левую часть `"8000:8000"`
  в `docker-compose.yml`, например на `"8001:8000"`.
- **`llm_ready: false`.** Режим mock/replay или не заданы ключи шлюза, см. таблицу проверки выше.
- **Ответы 429 или «бюджет исчерпан».** См. README, разделы «Надёжность» и «Ограничения». Суточный предохранитель —
  `LLM_DAILY_BUDGET_RUB`.
