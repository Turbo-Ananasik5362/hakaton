# Контракт API

> **СТАТУС: ВРЕМЕННЫЙ.** Формат, по которому проверяющая система дёргает агента на автопрогонах, нужно получить
> у организаторов (платформа / Telegram-чат). Когда придёт спецификация — вставить её в раздел «Официальный контракт»
> ниже как есть и привести `app/api` к официальному контракту, временные эндпоинты оставить.
> Логика агента от формата не зависит: весь транспорт живёт в `app/api/`, ядро работает с `app/models.py`.

## Официальный контракт
(пусто — вставить сюда)

## Временный контракт (наш)

Базовый адрес: `http://<публичный адрес ВМ>:8000`

### POST /api/v1/sessions
Начать работу по запросу.
```json
{
  "request": "Нужна программа повышения квалификации на 72 часа ...",
  "attachments": [{"filename": "ref.docx", "content_base64": "..."}],
  "answers": {"program_type": "ПК", "hours": 72},
  "auto_approve": false
}
```
Ответ — одно из состояний:
```json
{"session_id": "s_...", "status": "need_clarification", "questions": [{"id": "hours", "text": "Сколько часов ...?", "options": ["16", "36", "72", "другое"]}]}
{"session_id": "s_...", "status": "skeleton_ready", "skeleton": { "...": "каркас: цель, компетенции, учебный план" }, "validation": { "...": "отчёт по часам" }}
{"session_id": "s_...", "status": "done", "kit": { "...": "полный JSON комплекта" }, "files": {"program_docx": "/api/v1/sessions/s_.../files/program.docx", "slides_pptx": "...", "tests_gift": "...", "kit_json": "..."}, "report": { "...": "отчёт самопроверки" }}
```
`auto_approve: true` — режим для автоматической проверки: каркас утверждается автоматически и сразу собирается комплект
(уточняющие вопросы всё равно задаются, если данных не хватает).

### POST /api/v1/sessions/{id}/answers
Ответы на уточняющие вопросы: `{"answers": {"hours": "72", ...}}` → следующее состояние.

### POST /api/v1/sessions/{id}/approve
Утвердить каркас (опционально с правками: `{"skeleton": {...}}`) → генерация комплекта → `status: done`.
Правки каркаса (все поля необязательные; объекты — по ID или по-человечески, «тема 2.3»):
```json
{"skeleton": {
  "title": "...", "goal": "...", "tasks": ["..."],
  "competencies": [{"id": "C1", "title": "...", "activity": "..."}],
  "outcomes": [{"id": "LO1.2", "text": "..."}],
  "modules": [{"id": "M2", "title": "...", "control_form": "..."}],
  "topics": [{"id": "тема 2.3", "title": "...", "hours": {"lk": 2, "pr": 4, "lb": 0, "srs": 1, "distance": 0}}],
  "hours_scope": "module"
}}
```
Часы темы фиксируются, остальное перераспределяется кодом в пределах модуля (`"program"` — по всей программе),
сумма остаётся равной объёму. Если часы не сходятся — `422 hours_conflict`, каркас не меняется.
Повторный `approve` после `failed` продолжает генерацию: готовые шаги не повторяются.

### POST /api/v1/sessions/{id}/patch
Точечная правка: `{"target": "T2.3", "instruction": "заменить тему на ..."}` → пересобирается только затронутое,
в ответе `changed_ids` и новый отчёт проверки.

### GET /api/v1/sessions/{id}
Текущее состояние сессии. `GET /api/v1/sessions/{id}/files/{name}` — скачать файл.

### POST /api/v1/run  (однократный вызов для скриптов и, возможно, для автопрогона)
`{"request": "...", "answers": {...}, "attachments": [...]}` → сразу `done` (или `need_clarification`, если ответов не хватило).

### GET /health → `{"status": "ok"}`

### POST /api/v1/hours/rebalance  (служебный, для редактора каркаса)
`{"session_id": "s_...", "locked": {"T2.3": {"lk": 2, "pr": 4}}, "scope": "module"}` (или `"kit": {...}` вместо
`session_id`) → предпросмотр: часы всех тем и модулей, `total_hours`, `ok`, `conflicts` с текстом для методиста.
Сессия не меняется.

### Состояния сессии
`new` (разбор запроса) → `need_clarification` → `skeleton_ready` → `queued` (заняты оба слота генерации) →
`generating` → `done`; после `done` — `patching` → `done`. При сбое — `failed` с полем `error` и тем, что успели.
В каждом ответе есть `message` — что происходит и что делать дальше, по-русски; `progress` — шаги со статусами
(`pending`, `running`, `done`, `failed`, `skipped`). Одновременно генерируются не больше 2 сессий.
После перезапуска сервера незавершённые сессии продолжаются с последнего готового шага.

### Ожидание ответа (`?wait=`)
| Запрос | По умолчанию | Что значит |
|---|---|---|
| `POST /sessions`, `/answers` | `wait=true` | ждать вопросов, каркаса или (при `auto_approve`) готового комплекта |
| `POST /approve` | `wait=false` | сразу `generating` + `progress`; опрашивать `GET /sessions/{id}` раз в 2 с |
| `POST /patch` | `wait=true` | ждать результата правки |
| `POST /run` | всегда синхронно | |

### Ошибки
Всегда JSON: `{"status": "error", "error": {"code": "...", "message": "понятный текст", "details": ...}}`.
`400 invalid_input` (битое вложение), `404 session_not_found` / `file_not_found`, `409 invalid_state` / `session_busy`,
`413 payload_too_large`, `415 unsupported_attachment` (нужны .docx, .pdf, .txt), `422 invalid_request` (поля запроса),
`422 unknown_target` / `hours_conflict`. Ошибка внутри шага агента не даёт 500: сессия переходит в `failed`,
код — в `error.code` сессии: `step_unavailable` (шаг ещё не подключён), `llm_error` (сбой шлюза), `budget_exceeded`
(дневной предохранитель), `interrupted` (сессия прервана перезапуском сервера и не продолжена — старше 2 ч),
`internal_error`. Неподдерживаемое или битое вложение запрос не отклоняет: файл пропускается, причина — в
`skeleton.assumptions` (формат определяется по содержимому: PDF с расширением .docx будет прочитан как PDF).

### Файлы (`files` в ответе `done`)
Сейчас: `kit_json` (комплект без разобранного образца — его реквизиты и текст в выгрузку не попадают) и `report_json`.
Появятся с выгрузками: `program_docx`, `slides_pptx`, `tests_gift` (и Moodle XML / CSV). После правки (`/patch`)
в ответе есть `patch`: `{target_id, ok, changed_ids, error, revision}` — неудачная правка комплект не меняет.
`GET /` показывает `llm_mode` сервера: для автопрогона на ВМ должно быть `live`.
