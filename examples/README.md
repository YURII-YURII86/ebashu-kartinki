# Сценарии презентаций

Стартовые структуры, когда пользователь назвал жанр, но не дал план. Это **каркас**: замени тему, факты и цифры на данные пользователя. Вымышленные цифры в итоговую презентацию не переносить.

| Сценарий | Папка | Стиль |
|---|---|---|
| Запуск продукта | `examples/product-launch/slides_plan.md` | `styles/base/gradient-glass.md` |
| Инвест-питч | `examples/investor-pitch/slides_plan.md` | `styles/base/clean-tech-blue.md` |
| Еженедельный отчёт | `examples/weekly-report/slides_plan.md` | `styles/niche/meeting-agenda.md` |
| Урок / обучение | `examples/courseware/slides_plan.md` | `styles/base/vector-illustration.md` |
| Защита работы | `examples/thesis-defense/slides_plan.md` | `styles/niche/final-year-project-thesis-defense.md` |
| Разбор книги / выступление | `examples/book-sharing/slides_plan.md` | `styles/base/editorial-mono.md` |

Каждый план сразу проверяется так:
```bash
python3 scripts/md_to_plan.py examples/product-launch/slides_plan.md -o /tmp/plan.json
```
