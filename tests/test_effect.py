"""Расчёт эффекта на синтетических измерениях, без модели и рабочего архива прогонов."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from scripts.effect import MISSING, collect_runs, main, read_run, render_report


def write_run(root: Path, name: str = "batch/q1", **files: Any) -> Path:
    path = root / name
    path.mkdir(parents=True)
    for stem, value in files.items():
        (path / f"{stem}.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def test_table_and_cli_use_saved_measurements(tmp_path: Path) -> None:
    root = tmp_path / "runs"
    write_run(
        root,
        kit={"program": {"type": "dop", "total_hours": 24}},
        transcript={
            "mode": "live",
            "complete": True,
            "final_status": "done",
            "events": [
                {"t": 12, "status": "skeleton_ready", "action": "answer"},
                {"t": 15, "status": "generating", "action": "approve"},
                {"t": 115, "status": "done", "action": "result"},
            ],
        },
        cost={
            "mode": "live",
            "total_rub": 7.25,
            "by_step": {"intake": {"calls": 2, "cached": 1}, "content": {"calls": 6, "cached": 0}},
        },
        report={"status": "warnings", "cost_rub": 5},
    )
    out = tmp_path / "effect.md"
    assert main(["--runs-dir", str(root), "--out", str(out)]) == 0
    text = out.read_text(encoding="utf-8")
    assert "| runs/batch/q1 | dop | 24 | 12 | 100 | 7,25 | 8 | 1 | live | done / да / warnings |" in text
    assert "7,25 / 7,25 (n=1)" in text
    assert "76–316 ч" in text and "95–98,75 %" in text


def test_missing_fields_do_not_turn_into_zeroes(tmp_path: Path) -> None:
    path = write_run(tmp_path, kit={"program": {}}, cost={"by_step": {}}, transcript={}, report={})
    run = read_run(path, "batch/q1")
    assert run.cost_rub is None and run.calls is None and run.hours is None
    text = render_report([run])
    row = next(line for line in text.splitlines() if line.startswith("| batch/q1 |"))
    assert row.count(MISSING) == 11
    assert "| 0 |" not in row


def test_mock_replay_and_unknown_modes_are_excluded_from_cost_summary(tmp_path: Path) -> None:
    for index, (mode, amount) in enumerate(
        (("live", 10), ("live", 20), ("mock", 500), ("replay", 900), (None, 777))
    ):
        write_run(
            tmp_path,
            f"batch/q{index}",
            kit={"program": {"total_hours": 48}},
            cost={"mode": mode, "total_rub": amount},
        )
    text = render_report(collect_runs(tmp_path))
    summary = text.split("## Сводка по объёмам", 1)[1].split("## Экономия", 1)[0]
    assert "15 / 20 (n=2)" in summary
    assert "500" not in summary and "900" not in summary and "777" not in summary
    for mode in ("mock", "replay"):
        row = next(line for line in summary.splitlines() if f"| {mode} |" in line)
        assert f"| {MISSING} / {MISSING} |" in row


def test_timing_uses_first_result_not_parallel_steps_or_later_patches(tmp_path: Path) -> None:
    path = write_run(
        tmp_path,
        transcript={
            "events": [
                {"action": "create", "status": "need_clarification", "t": 3},
                {"action": "answer", "status": "skeleton_ready", "t": 25},
                {"action": "approve", "status": "generating", "t": 30},
                {"action": "result", "status": "done", "t": 80},
                {"action": "patch", "status": "done", "t": 200},
            ],
            "seconds": 200,
            "patches": [{"seconds": 120}],
        },
        report={"generation_seconds": 999, "timing": {"lectures": 300, "tests": 400}},
    )
    run = read_run(path, "q1")
    assert run.skeleton_seconds == 25 and run.generation_seconds == 50
    assert any("правки" in note for note in run.notes)


def test_explicit_generation_time_and_zero_cost_are_preserved(tmp_path: Path) -> None:
    path = write_run(
        tmp_path,
        cost={"mode": "live", "total_rub": 0, "by_step": {"cached": {"calls": 3, "cached": 3}}},
        report={"generation_seconds": 0},
    )
    run = read_run(path, "q1")
    assert run.cost_rub == 0 and run.generation_seconds == 0 and run.calls == 3
    assert "0 / 0 (n=1)" in render_report([run])


def test_remote_zero_is_not_a_cost_measurement(tmp_path: Path) -> None:
    path = write_run(
        tmp_path,
        cost={"mode": "server", "total_rub": 0},
        transcript={"mode": "server", "transport": "http://example.test:8000"},
        report={"cost_rub": 5, "llm_calls": 8},
    )
    run = read_run(path, "q1")
    assert run.cost_rub is None and run.calls is None


def test_incomplete_runs_are_visible_but_not_full_kit_timing(tmp_path: Path) -> None:
    path = write_run(
        tmp_path,
        transcript={"final_status": "done", "complete": False},
        report={"generation_seconds": 8},
        cost={"mode": "live", "total_rub": 2},
    )
    text = render_report([read_run(path, "q1")])
    summary = text.split("## Сводка по объёмам", 1)[1].split("## Экономия", 1)[0]
    assert "| 1 / 0 |" in summary
    assert "8 / 8" not in summary and "2 / 2 (n=1)" in summary


def test_conflicting_modes_do_not_enter_live_cost_summary(tmp_path: Path) -> None:
    path = write_run(tmp_path, cost={"mode": "live", "total_rub": 11}, transcript={"mode": "mock"})
    text = render_report([read_run(path, "q1")])
    assert "live / mock" in text
    assert "11 / 11 (n=1)" not in text


@pytest.mark.parametrize("value", [None, "5", True, -1, float("nan"), float("inf")])
def test_invalid_numbers_and_partial_call_counts_are_missing(tmp_path: Path, value: Any) -> None:
    path = write_run(
        tmp_path,
        cost={"total_rub": value, "by_step": {"one": {"calls": 2}, "two": {}}},
        kit={"program": {"total_hours": value}},
    )
    run = read_run(path, "q1")
    assert run.cost_rub is None and run.hours is None and run.calls is None


def test_broken_json_and_empty_archive_are_reported(tmp_path: Path) -> None:
    assert "Прогоны не найдены" in render_report(collect_runs(tmp_path))
    path = write_run(tmp_path, cost=[])
    (path / "kit.json").write_text("{broken", encoding="utf-8")
    text = render_report(collect_runs(tmp_path))
    assert "kit.json: не удалось прочитать JSON" in text
    assert "cost.json: ожидался объект JSON" in text


def test_savings_parameters_are_explicit_and_validated() -> None:
    text = render_report([], hours_per_week=30, review_hours=6)
    assert "54–234 ч" in text and "В рабочей неделе 30 часов" in text
    assert "занимают 6 часа" in text and "длительность правки пока не измерена" in text
    with pytest.raises(ValueError, match="положительными"):
        render_report([], review_hours=0)
