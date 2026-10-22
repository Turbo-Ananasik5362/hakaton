# Контракт API

> **Статус: основной.** Отдельного формата вызова агента организаторы не прислали; автопрогоны идут на этом контракте.
> Если формат придёт, к нему добавится тонкий адаптер в `app/api/` (их запрос → `RequestInput`, сессия → их ответ)
> с тестом на их пример, а спецификация — в раздел «Другой формат» ниже; эндпоинты `/api/v1` останутся рабочими.
> Логика агента от формата не зависит: весь транспорт живёт в `app/api/`, ядро работает с `app/models.py`.

## Контракт

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
{"session_id": "s_...", "status": "done", "kit": { "...": "полный JSON комплекта" }, "files": {"program_docx": "/api/v1/sessions/s_.../files/program.docx", "report_docx": "/api/v1/sessions/s_.../files/report.docx", "slides_pptx": "...", "tests_gift": "...", "kit_json": "..."}, "report": { "...": "отчёт самопроверки" }}
```
`auto_approve: true` — режим для автоматической проверки: каркас утверждается автоматически и сразу собирается комплект
(уточняющие вопросы всё равно задаются, если данных не хватает).

### POST /api/v1/sessions/{id}/answers
Ответы на уточняющие вопросы: `{"answers": {"hours": "72", ...}}` → следующее состояние.

### POST /api/v1/sessions/{id}/approve
Утвердить каркас (опционально с правками: `{"skeleton": {...}}`) → `generating` или `queued`; результат — опросом сессии.
С `?wait=true` запрос ждёт завершения генерации комплекта.
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
Точечная правка готового комплекта (`status: done`): `{"target": "тема 2.3", "instruction": "замени тему 2.3 на «…»"}`.
`target` — ID объекта или его название по-человечески («тема 2.3», «модуль 2», «итоговый тест», «результат 1.2»);
`instruction` — что сделать (пусто — пересобрать объект заново). По умолчанию ждёт результат (`?wait=false` — сразу
`patching`, затем опрос `GET /api/v1/sessions/{id}`). Пересобирается только затронутое (docs/ARCHITECTURE.md §5.2), затем
самопроверка и все выгрузки.

Ответ — состояние сессии (`done`) и поле `patch`:
`{"target_id": "T2.3", "ok": true, "operation": "replace_topic", "summary": "Тема 2.3 заменена: …", "changed_ids": [...], "revision": 1, "error": null}`;
`changed_ids` — и на верхнем уровне ответа; `kit.revision` и `kit.history` — версия и история правок.
Правка, которую выполнить нельзя (часы не сходятся, модель не ответила, удаление единственной темы модуля),
комплект не меняет: `patch.ok = false`, `patch.error` — причина и что можно сделать, HTTP 200. Объект не найден или
ссылка неоднозначна — `422 unknown_target`; сессия не в `done` — `409 invalid_state`.

### GET /api/v1/sessions/{id}
Текущее состояние сессии. `GET /api/v1/sessions/{id}/files/{name}` — скачать файл.

### POST /api/v1/run  (однократный вызов для скриптов и, возможно, для автопрогона)
`{"request": "...", "answers": {...}, "attachments": [...]}` → сразу `done` (или `need_clarification`, если ответов не хватило).

### GET /health → `{"status": "ok", "llm_mode": "live", "llm_ready": true, "version": "0.5.0+0633257"}`
`llm_mode` — действующий режим (`live` / `mock` / `replay`; `LLM_MODE=auto` превращается в `live` при заданных
`OPENAI_BASE_URL` и `OPENAI_API_KEY`, иначе в `mock`). `llm_ready` — режим `live` и шлюз настроен (адрес и ключ
не отдаются). `version` — версия и короткий хеш коммита. `GET /` дополнительно отдаёт `warning`, если ответы
модели не настоящие.

### POST /api/v1/hours/rebalance  (служебный, для редактора каркаса)
`{"session_id": "s_...", "locked": {"T2.3": {"lk": 2, "pr": 4}}, "scope": "module"}` (или `"kit": {...}` вместо
`session_id`) → предпросмотр: часы всех тем и модулей, `total_hours`, `ok`, `conflicts` с текстом для методиста.
Сессия не меняется.

### Состояния сессии
`new` (разбор запроса) → `need_clarification` → `skeleton_ready` → `queued` (заняты все слоты генерации) →
`generating` → `done`; после `done` — `patching` → `done`. При сбое — `failed` с полем `error` и тем, что успели.
В каждом ответе есть `message` — что происходит и что делать дальше, по-русски; `progress` — шаги со статусами
(`pending`, `running`, `done`, `failed`, `skipped`). Одновременно генерируются не больше `MAX_GENERATING_SESSIONS` сессий (по умолчанию 2).
После перезапуска сервера незавершённые сессии моложе 2 ч продолжаются с последнего готового шага.

### Ожидание ответа (`?wait=`)
| Запрос | По умолчанию | Что значит |
|---|---|---|
| `POST /sessions`, `/answers` | `wait=true` | ждать вопросов, каркаса или (при `auto_approve`) готового комплекта |
| `POST /approve` | `wait=false` | сразу `generating` или `queued` + `progress`; опрашивать `GET /api/v1/sessions/{id}` раз в 2 с |
| `POST /patch` | `wait=true` | ждать результата правки |
| `POST /run` | всегда синхронно | |

### Ошибки
Всегда JSON: `{"status": "error", "error": {"code": "...", "message": "понятный текст", "details": ...}}`.
`400 invalid_input` (нет текста и ни одного читаемого вложения), `404 session_not_found` / `file_not_found`, `409 invalid_state` / `session_busy`,
`413 payload_too_large`, `422 invalid_request` (поля запроса),
`422 unknown_target` / `hours_conflict`, `429 rate_limited` (больше `RATE_LIMIT_PER_MINUTE` новых сессий в минуту
с одного адреса или больше `MAX_ACTIVE_SESSIONS` сессий в работе; заголовок `Retry-After` — через сколько секунд
повторить). Ошибка внутри шага агента не даёт 500: сессия переходит в `failed`, код — в `error.code` сессии, в
`error.message` — что случилось и что делать (таксономия — `app/pipeline/errors.py`):
- временные: `llm_rate_limited` (шлюз перегружен, 429), `llm_unavailable` (5xx, обрыв, таймаут);
- бюджет: `budget_exceeded` (дневной предохранитель);
- модель: `llm_bad_answer` (ответ не по схеме после ремонта), `llm_truncated` (не уместился даже после дробления),
  `llm_bad_request` (шлюз не принял запрос шага, 400/422);
- настройка: `llm_rejected` (шлюз отклонил ключ, 401/403), `llm_not_configured` (нет адреса/ключа, кеша или фикстуры);
- время: `deadline_exceeded` (истекло `GENERATION_DEADLINE_S`; обычно не ведёт к `failed` — недостающее
  пропускается, комплект собирается из готового, пропущенное — в `notes`);
- прочие: `step_unavailable` (шаг ещё не подключён), `interrupted` (прервана перезапуском и не продолжена — старше
  2 ч), `internal_error` (ошибка программы; трассировка — в журнале сессии `data/sessions/<id>/log.jsonl`). Неподдерживаемое или битое вложение запрос не отклоняет: файл пропускается, причина — в
`skeleton.assumptions` (формат определяется по содержимому: PDF с расширением .docx будет прочитан как PDF).

### Файлы (`files` в ответе `done`)
Ключи `files`: `program_docx` (программа по шаблону АЦР без отчёта), `report_docx` (отдельный `report.docx` для методиста),
`slides_pptx` (слайды лекций из запроса), `tests_gift`,
`tests_moodle_xml` и `tests_csv` (тесты: импорт в Moodle и таблица), `report_md` (отчёт автопроверки для
методиста; те же разделы доступны в `report.docx`), `kit_json` (комплект без разобранного образца —
его реквизиты и текст в выгрузку не попадают), `report_json`. Программа, слайды и файлы тестов после записи перечитываются и сверяются с
комплектом (V31 в `report`); `report.docx` отдельно проверяется на открытие. Нет материала (лекций со слайдами, тестовых вопросов) — нет файла, причина в `notes`.
Сбой одной выгрузки не мешает остальным: файла нет, причина — в `notes` и в `report` (V31), JSON выдаётся всегда. После правки (`/patch`)
в ответе есть `patch`: `{target_id, ok, changed_ids, error, revision}` — неудачная правка комплект не меняет.
`GET /health` и `GET /` показывают `llm_mode` и `llm_ready` сервера: для автопрогона должно быть `live` и `true`.
`message` ответа `done` — итог автопроверки человеческим языком: «Комплект собран, ошибок нет,
3 предупреждения — см. отчёт», при ошибках — главная из них, затем заголовки предупреждений методисту.
`report_page` ответа `done` — адрес страницы отчёта для методиста в браузере (`/sessions/{id}/report`, HTML;
с `?partial=1` — только раздел отчёта для встраивания в другие страницы).

### Отчёт самопроверки (`report`, он же `report.json`)
| Поле | Что в нём |
|---|---|
| `status`, `summary` | `ok` / `warnings` / `errors`; итог одной фразой |
| `warnings_for_methodist` | предупреждения методисту по общим правилам: `type` (`reference_errors`, `expectation_gap`, `schedule_tight`, `norm_replaced`, `unverified_sources`, `constraint_conflict`), `title`, `text` (с цифрами), `figures` (`[{label, value}]` — главные цифры), `options` (варианты решения), `program_text` (сохранённый для совместимости вариант текста без цитат образца и номеров утративших силу актов; пусто — годится `text`; в program.docx отчёт не включается), `code` (уточнение вида, например код конфликта солвера), `target_ids` |
| `issues` | замечания проверок V01–V46: `check`, `severity`, `message` (для методиста), `detail` (для разработчика), `fix_by`, `target_ids` |
| `hours` | часы по уровням: `declared`, `total`, `matches`, `topics`, `final_attestation`, `lk`, `pr`, `lb`, `srs`, `contact`, `distance`, `in_person`; `lines` — модуль → его темы, итоговая аттестация, итог (`level`, `id`, `number`, `title`, `total`, `hours`) |
| `coverage` | матрица «результат обучения × темы × оценочные средства»: `lo_id`, `text`, `topic_ids`, `assessment_ids`, `by_kind`, `status` (`ok` — два и более средства, `single`, `unassessed`, `no_topic`) |
| `sources` | `total`, `confirmed`, `unverified`, `rejected` (не прошли проверку ISBN/года/издательства), `dropped` (сверх лимита), `by_verification`, `by_section`, `unverified_ids` |
| `sections` | раздел шаблона → заполнен |
| `copying` | антиплагиат против приложенного образца: `references`, `ratio`, `threshold`, `fields_flagged`, `passed` |
| `judge` | смысловая проверка: `criterion` (J1–J9), `title`, `passed`, `reason` |
| `cost`, `cost_rub`, `llm_calls` | вызовы, токены, ₽ и секунды по навыкам; итоги |
| `timing`, `generation_seconds` | секунды по шагам генерации; время от начала генерации до выгрузки файлов |

Конфликты в ответе правки часов (`/hours/rebalance`, ошибка правки каркаса) — тоже с `title` и `options`.
В `kit.meta`: `generation_mode` (`full` / `economy`),
`budget` (оценка ₽ полного и экономного режима, остаток, оценка секунд), `generation_notes` (что пропущено и почему),
`literature_checks` (кандидаты литературы от модели по итогу проверки: `rejected`, `dropped`, `unverified` → сколько).

## Другой формат

(не присылали — если организаторы пришлют свой формат вызова, вставить спецификацию сюда как есть)
