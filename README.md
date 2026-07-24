# Relay Agent

Локальный контентный Relay для разработчика AI-агентов. Python-модуль `agent_content` собирает сигналы из Git, Codex-сессий и ручных заметок, ищет сюжет дня, оценивает content potential, маскирует чувствительные детали и сохраняет дневной content pack в Markdown и JSON.

## Почему Python

В текущем окружении нет `node/npm`, зато доступен Python. Для первого CLI-MVP Python подходит лучше: нет внешних зависимостей, проще запускать локально, а модульная структура оставляет место для будущей VS Code extension, логов терминала, AI-провайдера и интеграций с соцсетями.

## Быстрый старт

Самый простой сценарий для VS Code описан в `QUICKSTART.md`.

```bash
python -m agent_content daily
```

Результаты появятся в `outputs/`:

- `YYYY-MM-DD-content-pack.md`
- `YYYY-MM-DD-content-pack.json`

Запуск с параметрами:

```bash
python -m agent_content daily --date 2026-07-02 --repo . --notes content-notes --outputs outputs
```

## Живая летопись

Для режима почти realtime запусти:

```bash
python -m agent_content watch
```

Агент будет обновлять:

- `outputs/live-chronicle.md`
- `outputs/live-chronicle.json`

По умолчанию обновление идет раз в 60 секунд. Можно изменить интервал:

```bash
python -m agent_content watch --interval 20
```

Проверочный запуск без бесконечного цикла:

```bash
python -m agent_content watch --once
```

Отправить полный пакет в Telegram при остановке live-сессии:

```powershell
.\scripts\start-live-session.ps1
```

Или напрямую:

```bash
python -m agent_content watch --send-on-stop --send-kind live
python -m agent_content watch --send-on-stop --send-kind daily
python -m agent_content watch --send-on-stop --send-kind full
```

Это не прямое чтение внутреннего состояния VS Code. MVP наблюдает за рабочими папками через git, заметки и будущие логи. Если в трех окнах VS Code открыты три разные папки, добавь их в `projects` в `config.json`.

## Терминальные логи

MVP не перехватывает весь терминал автоматически. Это сделано специально: в терминале часто бывают токены, приватные URL и клиентские детали. Безопасный режим такой: сохранять только важные команды, ошибки, результаты тестов и короткие выводы.

Быстро добавить запись:

```bash
python -m agent_content terminal-note "pytest упал на privacy scanner: агент поймал внутренний URL и не дал ему попасть в контент"
```

Запись попадет в:

```text
terminal-logs/YYYY-MM-DD.md
```

Можно писать руками в файлы `.md`, `.txt` или `.log`, если имя содержит дату:

```text
terminal-logs/2026-07-03.md
```

Для проекта из мульти-проектного конфига:

```bash
python -m agent_content terminal-note --project project-2 "npm run build прошел, но пришлось упростить конфиг"
```

После этого `daily` и `watch` будут учитывать терминальный лог вместе с git и заметками.

## Telegram-отчеты

Агент может отправлять дневной content pack или live-летопись в Telegram через твоего бота.

1. В Telegram открой `@BotFather`.
2. Создай бота командой `/newbot`.
3. Скопируй bot token.
4. Напиши любое сообщение своему боту.
5. Узнай `chat_id`, открыв в браузере:

```text
https://api.telegram.org/bot<ТВОЙ_TOKEN>/getUpdates
```

6. Создай локальный `.env` рядом с `config.json`:

```env
TELEGRAM_BOT_TOKEN=123456789:replace_me
TELEGRAM_CHAT_ID=123456789
```

`.env` добавлен в `.gitignore`, его нельзя коммитить.

Отправить дневной пакет:

```powershell
.\scripts\send-daily-telegram.ps1
```

Отправить live-летопись:

```powershell
.\scripts\send-live-telegram.ps1
```

Через CLI:

```bash
python -m agent_content send-telegram --kind daily
python -m agent_content send-telegram --kind live
python -m agent_content send-telegram --kind file --file outputs/example-content-pack.md
python -m agent_content send-session
python -m agent_content pick --send
python -m agent_content nazai-edit --send
```

## Локальный редакторский backend

Если рядом лежит локальный backend редакторского бота, агент может отправлять туда `today-pick` или дневной пакет на редактуру. По умолчанию проверяется соседний путь `..\\Naz-AI_Bot`; другой путь задаётся через `NAZAI_LOCAL_PATH`.

Добавь в `.env`:

```env
NAZAI_API_URL=http://localhost:8000/api/content/edit
NAZAI_API_KEY=
NAZAI_LOCAL_PATH=..\\Naz-AI_Bot
```

Локально сохранить редактуру:

```powershell
.\scripts\make-nazai-edit.ps1
```

Отправить в Naz_Ai_Bot и переслать результат себе в Telegram:

```powershell
.\scripts\send-nazai-edit.ps1
```

## Inbox для Naz_Ai_Bot

Рекомендуемая схема без ручного участия:

```text
Codex user chats -> тематические эпизоды -> доказательный рассказ -> Naz_Ai_Bot -> выбор конкретной темы
```

Передать очищенные тексты пользовательских чатов за текущий день в соседний проект:

```powershell
.\scripts\export-nazai-inbox.ps1
```

Передать сводки текущего дня в соседний проект и сразу на VPS:

```powershell
.\scripts\export-nazai-vps.ps1
```

Передать весь существующий архив пользовательских чатов:

```powershell
.\scripts\export-nazai-all.ps1
.\scripts\export-nazai-all-vps.ps1
```

По умолчанию inbox зеркально организован по проектам, а внутри — по датам и темам:

```text
<NAZAI_LOCAL_PATH>\content_inbox\agent_content\
  Naz-AI_Bot\
    YYYY-MM-DD\
      YYYY-MM-DD-HHMM--тема--t-<id>.md
  Void-entity\
    YYYY-MM-DD\
      YYYY-MM-DD-HHMM--тема--t-<id>.md
```

`ai-logs/<проект>/` остаётся полным каноническим архивом диалогов: там все очищенные реплики сохраняются ровно по одному разу. В Naz один Markdown-файл соответствует одной содержательной теме, но содержит уже не лог по ролям, а читабельный редакторский рассказ: постановку задачи, важные ограничения, повороты работы и подтверждённый итог. Конкретные сведения извлекаются только из исходного эпизода; генеративная модель для пересказа не вызывается. Приветствия, опечатки и пустые подтверждения остаются только в полном архиве, а открытые, отменённые и не имеющие подтверждённого результата истории запрещены для автопубликации.

Внутри inbox находятся только UTF-8 Markdown-рассказы с `## История` и `## Итог`. Ролевые логи, JSON, изображения, `manifest.json`, topic-index и служебные README туда не попадают. Каждый рассказ хранит стабильный Topic-ID и SHA-256 исходного эпизода для проверки происхождения.

Naz поддерживает точный выбор: `/agent_content YYYY-MM-DD project:Naz-AI_Bot topic:контур публикации в VK`. Если проект или тема не найдены однозначно, случайный материал не подставляется.

VPS sync атомарно копирует полное проектно-тематическое дерево inbox в:

```text
/opt/naz-ai-bot/content_inbox/agent_content/
```

## Входные данные

### Git

Если текущая папка является git-репозиторием, агент соберет:

- текущую ветку;
- последние коммиты за день;
- измененные файлы;
- статистику добавленных и удаленных строк.

Если git недоступен, агент не падает и работает по заметкам.

### Ручные заметки

Клади `.md` или `.txt` файлы в `content-notes/`. Для дневного запуска имя файла должно содержать дату, например:

```text
content-notes/2026-07-02.md
```

В проекте уже есть пример: `content-notes/example-2026-07-02.md`.

### AI-логи

Позже можно добавлять markdown/txt-экспорты AI-сессий в `ai-logs/`. MVP уже содержит collector под эту папку.

## Конфиг

Основной конфиг: `config.json`. Пример с подсказками лежит в `config.example.json`.

```json
{
  "repo_path": ".",
  "projects": [
    {
      "name": "project-1",
      "path": "C:\\Projects\\project-1",
      "notes_dir": "content-notes"
    },
    {
      "name": "project-2",
      "path": "C:\\Projects\\project-2",
      "notes_dir": "content-notes"
    },
    {
      "name": "project-3",
      "path": "C:\\Projects\\project-3",
      "notes_dir": "content-notes"
    }
  ],
  "notes_dir": "content-notes",
  "terminal_logs_dir": "terminal-logs",
  "ai_logs_dir": "ai-logs",
  "outputs_dir": "outputs",
  "max_commits": 8,
  "story_count": 7,
  "recent_tones": []
}
```

## Архитектура

```text
agent_content/
  collectors/
    git_collector.py
    notes_collector.py
    ai_logs_collector.py
    terminal_collector.py
  analyzers/
    event_analyzer.py
    content_potential_scorer.py
    privacy_scanner.py
    tone_selector.py
  generators/
    story_generator.py
    reel_generator.py
    post_generator.py
    hook_generator.py
    content_pack_generator.py
  outputs/
    markdown_writer.py
    json_writer.py
  config/
    tone_profiles.py
    content_formats.py
    privacy_rules.py
```

## Privacy scanner

Перед генерацией публичного текста агент маскирует:

- email;
- API keys и токены;
- пароли и secret assignments;
- внутренние URL;
- локальные пути;
- чувствительные ключевые слова, которые требуют ручной проверки.

Обычные команды только готовят материалы и складывают их в review queue. Публикация и режим `autopost` существуют как отдельные явно запускаемые команды; без их запуска Relay ничего не публикует.

## Что развивать дальше

- память использованных тонов;
- чтение терминальных логов и результатов тестов;
- VS Code extension;
- локальный watcher активных файлов;
- screen captures;
- генерация изображений и видео;
- календарь контента;
- интеграции с Instagram, TikTok и YouTube Shorts.
