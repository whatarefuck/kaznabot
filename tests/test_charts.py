import warnings

from kaznabot.charts import ChartRow, render_chart
from kaznabot.reports import CAPTION_LIMIT, Summary, render_chart_caption
from kaznabot.settlement import Transfer


def test_chart_renders_all_cases_without_missing_glyphs() -> None:
    rows = [
        ChartRow("Бай", paid=1370000, owed=468497, balance=901503),
        ChartRow("Арслан", paid=0, owed=408498, balance=-408498),
        ChartRow("Саша", paid=100, owed=100, balance=0),  # в расчёте
        ChartRow("Очень-очень длинное имя соседа по квартире", paid=1, owed=0, balance=1),
    ]
    with warnings.catch_warnings(record=True) as caught:
        warnings.simplefilter("always")
        png = render_chart(rows, "Траты с начала учёта", "Всего 13 700,01 ₽")
    assert png.startswith(b"\x89PNG")
    assert [str(w.message) for w in caught if "Glyph" in str(w.message)] == []  # кириллица и ₽ есть в шрифте


def test_chart_with_single_person_and_no_money() -> None:
    assert render_chart([ChartRow("Бай", 0, 0, 0)], "Траты", "Всего 0 ₽").startswith(b"\x89PNG")


def test_caption_fits_telegram_limit() -> None:
    n = 60
    names = {i: f"Сосед номер {i}" for i in range(n + 1)}
    transfers = [Transfer(i, 0, 100000 + i) for i in range(1, n + 1)]
    caption = render_chart_caption(Summary(None, [], names, list(names), {}, {}, {}, transfers))
    assert len(caption) <= CAPTION_LIMIT
    assert caption.endswith("/debts")
