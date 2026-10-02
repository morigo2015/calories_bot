from __future__ import annotations

import os
import tempfile
from dataclasses import dataclass
from datetime import date
from io import BytesIO
from pathlib import Path

_MATPLOTLIB_CONFIG_DIR = (
    Path(tempfile.gettempdir()) / f"calories-bot-matplotlib-{os.getuid()}"
)
_MATPLOTLIB_CONFIG_DIR.mkdir(mode=0o700, exist_ok=True)
_MATPLOTLIB_CONFIG_DIR.chmod(0o700)
os.environ.setdefault("MPLCONFIGDIR", str(_MATPLOTLIB_CONFIG_DIR))

from matplotlib.backends.backend_agg import FigureCanvasAgg  # noqa: E402
from matplotlib.figure import Figure  # noqa: E402
from matplotlib.lines import Line2D  # noqa: E402
from matplotlib.patches import Patch, Rectangle  # noqa: E402
from matplotlib.ticker import FuncFormatter  # noqa: E402


@dataclass(frozen=True)
class WeeklyChartPoint:
    start_day: date
    end_day: date
    average_balance_kcal: float | None
    average_weight_kg: float | None


def render_weekly_chart(
    points: list[WeeklyChartPoint],
    reliable_start: date | None = None,
    reliable_end: date | None = None,
) -> bytes:
    if not points:
        raise ValueError("Chart requires at least one weekly point")

    background = "#F7F8FC"
    text = "#25324A"
    muted = "#6B7280"
    grid = "#DDE3EC"
    deficit = "#35A77A"
    surplus = "#E66B5B"
    weight_color = "#3867D6"
    missing = "#C9D0DB"
    reliable = "#F4C95D"

    figure = Figure(figsize=(12.8, 7.2), dpi=150, facecolor=background)
    FigureCanvasAgg(figure)
    axis = figure.add_subplot(111)
    axis.set_facecolor(background)
    x_values = list(range(len(points)))
    has_reliable_period = (
        reliable_start is not None
        and reliable_end is not None
        and reliable_start <= reliable_end
    )
    if has_reliable_period:
        assert reliable_start is not None
        assert reliable_end is not None

        def day_boundary_x(day: date, *, after: bool = False) -> float:
            for index, point in enumerate(points):
                if point.start_day <= day <= point.end_day:
                    period_days = (point.end_day - point.start_day).days + 1
                    day_offset = (day - point.start_day).days + int(after)
                    return index - 0.5 + day_offset / period_days
            return -0.5 if day < points[0].start_day else len(points) - 0.5

        axis.axvspan(
            day_boundary_x(reliable_start),
            day_boundary_x(reliable_end, after=True),
            color=reliable,
            alpha=0.16,
            zorder=0,
        )
    known_balances = [
        point.average_balance_kcal
        for point in points
        if point.average_balance_kcal is not None
    ]
    missing_balance_level = (
        sum(known_balances) / len(known_balances) if known_balances else 0
    )
    balances = [
        0 if point.average_balance_kcal is None else point.average_balance_kcal
        for point in points
    ]
    colors = [
        "none"
        if point.average_balance_kcal is None
        else deficit
        if point.average_balance_kcal < 0
        else surplus
        for point in points
    ]
    bars = axis.bar(
        x_values,
        balances,
        width=0.62,
        color=colors,
        edgecolor="white",
        linewidth=1.2,
        zorder=3,
    )
    axis.margins(y=0.16)
    if not known_balances:
        axis.set_ylim(-100, 100)
    y_min, y_max = axis.get_ylim()
    axis.set_ylim(y_min, y_max)
    placeholder_height = (y_max - y_min) * 0.045
    for index, point in enumerate(points):
        if point.average_balance_kcal is not None:
            continue
        axis.add_patch(
            Rectangle(
                (index - 0.31, missing_balance_level - placeholder_height / 2),
                0.62,
                placeholder_height,
                facecolor=missing,
                edgecolor="#AAB3C2",
                hatch="///",
                linewidth=1,
                zorder=3,
            )
        )
        axis.text(
            index,
            missing_balance_level,
            "НЕМАЄ\nДАНИХ",
            ha="center",
            va="center",
            color=muted,
            fontsize=6.3,
            fontweight="bold",
            zorder=8,
        )

    axis.axhline(0, color="#8792A5", linewidth=1.3, zorder=2)
    axis.grid(axis="y", color=grid, linewidth=0.9, alpha=0.9, zorder=1)
    axis.spines[["top", "right", "left", "bottom"]].set_visible(False)
    axis.tick_params(axis="both", colors=muted, length=0, labelsize=9)
    axis.set_ylabel("Баланс, ккал/день", color=text, fontsize=11, labelpad=12)
    axis.yaxis.set_major_formatter(
        FuncFormatter(lambda value, _position: f"{value:,.0f}".replace(",", " "))
    )
    labels = [f"{point.start_day:%d.%m}\n{point.end_day:%d.%m}" for point in points]
    axis.set_xticks(x_values, labels)
    axis.set_xlabel("Початок і кінець періоду", color=muted, labelpad=12)

    balance_values = [value for value in balances if value != 0]
    balance_span = max((abs(value) for value in balance_values), default=100)
    annotation_offset = max(balance_span * 0.035, 18)
    for bar, point in zip(bars, points, strict=True):
        value = point.average_balance_kcal
        if value is None:
            continue
        axis.text(
            bar.get_x() + bar.get_width() / 2,
            value + (annotation_offset if value >= 0 else -annotation_offset),
            f"{value:+.0f}",
            ha="center",
            va="bottom" if value >= 0 else "top",
            color=text,
            fontsize=8.5,
            fontweight="bold",
        )

    weight_axis = axis.twinx()
    weight_points = [
        (index, point.average_weight_kg)
        for index, point in enumerate(points)
        if point.average_weight_kg is not None
    ]
    weight_axis.plot(
        [index for index, _value in weight_points],
        [value for _index, value in weight_points],
        color=weight_color,
        linewidth=4.2,
        marker="o",
        markersize=9,
        markerfacecolor=background,
        markeredgecolor=weight_color,
        markeredgewidth=3,
        zorder=7,
    )
    weight_axis.spines[["top", "right", "left", "bottom"]].set_visible(False)
    weight_axis.tick_params(axis="y", colors=weight_color, length=0, labelsize=9)
    weight_axis.set_ylabel(
        "Середня вага, кг",
        color=weight_color,
        fontsize=11,
        labelpad=14,
    )
    weight_axis.yaxis.set_major_formatter(
        FuncFormatter(lambda value, _: f"{value:.1f}")
    )
    known_weights = [
        point.average_weight_kg
        for point in points
        if point.average_weight_kg is not None
    ]
    if known_weights:
        low = min(known_weights)
        high = max(known_weights)
        padding = max((high - low) * 0.35, 0.8)
        weight_axis.set_ylim(low - padding, high + padding)
        for index, value in weight_points:
            weight_axis.annotate(
                f"{value:.1f}",
                (index, value),
                xytext=(0, 10),
                textcoords="offset points",
                ha="center",
                color=weight_color,
                fontsize=8.5,
                fontweight="bold",
            )
    else:
        weight_axis.set_yticks([])

    chart_days = (points[-1].end_day - points[0].start_day).days + 1
    figure.suptitle(
        "Баланс калорій і середня вага",
        x=0.075,
        y=0.965,
        ha="left",
        color=text,
        fontsize=20,
        fontweight="bold",
    )
    subtitle = f"Останні {chart_days} завершених днів"
    if has_reliable_period:
        subtitle += " · жовтий фон — період із достатньою кількістю даних"
    axis.set_title(
        subtitle,
        loc="left",
        color=muted,
        fontsize=10.5,
        pad=24,
    )
    legend = [
        Patch(facecolor=deficit, label="Дефіцит"),
        Patch(facecolor=surplus, label="Профіцит"),
        Patch(
            facecolor=missing,
            edgecolor="#AAB3C2",
            hatch="///",
            alpha=0.35,
            label="Немає жодної пари",
        ),
        Line2D(
            [0],
            [0],
            color=weight_color,
            marker="o",
            linewidth=4.2,
            label="Середня вага",
        ),
    ]
    if has_reliable_period:
        legend.append(Patch(facecolor=reliable, alpha=0.3, label="Надійний період"))
    axis.legend(
        handles=legend,
        loc="upper center",
        bbox_to_anchor=(0.5, 1.08),
        ncols=len(legend),
        frameon=False,
        labelcolor=text,
        fontsize=9.5,
    )
    figure.subplots_adjust(left=0.085, right=0.91, top=0.83, bottom=0.16)

    output = BytesIO()
    figure.savefig(
        output,
        format="png",
        dpi=150,
        facecolor=background,
        bbox_inches="tight",
    )
    return output.getvalue()
