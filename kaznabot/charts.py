"""Картинка для /chart: кто сколько заплатил, какая у кого доля и кто кому должен.

Две панели с одним порядком людей (по убыванию оплат):
1. «Заплатил и доля» — полоса = сколько заплатил, вертикальная черта = его доля в общих покупках.
2. «Баланс» — расходящиеся полосы от нуля: вправо (синий) получает, влево (красный) отдаёт.

Цвета — синий/красный из проверенной палитры (валидатор dataviz: все проверки, в том числе для дальтоников),
но смысл никогда не держится на одном цвете: у каждой полосы есть подпись со знаком и легенда.
matplotlib импортируется лениво: на сервере мало памяти, а график нужен редко.
"""

import io
import threading
from dataclasses import dataclass
from typing import TYPE_CHECKING

from kaznabot.money import fmt_rub

if TYPE_CHECKING:
    from matplotlib.axes import Axes
    from matplotlib.figure import Figure

# Палитра и «чернила» — reference palette из dataviz (светлая поверхность)
SURFACE = "#fcfcfb"
INK = "#0b0b0b"
INK_SECONDARY = "#52514e"
INK_MUTED = "#898781"
BASELINE = "#c3c2b7"
POSITIVE = "#2a78d6"  # заплатил / получает
NEGATIVE = "#e34948"  # отдаёт

WIDTH_IN = 8.0
DPI = 150
ROW_IN = 0.42  # высота строки одного человека
BAR_IN = 0.16  # толщина полосы (≈24 px при DPI 150)
RADIUS_PX = 4.0  # скругление на конце полосы, у нуля — прямой угол
NAME_MAX = 18

_lock = threading.Lock()  # matplotlib не потокобезопасен, а рисуем в asyncio.to_thread


@dataclass(frozen=True)
class ChartRow:
    name: str
    paid: int  # копейки
    owed: int  # копейки
    balance: int  # копейки, paid − owed


def render_chart(rows: list[ChartRow], title: str, subtitle: str) -> bytes:
    with _lock:
        return _render(rows, title, subtitle)


def _render(rows: list[ChartRow], title: str, subtitle: str) -> bytes:
    from matplotlib.backends.backend_agg import FigureCanvasAgg
    from matplotlib.figure import Figure
    from matplotlib.lines import Line2D
    from matplotlib.patches import Patch

    rows = sorted(rows, key=lambda r: (-r.paid, r.name))
    n = len(rows)
    header_in, panel_head_in, gap_in, bottom_in = 0.85, 0.55, 0.35, 0.2
    panel_in = n * ROW_IN
    height_in = header_in + 2 * (panel_head_in + panel_in) + gap_in + bottom_in

    fig = Figure(figsize=(WIDTH_IN, height_in), dpi=DPI, facecolor=SURFACE)
    FigureCanvasAgg(fig)
    fig.text(0.03, 1 - 0.32 / height_in, title, fontsize=15, fontweight="bold", color=INK, va="center")
    fig.text(0.03, 1 - 0.62 / height_in, subtitle, fontsize=10.5, color=INK_SECONDARY, va="center")

    left, width = 0.24, 0.72

    def panel(top_in: float) -> "Axes":
        bottom = 1 - (top_in + panel_head_in + panel_in) / height_in
        ax = fig.add_axes((left, bottom, width, panel_in / height_in))
        ax.set_facecolor(SURFACE)
        ax.set_ylim(n - 0.5, -0.5)
        ax.set_yticks(range(n), [_short(r.name) for r in rows], fontsize=10.5, color=INK)
        ax.tick_params(axis="y", length=0, pad=8)
        ax.set_xticks([])
        for side in ("top", "right", "bottom", "left"):
            ax.spines[side].set_visible(False)
        return ax

    # --- 1. заплатил и доля ---
    ax1 = panel(header_in)
    top = max(max(r.paid, r.owed) for r in rows) or 1
    ax1.set_xlim(0, top * 1.55)  # справа место под подписи
    ax1.axvline(0, color=BASELINE, linewidth=1)
    for i, r in enumerate(rows):
        _bar(fig, ax1, i, 0, r.paid, POSITIVE)
        ax1.plot([r.owed, r.owed], [i - 0.3, i + 0.3], color=INK_SECONDARY, linewidth=2, solid_capstyle="butt")
        ax1.text(
            max(r.paid, r.owed) + top * 0.03,
            i,
            f"{fmt_rub(r.paid)}  ·  доля {fmt_rub(r.owed)}",
            va="center",
            fontsize=9.5,
            color=INK_SECONDARY,
        )
    _panel_title(ax1, "Заплатил и доля")
    ax1.legend(
        handles=[
            Patch(facecolor=POSITIVE, label="заплатил"),
            Line2D([], [], color=INK_SECONDARY, linewidth=2, label="доля в общих покупках"),
        ],
        loc="lower right",
        bbox_to_anchor=(1.0, 1.0),
        ncol=2,
        frameon=False,
        fontsize=9,
        labelcolor=INK_SECONDARY,
        handlelength=1.2,
        borderaxespad=0.3,
    )

    # --- 2. баланс ---
    ax2 = panel(header_in + panel_head_in + panel_in + gap_in)
    span = max(abs(r.balance) for r in rows) or 1
    ax2.set_xlim(-span * 1.6, span * 1.6)  # симметрично: ноль всегда посередине
    ax2.axvline(0, color=BASELINE, linewidth=1)
    for i, r in enumerate(rows):
        if r.balance == 0:
            ax2.text(span * 0.04, i, "в расчёте", va="center", fontsize=9.5, color=INK_MUTED)
            continue
        positive = r.balance > 0
        _bar(fig, ax2, i, 0, r.balance, POSITIVE if positive else NEGATIVE)
        offset = span * 0.04
        ax2.text(
            r.balance + (offset if positive else -offset),
            i,
            fmt_rub(r.balance, signed=True),
            va="center",
            ha="left" if positive else "right",
            fontsize=9.5,
            color=INK_SECONDARY,
        )
    _panel_title(ax2, "Баланс")
    ax2.legend(
        handles=[Patch(facecolor=NEGATIVE, label="− отдаёт"), Patch(facecolor=POSITIVE, label="+ получает")],
        loc="lower right",
        bbox_to_anchor=(1.0, 1.0),
        ncol=2,
        frameon=False,
        fontsize=9,
        labelcolor=INK_SECONDARY,
        handlelength=1.2,
        borderaxespad=0.3,
    )

    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor=SURFACE)
    return buf.getvalue()


def _short(name: str) -> str:
    return name if len(name) <= NAME_MAX else name[: NAME_MAX - 1] + "…"


def _panel_title(ax: "Axes", text: str) -> None:
    ax.set_title(text, loc="left", fontsize=11.5, fontweight="bold", color=INK, pad=10, x=-0.3)


def _bar(fig: "Figure", ax: "Axes", y: float, x0: float, x1: float, color: str) -> None:
    """Горизонтальная полоса от x0 до x1: прямой угол у нуля, скругление 4 px на конце."""
    from matplotlib.patches import PathPatch
    from matplotlib.path import Path

    if x1 == x0:
        return
    # Перевод пикселей в единицы данных: лимиты осей уже заданы, размер осей фиксирован
    bbox = ax.get_position()
    fig_w, fig_h = fig.get_size_inches() * fig.dpi
    xmin, xmax = ax.get_xlim()
    ymax, ymin = ax.get_ylim()  # ось y перевёрнута
    px_x = bbox.width * fig_w / (xmax - xmin)
    px_y = bbox.height * fig_h / (ymax - ymin)

    half = BAR_IN * fig.dpi / 2 / px_y
    sign = 1 if x1 > x0 else -1
    rx = min(RADIUS_PX / px_x, abs(x1 - x0))
    ry = min(RADIUS_PX / px_y, half)
    lo, hi = y - half, y + half
    tip = x1 - sign * rx
    verts = [
        (x0, lo),
        (tip, lo),
        (x1, lo),
        (x1, lo + ry),
        (x1, hi - ry),
        (x1, hi),
        (tip, hi),
        (x0, hi),
        (x0, lo),
    ]
    codes = [
        Path.MOVETO,
        Path.LINETO,
        Path.CURVE3,
        Path.CURVE3,
        Path.LINETO,
        Path.CURVE3,
        Path.CURVE3,
        Path.LINETO,
        Path.CLOSEPOLY,
    ]
    ax.add_patch(PathPatch(Path(verts, codes), facecolor=color, edgecolor="none"))
