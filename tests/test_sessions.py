"""Хранилище сессий: пути, атомарная запись, защита от выхода за папку сессии."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.models import Kit, SessionStatus
from app.pipeline.base import RequestInput, SessionNotFound
from app.storage.sessions import SessionCorrupted, SessionStore, safe_filename
from tests.validate_kits import make_pk_kit


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("Программа ПК.docx", "Программа ПК.docx"),
        ("../../etc/passwd", "passwd"),
        ("a/b\\c.pdf", "c.pdf"),
        ("", "file"),
        ("...", "file"),
        ("отчёт: итог?.txt", "отчёт_ итог_.txt"),
    ],
)
def test_safe_filename(raw: str, expected: str) -> None:
    assert safe_filename(raw) == expected


def test_safe_filename_keeps_extension_when_truncated() -> None:
    name = safe_filename("а" * 300 + ".docx")
    assert name.endswith(".docx") and len(name) <= 120


def test_create_load_roundtrip(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    state = store.create(
        RequestInput(text="запрос", answers={"hours": "36"}), [("../образец.docx", b"PK\x03\x04")]
    )
    loaded = store.load(state.id)
    assert loaded.status == SessionStatus.NEW
    assert loaded.answers == {"hours": "36"}
    [attachment] = loaded.request.attachments
    assert attachment.filename == "../образец.docx"
    assert (store.inputs_dir(state.id) / attachment.stored_as).read_bytes() == b"PK\x03\x04"
    assert attachment.stored_as == "01_образец.docx"
    assert store.ids() == [state.id]
    assert (tmp_path / state.id / "log.jsonl").is_file()


def test_kit_roundtrip_and_absent_kit(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    state = store.create(RequestInput(text="запрос"))
    assert store.load_kit(state.id) is None
    kit = make_pk_kit()
    store.save_kit(state.id, kit)
    loaded = store.load_kit(state.id)
    assert isinstance(loaded, Kit) and loaded.total_hours() == kit.total_hours()
    assert not list((tmp_path / state.id).glob("*.tmp"))  # временные файлы атомарной записи не остаются


@pytest.mark.parametrize("bad", ["", "s_123", "../s_000000000000", "s_ZZZZZZZZZZZZ", "s_000000000000/.."])
def test_bad_session_ids_are_not_found(tmp_path: Path, bad: str) -> None:
    store = SessionStore(tmp_path)
    with pytest.raises(SessionNotFound):
        store.load(bad)


def test_file_path_stays_inside_files_dir(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    state = store.create(RequestInput(text="запрос"))
    (store.files_dir(state.id) / "kit.json").write_text("{}", encoding="utf-8")
    assert store.file_path(state.id, "kit.json") is not None
    for name in ("../state.json", "..", ".hidden", "missing.json", "sub/kit.json"):
        assert store.file_path(state.id, name) is None


def test_corrupted_state_is_reported(tmp_path: Path) -> None:
    store = SessionStore(tmp_path)
    state = store.create(RequestInput(text="запрос"))
    (tmp_path / state.id / "state.json").write_text("{not json", encoding="utf-8")
    with pytest.raises(SessionCorrupted):
        store.load(state.id)
