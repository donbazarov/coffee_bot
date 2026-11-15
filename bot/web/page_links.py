"""Ссылки на страницы базы знаний внутри текста любого модуля.

Единый формат для статей, анонсов и чата — `@[Заголовок](kb:12)`.

Маркер намеренно текстовый, а не HTML-ссылка: одно и то же значение живёт и в
Markdown-строке, и в JSON блоков Editor.js, и в `TEXT` из SQLite, поэтому схему
БД менять не нужно, а клиентский рендер (`kbLinkify` в `app.js`) превращает его
в ссылку `/?view=kb&page=12`.

Подпись ссылки — свободный текст автора: `@[статье о чае](kb:3)` ведёт на
страницу 3, как бы она ни называлась. Поэтому при сохранении подпись НЕ
переписывается — иначе авторская формулировка терялась бы при публикации.

Серверу остаются три задачи:

* `plain_text` — снять маркеры там, где текст уходит в плейн-текст (push и
  `.ics`-фид): `@[Заголовок](kb:12)` → `Заголовок`;
* `mention` — собрать маркер там, где ссылка не вводится руками (имя и id
  приходят из БД), почистив подпись под формат маркера;
* `repair_links` — ссылки на удалённые страницы деградируют до простого текста
  (без «мёртвого» перехода); живые ссылки остаются как есть;
* `truncate_safe` — обрезка по лимиту, которая не рвёт маркер пополам.

Модуль изолирован, как `modules.py`: только регулярки и чтение `kb_pages`.
"""

from __future__ import annotations

import re
from typing import Any

from sqlalchemy import bindparam, text
from sqlalchemy.engine import Engine

# `@[Заголовок](kb:12)`. Заголовок без `]` и переводов строк — так регэкспе
# достаточно, а вставка на клиенте чистит недопустимые символы заранее.
PAGE_LINK_RE = re.compile(r"@\[([^\]\n]{1,200})\]\(kb:(\d{1,12})\)")


def page_ids(value: Any) -> set[int]:
    """id страниц, на которые ссылается текст (для проверки и подсчётов)."""
    return {int(match.group(2)) for match in PAGE_LINK_RE.finditer(str(value or ""))}


def plain_text(value: Any) -> str:
    """Маркеры → простой текст: подпись ссылки без служебной разметки."""
    return PAGE_LINK_RE.sub(lambda match: match.group(1), str(value or ""))


def mention(page_id: Any, title: Any) -> str:
    """Маркер ссылки на страницу из данных БД: `@[Заголовок](kb:12)`.

    Нужен там, где подпись не пишет человек, а берётся из записи (например,
    авто-задача ТО ссылается на запчасть, карточкой которой владеет страница
    БЗ). Подпись чистим под `PAGE_LINK_RE`: `]` и переводы строк ломают маркер,
    а слишком длинное имя не переживёт разбор на клиенте.
    """
    number = int(page_id or 0)
    if not number:
        return str(title or "")
    label = " ".join(str(title or "").replace("]", " ").split())[:120]
    return f"@[{label or 'Страница'}](kb:{number})"


def truncate_safe(value: Any, limit: int) -> str:
    """Обрезка по лимиту без разрыва маркера (иначе на клиенте останется мусор)."""
    text_value = str(value or "")
    if len(text_value) <= limit:
        return text_value
    cut = text_value[:limit]
    start = cut.rfind("@[")
    if start != -1 and PAGE_LINK_RE.match(cut, start) is None:
        cut = cut[:start]
    return cut


def repair_links(database_engine: Engine, value: Any) -> str:
    """Снимает переходы на удалённые страницы, сохраняя подпись автора.

    Подписи существующих ссылок не трогаем: `@[статье о чае](kb:3)` — это
    формулировка автора, и после сохранения она должна остаться такой, какой её
    написали, даже если сама страница называется иначе. Переход всё равно по id.
    """
    text_value = str(value or "")
    ids = page_ids(text_value)
    if not ids:
        return text_value
    with database_engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id FROM kb_pages WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": sorted(ids)},
        ).all()
    alive = {int(row[0]) for row in rows}

    def replace(match: re.Match[str]) -> str:
        # Страница удалена — оставляем только текст подписи, ссылка не нужна.
        return match.group(0) if int(match.group(2)) in alive else match.group(1)

    return PAGE_LINK_RE.sub(replace, text_value)


__all__ = ["PAGE_LINK_RE", "mention", "page_ids", "plain_text", "repair_links", "truncate_safe"]