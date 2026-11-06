"""Ссылки на страницы базы знаний внутри текста любого модуля.

Единый формат для статей, анонсов и чата — `@[Заголовок](kb:12)`.

Маркер намеренно текстовый, а не HTML-ссылка: одно и то же значение живёт и в
Markdown-строке, и в JSON блоков Editor.js, и в `TEXT` из SQLite, поэтому схему
БД менять не нужно, а клиентский рендер (`kbLinkify` в `app.js`) превращает его
в ссылку `/?view=kb&page=12`.

Серверу остаются три задачи:

* `plain_text` — снять маркеры там, где текст уходит в плейн-текст (push и
  `.ics`-фид): `@[Заголовок](kb:12)` → `Заголовок`;
* `refresh_titles` — при сохранении подтянуть актуальные заголовки страниц
  (переименование статьи обновляет ссылки на неё, как в Notion), а ссылки на
  удалённые страницы деградируют до простого текста;
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

MAX_TITLE = 200


def sanitize_title(value: Any, limit: int = MAX_TITLE) -> str:
    """Чистит заголовок для маркера.

    Убираем `[]()` (они ломают разбор), плюс HTML-спецсимволы: в блоках
    Editor.js текст сохраняется как HTML, и `&`/`<` дали бы двойное
    экранирование. Клиент чистит так же (`kbPageLinkTitle`).
    """
    text_value = re.sub(r"[\[\]()<>&\n\r]+", " ", str(value or ""))
    return " ".join(text_value.split())[:limit]


def page_ids(value: Any) -> set[int]:
    """id страниц, на которые ссылается текст (для проверки и подсчётов)."""
    return {int(match.group(2)) for match in PAGE_LINK_RE.finditer(str(value or ""))}


def plain_text(value: Any) -> str:
    """Маркеры → простой текст: подпись ссылки без служебной разметки."""
    return PAGE_LINK_RE.sub(lambda match: match.group(1), str(value or ""))


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


def refresh_titles(database_engine: Engine, value: Any) -> str:
    """Приводит подписи ссылок к текущим заголовкам страниц.

    Переименовали статью — текст ссылок на неё в других статьях обновляется при
    их следующем сохранении; удалили страницу — ссылка становится обычным
    текстом (без «мёртвого» перехода).
    """
    text_value = str(value or "")
    ids = page_ids(text_value)
    if not ids:
        return text_value
    with database_engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, title FROM kb_pages WHERE id IN :ids").bindparams(
                bindparam("ids", expanding=True)
            ),
            {"ids": sorted(ids)},
        ).all()
    titles = {int(row[0]): str(row[1]) for row in rows}

    def replace(match: re.Match[str]) -> str:
        page_id = int(match.group(2))
        title = titles.get(page_id)
        if title is None:
            return match.group(1)
        return f"@[{sanitize_title(title) or match.group(1)}](kb:{page_id})"

    return PAGE_LINK_RE.sub(replace, text_value)


__all__ = ["PAGE_LINK_RE", "page_ids", "plain_text", "refresh_titles", "sanitize_title", "truncate_safe"]