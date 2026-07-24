# Как этим пользоваться в VS Code

## 1. Live-летопись

В одном терминале запусти:

```powershell
.\scripts\start-live.ps1
```

Он будет обновлять:

```text
outputs/live-chronicle.md
```

Остановить можно через `Ctrl+C`.

Если хочешь, чтобы после остановки сессии отчет сам пришел в Telegram, запускай так:

```powershell
.\scripts\start-live-session.ps1
```

Когда закончишь работу, нажми `Ctrl+C`. Агент обновит live-летопись и отправит тебе полный пакет сессии: live-файл, дневной content pack, JSON и сегодняшние заметки/логи.

Чтобы после остановки присылался только live-файл или только дневной content pack:

```powershell
.\scripts\start-live-session.ps1 -SendKind live
.\scripts\start-live-session.ps1 -SendKind daily
```

## 2. Открыть летопись как документ

Во втором терминале:

```powershell
.\scripts\open-live.ps1
```

Или руками открой в Explorer:

```text
outputs/live-chronicle.md
```

Именно этот файл надо читать. Терминал с `watch` — это просто моторчик, не интерфейс.

## 3. Добавлять важные события из терминала

Когда произошло что-то интересное:

```powershell
.\scripts\add-terminal-note.ps1 "pytest упал, privacy scanner поймал внутренний URL"
```

Это попадет в:

```text
terminal-logs/YYYY-MM-DD.md
```

И появится в live-летописи при следующем обновлении.

## 4. Писать мысли руками

Для человеческого контекста пиши короткие заметки в:

```text
content-notes/YYYY-MM-DD.md
```

Например:

```markdown
# Что сегодня понял

- Агент должен не просто собирать diff, а искать сюжет.
- Самая сильная идея дня: AI может быть редактором моей работы.
```

## 5. Сделать финальный content pack за день

В конце дня:

```powershell
.\scripts\make-daily.ps1
```

Результат:

```text
outputs/YYYY-MM-DD-content-pack.md
outputs/YYYY-MM-DD-content-pack.json
```

Если не хочешь разбирать весь пакет, попроси агента выбрать за тебя:

```powershell
.\scripts\make-pick.ps1
```

Он создаст:

```text
outputs/YYYY-MM-DD-today-pick.md
```

Это главный файл для публикации: один выбранный формат, готовый пост, сторис на 3 кадра и чеклист.

## 6. Отправить отчет в Telegram

Сначала создай `.env` по примеру `.env.example`, затем:

```powershell
.\scripts\send-daily-telegram.ps1
```

Отправить live-летопись:

```powershell
.\scripts\send-live-telegram.ps1
```

Отправить полный пакет текущей сессии:

```powershell
.\scripts\send-session-telegram.ps1
```

Отправить только выбор редактора:

```powershell
.\scripts\send-pick-telegram.ps1
```

Отправить выбор редактора в локальный редакторский backend, получить редактуру и переслать себе:

```powershell
.\scripts\send-nazai-edit.ps1
```

## 7. Передача истории чатов в Naz_Ai_Bot

Передать очищенные пользовательские чаты текущего дня в inbox-папку локального `Naz_Ai_Bot`:

```powershell
.\scripts\export-nazai-inbox.ps1
```

Передать сводки сразу локально и на VPS:

```powershell
.\scripts\export-nazai-vps.ps1
```

Передать весь существующий архив пользовательских чатов в inbox:

```powershell
.\scripts\export-nazai-all.ps1
```

Передать весь существующий архив агента сразу на VPS:

```powershell
.\scripts\export-nazai-all-vps.ps1
```

Цепочка:

```text
Codex user chats -> проект/дата/тема -> читабельный рассказ -> Naz_Ai_Bot -> точный выбор темы
```

Inbox содержит только UTF-8 Markdown: `проект\дата\тематический-рассказ.md`, без ролевых логов, JSON, изображений, manifest, topic-index и служебных README. Полные очищенные реплики без потерь и дублей остаются в `ai-logs`; в Naz передаются задача, ход работы и подтверждённый итог. Приветствия, опечатки и пустые подтверждения не засоряют inbox. Пустые даты не экспортируются.

Пример точечного выбора в Naz:

```text
/agent_content 2026-07-09 project:Naz-AI_Bot topic:контур публикации в VK
```

Если тема не найдена однозначно, Naz останавливается без случайного fallback.

## Мини-режим на каждый день

1. Утром: `.\scripts\start-live.ps1`
2. Открыть: `.\scripts\open-live.ps1`
3. По ходу дня: `.\scripts\add-terminal-note.ps1 "что случилось"`
4. Иногда дописывать мысли в `content-notes/YYYY-MM-DD.md`
5. Вечером: `.\scripts\make-daily.ps1`
6. Отправить себе: `.\scripts\send-daily-telegram.ps1`

Если Telegram уже настроен, вместо первого пункта удобнее запускать `.\scripts\start-live-session.ps1`: тогда при `Ctrl+C` агент сам пришлет итог сессии.
