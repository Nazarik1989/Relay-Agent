# Multi-Project Monitoring

Агент не читает сами окна VS Code. Он смотрит на папки проектов: git-коммиты, git diff/status, заметки и важные логи. Поэтому рабочие репозитории должны быть прописаны в `config.json`.

## Команды

Найти git-проекты под `C:\Projects` и записать их в конфиг:

```powershell
.\scripts\discover-projects.ps1 -Root C:\Projects -Write
```

Посмотреть, что сейчас под наблюдением:

```powershell
.\scripts\list-projects.ps1
```

Добавить конкретную папку вручную:

```powershell
.\scripts\add-project.ps1 -Path C:\Projects\my-project -Name my-project
```

После этого `start-live`, `make-daily` и экспорт в Naz_Ai_Bot собирают летопись по подключенным проектам.

## Что попадает в летопись

- сегодняшние git-коммиты;
- текущие измененные файлы и diff/stat;
- заметки из `content-notes`;
- важные терминальные заметки из `terminal-logs`;
- AI-логи из `ai-logs`, если они есть.

## Текущая схема

```text
working projects -> content-agent live/daily -> Naz_Ai_Bot inbox -> VPS
```
