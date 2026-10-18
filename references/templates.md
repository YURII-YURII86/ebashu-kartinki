# Клонирование .pptx-шаблона

Пользователь дал свой шаблон → новые слайды повторяют его стиль и сетку с новым содержимым.

## 1. Проверить, чем рендерить .pptx в картинки
```bash
$PY scripts/render_template.py --check
```
macOS: Keynote → LibreOffice. Windows: PowerPoint → LibreOffice. Linux: LibreOffice.
Ничего нет → предложи `brew install --cask libreoffice` или попроси пользователя экспортировать страницы шаблона в `page-01.png`, `page-02.png`… и передай папку через `--template-images`.

## 2. Отрендерить и выбрать страницы
```bash
$PY scripts/render_template.py company.pptx      # → template_renders/company/page-NN.png
```
Шаблон больше 15 страниц → отбери 8–12 самых разных в `template_renders/company_curated/`.

## 3. Профиль шаблона пишешь ты сам
Claude видит картинки, внешний vision-сервис не нужен. Посмотри страницы через Read и запиши `template_profile.json` по схеме `TemplateProfile` из `scripts/template_analyzer.py`. Для каждого layout укажи:
`id`, `page_type`, `summary`, `visual_signature`, `content_capacity`, `best_for` / `avoid_for`, `reuse_friendly`, `reference_image` (абсолютный путь к PNG страницы).

## 4. Пробный слайд
```bash
$PY scripts/generate_ppt.py --plan slides_plan.json --template-profile template_profile.json --template-strict --slides 1
```
`--template-strict` отправляет страницу шаблона как референс для каждого слайда, так сходство максимальное. Конкретную страницу шаблона для слайда задают в плане: `## 3. [content, layout=layout-05] Заголовок`.

## Правило «1 слайд — 1 layout»
Не повторяй уникальные страницы шаблона (обложку, иллюстрации со сценой): зритель это заметит. Повторять можно только нейтральные страницы (текст, сетки карточек), и то лучше чередовать. Перед генерацией скрипт сам предупреждает о повторах: `(!)` нужно исправить, `(i)` по ситуации.

Временные папки `template_renders/`, `template_cache/` и `outputs/` создаются в текущей директории.
