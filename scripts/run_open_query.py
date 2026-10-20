#!/usr/bin/env python3
"""Прогон открытого (или стресс-) запроса через API агента: запрос → уточнения → каркас → утверждение → комплект.

    python scripts/run_open_query.py --id 4 --mode mock --out eval/runs/20261019-1100/q4
    python scripts/run_open_query.py --id 4 --url http://<адрес ВМ>:8000 --out eval/runs/vm-check/q4

По умолчанию сервер поднимается в этом же процессе (FastAPI TestClient, без сети), с режимом LLM из --mode.
С --url сценарий идёт по HTTP к уже запущенному серверу (режим LLM задаёт сервер; на ВМ — live).

На уточняющие вопросы отвечает methodist_answers запроса, на остальные — «_default» («на ваше усмотрение»).
Сохраняет в --out: transcript.json (весь диалог), skeleton.json, kit.json, report.json, cost.json и все файлы,
которые отдаёт сервер. Если комплект собран не полностью (шаги генерации ещё не подключены), это честно записано
в transcript.json → limitations.

Live (и любой --url) с 00:00 до 10:00 МСК не запускается без --force: бюджет нужен утреннему автопрогону.
Коды выхода: 0 — сценарий дошёл до конца; 1 — сессия завершилась ошибкой; 2 — отказ (время, бюджет, ввод);
3 — не дождались генерации.
"""

from __future__ import annotations

import argparse
import base64
import json
import logging
import sys
import time
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import httpx  # noqa: E402

from app.config import DEFAULT_MODEL, Settings, now_msk  # noqa: E402
from app.llm import estimate_cost, spent_today_rub  # noqa: E402
from app.render.bundle import NO_SLIDES_NOTE, NO_TESTS_NOTE  # noqa: E402

NO_MATERIAL_NOTES = (NO_SLIDES_NOTE, NO_TESTS_NOTE.split("(")[0])

API = "/api/v1"
NIGHT_UNTIL_HOUR = 10  # 00:00–10:00 МСК — бюджет утреннего автопрогона
MAX_CLARIFY_ROUNDS = 5
POLL_SECONDS = 2.0
DEFAULT_ANSWER = "на ваше усмотрение"
EXPECTED_FILES = {
    "program_docx": "программа .docx",
    "slides_pptx": "слайды .pptx",
    "tests_gift": "тесты GIFT",
    "tests_moodle_xml": "тесты Moodle XML",
    "tests_csv": "тесты CSV",
}
# Выгрузка ожидается, только если в комплекте есть материал для неё (у стажировки без лекций и тестов их нет —
# сервер объясняет это в notes)
NEEDS_MATERIAL = {
    "slides_pptx": lambda kit: any(lec.get("slides") for lec in kit.get("lectures", [])),
    "tests_gift": lambda kit: bool(kit.get("questions")),
    "tests_moodle_xml": lambda kit: bool(kit.get("questions")),
    "tests_csv": lambda kit: bool(kit.get("questions")),
}
OWN_FILES = ("kit.json", "report.json")  # сохраняются из ответа API

# Оценка вызовов LLM на один запрос: (шаг, вызовов, видимый вход, выход); 1200 скрытых токенов шлюза добавляет
# estimate_cost. Каркас — верх живого замера 19.10.2026 (gigachat-gateway, «Проверено на шлюзе»); содержание —
# комплект 72 ч в полном режиме (docs/ARCHITECTURE.md §7.3; точную оценку по каркасу делает шаг plan).
STEP_ESTIMATES: list[tuple[str, int, int, int]] = [
    ("intake", 1, 1_500, 400),
    ("skeleton", 1, 3_500, 5_500),  # вход вырос: до 40 ТФ из реестра в промпте
    ("content", 27, 48_000, 27_000),  # слайды собирает код — без вызовов модели
]
CLARIFY_ESTIMATE = ("clarify", 1, 2_500, 400)  # только если в запросе противоречие


# --------------------------------------------------------------------------------------------------
# Защита бюджета
# --------------------------------------------------------------------------------------------------


def night_refusal(now: datetime, live: bool, force: bool) -> str | None:
    if live and not force and now.hour < NIGHT_UNTIL_HOUR:
        return (
            f"Сейчас {now:%H:%M} МСК: с 00:00 до {NIGHT_UNTIL_HOUR}:00 живые вызовы не запускаем — дневной бюджет "
            "нужен утреннему автопрогону. Используйте --mode mock или replay, или --force, если это осознанно."
        )
    return None


def estimate_rub(with_clarify: bool = True) -> float:
    steps = [*STEP_ESTIMATES, *([CLARIFY_ESTIMATE] if with_clarify else [])]
    return sum(estimate_cost(calls, inp, out, DEFAULT_MODEL) for _, calls, inp, out in steps)


# --------------------------------------------------------------------------------------------------
# Запись диалога
# --------------------------------------------------------------------------------------------------


@dataclass
class Transcript:
    query: dict[str, Any]
    mode: str
    transport: str
    started: float = field(default_factory=time.monotonic)
    started_at: str = field(default_factory=lambda: now_msk().isoformat(timespec="seconds"))
    events: list[dict[str, Any]] = field(default_factory=list)
    questions: list[dict[str, Any]] = field(default_factory=list)
    limitations: list[str] = field(default_factory=list)
    session_id: str | None = None
    last_progress: list[dict[str, Any]] = field(default_factory=list)
    final_status: str | None = None
    error: dict[str, Any] | None = None

    def event(self, action: str, method: str, path: str, response: httpx.Response, **extra: Any) -> dict:
        body = _json(response)
        item = {
            "t": round(time.monotonic() - self.started, 2),
            "action": action,
            "request": f"{method} {path}",
            "status_code": response.status_code,
            "status": body.get("status"),
            "message": body.get("message") or (body.get("error") or {}).get("message"),
            **extra,
        }
        if body.get("questions"):
            item["questions"] = body["questions"]
        if body.get("progress"):
            self.last_progress = body["progress"].get("steps", [])
            item["progress"] = [f"{s['name']}: {s['status']}" for s in self.last_progress]
        if body.get("validation"):
            item["validation"] = _report_summary(body["validation"])
        if body.get("report"):
            item["report"] = _report_summary(body["report"])
        self.events.append(item)
        return body

    def to_json(self) -> dict[str, Any]:
        query = self.query
        first = self.events[0]["status"] if self.events else None
        return {
            "query_id": query.get("id"),
            "title": query.get("title"),
            "mode": self.mode,
            "transport": self.transport,
            "started_at": self.started_at,
            "finished_at": now_msk().isoformat(timespec="seconds"),
            "seconds": round(time.monotonic() - self.started, 2),
            "request": {"text": query.get("request"), "attachments": query.get("attachments", [])},
            "session_id": self.session_id,
            "asked_before_building": first == "need_clarification",
            "questions": self.questions,
            "final_status": self.final_status,
            "complete": not self.limitations and self.final_status == "done",
            "limitations": self.limitations,
            "error": self.error,
            "events": self.events,
        }


def _json(response: httpx.Response) -> dict[str, Any]:
    try:
        data = response.json()
    except ValueError:
        return {"status": "error", "error": {"message": response.text[:500]}}
    return data if isinstance(data, dict) else {}


def _report_summary(report: dict[str, Any]) -> dict[str, Any]:
    counts: dict[str, int] = defaultdict(int)
    for issue in report.get("issues", []):
        counts[issue.get("severity", "?")] += 1
    return {
        "status": report.get("status"),
        "issues": dict(counts),
        "warnings_for_methodist": [w.get("type") for w in report.get("warnings_for_methodist", [])],
    }


# --------------------------------------------------------------------------------------------------
# Сценарий
# --------------------------------------------------------------------------------------------------


def load_query(path: Path, query_id: str) -> dict[str, Any]:
    data = json.loads(path.read_text(encoding="utf-8"))
    queries = data["queries"] if isinstance(data, dict) else data
    for query in queries:
        if str(query.get("id")) == str(query_id):
            return query
    known = ", ".join(str(q.get("id")) for q in queries)
    raise SystemExit(f"В {path} нет запроса с id={query_id}. Есть: {known}.")


def attachments_payload(query: dict[str, Any]) -> list[dict[str, str]]:
    payload = []
    for item in query.get("attachments", []):
        path = ROOT / item
        if not path.is_file():
            raise SystemExit(f"Нет файла вложения {item} (см. attachments запроса {query.get('id')}).")
        payload.append(
            {"filename": path.name, "content_base64": base64.b64encode(path.read_bytes()).decode()}
        )
    return payload


def answer_for(question: dict[str, Any], answers: dict[str, str]) -> tuple[str, str]:
    value = answers.get(question["id"])
    if value:
        return value, "methodist"
    return answers.get("_default", DEFAULT_ANSWER), "default"


def run_scenario(
    client: httpx.Client, query: dict[str, Any], out: Path, log: Transcript, timeout: float
) -> int:
    answers = {k: str(v) for k, v in (query.get("methodist_answers") or {}).items()}
    path = f"{API}/sessions"
    payload = {"request": query["request"], "attachments": attachments_payload(query), "auto_approve": False}
    response = client.post(path, json=payload)
    body = log.event("create", "POST", path, response)
    log.session_id = body.get("session_id")
    if response.status_code != 200 or not log.session_id:
        return _fail(log, body)

    rounds = 0
    while body.get("status") == "need_clarification":
        rounds += 1
        if rounds > MAX_CLARIFY_ROUNDS:
            log.limitations.append(f"Уточняющие вопросы не закончились за {MAX_CLARIFY_ROUNDS} раундов.")
            return _fail(log, body)
        given: dict[str, str] = {}
        for question in body.get("questions", []):
            value, source = answer_for(question, answers)
            given[question["id"]] = value
            log.questions.append({**question, "round": rounds, "answer": value, "answer_source": source})
        path = f"{API}/sessions/{log.session_id}/answers"
        response = client.post(path, json={"answers": given})
        body = log.event("answer", "POST", path, response, answers=given)

    if body.get("status") == "skeleton_ready":
        _write(
            out / "skeleton.json", {"skeleton": body.get("skeleton"), "validation": body.get("validation")}
        )
        path = f"{API}/sessions/{log.session_id}/approve"
        response = client.post(path)
        body = log.event("approve", "POST", path, response)

    deadline = time.monotonic() + timeout
    polls = 0
    while body.get("status") in ("queued", "generating", "patching", "new"):
        if time.monotonic() > deadline:
            log.final_status = body.get("status")
            log.limitations.append(f"Генерация не закончилась за {timeout:.0f} с — прогон остановлен.")
            return 3
        time.sleep(min(POLL_SECONDS, 0.2 * 2**polls))  # быстро для mock, раз в 2 с для живой генерации
        polls += 1
        path = f"{API}/sessions/{log.session_id}"
        response = client.get(path)
        body = _json(response)
        if response.status_code != 200:
            body = log.event("poll", "GET", path, response)
            return _fail(log, body)
    log.event(
        "result", "GET", f"{API}/sessions/{log.session_id}", client.get(f"{API}/sessions/{log.session_id}")
    )

    log.final_status = body.get("status")
    if body.get("status") != "done":
        return _fail(log, body)
    _save_kit(client, body, out, log)
    return 0


def _fail(log: Transcript, body: dict[str, Any]) -> int:
    log.final_status = body.get("status") or "error"
    log.error = body.get("error") or {"message": body.get("message")}
    message = (log.error or {}).get("message") or ""
    if "replay" in message.lower():
        log.limitations.append("В кеше нет ответа модели для этого запроса: нужен прогон в режиме live.")
    return 1


def _save_kit(client: httpx.Client, body: dict[str, Any], out: Path, log: Transcript) -> None:
    if body.get("kit") is not None:
        _write(out / "kit.json", body["kit"])
    if body.get("report") is not None:
        _write(out / "report.json", body["report"])
    files = body.get("files") or {}
    for key, url in files.items():
        name = url.rsplit("/", 1)[-1]
        if name in OWN_FILES:
            continue
        response = client.get(url)
        if response.status_code == 200:
            (out / name).write_bytes(response.content)
        else:
            log.limitations.append(f"Файл {name} ({key}) не скачан: HTTP {response.status_code}.")
    for note in body.get("notes") or []:
        if note.startswith(NO_MATERIAL_NOTES):  # нет лекций или тестов по замыслу программы — не недоделка
            continue
        log.limitations.append(note)
    skipped = [
        s for s in (body.get("progress") or {}).get("steps", []) if s.get("status") in ("skipped", "failed")
    ]
    for step in skipped:
        text = f"Шаг «{step['title']}» не выполнен: {step.get('error') or step['status']}."
        if not any(step["title"] in item for item in log.limitations):
            log.limitations.append(text)
    kit = body.get("kit") or {}
    missing = [
        title
        for key, title in EXPECTED_FILES.items()
        if key not in files and NEEDS_MATERIAL.get(key, lambda _kit: True)(kit)
    ]
    if missing:
        log.limitations.append(
            "Комплект собран не полностью: нет выгрузок — "
            + ", ".join(missing)
            + ". Есть каркас, отчёт и kit.json."
        )


# --------------------------------------------------------------------------------------------------
# Расход
# --------------------------------------------------------------------------------------------------


def cost_summary(log_path: Path, since: str, mode: str, remote: bool, progress: list[dict[str, Any]]) -> dict:
    by_step: dict[str, dict[str, float]] = {}
    if log_path.is_file() and not remote:
        for line in log_path.read_text(encoding="utf-8").splitlines():
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if str(row.get("ts", "")) < since:
                continue
            step = by_step.setdefault(
                row.get("step") or "?",
                {
                    "calls": 0,
                    "cached": 0,
                    "prompt_tokens": 0,
                    "completion_tokens": 0,
                    "cost_rub": 0.0,
                    "seconds": 0.0,
                },
            )
            step["calls"] += 1
            step["cached"] += 1 if row.get("cached") else 0
            step["prompt_tokens"] += int(row.get("prompt_tokens") or 0)
            step["completion_tokens"] += int(row.get("completion_tokens") or 0)
            step["cost_rub"] = round(step["cost_rub"] + float(row.get("cost_rub") or 0), 6)
            step["seconds"] = round(step["seconds"] + float(row.get("seconds") or 0), 3)
    total = round(sum(s["cost_rub"] for s in by_step.values()), 4)
    note = None
    if remote:
        note = "Прогон на удалённом сервере: его расход в локальном logs/llm_cost.jsonl не виден (смотрите на ВМ)."
    elif mode == "mock":
        note = "Режим mock: ответы модели из фикстур, вызовы не тарифицируются."
    return {
        "mode": mode,
        "total_rub": total,
        "by_step": by_step,
        "step_seconds": {
            f"{p['phase']}:{p['name']}": p.get("seconds") for p in progress if p.get("seconds") is not None
        },
        "note": note,
    }


def _write(path: Path, data: Any) -> None:
    path.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


# --------------------------------------------------------------------------------------------------
# Точка входа
# --------------------------------------------------------------------------------------------------


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    parser.add_argument("--id", required=True, help="id запроса в файле --queries")
    parser.add_argument(
        "--mode", choices=("mock", "replay", "live"), default="mock", help="режим LLM (без --url)"
    )
    parser.add_argument("--out", required=True, type=Path, help="папка прогона, например eval/runs/<дата>/q4")
    parser.add_argument("--queries", type=Path, default=ROOT / "eval" / "open_queries.json")
    parser.add_argument("--url", help="адрес запущенного сервера, например http://<адрес ВМ>:8000")
    parser.add_argument("--force", action="store_true", help="разрешить live ночью и сверх остатка бюджета")
    parser.add_argument("--timeout", type=float, default=1800.0, help="сколько ждать генерации, с")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    if not logging.getLogger().handlers:  # без построчного лога HTTP-запросов; итог печатается в конце
        logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")
    queries = args.queries if args.queries.is_absolute() else ROOT / args.queries
    query = load_query(queries, args.id)
    remote = bool(args.url)
    live = remote or args.mode == "live"
    mode = "server" if remote else args.mode

    refusal = night_refusal(now_msk(), live, args.force)
    if refusal:
        print(refusal, file=sys.stderr)
        return 2

    settings = Settings.from_env().model_copy(update={"llm_mode": args.mode})
    if live:
        estimate = estimate_rub()
        spent = spent_today_rub(settings.cost_log)
        left = max(settings.daily_budget_rub - spent, 0.0)
        print(
            f"Оценка запроса {query['id']}: ≈ {estimate:.2f} ₽ (разбор, при противоречиях уточнение, каркас; "
            f"содержание комплекта 72 ч в полном режиме). Сегодня потрачено {spent:.2f} ₽, до предохранителя {left:.2f} ₽ "
            "(траты коллег и ВМ здесь не видны)."
        )
        if not remote and estimate > left and not args.force:
            print(
                "Остатка бюджета не хватает — прогон не запущен (--force, если это осознанно).",
                file=sys.stderr,
            )
            return 2

    out = args.out if args.out.is_absolute() else ROOT / args.out
    out.mkdir(parents=True, exist_ok=True)
    since = now_msk().isoformat(timespec="seconds")
    log = Transcript(query=query, mode=mode, transport=args.url or "in-process")

    if remote:
        timeout = httpx.Timeout(600.0, connect=10.0)
        with httpx.Client(base_url=args.url.rstrip("/"), timeout=timeout) as client:
            code = run_scenario(client, query, out, log, args.timeout)
    else:
        from fastapi.testclient import TestClient

        from app.main import create_app

        settings = settings.model_copy(update={"sessions_dir": out / ".sessions"})
        with TestClient(create_app(settings=settings)) as client:
            code = run_scenario(client, query, out, log, args.timeout)

    transcript = log.to_json()
    _write(out / "transcript.json", transcript)
    _write(out / "cost.json", cost_summary(settings.cost_log, since, mode, remote, log.last_progress))

    cost = json.loads((out / "cost.json").read_text(encoding="utf-8"))
    asked = len(transcript["questions"])
    print(
        f"Запрос {query['id']}: {transcript['final_status']} за {transcript['seconds']:.1f} с; "
        f"вопросов {asked}; расход {cost['total_rub']:.2f} ₽; папка {out.relative_to(ROOT) if out.is_relative_to(ROOT) else out}"
    )
    for item in transcript["limitations"]:
        print(f"  — {item}")
    if transcript["error"]:
        print(f"  ошибка: {transcript['error'].get('message')}", file=sys.stderr)
    return code


if __name__ == "__main__":
    sys.exit(main())
