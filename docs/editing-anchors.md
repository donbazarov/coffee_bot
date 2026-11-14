# Составные якоря правок

Зачем: точечные правки по одному закрывающему тегу ломаются — в `index.html`
`</aside>` встречается четыре раза, `</section>` — 43, `</dialog>` — 15. Один и тот же
текст как якорь даёт неоднозначное совпадение, и правка попадает не в тот блок.

Правило: **якорь = 2–3 строки**, среди которых есть строка с уникальным признаком
(`id`, класс, Jinja-комментарий). Двухстрочный якорь `            </div></div>` +
`          </aside>` — НЕ уникален (встречается и у «Оглавления» БЗ, и у списка
модулей). Ниже — только проверенные якоря (каждый даёт ровно одно совпадение).

## `bot/web/templates/index.html` (единственный редактируемый SPA-шаблон)

| Файл | Якорь (3 строки, см. блоки ниже) | Что это за блок | Маркер для grep-проверки |
| --- | --- | --- | --- |
| `bot/web/templates/index.html` | стр. 337–339 | Закрытие левой колонки «Оглавление» базы знаний (`<aside class="panel settings-panel kb-tree-panel">`, открывается на стр. 333) | `id="kb-tree-status"` |
| `bot/web/templates/index.html` | стр. 360 | Список чатов экрана «Чат» — `<aside class="chat-list" id="chat-list">` целиком в одной строке, якорь уже уникален | `id="chat-list"` |
| `bot/web/templates/index.html` | стр. 555–557 | Закрытие левой колонки «Модули сайта» в «Панели управления» (`<aside class="panel settings-panel control-nav-panel">`, открывается на стр. 542) | `id="control-nav-status"` |
| `bot/web/templates/index.html` | стр. 786–788 | Закрытие главного меню-шторки `<aside class="drawer" id="drawer">` (открывается на стр. 761) | `id="drawer-logout"` |

Anchor 1 — «Оглавление» базы знаний (стр. 337–339):

```
              <p class="kb-tree-status" id="kb-tree-status" role="status"></p>
            </div></div>
          </aside>
```

Anchor 2 — список чатов (стр. 360, одна строка):

```
          <aside class="chat-list" id="chat-list" aria-label="Список чатов"></aside>
```

Anchor 3 — «Модули сайта» в панели управления (стр. 555–557):

```
              <p class="control-nav-status" id="control-nav-status" role="status"></p>
            </div></div>
          </aside>
```

Anchor 4 — меню-шторка `drawer` (стр. 786–788):

```
      <button class="button button-quiet drawer-logout" type="button" id="drawer-logout">Выйти</button>
    </div>
  </aside>
```

Для остальных повторяющихся закрывающих тегов (`</section>` — 43, `</dialog>` — 15,
`</article>` — 4, `</nav>` — 4, `</main>` — 2) правило то же: в якорь обязательно
попадает строка с `id`/классом блока или Jinja-комментарий рядом (например, перед
`</aside>` шторки стоит комментарий `{# Детальная оценка для бариста ... #}`).

## `.stitch/crewdesk.html` — НЕ редактировать

Это экспорт дизайна (Stitch) и копия шаблона; каталог `.stitch/` в `.gitignore`.
В файле те же четыре закрывающих `</aside>` (стр. 362, 587, 1155, плюс инлайновый
на стр. 383), поэтому правки по «голому» `</aside>` легко уходят не в тот файл.
Источник истины — `bot/web/templates/index.html`.

## Как проверить, что якорь уникален

```powershell
cd d:\coffee_bot
# 1) маркер внутри блока ровно один раз
foreach ($m in 'id="kb-tree-status"','id="chat-list"','id="control-nav-status"','id="drawer-logout"') {
    "$m -> " + (Select-String -Path 'bot\web\templates\index.html' -Pattern $m -SimpleMatch).Count
}
# 2) каждый 3-строчный якорь ровно один раз
python -c "import pathlib;t=pathlib.Path('bot/web/templates/index.html').read_text(encoding='utf-8');L=t.splitlines();print([ (s,e,t.count(chr(10).join(L[s-1:e])) ) for s,e in [(337,339),(360,360),(555,557),(786,788)] ])"
```

Ожидаемо: все счётчики равны `1`.
