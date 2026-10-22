"""Расчёт эффекта на синтетических измерениях, без модели и рабочего архива прогонов."""

from __future__ import annotations

import json
from dataclasses import replace
from pathlib import Path
from typing import Any

import pytest

from scripts.effect import (
    MISSING,
    QueryInfo,
    RunMetrics,
    collect_runs,
    exclusion_reasons,
    main,
    read_run,
    render_report,
)


def write_run(root: Path, name: str = "batch/q1", **files: Any) -> Path:
    path = root / name
    path.mkdir(parents=True)
    for stem, value in files.items():
        (path / f"{stem}.json").write_text(json.dumps(value, ensure_ascii=False), encoding="utf-8")
    return path


def test_table_and_cli_use_saved_measurements(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("scripts.effect.ROOT", tmp_path)
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
    out = tmp_path / "nested/effect.md"
    queries = tmp_path / "queries.json"
    queries.write_text(
        json.dumps({"queries": [{"id": 1, "expected": {"program_type": "dop", "total_hours": 24}}]})
    )
    monkeypatch.chdir(root)
    assert main(["--runs-dir", str(root), "--out", "nested/effect.md", "--queries", str(queries)]) == 0
    assert not (root / "nested/effect.md").exists()
    text = out.read_text(encoding="utf-8")
    assert "| runs/batch/q1 | dop | 24 | 12 | 100 | 7,25 | 8 | 1 | live | done / да / warnings |" in text
    assert "7,25 (n=1)" in text
    assert "76–316 ч" in text and "95–98,75 %" in text


def test_missing_fields_do_not_turn_into_zeroes(tmp_path: Path) -> None:
    path = write_run(tmp_path, kit={"program": {}}, cost={"by_step": {}}, transcript={}, report={})
    run = read_run(path, "batch/q1")
    assert run.cost_rub is None and run.calls is None and run.hours is None
    # Неизвестные измерения допустимы, если три условия отбора подтверждены другими полями.
    run = replace(run, mode="live", complete=True, status="done", calls=1, cached_calls=0)
    text = render_report([run])
    row = next(line for line in text.splitlines() if line.startswith("| batch/q1 |"))
    assert row.count(MISSING) == 6
    assert all(value.strip() == MISSING for value in row.split("|")[2:7])


def test_mock_replay_and_unknown_modes_are_excluded_from_cost_summary(tmp_path: Path) -> None:
    for index, (mode, amount) in enumerate(
        (("live", 10), ("live", 20), ("mock", 500), ("replay", 900), (None, 777))
    ):
        write_run(
            tmp_path,
            f"batch/q{index}",
            kit={"program": {"total_hours": 48}},
            transcript={"complete": True, "final_status": "done"},
            cost={"mode": mode, "total_rub": amount, "by_step": {"step": {"calls": 2, "cached": 0}}},
        )
    text = render_report(collect_runs(tmp_path))
    summary = text.split("## Сводка по открытым запросам", 1)[1].split("## Экономия", 1)[0]
    assert "15 (n=2)" in summary
    assert "500" not in summary and "900" not in summary and "777" not in summary
    assert "Исключено 3 отладочных прогонов" in text
    for index in (2, 3, 4):
        assert f"| {MISSING} | {MISSING} | {MISSING} | {MISSING} |" in next(
            line for line in summary.splitlines() if line.startswith(f"| {index} |")
        )


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
    assert "| q1 |" not in render_report([run])
    assert "доля кеша больше 20 %" in render_report([run])


def test_remote_zero_is_not_a_cost_measurement(tmp_path: Path) -> None:
    path = write_run(
        tmp_path,
        cost={"mode": "server", "total_rub": 0},
        transcript={"mode": "server", "transport": "http://example.test:8000"},
        report={"cost_rub": 5, "llm_calls": 8},
    )
    run = read_run(path, "q1")
    assert run.cost_rub is None and run.calls is None


def test_incomplete_runs_are_only_counted_in_exclusions(tmp_path: Path) -> None:
    path = write_run(
        tmp_path,
        transcript={"final_status": "done", "complete": False},
        report={"generation_seconds": 8},
        cost={"mode": "live", "total_rub": 2, "by_step": {"step": {"calls": 2, "cached": 0}}},
    )
    text = render_report([read_run(path, "q1")])
    assert "| q1 |" not in text
    assert "Исключено 1 отладочных прогонов" in text
    assert "нет подтверждения полного комплекта" in text
    assert "8 (n=1)" not in text and "2 (n=1)" not in text


def test_conflicting_modes_do_not_enter_live_cost_summary(tmp_path: Path) -> None:
    path = write_run(tmp_path, cost={"mode": "live", "total_rub": 11}, transcript={"mode": "mock"})
    run = read_run(path, "q1")
    assert run.mode == "live / mock"
    text = render_report([run])
    assert "режим не live или не определён однозначно" in text
    assert "11 (n=1)" not in text


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
    run = read_run(path, "q1")
    assert any("kit.json: не удалось прочитать JSON" in note for note in run.notes)
    assert any("cost.json: ожидался объект JSON" in note for note in run.notes)
    text = render_report(collect_runs(tmp_path))
    assert "Исключено 1 отладочных прогонов" in text


def test_savings_parameters_are_explicit_and_validated() -> None:
    text = render_report([], hours_per_week=30, review_hours=6)
    assert "54–234 ч" in text and "В рабочей неделе 30 часов" in text
    assert "занимают 6 часа" in text and "длительность правки пока не измерена" in text
    with pytest.raises(ValueError, match="положительными"):
        render_report([], review_hours=0)


def measured_run(tmp_path: Path) -> RunMetrics:
    path = write_run(
        tmp_path,
        kit={"program": {"type": "pk", "total_hours": 72}},
        transcript={
            "query_id": 1,
            "mode": "live",
            "complete": True,
            "final_status": "done",
            "finished_at": "2026-10-20T10:00:00+03:00",
        },
        cost={"mode": "live", "total_rub": 1, "by_step": {"step": {"calls": 10, "cached": 0}}},
        report={"status": "warnings", "generation_seconds": 100},
    )
    return read_run(path, "runs/batch/q1")


@pytest.mark.parametrize(("cached", "included"), [(0, True), (2, True), (3, False), (10, False)])
def test_cache_share_boundary_is_inclusive(tmp_path: Path, cached: int, included: bool) -> None:
    run = replace(measured_run(tmp_path), cached_calls=cached)
    assert (not exclusion_reasons(run)) is included
    assert ("| runs/batch/q1 |" in render_report([run])) is included


@pytest.mark.parametrize(
    ("calls", "cached"),
    [(None, None), (10, None), (0, 0), (2, 3), (2, -1)],
)
def test_unknown_or_invalid_cache_share_cannot_pass_filter(
    tmp_path: Path, calls: int | None, cached: int | None
) -> None:
    run = replace(measured_run(tmp_path), calls=calls, cached_calls=cached)
    assert run.cached_share is None
    assert "| runs/batch/q1 |" not in render_report([run])
    assert "доля кеша неизвестна или некорректна" in render_report([run])


def test_report_errors_do_not_add_a_fourth_filter(tmp_path: Path) -> None:
    run = replace(measured_run(tmp_path), report_status="errors")
    assert not exclusion_reasons(run)
    assert "| live | done / да / errors |" in render_report([run])


def test_summary_uses_last_eligible_run_per_query_and_averages_queries(tmp_path: Path) -> None:
    old = replace(measured_run(tmp_path), skeleton_seconds=10)
    # Поздний замер идёт раньше по имени папки; определяющей должна быть дата transcript.
    current = replace(
        old,
        path="runs/a/q1",
        skeleton_seconds=20,
        generation_seconds=200,
        cost_rub=2,
        recorded_at=old.recorded_at.replace(day=21),
    )
    rejected = replace(
        current, path="runs/new/q1", cached_calls=3, recorded_at=old.recorded_at.replace(day=22)
    )
    other = replace(
        old, path="runs/other/q2", query_id="2", skeleton_seconds=40, generation_seconds=400, cost_rub=4
    )
    queries = [QueryInfo("1", "pk", 72), QueryInfo("2", "pk", 72), QueryInfo("3", "dop", 16)]
    text = render_report([rejected, other, current, old], queries=queries)
    summary = text.split("## Сводка по открытым запросам", 1)[1].split("## Экономия", 1)[0]
    assert "| 1 | pk | 72 | runs/a/q1 | 20 | 200 | 2 |" in summary
    assert "| 2 | pk | 72 | runs/other/q2 | 40 | 400 | 4 |" in summary
    assert f"| 3 | dop | 16 | {MISSING} | {MISSING} | {MISSING} | {MISSING} |" in summary
    assert "| 30 (n=2) | 300 (n=2) | 3 (n=2) |" in summary
    assert "runs/new/q1" not in text
    assert "runs/batch/q1" not in summary


def test_transcript_id_and_real_timestamps_take_precedence_over_folder_names(tmp_path: Path) -> None:
    path = write_run(
        tmp_path,
        "batch/q99",
        transcript={"query_id": "separate-query", "finished_at": "2026-10-20T10:00:00+03:00"},
    )
    run = read_run(path, "batch/q99")
    assert run.query_id == "separate-query"
    assert run.recorded_at.isoformat() == "2026-10-20T07:00:00+00:00"


@pytest.mark.parametrize("out", ["../outside.md", "/outside-effect.md"])
def test_cli_rejects_output_outside_project(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str], out: str
) -> None:
    project = tmp_path / "project"
    project.mkdir()
    monkeypatch.setattr("scripts.effect.ROOT", project)
    with pytest.raises(SystemExit) as exc:
        main(["--out", out])
    assert exc.value.code == 2
    assert "Путь должен находиться внутри проекта" in capsys.readouterr().err
    assert not (tmp_path / "outside.md").exists()


def test_cli_rejects_output_through_external_symlink(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    project, external = tmp_path / "project", tmp_path / "external"
    project.mkdir()
    external.mkdir()
    (project / "escape").symlink_to(external, target_is_directory=True)
    monkeypatch.setattr("scripts.effect.ROOT", project)
    with pytest.raises(SystemExit) as exc:
        main(["--out", "escape/effect.md"])
    assert exc.value.code == 2
    assert "Путь должен находиться внутри проекта" in capsys.readouterr().err
    assert not (external / "effect.md").exists()
