# Кнопки v1

Все кнопки приложения живут в одном файле `app/static/buttons.css`. Он подключён последним в `index.html`. Админка сюда не входит.

Старые файлы стилей отвечают только за раскладку (display, width, margin, position, order). Высоту, радиус, шрифт, цвета, hover, active, focus и disabled задаёт `buttons.css`. Правила там идут с `!important`, чтобы старое правило с id не вернуло прежний вид.

## Роли

| Класс | Вид | Когда брать |
| --- | --- | --- |
| кнопка без класса или `.primary` | залитая зелёная | главное действие на экране или в диалоге |
| `.secondary` | светлая заливка | второе по важности действие |
| `.quiet` | контур без заливки | отмена, возврат, редкое действие |
| `.danger` | красно-коричневая заливка | удаление и другие необратимые шаги |
| `.text-button` | как ссылка | действие внутри текста или строки |
| `.icon-button` | квадрат с иконкой | действие без подписи, нужен `aria-label` |

Ссылки, похожие на кнопки, берут те же классы: `a.primary`, `a.secondary`, `a.quiet`. Для лендинга остались `a.cta-primary`, `a.cta-quiet`, `a.cta-channel`, они вписаны в ту же систему.

## Размеры

| Размер | Класс | Десктоп | Телефон (до 640px) | Шрифт |
| --- | --- | --- | --- | --- |
| малый | `.small` | 32px | 36px | 13px |
| обычный | без класса | 40px | 44px | 14px |
| большой | `.large` | 48px | 48px | 15px |

Радиус один, 10px. Вес один, 600. Межстрочный 1.25. Горизонтальные отступы 12, 16 и 20px.

Токены лежат в `:root` файла `buttons.css`, имена `--btn-h-sm`, `--btn-h`, `--btn-h-lg`, `--btn-radius`, `--btn-weight`, `--btn-fs-*`, `--btn-px-*`.

## Состояния

- hover темнит заливку или добавляет лёгкий фон у тихих кнопок.
- active сдвигает кнопку на 1px вниз.
- disabled даёт прозрачность 0.5 и курсор `not-allowed`.
- focus-visible один для всех: контур 2px цвета `--btn-ring`, отступ 2px. Внутри `.guide-bubble` контур белый, потому что фон там тёмный.

## Контраст

| Пара | Контраст |
| --- | --- |
| белый на `.primary` (#345943) | 7.9 |
| текст на `.secondary` (#264431 на #eaf0e6) | 9.3 |
| текст `.quiet` (#2d4d3a) на странице | 8.5 и выше |
| белый на `.danger` (#8c3b2b) | 7.6 |
| контур фокуса (#2f6b46) на странице | 5.8 и выше |

Требование было 4.5 для текста и 3 для контура фокуса. Запас везде есть.

## Старые имена

Эти классы получают роль и размер автоматически, переписывать разметку не нужно.

- Иконки (квадрат): `.tool-btn`, `.pop-close`, `.md-tool`, `.search-clear`, `.category-add`, `.category-more`, `.nav-more`, `.menu-button`, `.item-trash`, `.tk-trash`, `.item-remind`, кнопка закрытия в `.dialog-heading`.
- Таблетки с выбором: `.main-nav-item`, `.category-button`, `.source-filter button`, `.kind-toggle button`, `.view-switch button`, `.note-tabs button`, `.br-seg button`. Выбранная (`aria-pressed=true` или `aria-current`) заливается как primary.
- Строки меню: `.nav-action`, `.menu-item`, `.category-menu-item`, `.na-item`.
- Текстовые: `.reminder-link`, `.text-link`, `.tk-note`, `.cookie-settings-link`.
- Малые: `.reminder-small`, `.br-mini`, `.note-reload`, `.trash-restore`, `.trash-forget`, а также кнопки внутри `.structure-check`, `.timezone-banner`, `.notice-bar`, `.item`, `.category-row`.
- `.cookie-choice` работает как `.quiet`.

## Для агента A (вход и пароли)

- Кнопка отправки формы входа без класса или `.primary`. Нужен размер крупнее, добавьте `.large`.
- Второе действие `.secondary` или `.quiet`.
- Кнопка показа пароля `.icon-button` с `aria-label`.
- Не задавайте в своих файлах `height`, `min-height`, `border-radius`, `font-size`, `font-weight` и цвета кнопок. Если нужно, правьте `buttons.css` и согласуйте это.
- Ширину на всю строку задавайте своим правилом `width:100%` или `.actions`, это раскладка.

## Проверка

`tests/web_mobile_capture.cjs` проверяет, что `buttons.css` подключён последним и в нём есть токены шкалы и единый фокус. Внешний вид проверялся скриншотами на 1280, 390 и 360px.
