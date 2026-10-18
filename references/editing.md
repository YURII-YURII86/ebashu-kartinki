# Правки, откат, чужие презентации

## Поменять что-то на слайде
1. `$PY scripts/generate_ppt.py --list-sessions` → найди сессию.
2. Прочитай `outputs/<ts>/metadata.json` → найди слайд и id элемента.
3. Пользователь не сказал, на что менять → спроси: «сейчас тут X, на что заменить?»
4. Запусти правку:
```bash
$PY scripts/generate_ppt.py --session <ts> --edit 3 \
  --element-updates '{"subtitle": {"content": "Новый подзаголовок"}}' \
  --edit-instruction "Поменяй только подзаголовок. Макет, цвета и остальной текст не трогай."
```
Сильнее держать исходный вид: `--edit-prompt "Возьми исходник за основу, поменяй только …"`.
5. Посмотри новую PNG через Read. `.pptx` пересобирается сам.

## Откат
```bash
$PY scripts/generate_ppt.py --session <ts> --rollback 3 --to-version 1
```

## Чужая .pptx
```bash
$PY scripts/generate_ppt.py --ingest-pptx deck.pptx [--session my_deck]
```
После импорта посмотри слайды и заполни `slide_spec`. До этого не обещай точечных правок отдельных элементов.

## Редактируемый PPTX
Включать только если пользователь прямо просит редактируемый текст или отдельные элементы. По умолчанию выключено.

Принцип: сначала генерируется полноценный слайд, потом он разбирается на нативные объекты PowerPoint: `native_text`, `native_shape`, `connector`, `image_layer`, плюс чистый фон (`clean-plate`).

1. `$PY scripts/render_template.py --check` — без рендера готовый файл нечем проверить, тогда редактируемый режим не запускать.
2. Обычный план → пробный слайд.
3. Для каждого слайда: `editable_scenes/slide-XX.scene.json` (`slide_number`, `canvas`, `clean_plate`, `elements` по `z_index`), чистый фон, слои.
4. Запуск:
```bash
$PY scripts/generate_ppt.py --plan slides_plan.json --style styles/base/dark-aurora.md \
  --editable --editable-scenes editable_scenes/
```
5. Просмотри **каждую** страницу `outputs/<ts>/editable_renders/page-XX.png`: шрифты, переносы, сдвиги, края. Автоотчёт ручную проверку не заменяет.

Слои извлекаются по нарастающей: A1 — вырезать пиксели как есть → A2 — вырезать и дорисовать закрытое → B — сгенерировать элемент отдельно (только если A1 и A2 не справились: волосы, стекло, полупрозрачность). Каждый переход фиксируется в `quality-report.json`. Если сцена сломана, нельзя молча выдавать картинку за редактируемый слайд: нужно честно сообщить об ошибке.
