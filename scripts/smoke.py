#!/usr/bin/env python3
"""Смоук перед сдачей: сервер в этом же процессе (FastAPI TestClient), LLM в mock, без сети.

    python scripts/smoke.py

Проверяет: GET /health; POST /api/v1/run на запросе 1 из eval/open_queries.json доходит до «done»; комплект валиден
по app/models.py и сходится по часам; все выгрузки (.docx, .pptx, GIFT, Moodle XML, CSV, JSON) отдаются,
скачиваются и разбираются; точечная правка (POST /patch) проходит и даёт версию 1.
Код выхода 0 — всё в порядке, 1 — что-то сломано.
"""

from __future__ import annotations

import base64
import io
import json
import logging
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from fastapi.testclient import TestClient  # noqa: E402

from app.config import Settings  # noqa: E402
from app.main import create_app  # noqa: E402
from app.models import Kit  # noqa: E402


def _open_file(name: str, content: bytes) -> None:
    suffix = Path(name).suffix.lower()
    if suffix == ".json":
        json.loads(content)
    elif suffix == ".docx":
        import docx

        docx.Document(io.BytesIO(content))
    elif suffix == ".pptx":
        import pptx

        pptx.Presentation(io.BytesIO(content))
    elif suffix == ".xml":
        import xml.etree.ElementTree as ET

        if not ET.fromstring(content).findall("question"):
            raise ValueError("в Moodle XML нет вопросов")
    elif suffix == ".gift":
        from app.render.gift import parse_gift

        if not parse_gift(content.decode("utf-8")):
            raise ValueError("в GIFT нет вопросов")
    elif suffix in (".txt", ".csv"):
        if not content.decode("utf-8-sig").strip():
            raise ValueError("пустой файл")


def main() -> int:
    logging.basicConfig(level=logging.WARNING)
    query = json.loads((ROOT / "eval" / "open_queries.json").read_text(encoding="utf-8"))["queries"][0]
    attachments = [
        {"filename": Path(p).name, "content_base64": base64.b64encode((ROOT / p).read_bytes()).decode()}
        for p in query.get("attachments", [])
    ]
    problems: list[str] = []
    with tempfile.TemporaryDirectory(dir=ROOT / ".tmp" if (ROOT / ".tmp").is_dir() else None) as tmp:
        settings = Settings(llm_mode="mock", sessions_dir=Path(tmp) / "sessions")
        with TestClient(create_app(settings=settings)) as client:
            health = client.get("/health")
            if health.status_code != 200 or health.json().get("status") != "ok":
                problems.append(f"/health: {health.status_code} {health.text[:100]}")
            response = client.post(
                "/api/v1/run",
                json={
                    "request": query["request"],
                    "attachments": attachments,
                    "answers": query["methodist_answers"],
                },
            )
            body = response.json()
            if response.status_code != 200 or body.get("status") != "done":
                problems.append(
                    f"/api/v1/run: {response.status_code} {body.get('status')} {body.get('error')}"
                )
            else:
                kit = Kit.model_validate(body["kit"])
                if kit.total_hours() != kit.program.total_hours:
                    problems.append(f"часы не сходятся: {kit.total_hours()} ≠ {kit.program.total_hours}")
                for key, url in body.get("files", {}).items():
                    file = client.get(url)
                    try:
                        if file.status_code != 200:
                            raise ValueError(f"HTTP {file.status_code}")
                        _open_file(url.rsplit("/", 1)[-1], file.content)
                    except Exception as exc:  # noqa: BLE001 — смоук перечисляет все проблемы
                        problems.append(f"файл {key}: {exc}")
                missing = [
                    k
                    for k in ("program_docx", "slides_pptx", "tests_gift", "tests_moodle_xml", "tests_csv")
                    if k not in body.get("files", {})
                ]
                print(
                    f"ok: {kit.program.type.value}, {kit.total_hours():g} ч, файлы: {', '.join(body['files'])}"
                )
                if missing:
                    problems.append(f"нет выгрузок: {', '.join(missing)}")
                patched = client.post(  # точечная правка на основном пути: done → patch → done
                    f"/api/v1/sessions/{body['session_id']}/patch",
                    json={"target": "тема 1.2", "instruction": "пересобери тему 1.2"},
                )
                patch = patched.json().get("patch") or {}
                if patched.status_code != 200 or not patch.get("ok") or patch.get("revision") != 1:
                    problems.append(
                        f"/patch: {patched.status_code} {patch.get('error') or patched.text[:200]}"
                    )
                else:
                    print(f"ok: правка — {patch.get('summary')}")
    for problem in problems:
        print(f"ОШИБКА: {problem}", file=sys.stderr)
    return 1 if problems else 0


if __name__ == "__main__":
    sys.exit(main())
