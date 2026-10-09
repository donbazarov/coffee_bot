"""Снимок графика смен для типа правки «Расписание».

Рисует JPEG-таблицу: месяц и год, колонки «день недели / число», строки —
задействованные баристы, ячейки — время смены (цвет кодирует точку УЯ/ДЕ).
Файлы лежат в ``SNAPSHOTS_DIR`` и раздаются роутом ``/calendar/snapshots/{filename}``.

Модуль намеренно переиспользуемый: тот же рендер понадобится, когда Telegram-бот
начнёт публиковать снимок графика в канал анонсов.
"""

from __future__ import annotations

import os
import uuid
from datetime import date, datetime, timedelta
from functools import lru_cache
from pathlib import Path
from typing import Any

from PIL import Image, ImageDraw, ImageFont
from sqlalchemy import text
from sqlalchemy.engine import Connection, Engine

REPO_ROOT = Path(__file__).resolve().parents[2]

# Каталог со снимками. В Docker это ./data/schedule_snapshots внутри тома,
# поэтому файлы переживают пересборку образа. Можно вынести на диск хостинга.
SNAPSHOTS_DIR = Path(
    os.environ.get("NEFT_SCHEDULE_SNAPSHOTS_DIR") or (REPO_ROOT / "data" / "schedule_snapshots")
).expanduser().resolve()

URL_PREFIX = "/calendar/snapshots/"
EXTENSION = ".jpg"

FONT_DIR = Path(__file__).resolve().parent / "static" / "fonts"
FONT_REGULAR = FONT_DIR / "DejaVuSans.ttf"
FONT_BOLD = FONT_DIR / "DejaVuSans-Bold.ttf"

WEEKDAYS = ["ПН", "ВТ", "СР", "ЧТ", "ПТ", "СБ", "ВС"]
MONTHS = ["Январь", "Февраль", "Март", "Апрель", "Май", "Июнь",
          "Июль", "Август", "Сентябрь", "Октябрь", "Ноябрь", "Декабрь"]

# Палитра согласована с интерфейсом сайта.
BG = (16, 17, 15)
PANEL = (32, 33, 29)
PANEL_WEEKEND = (24, 25, 21)
LINE = (58, 59, 52)
TEXT = (222, 219, 210)
MUTED = (150, 150, 140)
UY_FILL = (150, 45, 40)
DE_FILL = (40, 90, 170)
WHITE = (255, 255, 255)

# Публикация «Расписания» режется на снимки не длиннее этого окна: диапазон
# 01–15 превращается в два снимка (01–08 и 09–15), чтобы таблица читалась на телефоне.
SNAPSHOT_CHUNK_DAYS = 8

BASE_W = 1280
BASE_H = 720
NAME_W = 250
PAD = 16
TITLE_H = 46
DAY_HEAD_H = 56
FOOTER_H = 34
MIN_DAY_W = 56
MAX_DAY_W = 132
MIN_ROW_H = 40
MAX_ROW_H = 56


@lru_cache(maxsize=32)
def _font(bold: bool, size: int) -> ImageFont.FreeTypeFont:
    path = FONT_BOLD if bold else FONT_REGULAR
    try:
        return ImageFont.truetype(str(path), size)
    except OSError:
        # Запасной вариант, если шрифты не попали в образ: текст может быть без кириллицы.
        return ImageFont.load_default()


def _as_date(value: Any) -> date:
    return value if isinstance(value, date) else date.fromisoformat(str(value)[:10])


def _time_text(value: Any) -> str:
    return str(value or "")[:5]


def _fit_text(draw: ImageDraw.ImageDraw, value: str, font: ImageFont.FreeTypeFont, max_width: int) -> str:
    if draw.textlength(value, font=font) <= max_width:
        return value
    trimmed = value
    while trimmed and draw.textlength(trimmed + "…", font=font) > max_width:
        trimmed = trimmed[:-1]
    return (trimmed + "…") if trimmed else ""


def active_employee_ids(connection: Connection) -> list[int]:
    """Все активные сотрудники графика — тот же состав, что и в таблице на сайте."""
    rows = connection.execute(text("""
        SELECT id FROM users
        WHERE is_active = 1 AND role IN ('barista', 'senior', 'mentor')
        ORDER BY COALESCE(display_name, name) COLLATE NOCASE
    """)).scalars().all()
    return [int(row) for row in rows]


def split_period(start_date: Any, end_date: Any, max_days: int = SNAPSHOT_CHUNK_DAYS) -> list[tuple[date, date]]:
    """Режет диапазон на последовательные окна длиной не больше ``max_days``.

    Окна выравниваются по длине, а не «по 8 плюс огрызок»: 01–15 → 01–08 и 09–15.
    """
    start, end = _as_date(start_date), _as_date(end_date)
    if end < start:
        start, end = end, start
    total = (end - start).days + 1
    chunks = -(-total // max(1, max_days))  # округление вверх
    base, extra = divmod(total, chunks)
    windows: list[tuple[date, date]] = []
    cursor = start
    for index in range(chunks):
        window_end = cursor + timedelta(days=base + (1 if index < extra else 0) - 1)
        windows.append((cursor, window_end))
        cursor = window_end + timedelta(days=1)
    return windows


def _load_employees(connection: Connection, user_ids: list[int], params: dict[str, Any]) -> list[Any]:
    placeholders = ", ".join(f":u{i}" for i in range(len(user_ids)))
    rows = connection.execute(text(f"""
        SELECT id, COALESCE(display_name, name) AS name FROM users WHERE id IN ({placeholders})
        ORDER BY COALESCE(display_name, name) COLLATE NOCASE
    """), params).mappings().all()
    return list(rows)


def _load_shifts(connection: Connection, user_ids: list[int], start: date, end: date) -> dict[tuple[int, str], dict[str, str]]:
    if not user_ids:
        return {}
    placeholders = ", ".join(f":u{i}" for i in range(len(user_ids)))
    params: dict[str, Any] = {f"u{i}": user_id for i, user_id in enumerate(user_ids)}
    params.update({"start": start.isoformat(), "end": end.isoformat()})
    rows = connection.execute(text(f"""
        SELECT u.id AS user_id, s.shift_date, st.start_time, st.end_time, st.point
        FROM users AS u
        LEFT JOIN schedule AS s
               ON CAST(u.iiko_id AS TEXT) = s.iiko_id
              AND s.shift_date >= :start AND s.shift_date <= :end
              AND COALESCE(s.is_active, 1) = 1
        LEFT JOIN shift_types AS st ON st.id = s.shift_type_id
        WHERE u.id IN ({placeholders})
        ORDER BY s.shift_date, st.start_time
    """), params).mappings().all()
    shifts: dict[tuple[int, str], dict[str, str]] = {}
    for row in rows:
        if row["shift_date"] is None:
            continue
        key = (row["user_id"], str(row["shift_date"])[:10])
        # При нескольких сменах в один день в снимке показываем первую.
        shifts.setdefault(key, {
            "start": _time_text(row["start_time"]),
            "end": _time_text(row["end_time"]),
            "point": row["point"] or "",
        })
    return shifts


def render_schedule_snapshot(connection: Connection, user_ids: list[int], start_date: Any, end_date: Any) -> str | None:
    """Рисует снимок и возвращает URL файла. ``None`` — если баристов нет."""
    start, end = _as_date(start_date), _as_date(end_date)
    if end < start:
        start, end = end, start

    user_ids = sorted({int(user_id) for user_id in user_ids})
    params: dict[str, Any] = {f"u{i}": user_id for i, user_id in enumerate(user_ids)}
    employees = _load_employees(connection, user_ids, params)
    if not employees:
        return None

    days = [start + timedelta(days=offset) for offset in range((end - start).days + 1)]
    shifts = _load_shifts(connection, user_ids, start, end)

    content_w = BASE_W - NAME_W - PAD * 2
    day_w = max(MIN_DAY_W, min(MAX_DAY_W, content_w // max(1, len(days))))
    width = PAD * 2 + NAME_W + day_w * len(days)
    body_budget = BASE_H - PAD * 2 - TITLE_H - DAY_HEAD_H - FOOTER_H
    row_h = max(MIN_ROW_H, min(MAX_ROW_H, body_budget // max(1, len(employees))))
    height = PAD * 2 + TITLE_H + DAY_HEAD_H + row_h * len(employees) + FOOTER_H

    image = Image.new("RGB", (width, height), BG)
    draw = ImageDraw.Draw(image)

    grid_left = PAD + NAME_W
    grid_top = PAD + TITLE_H

    title_font = _font(True, 26)
    subtitle_font = _font(False, 15)
    weekday_font = _font(False, 15)
    number_font = _font(True, 22)
    name_font = _font(False, 18)
    time_font = _font(True, 18)
    end_font = _font(False, 14)
    legend_font = _font(False, 14)

    month_label = f"{MONTHS[start.month - 1]} {start.year}"
    if (start.month, start.year) != (end.month, end.year):
        month_label = f"{MONTHS[start.month - 1]} {start.year} – {MONTHS[end.month - 1]} {end.year}"
    period_label = f"{start.day:02d}.{start.month:02d} – {end.day:02d}.{end.month:02d} · дней: {len(days)}"
    draw.text((PAD, PAD + 4), month_label, font=title_font, fill=TEXT)
    title_width = draw.textlength(month_label, font=title_font)
    draw.text((PAD + title_width + 14, PAD + 14), period_label, font=subtitle_font, fill=MUTED)

    # Шапка: колонки дней недели и чисел.
    head_top = grid_top
    head_bottom = grid_top + DAY_HEAD_H
    draw.rectangle([PAD, head_top, grid_left, head_bottom], fill=PANEL)
    draw.text((PAD + 14, (head_top + head_bottom) / 2), "БАРИСТА", font=weekday_font, fill=MUTED, anchor="lm")
    for index, day in enumerate(days):
        left = grid_left + index * day_w
        weekend = day.weekday() >= 5
        draw.rectangle([left, head_top, left + day_w, head_bottom], fill=PANEL_WEEKEND if weekend else PANEL)
        center = left + day_w / 2
        draw.text((center, head_top + 18), WEEKDAYS[day.weekday()], font=weekday_font, fill=MUTED, anchor="mm")
        draw.text((center, head_top + 39), str(day.day), font=number_font, fill=TEXT, anchor="mm")

    # Строки баристов.
    for row_index, employee in enumerate(employees):
        top = head_bottom + row_index * row_h
        bottom = top + row_h
        draw.rectangle([PAD, top, grid_left, bottom], fill=PANEL if row_index % 2 == 0 else BG)
        name = _fit_text(draw, str(employee["name"]), name_font, NAME_W - 28)
        draw.text((PAD + 14, (top + bottom) / 2), name, font=name_font, fill=TEXT, anchor="lm")
        for day_index, day in enumerate(days):
            left = grid_left + day_index * day_w
            shift = shifts.get((employee["id"], day.isoformat()))
            cell = [left + 1, top + 1, left + day_w - 1, bottom - 1]
            if not shift:
                weekend = day.weekday() >= 5
                draw.rectangle(cell, fill=PANEL_WEEKEND if weekend else BG, outline=LINE)
                continue
            fill = UY_FILL if shift["point"] == "УЯ" else DE_FILL
            draw.rectangle(cell, fill=fill)
            center = left + day_w / 2
            draw.text((center, top + row_h * 0.42), shift["start"], font=time_font, fill=WHITE, anchor="mm")
            if shift["end"]:
                draw.text((center, top + row_h * 0.72), shift["end"], font=end_font, fill=(235, 235, 230), anchor="mm")

    # Сетка и подписи.
    for index in range(len(days) + 1):
        x = grid_left + index * day_w
        draw.line([x, head_top, x, head_bottom + row_h * len(employees)], fill=LINE)
    for index in range(len(employees) + 1):
        y = head_bottom + index * row_h
        draw.line([PAD, y, width - PAD, y], fill=LINE)
    draw.line([grid_left, head_top, grid_left, head_bottom + row_h * len(employees)], fill=LINE)

    # Легенда и время генерации.
    footer_y = head_bottom + row_h * len(employees) + FOOTER_H / 2
    swatch = 14
    draw.rectangle([PAD, footer_y - swatch / 2, PAD + swatch, footer_y + swatch / 2], fill=UY_FILL)
    draw.text((PAD + swatch + 7, footer_y), "УЯ", font=legend_font, fill=MUTED, anchor="lm")
    draw.rectangle([PAD + 70, footer_y - swatch / 2, PAD + 70 + swatch, footer_y + swatch / 2], fill=DE_FILL)
    draw.text((PAD + 70 + swatch + 7, footer_y), "ДЕ", font=legend_font, fill=MUTED, anchor="lm")
    stamp = datetime.now().strftime("%d.%m.%Y %H:%M")
    draw.text((width - PAD, footer_y), f"НЕФТЬ · сформировано {stamp}", font=legend_font, fill=MUTED, anchor="rm")

    SNAPSHOTS_DIR.mkdir(parents=True, exist_ok=True)
    filename = f"{datetime.now():%Y%m%d}-{datetime.now():%H%M%S}-{uuid.uuid4().hex[:10]}{EXTENSION}"
    image.save(SNAPSHOTS_DIR / filename, "JPEG", quality=88, optimize=True)
    return f"{URL_PREFIX}{filename}"


def render_schedule_snapshots(connection: Connection, user_ids: list[int], start_date: Any, end_date: Any,
                              max_days: int = SNAPSHOT_CHUNK_DAYS) -> list[dict[str, str]]:
    """Снимки на весь диапазон: по одному файлу на каждое окно до ``max_days`` дней."""
    snapshots: list[dict[str, str]] = []
    for window_start, window_end in split_period(start_date, end_date, max_days):
        url = render_schedule_snapshot(connection, user_ids, window_start, window_end)
        if url:
            snapshots.append({"url": url, "start": window_start.isoformat(), "end": window_end.isoformat()})
    return snapshots


def read_snapshot(filename: str) -> Path | None:
    """Безопасно достаёт файл снимка из каталога. Защита от path traversal."""
    safe = os.path.basename(filename)
    if safe != filename or not safe.lower().endswith(EXTENSION):
        return None
    target = (SNAPSHOTS_DIR / safe).resolve()
    try:
        target.relative_to(SNAPSHOTS_DIR)
    except ValueError:
        return None
    return target if target.is_file() else None


def prune_snapshots(engine: Engine, keep_days: int = 35) -> dict[str, int]:
    """Чистит старые снимки (по умолчанию держим текущий и прошлый месяц).

    Файлы удаляются, а ссылка в логе обнуляется — история правок остаётся,
    но без мёртвых ссылок на несуществующие картинки.
    """
    removed_files = 0
    if SNAPSHOTS_DIR.is_dir():
        cutoff_ts = datetime.now().timestamp() - keep_days * 86400
        for item in SNAPSHOTS_DIR.glob(f"*{EXTENSION}"):
            try:
                if item.stat().st_mtime < cutoff_ts:
                    item.unlink(missing_ok=True)
                    removed_files += 1
            except OSError:
                continue

    cutoff_date = (datetime.now().date() - timedelta(days=keep_days)).isoformat()
    with engine.begin() as connection:
        cleared = connection.execute(text("""
            UPDATE web_shift_change_log SET snapshot_path = NULL, snapshot_paths = NULL
            WHERE (snapshot_path IS NOT NULL OR snapshot_paths IS NOT NULL) AND period_end IS NOT NULL
              AND date(period_end) < date(:cutoff)
        """), {"cutoff": cutoff_date}).rowcount
    return {"removed_files": removed_files, "cleared_links": cleared}


__all__ = [
    "SNAPSHOTS_DIR",
    "SNAPSHOT_CHUNK_DAYS",
    "URL_PREFIX",
    "active_employee_ids",
    "prune_snapshots",
    "read_snapshot",
    "render_schedule_snapshot",
    "render_schedule_snapshots",
    "split_period",
]
