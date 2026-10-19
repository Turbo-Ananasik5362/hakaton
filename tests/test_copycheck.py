from __future__ import annotations

from app.core.copycheck import copied_fragments, copy_ratio, shingles, tokenize

REFERENCE = (
    "Слушатели изучают роли бизнес-аналитика и системного аналитика, осваивают сбор требований "
    "через интервью и анкетирование, учатся формировать бэклог пользовательских историй."
)


def test_tokenize_normalizes() -> None:
    assert tokenize("Ёлка, ЁЖ и «Python 3.12»") == ["елка", "еж", "и", "python", "3", "12"]


def test_identical_text_is_fully_copied() -> None:
    assert copy_ratio(REFERENCE, [REFERENCE]) == 1.0


def test_unrelated_text_is_original() -> None:
    text = (
        "Школьники пишут первую программу на Python и разбирают переменные, условия и циклы на примерах игр."
    )
    assert copy_ratio(text, [REFERENCE]) == 0.0


def test_partial_copy_ratio_between_zero_and_one() -> None:
    text = (
        "Новое вступление про банковские данные. " + REFERENCE.split(",")[0] + ". Дальше совсем другой текст."
    )
    ratio = copy_ratio(text, [REFERENCE])
    assert 0 < ratio < 1


def test_whitelist_phrases_do_not_count() -> None:
    phrase = "Федеральный закон от 29.12.2012 № 273-ФЗ «Об образовании в Российской Федерации»"
    assert copy_ratio(phrase, [phrase]) == 0.0
    assert copy_ratio(phrase, [phrase], whitelist=()) == 1.0


def test_short_text_is_zero() -> None:
    assert shingles("всего три слова") == set()
    assert copy_ratio("всего три слова", ["всего три слова"]) == 0.0


def test_copied_fragments_reports_longest_run() -> None:
    text = "Вступление. " + REFERENCE + " Заключение своими словами без повторов."
    fragments = copied_fragments(text, [REFERENCE])
    assert fragments and fragments[0].startswith("слушатели изучают роли")
