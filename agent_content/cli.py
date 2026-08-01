from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import shlex
import stat
import subprocess
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path
from shutil import copyfileobj, rmtree
from tempfile import mkdtemp
from uuid import uuid4

from agent_content.analyzers.content_potential_scorer import ContentPotentialScorer
from agent_content.analyzers.event_analyzer import EventAnalyzer
from agent_content.analyzers.privacy_scanner import PrivacyScanner
from agent_content.analyzers.tone_selector import ToneSelector
from agent_content.collectors.ai_logs_collector import AiLogsCollector
from agent_content.collectors.notes_collector import NotesCollector
from agent_content.collectors.terminal_collector import TerminalCollector
from agent_content.generators.content_pack_generator import ContentPackGenerator
from agent_content.integrations.codex_session_importer import CodexSessionImporter
from agent_content.integrations.codex_story_exporter import CodexStoryExporter
from agent_content.integrations.codex_topic_exporter import CodexTopicExporter
from agent_content.integrations.nazai_client import NazAiClient, nazai_response_to_markdown
from agent_content.integrations.nazai_inbox import NazAiInbox
from agent_content.integrations.nazai_publisher import NazAiPublisher, extract_publish_text
from agent_content.integrations.operator_event_exporter import OperatorEventExporter
from agent_content.integrations.telegram_sender import TelegramSender
from agent_content.models import Note, TerminalLog
from agent_content.outputs.json_writer import JsonWriter
from agent_content.outputs.live_writer import LiveWriter, now_local_iso
from agent_content.outputs.markdown_writer import MarkdownWriter
from agent_content.outputs.today_pick_writer import TodayPickWriter
from agent_content.utils import configured_projects, ensure_dir, load_config, today_iso


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="agent-content", description="Локальный AI-летописец разработки.")
    subparsers = parser.add_subparsers(dest="command", required=True)

    daily = subparsers.add_parser("daily", help="Собрать дневной content pack.")
    daily.add_argument("--config", default=None, help="Путь к config.json.")
    daily.add_argument("--date", default=today_iso(), help="Дата в формате YYYY-MM-DD.")
    daily.add_argument("--repo", default=None, help="Путь к рабочему проекту или папке.")
    daily.add_argument("--notes", default=None, help="Папка с markdown/txt заметками.")
    daily.add_argument("--outputs", default=None, help="Папка для результатов.")

    watch = subparsers.add_parser("watch", help="Обновлять живую летопись по нескольким проектам.")
    watch.add_argument("--config", default=None, help="Путь к config.json.")
    watch.add_argument("--date", default=today_iso(), help="Дата в формате YYYY-MM-DD.")
    watch.add_argument("--outputs", default=None, help="Папка для live-журнала.")
    watch.add_argument("--interval", type=int, default=60, help="Интервал обновления в секундах.")
    watch.add_argument("--once", action="store_true", help="Один раз обновить live-журнал и выйти.")
    watch.add_argument("--send-on-stop", action="store_true", help="Отправить live-летопись в Telegram при остановке Ctrl+C.")
    watch.add_argument(
        "--send-kind",
        choices=["live", "daily", "full"],
        default="full",
        help="Что отправить при --send-on-stop: live-летопись, дневной content pack или полный пакет сессии.",
    )

    terminal_note = subparsers.add_parser("terminal-note", help="Дописать важный терминальный сигнал в дневной лог.")
    terminal_note.add_argument("text", nargs="*", help="Текст заметки. Если не передан, читается stdin.")
    terminal_note.add_argument("--config", default=None, help="Путь к config.json.")
    terminal_note.add_argument("--date", default=today_iso(), help="Дата в формате YYYY-MM-DD.")
    terminal_note.add_argument("--project", default=None, help="Имя проекта из config.json.")

    send_telegram = subparsers.add_parser("send-telegram", help="Отправить отчет в Telegram.")
    send_telegram.add_argument("--config", default=None, help="Путь к config.json.")
    send_telegram.add_argument("--date", default=today_iso(), help="Дата в формате YYYY-MM-DD.")
    send_telegram.add_argument(
        "--kind",
        choices=["daily", "live", "file"],
        default="daily",
        help="Что отправить: daily content pack, live-летопись или файл.",
    )
    send_telegram.add_argument("--file", default=None, help="Путь к файлу для --kind file.")
    send_telegram.add_argument("--message", default=None, help="Короткий текст перед файлом.")
    send_telegram.add_argument("--no-generate", action="store_true", help="Не пересобирать daily/live перед отправкой.")
    send_telegram.add_argument("--as-text", action="store_true", help="Отправить содержимое текстом, а не документом.")
    send_all = subparsers.add_parser("send-session", help="Отправить полный пакет текущей сессии в Telegram.")
    send_all.add_argument("--config", default=None, help="Путь к config.json.")
    send_all.add_argument("--date", default=today_iso(), help="Дата в формате YYYY-MM-DD.")

    pick = subparsers.add_parser("pick", help="Собрать один готовый вариант: что постить сегодня.")
    pick.add_argument("--config", default=None, help="Путь к config.json.")
    pick.add_argument("--date", default=today_iso(), help="Дата в формате YYYY-MM-DD.")
    pick.add_argument("--send", action="store_true", help="Отправить выбор редактора в Telegram.")

    nazai = subparsers.add_parser("nazai-edit", help="Отправить today-pick в backend Naz_Ai_Bot на редактуру.")
    nazai.add_argument("--config", default=None, help="Путь к config.json.")
    nazai.add_argument("--date", default=today_iso(), help="Дата в формате YYYY-MM-DD.")
    nazai.add_argument("--send", action="store_true", help="Отправить результат Naz_Ai_Bot в Telegram.")
    nazai.add_argument("--source", default="pick", choices=["pick", "daily"], help="Что отправить в Naz_Ai_Bot.")

    publish = subparsers.add_parser("publish-nazai", help="Сгенерировать через Naz_Ai_Bot и опубликовать в его канал.")
    publish.add_argument("--config", default=None, help="Путь к config.json.")
    publish.add_argument("--date", default=today_iso(), help="Дата в формате YYYY-MM-DD.")
    publish.add_argument("--source", default="pick", choices=["pick", "daily"], help="Что отправить в Naz_Ai_Bot.")
    publish.add_argument("--dry-run", action="store_true", help="Сохранить результат, но не публиковать в канал.")

    autopost = subparsers.add_parser("autopost", help="Публиковать через Naz_Ai_Bot по расписанию.")
    autopost.add_argument("--config", default=None, help="Путь к config.json.")
    autopost.add_argument("--times", default=None, help="Время публикаций через запятую, например 10:00,20:00.")
    autopost.add_argument("--interval", type=int, default=30, help="Интервал проверки расписания в секундах.")
    autopost.add_argument("--source", default="pick", choices=["pick", "daily"], help="Что отправить в Naz_Ai_Bot.")
    autopost.add_argument("--once", action="store_true", help="Один раз опубликовать сейчас и выйти.")
    autopost.add_argument("--dry-run", action="store_true", help="Не публиковать, только собрать и сохранить результат.")

    inbox = subparsers.add_parser("export-nazai-inbox", help="Сложить сводки в inbox-папку Naz_Ai_Bot.")
    inbox.add_argument("--config", default=None, help="Путь к config.json.")
    inbox.add_argument("--date", default=today_iso(), help="Дата в формате YYYY-MM-DD.")
    inbox.add_argument("--sync-vps", action="store_true", help="После локального экспорта скопировать inbox дня на VPS.")
    inbox.add_argument("--vps-host", default=None, help="SSH host, например deploy@your-vps-host.")
    inbox.add_argument("--vps-path", default=None, help="Путь проекта Naz_Ai_Bot на VPS.")
    inbox.add_argument("--skip-codex-import", action="store_true", help="Do not refresh Codex chat summaries before exporting.")

    inbox_all = subparsers.add_parser("export-nazai-inbox-all", help="Сложить весь существующий архив в inbox Naz_Ai_Bot.")
    inbox_all.add_argument("--config", default=None, help="Путь к config.json.")
    inbox_all.add_argument("--sync-vps", action="store_true", help="После локального экспорта скопировать все даты на VPS.")
    inbox_all.add_argument("--vps-host", default=None, help="SSH host, например deploy@your-vps-host.")
    inbox_all.add_argument("--vps-path", default=None, help="Путь проекта Naz_Ai_Bot на VPS.")
    inbox_all.add_argument("--skip-codex-import", action="store_true", help="Do not refresh Codex chat summaries before exporting.")

    list_projects = subparsers.add_parser("list-projects", help="List watched projects.")
    list_projects.add_argument("--config", default=None, help="Path to config.json.")

    add_project = subparsers.add_parser("add-project", help="Add one project folder to the watcher config.")
    add_project.add_argument("path", help="Path to project folder.")
    add_project.add_argument("--name", default=None, help="Project name shown in reports.")
    add_project.add_argument("--config", default=None, help="Path to config.json.")

    discover_projects = subparsers.add_parser("discover-projects", help="Find work projects under a folder.")
    discover_projects.add_argument("--root", default="C:\\Projects", help="Root folder to scan.")
    discover_projects.add_argument("--config", default=None, help="Path to config.json.")
    discover_projects.add_argument("--write", action="store_true", help="Save discovered projects to config.json.")

    restore_history = subparsers.add_parser("restore-history", help="Rebuild daily packs for dates that have Codex chat summaries or notes.")
    restore_history.add_argument("--config", default=None, help="Path to config.json.")
    restore_history.add_argument("--sync-vps", action="store_true", help="Export rebuilt archive to Naz_Ai_Bot inbox and VPS.")
    restore_history.add_argument("--vps-host", default=None, help="SSH host, for example deploy@your-vps-host.")
    restore_history.add_argument("--vps-path", default=None, help="Naz_Ai_Bot path on VPS.")
    restore_history.add_argument("--skip-codex-import", action="store_true", help="Do not refresh Codex chat summaries before rebuilding.")

    import_codex = subparsers.add_parser("import-codex-sessions", help="Import safe summaries from local Codex VS Code chat sessions.")
    import_codex.add_argument("--config", default=None, help="Path to config.json.")
    import_codex.add_argument("--sessions-dir", default=None, help="Path to Codex sessions directory.")
    import_codex.add_argument("--output", default=None, help="Directory for generated AI logs.")
    import_codex.add_argument("--clear", action="store_true", help="Clear generated AI logs before import.")
    import_codex.add_argument(
        "--format",
        choices=["brief", "detailed", "both"],
        default="detailed",
        help="Chat note format: brief legacy summary, detailed work digest, or both.",
    )
    import_codex.add_argument(
        "--layout",
        choices=["central", "project"],
        default="central",
        help="Where to write imported notes: central output/project-name folders or each project's ai_logs_dir.",
    )

    args = parser.parse_args(argv)
    if args.command == "daily":
        return run_daily(args)
    if args.command == "watch":
        return run_watch(args)
    if args.command == "terminal-note":
        return run_terminal_note(args)
    if args.command == "send-telegram":
        return run_send_telegram(args)
    if args.command == "send-session":
        return run_send_session(args)
    if args.command == "pick":
        return run_pick(args)
    if args.command == "nazai-edit":
        return run_nazai_edit(args)
    if args.command == "publish-nazai":
        return run_publish_nazai(args)
    if args.command == "autopost":
        return run_autopost(args)
    if args.command == "export-nazai-inbox":
        return run_export_nazai_inbox(args)
    if args.command == "export-nazai-inbox-all":
        return run_export_nazai_inbox_all(args)
    if args.command == "list-projects":
        return run_list_projects(args)
    if args.command == "add-project":
        return run_add_project(args)
    if args.command == "discover-projects":
        return run_discover_projects(args)
    if args.command == "restore-history":
        return run_restore_history(args)
    if args.command == "import-codex-sessions":
        return run_import_codex_sessions(args)
    return 1


def run_daily(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    outputs_dir = ensure_dir(args.outputs or config["outputs_dir"])
    target_date = args.date
    projects = configured_projects(config, args.repo, args.notes) if args.repo else _content_projects(config)

    packs = [_build_pack_for_project(project, config, target_date) for project in projects]
    pack = _select_daily_pack(target_date, packs)
    if pack is None:
        print(f"За {target_date} нет реальных исходников; хроника не создавалась.")
        return 0

    md_path = outputs_dir / f"{target_date}-content-pack.md"
    json_path = outputs_dir / f"{target_date}-content-pack.json"
    MarkdownWriter().write(md_path, pack)
    JsonWriter().write(json_path, pack)

    print(f"Готово: {md_path}")
    print(f"JSON: {json_path}")
    print(f"Главный формат: {pack['best_format']['format']}")
    return 0


def run_watch(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    outputs_dir = ensure_dir(args.outputs or config["outputs_dir"])
    interval = max(5, int(args.interval))

    print(f"Живая летопись запущена. Интервал: {interval} сек.")
    print("Остановить: Ctrl+C")
    while True:
        payload = build_live_payload(config, args.date)
        if not payload["projects"]:
            print(f"[{payload['updated_at']}] no real source material for {args.date}; live chronicle was not written")
            if args.once:
                return 0
            try:
                time.sleep(interval)
            except KeyboardInterrupt:
                print("Live chronicle stopped.")
                return 0
            continue
        md_path, json_path = LiveWriter().write(outputs_dir, payload)
        print(f"[{payload['updated_at']}] обновлено: {md_path} / {json_path}")
        if args.once:
            return 0
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            if args.send_on_stop:
                _send_session_report(config, outputs_dir, args.date, args.send_kind)
            print("Живая летопись остановлена.")
            return 0


def run_list_projects(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    projects = configured_projects(config)
    print(f"Watched projects: {len(projects)}")
    for project in projects:
        print(f"- {project['name']}: {project['repo_path']}")
    return 0


def run_add_project(args: argparse.Namespace) -> int:
    config_path = _config_path(args.config)
    config = load_config(args.config)
    project = _project_config_entry(Path(args.path), args.name)
    action = _upsert_project(config, project)
    _write_config(config_path, config)
    print(f"{action}: {project['name']} -> {project['path']}")
    return 0


def run_discover_projects(args: argparse.Namespace) -> int:
    config_path = _config_path(args.config)
    config = load_config(args.config)
    discovered = [_project_config_entry(path) for path in _discover_project_paths(Path(args.root))]
    print(f"Discovered work projects under {Path(args.root).resolve()}: {len(discovered)}")
    existing = _configured_project_path_keys(config)
    for project in discovered:
        marker = "already" if _project_path_key(project["path"]) in existing else "new"
        print(f"- [{marker}] {project['name']}: {project['path']}")

    if args.write:
        added = 0
        updated = 0
        for project in discovered:
            action = _upsert_project(config, project)
            if action == "added":
                added += 1
            else:
                updated += 1
        _write_config(config_path, config)
        print(f"Saved to {config_path}: added {added}, updated {updated}")
    else:
        print("Dry run only. Add --write to save these projects to config.json.")
    return 0


def run_restore_history(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    summaries = _refresh_codex_summaries(config, args)
    outputs_dir = ensure_dir(config["outputs_dir"])
    dates = _discover_content_source_dates(config)
    if not dates:
        print("No Codex chat summaries or note dates found in watched projects.")
        return 1 if args.sync_vps else 0

    success_messages = [
        f"Restoring dates from Codex chat summaries and notes: {', '.join(dates)}"
    ]
    for target_date in dates:
        packs = [_build_pack_for_project(project, config, target_date) for project in _content_projects(config)]
        pack = _select_daily_pack(target_date, packs)
        if pack is None:
            success_messages.append(f"skipped {target_date}: no real source material")
            continue
        md_path = outputs_dir / f"{target_date}-content-pack.md"
        json_path = outputs_dir / f"{target_date}-content-pack.json"
        pick_path = outputs_dir / f"{target_date}-today-pick.md"
        MarkdownWriter().write(md_path, pack)
        JsonWriter().write(json_path, pack)
        TodayPickWriter().write(pick_path, pack)
        success_messages.append(f"rebuilt {target_date}: {md_path}")

    if args.sync_vps:
        try:
            result, topics = _write_topic_inbox_for_sync(summaries)
            _sync_vps_if_requested(args, result)
        except VpsSyncError as exc:
            print(f"VPS sync failed: {exc.reason_code}")
            return 1
    for message in success_messages:
        print(message)
    if args.sync_vps:
        print(f"synced {len(topics)} topics to the configured VPS release")
    return 0


def run_import_codex_sessions(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    output_root = args.output or config.get("ai_logs_dir") or "ai-logs"
    written = CodexSessionImporter(args.sessions_dir).import_sessions(
        configured_projects(config),
        output_root,
        clear=args.clear,
        format=args.format,
        layout=args.layout,
    )
    print(f"Imported Codex session summaries: {len(written)}")
    for path in written[:20]:
        print(f"- {path}")
    if len(written) > 20:
        print(f"...and {len(written) - 20} more")
    return 0


def _refresh_codex_summaries(config: dict, args: argparse.Namespace) -> list:
    output_root = config.get("ai_logs_dir") or "ai-logs"
    projects = configured_projects(config)
    importer = CodexSessionImporter()
    summaries = importer.collect_summaries(projects)
    if getattr(args, "skip_codex_import", False):
        print("Codex transcript files were not rewritten; topic source was read directly from sessions.")
        return summaries
    written = importer.write_summaries(
        summaries,
        projects,
        output_root,
        clear=True,
        format="detailed",
        layout="central",
    )
    print(f"Codex session summaries refreshed: {len(written)}")
    return summaries


def run_terminal_note(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    projects = configured_projects(config)
    project = projects[0]
    if args.project:
        matches = [item for item in projects if item["name"] == args.project]
        if not matches:
            print(f"Проект не найден в config.json: {args.project}")
            return 1
        project = matches[0]

    text = " ".join(args.text).strip()
    if not text:
        import sys

        text = sys.stdin.read().strip()
    if not text:
        print("Нет текста для записи.")
        return 1

    collector = TerminalCollector(project["terminal_logs_dir"])
    path = collector.append_note(args.date, text)
    print(f"Записано в терминальный лог: {path}")
    return 0


def run_send_telegram(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    outputs_dir = ensure_dir(config["outputs_dir"])
    sender = TelegramSender()
    if not sender.is_configured():
        print("Telegram не настроен. Заполни .env: TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID.")
        return 1

    if args.kind == "daily":
        path = outputs_dir / f"{args.date}-content-pack.md"
        if not args.no_generate or not path.exists():
            packs = [_build_pack_for_project(project, config, args.date) for project in _content_projects(config)]
            pack = _select_daily_pack(args.date, packs)
            if pack is None:
                print(f"За {args.date} нет реальных исходников; отправлять нечего.")
                return 1
            MarkdownWriter().write(path, pack)
            JsonWriter().write(outputs_dir / f"{args.date}-content-pack.json", pack)
    elif args.kind == "live":
        path = outputs_dir / "live-chronicle.md"
        if not args.no_generate or not path.exists():
            payload = build_live_payload(config, args.date)
            LiveWriter().write(outputs_dir, payload)
    else:
        if not args.file:
            print("Для --kind file нужен --file.")
            return 1
        path = Path(args.file)

    if not path.exists():
        print(f"Файл не найден: {path}")
        return 1

    message = args.message or _default_telegram_message(args.kind, args.date)
    if message:
        sender.send_text(message)
    if args.as_text:
        sender.send_text(path.read_text(encoding="utf-8", errors="replace"))
    else:
        sender.send_file(path, caption=f"Agent Content: {path.name}")
    print(f"Отправлено в Telegram: {path}")
    return 0


def run_send_session(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    outputs_dir = ensure_dir(config["outputs_dir"])
    sender = TelegramSender()
    if not sender.is_configured():
        print("Telegram не настроен. Заполни .env: TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID.")
        return 1
    sent = _send_full_session(sender, config, outputs_dir, args.date, "Полный пакет текущей сессии.")
    print(f"Отправлено файлов в Telegram: {sent}")
    return 0


def run_pick(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    outputs_dir = ensure_dir(config["outputs_dir"])
    path = _build_today_pick(config, outputs_dir, args.date)
    print(f"Готов выбор редактора: {path}")
    if args.send:
        sender = TelegramSender()
        if not sender.is_configured():
            print("Telegram не настроен. Заполни .env: TELEGRAM_BOT_TOKEN и TELEGRAM_CHAT_ID.")
            return 1
        sender.send_text(f"Выбор редактора за {args.date}: что постить сегодня.")
        sender.send_file(path, caption=f"Agent Content: {path.name}")
        print(f"Отправлено в Telegram: {path}")
    return 0


def run_nazai_edit(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    outputs_dir = ensure_dir(config["outputs_dir"])
    client = NazAiClient()
    if not client.is_configured():
        print("Naz_Ai_Bot не настроен. Добавь NAZAI_API_URL или NAZAI_LOCAL_PATH в .env.")
        return 1

    source_path, payload = _build_nazai_payload(config, outputs_dir, args.date, args.source)
    response = client.edit(payload)
    json_path = outputs_dir / f"{args.date}-nazai-edited.json"
    md_path = outputs_dir / f"{args.date}-nazai-edited.md"
    JsonWriter().write(json_path, response)
    md_path.write_text(nazai_response_to_markdown(response, source_path), encoding="utf-8")
    print(f"Naz_Ai_Bot редактура сохранена: {md_path}")
    print(f"Naz_Ai_Bot raw JSON: {json_path}")

    if args.send:
        sender = TelegramSender()
        if not sender.is_configured():
            print("Telegram не настроен. Результат Naz_Ai_Bot сохранен локально, но не отправлен.")
            return 1
        sender.send_text(f"Naz_Ai_Bot отредактировал контент за {args.date}.")
        sender.send_file(md_path, caption=f"Agent Content: {md_path.name}")
        print(f"Отправлено в Telegram: {md_path}")
    return 0


def run_publish_nazai(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    outputs_dir = ensure_dir(config["outputs_dir"])
    result = _build_nazai_edit(config, outputs_dir, args.date, args.source)
    if args.dry_run:
        print(f"Dry run: публикация не выполнена. Текст сохранен: {result['md_path']}")
        return 0
    _publish_nazai_response(result["response"])
    print(f"Опубликовано через Naz_Ai_Bot: {result['md_path']}")
    return 0


def run_autopost(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    outputs_dir = ensure_dir(config["outputs_dir"])
    times = _parse_autopost_times(args.times, config)
    interval = max(5, int(args.interval))
    state_path = outputs_dir / "autopost-state.json"

    if args.once:
        date = today_iso()
        result = _build_nazai_edit(config, outputs_dir, date, args.source)
        if args.dry_run:
            print(f"Dry run: публикация не выполнена. Текст сохранен: {result['md_path']}")
        else:
            _publish_nazai_response(result["response"])
            print(f"Опубликовано через Naz_Ai_Bot: {result['md_path']}")
        return 0

    print(f"Автопостинг запущен. Слоты: {', '.join(times)}. Проверка каждые {interval} сек.")
    print("Остановить: Ctrl+C")
    while True:
        now = datetime.now().astimezone()
        current_time = now.strftime("%H:%M")
        current_date = now.date().isoformat()
        slot_key = f"{current_date} {current_time}"
        if current_time in times and not _autopost_was_done(state_path, slot_key):
            try:
                result = _build_nazai_edit(config, outputs_dir, current_date, args.source)
                if args.dry_run:
                    print(f"[{slot_key}] dry run: {result['md_path']}")
                else:
                    _publish_nazai_response(result["response"])
                    print(f"[{slot_key}] опубликовано через Naz_Ai_Bot: {result['md_path']}")
                _mark_autopost_done(state_path, slot_key, str(result["md_path"]), dry_run=args.dry_run)
            except Exception as exc:
                print(f"[{slot_key}] ошибка автопостинга: {exc}")
        try:
            time.sleep(interval)
        except KeyboardInterrupt:
            print("Автопостинг остановлен.")
            return 0


def run_export_nazai_inbox(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    summaries = _refresh_codex_summaries(config, args)
    if not any(str(getattr(summary, "date", "")) == args.date for summary in summaries):
        print(f"За {args.date} нет пользовательских сообщений Codex; inbox не изменялся.")
        return 1 if args.sync_vps else 0
    if args.sync_vps:
        try:
            result, topics = _write_topic_inbox_for_sync(summaries)
        except VpsSyncError as exc:
            print(f"VPS sync failed: {exc.reason_code}")
            return 1
    else:
        result, topics = _write_topic_inbox(summaries)
    selected = [topic for topic in topics if topic.date == args.date]
    success_messages = [
        f"Тематический архив передан в Naz_Ai_Bot inbox: {result['inbox_dir']}",
        f"Проектов: {result['project_count']}; дат: {result['date_count']}; "
        f"рассказов: {result['document_count']} (за {args.date}: {len(selected)})",
    ]
    if args.sync_vps:
        try:
            _sync_vps_if_requested(args, result)
        except VpsSyncError as exc:
            print(f"VPS sync failed: {exc.reason_code}")
            return 1
    for message in success_messages:
        print(message)
    if args.sync_vps:
        print("Тематический inbox и OperatorEvent атомарно скопированы на VPS.")
    return 0


def run_export_nazai_inbox_all(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    summaries = _refresh_codex_summaries(config, args)
    if not summaries:
        print("Не найдено пользовательской истории Codex для экспорта.")
        return 1 if args.sync_vps else 0

    if args.sync_vps:
        try:
            result, topics = _write_topic_inbox_for_sync(summaries)
        except VpsSyncError as exc:
            print(f"VPS sync failed: {exc.reason_code}")
            return 1
    else:
        result, topics = _write_topic_inbox(summaries)
    success_messages = [
        f"Полная тематическая история передана в Naz_Ai_Bot inbox: {result['inbox_dir']}",
        f"Проектов: {result['project_count']}; дат: {result['date_count']}; "
        f"текстовых рассказов: {result['document_count']}",
    ]

    if args.sync_vps:
        try:
            _sync_vps_if_requested(args, result)
        except VpsSyncError as exc:
            print(f"VPS sync failed: {exc.reason_code}")
            return 1
    for message in success_messages:
        print(message)
    if args.sync_vps:
        print("Тематический inbox и OperatorEvent атомарно скопированы на VPS.")
    return 0


def _write_topic_inbox(
    summaries: list, *, prepare_sync_receipt: bool = False,
) -> tuple[dict[str, object], list]:
    topics = CodexTopicExporter().build_documents(summaries)
    stories = CodexStoryExporter().build_documents(topics)
    documents = {story.relative_path: story.text for story in stories}
    source_topic_ids = {topic.topic_id for topic in topics}
    if len(documents) != len(stories) or any(story.topic_id not in source_topic_ids for story in stories):
        raise RuntimeError("Story path collision or unknown topic while building NazAI inbox")
    inbox = NazAiInbox()
    operator_events = OperatorEventExporter(inbox.inbox_dir.parent / "operator_events")
    event_documents = None
    try:
        event_documents = operator_events.build_documents(topics, stories)
    except Exception:
        # Shadow metadata must never block the established text-only inbox.
        event_documents = None

    result = inbox.write_documents(documents)
    result["operator_events_dir"] = operator_events.output_root
    result["operator_events_status"] = "failed"
    result["operator_events_reason_code"] = "operator_event_export_failed"
    result["operator_event_count"] = 0
    if event_documents is not None:
        if event_documents:
            try:
                event_result = operator_events.write_documents(event_documents)
            except Exception:
                pass
            else:
                result["operator_events_status"] = "ready"
                result["operator_events_reason_code"] = None
                result["operator_event_count"] = event_result["document_count"]
        else:
            try:
                _replace_tree_with_fresh_empty(operator_events.output_root)
            except Exception:
                pass
            else:
                result["operator_events_status"] = "completed_empty"
                result["operator_events_reason_code"] = None
    if prepare_sync_receipt and result["operator_events_status"] in {"ready", "completed_empty"}:
        result["sync_receipt"] = _build_current_run_receipt(result)
    return result, stories


def _write_topic_inbox_for_sync(summaries: list) -> tuple[dict[str, object], list]:
    if not summaries:
        raise VpsSyncError("vps_sync_current_run_export_missing")
    return _write_topic_inbox(summaries, prepare_sync_receipt=True)


def _send_session_report(config: dict, outputs_dir: Path, target_date: str, kind: str) -> None:
    sender = TelegramSender()
    if not sender.is_configured():
        print("Telegram не настроен, отчет после сессии не отправлен.")
        return

    try:
        if kind == "full":
            sent = _send_full_session(sender, config, outputs_dir, target_date, f"Сессия завершена. Полный пакет за {target_date}.")
            print(f"Отчет после сессии отправлен в Telegram. Файлов: {sent}")
            return
        if kind == "daily":
            packs = [_build_pack_for_project(project, config, target_date) for project in _content_projects(config)]
            pack = _select_daily_pack(target_date, packs)
            if pack is None:
                print(f"За {target_date} нет реальных исходников; отчет не отправлен.")
                return
            path = outputs_dir / f"{target_date}-content-pack.md"
            MarkdownWriter().write(path, pack)
            JsonWriter().write(outputs_dir / f"{target_date}-content-pack.json", pack)
            message = f"Сессия завершена. Готов дневной content pack за {target_date}."
        else:
            payload = build_live_payload(config, target_date)
            path, _ = LiveWriter().write(outputs_dir, payload)
            message = f"Сессия завершена. Свежая live-летопись за {target_date}."

        sender.send_text(message)
        sender.send_file(path, caption=f"Agent Content: {path.name}")
        print(f"Отчет после сессии отправлен в Telegram: {path}")
    except Exception as exc:
        print(f"Не удалось отправить отчет после сессии: {exc}")


def _send_full_session(sender: TelegramSender, config: dict, outputs_dir: Path, target_date: str, message: str) -> int:
    paths = _build_session_files(config, outputs_dir, target_date)
    sender.send_text(message)
    sent = 0
    for path in paths:
        if path.exists() and path.is_file():
            sender.send_file(path, caption=f"Agent Content: {path.name}")
            sent += 1
    return sent


def _build_session_files(config: dict, outputs_dir: Path, target_date: str) -> list[Path]:
    packs = [_build_pack_for_project(project, config, target_date) for project in _content_projects(config)]
    pack = _select_daily_pack(target_date, packs)
    if pack is None:
        return []
    payload = build_live_payload(config, target_date)
    live_md, _ = LiveWriter().write(outputs_dir, payload)
    daily_md = outputs_dir / f"{target_date}-content-pack.md"
    daily_json = outputs_dir / f"{target_date}-content-pack.json"
    MarkdownWriter().write(daily_md, pack)
    JsonWriter().write(daily_json, pack)

    today_pick = outputs_dir / f"{target_date}-today-pick.md"
    TodayPickWriter().write(today_pick, pack)

    paths = [today_pick, live_md, daily_md, daily_json]
    for output_path in sorted(outputs_dir.glob(f"{target_date}-*.md")) + sorted(outputs_dir.glob(f"{target_date}-*.json")):
        paths.append(output_path)
    live_json = outputs_dir / "live-chronicle.json"
    if live_json.exists():
        paths.append(live_json)
    for project in _content_projects(config):
        for folder_key in ["notes_dir", "terminal_logs_dir", "ai_logs_dir"]:
            folder = Path(project[folder_key])
            if not folder.exists():
                continue
            for path in sorted(folder.rglob("*")):
                if path.is_file() and target_date in path.name and not path.name.startswith("example"):
                    paths.append(path)
    return _unique_paths(paths)


def _discover_existing_archive_dates(config: dict) -> list[str]:
    candidates: set[str] = set()
    folders = [Path(config["outputs_dir"])] + _project_source_folders(config)
    for folder in folders:
        if not folder.exists():
            continue
        for path in folder.rglob("*"):
            if not path.is_file() or path.name.startswith("example"):
                continue
            match = re.search(r"\d{4}-\d{2}-\d{2}", path.name)
            if match:
                candidates.add(match.group(0))
    return sorted(candidates)


def _discover_codex_history_dates(config: dict) -> list[str]:
    candidates: set[str] = set()
    root = Path(config.get("ai_logs_dir") or "ai-logs")
    if not root.exists():
        return []
    for path in root.rglob("*.md"):
        if not path.is_file() or path.is_symlink() or "-codex-" not in path.name:
            continue
        match = re.match(r"(\d{4}-\d{2}-\d{2})-codex-", path.name)
        if match:
            candidates.add(match.group(1))
    return sorted(candidates)


def _codex_history_files_for_date(config: dict, target_date: str) -> list[Path]:
    root = Path(config.get("ai_logs_dir") or "ai-logs")
    if not root.exists():
        return []
    prefix = f"{target_date}-codex-"
    return sorted(
        (
            path
            for path in root.rglob(f"{prefix}*.md")
            if path.is_file() and not path.is_symlink() and path.name.startswith(prefix)
        ),
        key=lambda path: str(path).casefold(),
    )


def _discover_content_source_dates(config: dict) -> list[str]:
    candidates: set[str] = set()
    for folder in _project_source_folders(config):
        if not folder.exists():
            continue
        for path in folder.rglob("*"):
            if not path.is_file() or path.name.startswith("example"):
                continue
            match = re.search(r"\d{4}-\d{2}-\d{2}", path.name)
            if match:
                candidates.add(match.group(0))
    return sorted(candidates)


def _existing_archive_files_for_date(config: dict, target_date: str) -> list[Path]:
    paths: list[Path] = []
    outputs_dir = Path(config["outputs_dir"])
    if outputs_dir.exists():
        for path in sorted(outputs_dir.glob(f"{target_date}-*.md")) + sorted(outputs_dir.glob(f"{target_date}-*.json")):
            if path.is_file() and not path.name.startswith("example"):
                paths.append(path)

    for folder in _project_source_folders(config):
        if not folder.exists():
            continue
        for path in sorted(folder.rglob("*")):
            if path.is_file() and target_date in path.name and not path.name.startswith("example"):
                paths.append(path)

    today = today_iso()
    if target_date == today:
        for live_name in ["live-chronicle.md", "live-chronicle.json"]:
            live_path = outputs_dir / live_name
            if live_path.exists():
                paths.append(live_path)
    return _unique_paths(paths)


def _project_source_folders(config: dict) -> list[Path]:
    folders = []
    for project in _content_projects(config):
        for folder_key in ["notes_dir", "terminal_logs_dir", "ai_logs_dir"]:
            folder = Path(project[folder_key])
            folders.append(folder)
    return _unique_paths(folders)


def _build_today_pick(config: dict, outputs_dir: Path, target_date: str) -> Path:
    packs = [_build_pack_for_project(project, config, target_date) for project in _content_projects(config)]
    pack = _select_daily_pack(target_date, packs)
    if pack is None:
        raise RuntimeError(f"No real source material for {target_date}")
    path = outputs_dir / f"{target_date}-today-pick.md"
    TodayPickWriter().write(path, pack)
    return path


def _build_nazai_payload(config: dict, outputs_dir: Path, target_date: str, source: str) -> tuple[Path, dict]:
    packs = [_build_pack_for_project(project, config, target_date) for project in _content_projects(config)]
    pack = _select_daily_pack(target_date, packs)
    if pack is None:
        raise RuntimeError(f"No real source material for {target_date}")
    if source == "daily":
        source_path = outputs_dir / f"{target_date}-content-pack.md"
        MarkdownWriter().write(source_path, pack)
    else:
        source_path = outputs_dir / f"{target_date}-today-pick.md"
        TodayPickWriter().write(source_path, pack)

    return source_path, {
        "source": "agent-content",
        "task": "edit_content_for_publication",
        "date": target_date,
        "language": config.get("language", "ru"),
        "instructions": (
            "Отредактируй материал для соцсетей. Сохрани живой человеческий тон, "
            "убери лишнюю техническую сухость, не добавляй секреты, не выдумывай факты. "
            "Верни готовый пост, 3 сторис и короткий комментарий редактора."
        ),
        "source_file": str(source_path),
        "markdown": source_path.read_text(encoding="utf-8", errors="replace"),
        "content_pack": pack,
    }


def _build_nazai_edit(config: dict, outputs_dir: Path, target_date: str, source: str) -> dict:
    client = NazAiClient()
    if not client.is_configured():
        raise RuntimeError("Naz_Ai_Bot не настроен. Добавь NAZAI_LOCAL_PATH в .env.")
    source_path, payload = _build_nazai_payload(config, outputs_dir, target_date, source)
    response = client.edit(payload)
    json_path = outputs_dir / f"{target_date}-nazai-edited.json"
    md_path = outputs_dir / f"{target_date}-nazai-edited.md"
    JsonWriter().write(json_path, response)
    md_path.write_text(nazai_response_to_markdown(response, source_path), encoding="utf-8")
    return {"source_path": source_path, "response": response, "json_path": json_path, "md_path": md_path}


def _publish_nazai_response(response: dict) -> None:
    text = extract_publish_text(response)
    NazAiPublisher().publish_text(text)


def _parse_autopost_times(raw_times: str | None, config: dict) -> list[str]:
    value = raw_times or ",".join(config.get("autopost_times") or [])
    times = []
    for item in value.split(","):
        clean = item.strip()
        if not clean:
            continue
        datetime.strptime(clean, "%H:%M")
        times.append(clean)
    if not times:
        raise RuntimeError("Не задано ни одного времени автопостинга.")
    return times


def _config_path(path: str | None) -> Path:
    return Path(path or "config.json")


def _write_config(path: Path, config: dict) -> None:
    path.write_text(json.dumps(config, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")


def _project_config_entry(path: Path, name: str | None = None) -> dict:
    project_path = path.resolve()
    return {
        "name": name or project_path.name,
        "path": str(project_path),
        "notes_dir": str(project_path / "content-notes"),
        "terminal_logs_dir": str(project_path / "terminal-logs"),
        "ai_logs_dir": str(project_path / "ai-logs"),
    }


def _upsert_project(config: dict, project: dict) -> str:
    projects = list(config.get("projects") or [])
    project_key = _project_path_key(project["path"])
    for index, existing in enumerate(projects):
        existing_path = existing.get("path") or existing.get("repo_path") or "."
        if _project_path_key(existing_path) == project_key:
            merged = dict(existing)
            merged.update(project)
            projects[index] = merged
            config["projects"] = projects
            return "updated"
    projects.append(project)
    config["projects"] = projects
    return "added"


def _configured_project_path_keys(config: dict) -> set[str]:
    keys: set[str] = set()
    for project in config.get("projects") or []:
        keys.add(_project_path_key(project.get("path") or project.get("repo_path") or "."))
    return keys


def _project_path_key(path: str | Path) -> str:
    return str(Path(path).resolve()).casefold()


def _discover_project_paths(root: Path) -> list[Path]:
    root = root.resolve()
    if not root.exists():
        return []
    skip_parts = {".git", ".venv", "venv", "node_modules", "__pycache__", ".mypy_cache", ".pytest_cache"}
    projects: set[Path] = set()
    for candidate in root.rglob(".git"):
        try:
            if any(part in skip_parts for part in candidate.relative_to(root).parts[:-1]):
                continue
        except ValueError:
            continue
        if candidate.is_dir() or candidate.is_file():
            projects.add(candidate.parent.resolve())
    return sorted(projects, key=lambda item: str(item).casefold())


def _autopost_was_done(state_path: Path, slot_key: str) -> bool:
    state = _read_autopost_state(state_path)
    return slot_key in state.get("posted_slots", {})


def _mark_autopost_done(state_path: Path, slot_key: str, output_path: str, dry_run: bool) -> None:
    state = _read_autopost_state(state_path)
    posted = state.setdefault("posted_slots", {})
    posted[slot_key] = {"output_path": output_path, "dry_run": dry_run, "created_at": datetime.now().astimezone().isoformat(timespec="seconds")}
    state_path.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def _read_autopost_state(state_path: Path) -> dict:
    if not state_path.exists():
        return {"posted_slots": {}}
    try:
        return json.loads(state_path.read_text(encoding="utf-8"))
    except json.JSONDecodeError:
        return {"posted_slots": {}}


class VpsSyncError(RuntimeError):
    """A transport failure that exposes only a stable, non-sensitive code."""

    def __init__(self, reason_code: str) -> None:
        self.reason_code = reason_code
        super().__init__(reason_code)


_VPS_HOST_RE = re.compile(
    r"(?=.{1,320}\Z)(?:[A-Za-z0-9._-]+@)?"
    r"[A-Za-z0-9](?:[A-Za-z0-9._-]*[A-Za-z0-9])?\Z"
)
_VPS_PATH_RE = re.compile(r"/[A-Za-z0-9._-]+(?:/[A-Za-z0-9._-]+)*\Z")
_SSH_OPTIONS = (
    "-o", "BatchMode=yes",
    "-o", "ConnectTimeout=15",
    "-o", "StrictHostKeyChecking=yes",
)
_REMOTE_COMMAND_TIMEOUT_SECONDS = 180
_REMOTE_LOCK_BUSY_EXIT = 73
_REMOTE_RECOVERY_REQUIRED_EXIT = 75
_REMOTE_COMMIT_CLEANUP_FAILED_EXIT = 76
_REMOTE_CLEANUP_FAILED_EXIT = 79


def _sync_vps_if_requested(args: argparse.Namespace, result: dict[str, object]) -> bool:
    """Run transport only for an explicit command-line ``--sync-vps`` request."""

    if not bool(getattr(args, "sync_vps", False)):
        return False
    host = getattr(args, "vps_host", None) or os.getenv("NAZAI_VPS_HOST")
    vps_path = getattr(args, "vps_path", None) or os.getenv("NAZAI_VPS_PATH") or "/opt/naz-ai-bot"
    _sync_nazai_release_to_vps(result, host, vps_path)
    return True


def _sync_nazai_release_to_vps(
    result: dict[str, object], host: object, vps_path: object,
) -> None:
    """Upload and atomically activate Markdown and OperatorEvent as one release."""

    safe_host = _validate_vps_host(host)
    safe_vps_path = _validate_vps_path(vps_path)
    snapshot_result: dict[str, object] | None = None
    snapshot_root: Path | None = None
    remote: dict[str, str] | None = None
    receipt: dict[str, object] | None = None
    try:
        snapshot_result, snapshot_root = _create_current_release_snapshot(result)
        remote, receipt = _stage_prepared_nazai_release(
            snapshot_result, safe_host, safe_vps_path
        )
        _cleanup_local_release_snapshot(snapshot_root, suppress_errors=False)
        snapshot_root = None
    except BaseException:
        if snapshot_root is not None:
            _cleanup_local_release_snapshot(snapshot_root, suppress_errors=True)
        if remote is not None:
            _cleanup_remote_transaction(safe_host, remote)
        raise
    if remote is None or receipt is None:
        raise VpsSyncError("vps_sync_stage_init_failed")
    _activate_prepared_nazai_release(safe_host, remote, receipt)


def _stage_prepared_nazai_release(
    result: dict[str, object], safe_host: str, safe_vps_path: str,
) -> tuple[dict[str, str], dict[str, object]]:
    markdown_root, events_root, receipt = _preflight_current_release(result)
    transaction_id = uuid4().hex
    remote = _remote_release_paths(safe_vps_path, transaction_id)

    prepare_script = " && ".join(
        (
            f"mkdir -p -- {shlex.quote(remote['parent'])}",
            f"test ! -e {shlex.quote(remote['markdown_staging'])} && test ! -L {shlex.quote(remote['markdown_staging'])}",
            f"test ! -e {shlex.quote(remote['events_staging'])} && test ! -L {shlex.quote(remote['events_staging'])}",
            f"test ! -e {shlex.quote(remote['markdown_old_hold'])} && test ! -L {shlex.quote(remote['markdown_old_hold'])}",
            f"test ! -e {shlex.quote(remote['events_old_hold'])} && test ! -L {shlex.quote(remote['events_old_hold'])}",
            f"test ! -e {shlex.quote(remote['markdown_recovery'])} && test ! -L {shlex.quote(remote['markdown_recovery'])}",
            f"test ! -e {shlex.quote(remote['events_recovery'])} && test ! -L {shlex.quote(remote['events_recovery'])}",
            f"test ! -e {shlex.quote(remote['markdown_failed_new'])} && test ! -L {shlex.quote(remote['markdown_failed_new'])}",
            f"test ! -e {shlex.quote(remote['events_failed_new'])} && test ! -L {shlex.quote(remote['events_failed_new'])}",
        )
    )
    try:
        _run_remote_script(
            safe_host,
            prepare_script,
            failure_code="vps_sync_stage_init_failed",
        )
        _run_checked(
            _scp_args(markdown_root, safe_host, remote["markdown_staging"]),
            failure_code="vps_sync_markdown_upload_failed",
        )
        _run_checked(
            _scp_args(events_root, safe_host, remote["events_staging"]),
            failure_code="vps_sync_operator_events_upload_failed",
        )
    except VpsSyncError:
        _cleanup_remote_transaction(safe_host, remote)
        raise
    return remote, receipt


def _activate_prepared_nazai_release(
    safe_host: str, remote: dict[str, str], receipt: dict[str, object],
) -> None:
    try:
        _run_remote_script(
            safe_host,
            _remote_release_transaction_script(remote, receipt),
            failure_code="vps_sync_remote_transaction_failed",
            lock_busy_exit=_REMOTE_LOCK_BUSY_EXIT,
            exit_reason_codes={
                _REMOTE_RECOVERY_REQUIRED_EXIT: "vps_sync_remote_recovery_required",
                _REMOTE_COMMIT_CLEANUP_FAILED_EXIT: "vps_sync_remote_commit_cleanup_failed",
                _REMOTE_CLEANUP_FAILED_EXIT: "vps_sync_remote_cleanup_failed",
            },
        )
    except VpsSyncError:
        _cleanup_remote_transaction(safe_host, remote)
        raise


def _build_current_run_receipt(result: dict[str, object]) -> dict[str, object]:
    status = result.get("operator_events_status")
    if status == "failed":
        raise VpsSyncError("vps_sync_operator_events_current_run_failed")
    if status not in {"ready", "completed_empty"}:
        raise VpsSyncError("vps_sync_operator_events_status_invalid")
    markdown_root = _required_result_path(
        result, "inbox_dir", "vps_sync_markdown_result_invalid"
    )
    events_root = _required_result_path(
        result, "operator_events_dir", "vps_sync_operator_events_result_invalid"
    )
    if (
        markdown_root == events_root
        or markdown_root in events_root.parents
        or events_root in markdown_root.parents
        or markdown_root.parent != events_root.parent
    ):
        raise VpsSyncError("vps_sync_local_roots_not_separate")
    _reject_local_release_artifacts(markdown_root, events_root)

    markdown_count = _validate_markdown_tree(markdown_root)
    event_count = _validate_operator_events_tree(
        events_root, allow_empty=status == "completed_empty"
    )
    if _required_nonnegative_count(result, "document_count") != markdown_count:
        raise VpsSyncError("vps_sync_markdown_current_run_mismatch")
    if _required_nonnegative_count(result, "operator_event_count") != event_count:
        raise VpsSyncError("vps_sync_operator_events_current_run_mismatch")
    return {
        "contract_version": "nazai-release-receipt.v1",
        "run_id": uuid4().hex,
        "markdown_root": str(markdown_root),
        "operator_events_root": str(events_root),
        "operator_events_status": status,
        "markdown_inventory": _build_tree_inventory(
            markdown_root, "vps_sync_markdown_tree_invalid"
        ),
        "operator_events_inventory": _build_tree_inventory(
            events_root, "vps_sync_operator_events_tree_invalid"
        ),
    }


def _create_current_release_snapshot(
    result: dict[str, object],
) -> tuple[dict[str, object], Path]:
    status = result.get("operator_events_status")
    if status == "failed":
        raise VpsSyncError("vps_sync_operator_events_current_run_failed")
    if status not in {"ready", "completed_empty"}:
        raise VpsSyncError("vps_sync_operator_events_status_invalid")

    source_receipt = _required_sync_receipt(result, str(status))
    expected_markdown_inventory = _required_receipt_inventory(
        source_receipt, "markdown_inventory", allow_empty=False
    )
    expected_event_inventory = _required_receipt_inventory(
        source_receipt,
        "operator_events_inventory",
        allow_empty=status == "completed_empty",
    )
    markdown_root = _required_result_path(
        result, "inbox_dir", "vps_sync_markdown_result_invalid"
    )
    events_root = _required_result_path(
        result, "operator_events_dir", "vps_sync_operator_events_result_invalid"
    )
    if (
        markdown_root == events_root
        or markdown_root in events_root.parents
        or events_root in markdown_root.parents
        or markdown_root.parent != events_root.parent
    ):
        raise VpsSyncError("vps_sync_local_roots_not_separate")
    if (
        source_receipt.get("markdown_root") != str(markdown_root)
        or source_receipt.get("operator_events_root") != str(events_root)
    ):
        raise VpsSyncError("vps_sync_current_run_root_mismatch")
    _reject_local_release_artifacts(markdown_root, events_root)

    snapshot_root: Path | None = None
    try:
        snapshot_root = Path(mkdtemp(prefix="agent-content-release-"))
        snapshot_root.chmod(0o700)
        markdown_snapshot = snapshot_root / "markdown"
        events_snapshot = snapshot_root / "operator_events"
        _copy_tree_no_follow(
            markdown_root,
            markdown_snapshot,
            "vps_sync_markdown_tree_invalid",
        )
        _copy_tree_no_follow(
            events_root,
            events_snapshot,
            "vps_sync_operator_events_tree_invalid",
        )

        markdown_count = _validate_markdown_tree(markdown_snapshot)
        event_count = _validate_operator_events_tree(
            events_snapshot, allow_empty=status == "completed_empty"
        )
        if _required_nonnegative_count(result, "document_count") != markdown_count:
            raise VpsSyncError("vps_sync_markdown_current_run_mismatch")
        if _required_nonnegative_count(result, "operator_event_count") != event_count:
            raise VpsSyncError("vps_sync_operator_events_current_run_mismatch")

        markdown_inventory = _build_tree_inventory(
            markdown_snapshot, "vps_sync_markdown_tree_invalid"
        )
        event_inventory = _build_tree_inventory(
            events_snapshot, "vps_sync_operator_events_tree_invalid"
        )
        if markdown_inventory != expected_markdown_inventory:
            raise VpsSyncError("vps_sync_markdown_current_run_mismatch")
        if event_inventory != expected_event_inventory:
            raise VpsSyncError("vps_sync_operator_events_current_run_mismatch")
        snapshot_result = dict(result)
        snapshot_result.pop("sync_receipt", None)
        snapshot_result["inbox_dir"] = markdown_snapshot
        snapshot_result["operator_events_dir"] = events_snapshot
        snapshot_result["sync_receipt"] = {
            "contract_version": "nazai-release-receipt.v1",
            "run_id": source_receipt["run_id"],
            "markdown_root": str(markdown_snapshot.resolve(strict=True)),
            "operator_events_root": str(events_snapshot.resolve(strict=True)),
            "operator_events_status": status,
            "markdown_inventory": markdown_inventory,
            "operator_events_inventory": event_inventory,
        }
        _preflight_current_release(snapshot_result)
        return snapshot_result, snapshot_root
    except VpsSyncError:
        if snapshot_root is not None:
            _cleanup_local_release_snapshot(snapshot_root, suppress_errors=True)
        raise
    except (OSError, RuntimeError, ValueError):
        if snapshot_root is not None:
            _cleanup_local_release_snapshot(snapshot_root, suppress_errors=True)
        raise VpsSyncError("vps_sync_local_snapshot_failed") from None


def _cleanup_local_release_snapshot(root: Path, *, suppress_errors: bool) -> None:
    try:
        node = root.lstat()
        if _stat_is_reparse(node) or _path_is_junction(root) or not stat.S_ISDIR(node.st_mode):
            raise OSError("unsafe snapshot root")
        rmtree(root)
    except OSError:
        if not suppress_errors:
            raise VpsSyncError("vps_sync_local_snapshot_cleanup_failed") from None


def _copy_tree_no_follow(source: Path, target: Path, invalid_code: str) -> None:
    before = _safe_tree_entries(source, invalid_code)
    target.mkdir(mode=0o700, parents=False, exist_ok=False)
    for relative, _, kind, _ in before:
        if kind == "directory":
            (target / Path(relative)).mkdir(mode=0o700, parents=True, exist_ok=False)
    for relative, path, kind, node in before:
        if kind != "file":
            continue
        destination = target / Path(relative)
        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        _copy_file_no_follow(path, destination, node, invalid_code)
    after = _safe_tree_entries(source, invalid_code)
    if _tree_entry_signatures(before) != _tree_entry_signatures(after):
        raise VpsSyncError("vps_sync_local_tree_changed")
    _safe_tree_entries(target, invalid_code)


def _copy_file_no_follow(
    source: Path,
    target: Path,
    expected: os.stat_result,
    invalid_code: str,
) -> None:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    try:
        descriptor = os.open(source, flags)
        opened = os.fstat(descriptor)
        if _stat_is_reparse(opened) or not stat.S_ISREG(opened.st_mode):
            raise VpsSyncError(invalid_code)
        if _node_signature(opened) != _node_signature(expected):
            raise VpsSyncError("vps_sync_local_tree_changed")
        with os.fdopen(descriptor, "rb", closefd=False) as source_file:
            with target.open("xb") as target_file:
                copyfileobj(source_file, target_file)
        closed_over = os.fstat(descriptor)
        if _node_signature(closed_over) != _node_signature(opened):
            raise VpsSyncError("vps_sync_local_tree_changed")
    except VpsSyncError:
        raise
    except OSError:
        raise VpsSyncError(invalid_code) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)


def _safe_tree_entries(
    root: Path, invalid_code: str,
) -> tuple[tuple[str, Path, str, os.stat_result], ...]:
    absolute_root = Path(os.path.abspath(os.fspath(root)))
    root_node = _safe_lstat(absolute_root, invalid_code)
    if not stat.S_ISDIR(root_node.st_mode):
        raise VpsSyncError(invalid_code)
    collected: list[tuple[str, Path, str, os.stat_result]] = []

    def visit(directory: Path, relative_parent: Path) -> None:
        before = _safe_lstat(directory, invalid_code)
        if not stat.S_ISDIR(before.st_mode):
            raise VpsSyncError(invalid_code)
        try:
            with os.scandir(directory) as scanner:
                children = sorted(
                    tuple(scanner), key=lambda entry: (entry.name.casefold(), entry.name)
                )
        except OSError:
            raise VpsSyncError(invalid_code) from None
        for child in children:
            if not child.name or any(ord(character) < 32 for character in child.name):
                raise VpsSyncError(invalid_code)
            relative = relative_parent / child.name
            relative_text = relative.as_posix()
            if _is_partial_transport_name(child.name):
                raise VpsSyncError(invalid_code)
            path = directory / child.name
            node = _safe_lstat(path, invalid_code)
            if stat.S_ISDIR(node.st_mode):
                collected.append((relative_text, path, "directory", node))
                visit(path, relative)
            elif stat.S_ISREG(node.st_mode):
                collected.append((relative_text, path, "file", node))
            else:
                raise VpsSyncError(invalid_code)
        after = _safe_lstat(directory, invalid_code)
        if _node_signature(before) != _node_signature(after):
            raise VpsSyncError("vps_sync_local_tree_changed")

    visit(absolute_root, Path())
    return tuple(collected)


def _safe_lstat(path: Path, invalid_code: str) -> os.stat_result:
    try:
        node = path.lstat()
    except OSError:
        raise VpsSyncError(invalid_code) from None
    if _stat_is_reparse(node) or _path_is_junction(path) or stat.S_ISLNK(node.st_mode):
        raise VpsSyncError(invalid_code)
    return node


def _stat_is_reparse(node: os.stat_result) -> bool:
    flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
    return bool(getattr(node, "st_file_attributes", 0) & flag)


def _path_is_junction(path: Path) -> bool:
    checker = getattr(path, "is_junction", None)
    if checker is None:
        return False
    try:
        return bool(checker())
    except OSError:
        return True


def _node_signature(node: os.stat_result) -> tuple[int, int, int, int, int]:
    # Windows may expose a different ``st_ctime_ns`` for lstat() and fstat()
    # on the same open file.  Identity, size and mtime are stable across both
    # calls; content integrity is checked independently by SHA-256.
    return (
        stat.S_IFMT(node.st_mode),
        int(getattr(node, "st_dev", 0)),
        int(getattr(node, "st_ino", 0)),
        int(node.st_size),
        int(getattr(node, "st_mtime_ns", int(node.st_mtime * 1_000_000_000))),
    )


def _tree_entry_signatures(
    entries: tuple[tuple[str, Path, str, os.stat_result], ...],
) -> tuple[tuple[str, str, tuple[int, int, int, int, int]], ...]:
    return tuple(
        (relative, kind, _node_signature(node))
        for relative, _, kind, node in entries
    )


def _is_partial_transport_name(name: str) -> bool:
    folded = name.casefold()
    return folded.startswith(".") and any(
        marker in folded for marker in ("staging", "backup", "partial")
    )


def _preflight_current_release(
    result: dict[str, object],
) -> tuple[Path, Path, dict[str, object]]:
    status = result.get("operator_events_status")
    if status == "failed":
        raise VpsSyncError("vps_sync_operator_events_current_run_failed")
    if status not in {"ready", "completed_empty"}:
        raise VpsSyncError("vps_sync_operator_events_status_invalid")
    receipt = _required_sync_receipt(result, status)
    expected_markdown_inventory = _required_receipt_inventory(
        receipt, "markdown_inventory", allow_empty=False
    )
    expected_event_inventory = _required_receipt_inventory(
        receipt,
        "operator_events_inventory",
        allow_empty=status == "completed_empty",
    )

    markdown_root = _required_result_path(result, "inbox_dir", "vps_sync_markdown_result_invalid")
    events_root = _required_result_path(
        result, "operator_events_dir", "vps_sync_operator_events_result_invalid"
    )
    try:
        markdown_resolved = markdown_root.resolve(strict=True)
        events_resolved = events_root.resolve(strict=True)
    except OSError as exc:
        raise VpsSyncError("vps_sync_local_root_invalid") from exc
    if (
        markdown_resolved == events_resolved
        or markdown_resolved in events_resolved.parents
        or events_resolved in markdown_resolved.parents
        or markdown_resolved.parent != events_resolved.parent
    ):
        raise VpsSyncError("vps_sync_local_roots_not_separate")
    if (
        receipt.get("markdown_root") != str(markdown_resolved)
        or receipt.get("operator_events_root") != str(events_resolved)
    ):
        raise VpsSyncError("vps_sync_current_run_root_mismatch")
    _reject_local_release_artifacts(markdown_root, events_root)

    markdown_count = _validate_markdown_tree(markdown_root)
    actual_markdown_inventory = _build_tree_inventory(
        markdown_root, "vps_sync_markdown_tree_invalid"
    )
    if (
        _required_nonnegative_count(result, "document_count") != markdown_count
        or markdown_count != len(expected_markdown_inventory)
        or actual_markdown_inventory != expected_markdown_inventory
    ):
        raise VpsSyncError("vps_sync_markdown_current_run_mismatch")

    expected_event_count = _required_nonnegative_count(result, "operator_event_count")
    actual_event_count = _validate_operator_events_tree(
        events_root, allow_empty=status == "completed_empty"
    )
    actual_event_inventory = _build_tree_inventory(
        events_root, "vps_sync_operator_events_tree_invalid"
    )
    if status == "ready" and (expected_event_count == 0 or actual_event_count == 0):
        raise VpsSyncError("vps_sync_operator_events_current_run_mismatch")
    if status == "completed_empty" and (expected_event_count != 0 or actual_event_count != 0):
        raise VpsSyncError("vps_sync_operator_events_current_run_mismatch")
    if (
        actual_event_count != expected_event_count
        or actual_event_count != len(expected_event_inventory)
        or actual_event_inventory != expected_event_inventory
    ):
        raise VpsSyncError("vps_sync_operator_events_current_run_mismatch")
    return markdown_resolved, events_resolved, receipt


def _required_sync_receipt(
    result: dict[str, object], status: str,
) -> dict[str, object]:
    receipt = result.get("sync_receipt")
    if not isinstance(receipt, dict):
        raise VpsSyncError("vps_sync_current_run_receipt_invalid")
    if (
        receipt.get("contract_version") != "nazai-release-receipt.v1"
        or not re.fullmatch(r"[0-9a-f]{32}", str(receipt.get("run_id") or ""))
        or not isinstance(receipt.get("markdown_root"), str)
        or not isinstance(receipt.get("operator_events_root"), str)
        or receipt.get("operator_events_status") != status
    ):
        raise VpsSyncError("vps_sync_current_run_receipt_invalid")
    return receipt


def _required_receipt_inventory(
    receipt: dict[str, object], key: str, *, allow_empty: bool,
) -> tuple[tuple[str, str], ...]:
    raw = receipt.get(key)
    if not isinstance(raw, tuple):
        raise VpsSyncError("vps_sync_current_run_receipt_invalid")
    inventory: list[tuple[str, str]] = []
    for item in raw:
        if not isinstance(item, tuple) or len(item) != 2:
            raise VpsSyncError("vps_sync_current_run_receipt_invalid")
        relative, digest = item
        if not isinstance(relative, str) or not isinstance(digest, str):
            raise VpsSyncError("vps_sync_current_run_receipt_invalid")
        path = Path(relative)
        if (
            path.is_absolute()
            or not path.parts
            or any(part in {"", ".", ".."} for part in path.parts)
            or any(ord(char) < 32 for char in relative)
            or "\\" in relative
            or path.as_posix() != relative
            or not re.fullmatch(r"[0-9a-f]{64}", digest)
        ):
            raise VpsSyncError("vps_sync_current_run_receipt_invalid")
        inventory.append((relative, digest))
    canonical = tuple(sorted(inventory, key=lambda item: (item[0].casefold(), item[0])))
    if tuple(inventory) != canonical or len({item[0] for item in inventory}) != len(inventory):
        raise VpsSyncError("vps_sync_current_run_receipt_invalid")
    if not canonical and not allow_empty:
        raise VpsSyncError("vps_sync_current_run_receipt_invalid")
    return canonical


def _build_tree_inventory(
    root: Path, invalid_code: str,
) -> tuple[tuple[str, str], ...]:
    entries = _safe_tree_entries(root, invalid_code)
    files = [entry for entry in entries if entry[2] == "file"]
    inventory: list[tuple[str, str]] = []
    for relative, path, _, node in files:
        inventory.append((relative, _hash_file_no_follow(path, node, invalid_code)))
    expected_directories: set[str] = set()
    for relative, _, _, _ in files:
        current = Path(relative).parent
        while current != Path("."):
            expected_directories.add(current.as_posix())
            current = current.parent
    actual_directories = {
        relative for relative, _, kind, _ in entries if kind == "directory"
    }
    if actual_directories != expected_directories:
        raise VpsSyncError(invalid_code)
    return tuple(sorted(inventory, key=lambda item: (item[0].casefold(), item[0])))


def _hash_file_no_follow(
    path: Path, expected: os.stat_result, invalid_code: str,
) -> str:
    flags = os.O_RDONLY | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)
    descriptor: int | None = None
    digest = hashlib.sha256()
    try:
        descriptor = os.open(path, flags)
        opened = os.fstat(descriptor)
        if _stat_is_reparse(opened) or _node_signature(opened) != _node_signature(expected):
            raise VpsSyncError("vps_sync_local_tree_changed")
        with os.fdopen(descriptor, "rb", closefd=False) as handle:
            for chunk in iter(lambda: handle.read(1024 * 1024), b""):
                digest.update(chunk)
        if _node_signature(os.fstat(descriptor)) != _node_signature(opened):
            raise VpsSyncError("vps_sync_local_tree_changed")
    except VpsSyncError:
        raise
    except OSError:
        raise VpsSyncError(invalid_code) from None
    finally:
        if descriptor is not None:
            os.close(descriptor)
    return digest.hexdigest()


def _reject_local_release_artifacts(markdown_root: Path, events_root: Path) -> None:
    parent = markdown_root.parent
    prefixes = tuple(
        f".{root.name}." for root in (markdown_root, events_root)
    )
    try:
        siblings = tuple(parent.iterdir())
    except OSError as exc:
        raise VpsSyncError("vps_sync_local_artifact_check_failed") from exc
    for sibling in siblings:
        folded = sibling.name.casefold()
        if any(folded.startswith(prefix.casefold()) for prefix in prefixes) and any(
            marker in folded for marker in ("staging", "backup", "partial")
        ):
            raise VpsSyncError("vps_sync_local_partial_artifact_present")


def _required_result_path(
    result: dict[str, object], key: str, reason_code: str,
) -> Path:
    raw = result.get(key)
    if not isinstance(raw, (str, os.PathLike)):
        raise VpsSyncError(reason_code)
    path = Path(os.path.abspath(os.fspath(raw)))
    node = _safe_lstat(path, reason_code)
    if not stat.S_ISDIR(node.st_mode):
        raise VpsSyncError(reason_code)
    return path


def _required_nonnegative_count(result: dict[str, object], key: str) -> int:
    value = result.get(key)
    if type(value) is not int or value < 0:
        raise VpsSyncError("vps_sync_current_run_count_invalid")
    return value


def _validate_markdown_tree(root: Path) -> int:
    entries = _safe_tree_entries(root, "vps_sync_markdown_tree_invalid")
    files = [path for _, path, kind, _ in entries if kind == "file"]
    if not files or any(path.suffix.casefold() != ".md" for path in files):
        raise VpsSyncError("vps_sync_markdown_tree_invalid")
    return len(files)


def _validate_operator_events_tree(root: Path, *, allow_empty: bool) -> int:
    entries = _safe_tree_entries(root, "vps_sync_operator_events_tree_invalid")
    files = [path for _, path, kind, _ in entries if kind == "file"]
    if not files:
        if allow_empty and not entries:
            return 0
        raise VpsSyncError("vps_sync_operator_events_tree_invalid")
    if allow_empty or any(path.suffix.casefold() != ".json" for path in files):
        raise VpsSyncError("vps_sync_operator_events_tree_invalid")

    for path in files:
        relative = path.relative_to(root)
        if len(relative.parts) != 3:
            raise VpsSyncError("vps_sync_operator_events_tree_invalid")
        project, target_date, filename = relative.parts
        if (
            not project
            or project.strip(" .") != project
            or len(project) > 120
            or any(ord(char) < 32 for char in project)
            or not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target_date)
        ):
            raise VpsSyncError("vps_sync_operator_events_tree_invalid")
        match = re.fullmatch(r"t-([0-9a-f]{12})\.json", filename)
        if not match:
            raise VpsSyncError("vps_sync_operator_events_tree_invalid")
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise VpsSyncError("vps_sync_operator_events_json_invalid") from exc
        if not isinstance(payload, dict) or payload.get("contract_version") != "operator-event-set.v1":
            raise VpsSyncError("vps_sync_operator_events_contract_invalid")
        if (
            payload.get("project") != project
            or payload.get("date") != target_date
            or payload.get("topic_id") != match.group(1)
            or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("source_hash") or ""))
            or not isinstance(payload.get("events"), list)
            or len(payload["events"]) != 1
        ):
            raise VpsSyncError("vps_sync_operator_events_metadata_invalid")
    return len(files)


def _validate_vps_host(value: object) -> str:
    if not isinstance(value, str) or value != value.strip() or value.startswith("-"):
        raise VpsSyncError("vps_sync_host_invalid")
    if not _VPS_HOST_RE.fullmatch(value):
        raise VpsSyncError("vps_sync_host_invalid")
    return value


def _validate_vps_path(value: object) -> str:
    if not isinstance(value, str) or value != value.strip():
        raise VpsSyncError("vps_sync_path_invalid")
    clean = value.rstrip("/")
    if clean == "/" or not _VPS_PATH_RE.fullmatch(clean):
        raise VpsSyncError("vps_sync_path_invalid")
    if any(part in {"", ".", ".."} for part in clean.split("/")[1:]):
        raise VpsSyncError("vps_sync_path_invalid")
    return clean


def _remote_release_paths(vps_path: str, transaction_id: str) -> dict[str, str]:
    parent = f"{vps_path}/content_inbox"
    return {
        "transaction_id": transaction_id,
        "parent": parent,
        "lock": f"{parent}/.nazai-release.lock",
        "lock_owner": f"{parent}/.nazai-release.lock/owner",
        "lock_safe": f"{parent}/.nazai-release.lock/safe-to-release",
        "lock_commit": f"{parent}/.nazai-release.lock/committed",
        "lock_commit_pending": (
            f"{parent}/.nazai-release.lock/committed.pending-{transaction_id}"
        ),
        "lock_work": f"{parent}/.nazai-release.lock/work",
        "markdown_target": f"{parent}/agent_content",
        "events_target": f"{parent}/operator_events",
        "markdown_staging": f"{parent}/.agent_content.staging-{transaction_id}",
        "events_staging": f"{parent}/.operator_events.staging-{transaction_id}",
        "markdown_old_hold": f"{parent}/.agent_content.old-hold-{transaction_id}",
        "events_old_hold": f"{parent}/.operator_events.old-hold-{transaction_id}",
        "markdown_recovery": f"{parent}/.agent_content.recovery-{transaction_id}",
        "events_recovery": f"{parent}/.operator_events.recovery-{transaction_id}",
        "markdown_failed_new": f"{parent}/.agent_content.failed-new-{transaction_id}",
        "events_failed_new": f"{parent}/.operator_events.failed-new-{transaction_id}",
    }


def _remote_tree_validation_commands(
    root_variable: str,
    inventory: tuple[tuple[str, str], ...],
) -> str:
    directories: set[str] = set()
    checks: list[str] = []
    for relative, digest in inventory:
        parts = relative.split("/")
        for index in range(1, len(parts)):
            directories.add("/".join(parts[:index]))
        quoted_relative = shlex.quote(relative)
        checks.extend(
            (
                f"test -f \"${root_variable}\"/{quoted_relative}",
                f"test ! -L \"${root_variable}\"/{quoted_relative}",
                f"test -s \"${root_variable}\"/{quoted_relative}",
                f"actual_hash=$(sha256sum -- \"${root_variable}\"/{quoted_relative})",
                f"test \"${{actual_hash%% *}}\" = {shlex.quote(digest)}",
            )
        )
    expected_entries = len(inventory) + len(directories)
    prefix = root_variable.replace("_", "-")
    common = [
        f"test -d \"${root_variable}\"",
        f"test ! -L \"${root_variable}\"",
        f"scan_all=\"$lock_work/{prefix}.all\"",
        f"scan_files=\"$lock_work/{prefix}.files\"",
        f"scan_links=\"$lock_work/{prefix}.links\"",
        f"scan_special=\"$lock_work/{prefix}.special\"",
        f"scan_count=\"$lock_work/{prefix}.count\"",
        f"if ! find \"${root_variable}\" -mindepth 1 -print > \"$scan_all\"; then exit 78; fi",
        f"if ! find \"${root_variable}\" -mindepth 1 -type f -print > \"$scan_files\"; then exit 78; fi",
        f"if ! find \"${root_variable}\" -mindepth 1 -type l -print > \"$scan_links\"; then exit 78; fi",
        (
            f"if ! find \"${root_variable}\" -mindepth 1 ! -type d ! -type f "
            "-print > \"$scan_special\"; then exit 78; fi"
        ),
        'test ! -s "$scan_links"',
        'test ! -s "$scan_special"',
        'if ! wc -l < "$scan_files" > "$scan_count"; then exit 78; fi',
        'if ! actual_file_count=$(tr -d "[:space:]" < "$scan_count"); then exit 78; fi',
        'case "$actual_file_count" in ""|*[!0-9]*) exit 78;; esac',
        f'test "$actual_file_count" -eq {len(inventory)}',
        'if ! wc -l < "$scan_all" > "$scan_count"; then exit 78; fi',
        'if ! actual_entry_count=$(tr -d "[:space:]" < "$scan_count"); then exit 78; fi',
        'case "$actual_entry_count" in ""|*[!0-9]*) exit 78;; esac',
        f'test "$actual_entry_count" -eq {expected_entries}',
    ]
    return "\n".join(common + checks)


_REMOTE_SWAP_PLAN: tuple[tuple[str, str], ...] = (
    ("hold", "markdown"),
    ("hold", "events"),
    ("install", "markdown"),
    ("install", "events"),
)
_REMOTE_ROLLBACK_PLAN: tuple[tuple[str, str], ...] = (
    ("park_new", "markdown"),
    ("park_new", "events"),
    ("restore_old", "markdown"),
    ("restore_old", "events"),
    ("verify_recovery", "markdown"),
    ("verify_recovery", "events"),
)


def _remote_subject_phase(subject: str) -> str:
    if subject == "markdown":
        return "markdown"
    if subject == "events":
        return "operator_events"
    raise ValueError("unknown remote release subject")


def _render_remote_swap_plan(plan: tuple[tuple[str, str], ...]) -> str:
    lines: list[str] = []
    for action, subject in plan:
        phase_subject = _remote_subject_phase(subject)
        if action == "hold":
            lines.extend(
                (
                    f"phase=hold_{phase_subject}",
                    f'if [ "${subject}_existed" -eq 1 ]; then',
                    f"    {subject}_hold_started=1",
                    f'    mv -- "${subject}_target" "${subject}_old_hold"',
                    f"    {subject}_held=1",
                    "fi",
                )
            )
        elif action == "install":
            lines.extend(
                (
                    f"phase=install_{phase_subject}",
                    f"{subject}_install_started=1",
                    f'mv -- "${subject}_staging" "${subject}_target"',
                    f"{subject}_installed=1",
                )
            )
        else:
            raise ValueError("unknown remote swap action")
    return "\n".join(lines)


def _render_remote_rollback_plan(plan: tuple[tuple[str, str], ...]) -> str:
    chunks: list[str] = []
    for action, subject in plan:
        if subject not in {"markdown", "events"}:
            raise ValueError("unknown remote rollback subject")
        if action == "park_new":
            chunks.append(
                f'''        if [ "${subject}_install_started" -eq 1 ] && exists "${subject}_target"; then
            if exists "${subject}_failed_new"; then
                rollback_failed=1
            elif mv -- "${subject}_target" "${subject}_failed_new"; then
                {subject}_parked=1
            else
                rollback_failed=1
            fi
        fi'''
            )
        elif action == "restore_old":
            chunks.append(
                f'''        if [ "${subject}_existed" -eq 0 ]; then
            if exists "${subject}_target"; then
                rollback_failed=1
            fi
        elif exists "${subject}_target"; then
            trees_equal "${subject}_target" "${subject}_recovery" || rollback_failed=1
        elif exists "${subject}_old_hold"; then
            if mv -- "${subject}_old_hold" "${subject}_target"; then
                {subject}_restored=1
            elif ! exists "${subject}_target"; then
                if cp -a -- "${subject}_recovery" "${subject}_target"; then
                    {subject}_restored=1
                else
                    rollback_failed=1
                fi
            else
                trees_equal "${subject}_target" "${subject}_recovery" || rollback_failed=1
            fi
        elif cp -a -- "${subject}_recovery" "${subject}_target"; then
            {subject}_restored=1
        else
            rollback_failed=1
        fi'''
            )
        elif action == "verify_recovery":
            chunks.append(
                f'''        if [ "${subject}_recovery_ready" -ne 1 ]; then rollback_failed=1; fi
        if [ "${subject}_existed" -eq 1 ]; then
            test -d "${subject}_target" && test ! -L "${subject}_target" || rollback_failed=1
            trees_equal "${subject}_target" "${subject}_recovery" || rollback_failed=1
        else
            if exists "${subject}_target"; then rollback_failed=1; fi
            validate_empty "${subject}_recovery" "{subject}-rollback-empty" || rollback_failed=1
        fi'''
            )
        else:
            raise ValueError("unknown remote rollback action")
    return "\n".join(chunks)


def _remote_release_transaction_script(
    remote: dict[str, str], receipt: dict[str, object],
) -> str:
    markdown_inventory = _required_receipt_inventory(
        receipt, "markdown_inventory", allow_empty=False
    )
    event_inventory = _required_receipt_inventory(
        receipt,
        "operator_events_inventory",
        allow_empty=receipt.get("operator_events_status") == "completed_empty",
    )
    assignments = "\n".join(
        f"{name}={shlex.quote(remote[key])}"
        for name, key in (
            ("transaction_id", "transaction_id"),
            ("lock", "lock"),
            ("lock_owner", "lock_owner"),
            ("lock_safe", "lock_safe"),
            ("lock_commit", "lock_commit"),
            ("lock_commit_pending", "lock_commit_pending"),
            ("lock_work", "lock_work"),
            ("markdown_target", "markdown_target"),
            ("events_target", "events_target"),
            ("markdown_staging", "markdown_staging"),
            ("events_staging", "events_staging"),
            ("markdown_old_hold", "markdown_old_hold"),
            ("events_old_hold", "events_old_hold"),
            ("markdown_recovery", "markdown_recovery"),
            ("events_recovery", "events_recovery"),
            ("markdown_failed_new", "markdown_failed_new"),
            ("events_failed_new", "events_failed_new"),
        )
    )
    run_id = shlex.quote(str(receipt["run_id"]))
    validate_markdown_staging = _remote_tree_validation_commands(
        "markdown_staging", markdown_inventory
    )
    validate_events_staging = _remote_tree_validation_commands(
        "events_staging", event_inventory
    )
    validate_markdown_target = _remote_tree_validation_commands(
        "markdown_target", markdown_inventory
    )
    validate_events_target = _remote_tree_validation_commands(
        "events_target", event_inventory
    )
    swap_commands = _render_remote_swap_plan(_REMOTE_SWAP_PLAN)
    rollback_commands = _render_remote_rollback_plan(_REMOTE_ROLLBACK_PLAN)
    return f"""set -eu
{assignments}
run_id={run_id}
committed=0
lock_created=0
swap_started=0
rollback_verified=0
markdown_existed=0
events_existed=0
markdown_held=0
events_held=0
markdown_hold_started=0
events_hold_started=0
markdown_install_started=0
events_install_started=0
markdown_installed=0
events_installed=0
markdown_parked=0
events_parked=0
markdown_restored=0
events_restored=0
markdown_recovery_ready=0
events_recovery_ready=0
exists() {{ [ -e "$1" ] || [ -L "$1" ]; }}
trees_equal() {{ diff -qr -- "$1" "$2" >/dev/null 2>&1; }}
validate_shape() {{
    shape_root=$1
    shape_label=$2
    shape_links="$lock_work/$shape_label.links"
    shape_special="$lock_work/$shape_label.special"
    test -d "$shape_root" && test ! -L "$shape_root" || return 1
    if ! find "$shape_root" -mindepth 1 -type l -print > "$shape_links"; then return 1; fi
    if ! find "$shape_root" -mindepth 1 ! -type d ! -type f -print > "$shape_special"; then return 1; fi
    test ! -s "$shape_links" && test ! -s "$shape_special"
}}
validate_empty() {{
    empty_root=$1
    empty_label=$2
    empty_all="$lock_work/$empty_label.all"
    empty_count_file="$lock_work/$empty_label.count"
    validate_shape "$empty_root" "$empty_label" || return 1
    if ! find "$empty_root" -mindepth 1 -print > "$empty_all"; then return 1; fi
    if ! wc -l < "$empty_all" > "$empty_count_file"; then return 1; fi
    if ! empty_count=$(tr -d '[:space:]' < "$empty_count_file"); then return 1; fi
    case "$empty_count" in ""|*[!0-9]*) return 1;; esac
    test "$empty_count" -eq 0
}}
owns_lock() {{
    [ -f "$lock_owner" ] && [ "$(cat -- "$lock_owner" 2>/dev/null)" = "$transaction_id" ]
}}
may_release_lock() {{
    [ -f "$lock_safe" ] && [ "$(cat -- "$lock_safe" 2>/dev/null)" = "$transaction_id" ]
}}
release_lock() {{
    owns_lock || return 1
    may_release_lock || return 1
    rm -f -- "$lock_commit_pending"
    rm -f -- "$lock_commit"
    rm -f -- "$lock_safe"
    rm -f -- "$lock_owner"
    rmdir -- "$lock"
}}
finish() {{
    rc=$?
    trap - EXIT HUP INT TERM
    set +e
    cleanup_failed=0
    rollback_failed=0
    if [ "$committed" -eq 0 ] && owns_lock && [ -f "$lock_commit" ]; then
        commit_value=$(cat -- "$lock_commit" 2>/dev/null)
        if [ "$commit_value" = "$transaction_id $run_id" ]; then
            committed=1
        fi
    fi
    if [ "$committed" -eq 1 ]; then
        rm -f -- "$lock_commit_pending" || cleanup_failed=1
        rm -rf -- "$markdown_old_hold" "$events_old_hold" || cleanup_failed=1
        rm -rf -- "$markdown_staging" "$events_staging" || cleanup_failed=1
        if owns_lock; then
            rm -rf -- "$lock_work" || cleanup_failed=1
        else
            cleanup_failed=1
        fi
    elif [ "$swap_started" -eq 1 ]; then
{rollback_commands}
        if [ "$rollback_failed" -ne 0 ]; then exit {_REMOTE_RECOVERY_REQUIRED_EXIT}; fi
        rollback_verified=1
        rm -rf -- "$markdown_staging" "$events_staging" || cleanup_failed=1
        if owns_lock; then
            rm -rf -- "$lock_work" || cleanup_failed=1
        else
            cleanup_failed=1
        fi
    else
        rm -rf -- "$markdown_staging" "$events_staging" || cleanup_failed=1
        rm -rf -- "$markdown_recovery" "$events_recovery" || cleanup_failed=1
        rm -rf -- "$markdown_old_hold" "$events_old_hold" || cleanup_failed=1
        rm -rf -- "$markdown_failed_new" "$events_failed_new" || cleanup_failed=1
        if [ "$lock_created" -eq 1 ]; then
            if owns_lock; then
                rm -rf -- "$lock_work" || cleanup_failed=1
            else
                cleanup_failed=1
            fi
        fi
    fi
    if [ "$cleanup_failed" -ne 0 ]; then
        if [ "$committed" -eq 1 ]; then exit {_REMOTE_COMMIT_CLEANUP_FAILED_EXIT}; fi
        exit {_REMOTE_CLEANUP_FAILED_EXIT}
    fi
    if owns_lock; then
        (umask 077; printf '%s\\n' "$transaction_id" > "$lock_safe") || cleanup_failed=1
        if [ "$cleanup_failed" -eq 0 ]; then release_lock || cleanup_failed=1; fi
    elif [ "$lock_created" -eq 1 ]; then
        cleanup_failed=1
    fi
    if [ "$cleanup_failed" -ne 0 ]; then
        if [ "$committed" -eq 1 ]; then exit {_REMOTE_COMMIT_CLEANUP_FAILED_EXIT}; fi
        exit {_REMOTE_CLEANUP_FAILED_EXIT}
    fi
    exit "$rc"
}}
for required_tool in sh find wc tr sha256sum diff cp mv rm mkdir rmdir cat; do
    command -v "$required_tool" >/dev/null 2>&1 || exit 77
done
trap finish EXIT
trap 'exit 129' HUP
trap 'exit 130' INT
trap 'exit 143' TERM
phase=lock
if exists "$lock"; then exit {_REMOTE_LOCK_BUSY_EXIT}; fi
if ! mkdir "$lock" 2>/dev/null; then exit 74; fi
lock_created=1
if ! (umask 077; printf '%s\\n' "$transaction_id" > "$lock_owner"); then exit 74; fi
if ! test "$(cat -- "$lock_owner")" = "$transaction_id"; then exit 74; fi
mkdir -- "$lock_work"
phase=preflight_staging
{validate_markdown_staging}
{validate_events_staging}
! exists "$markdown_old_hold"
! exists "$events_old_hold"
! exists "$markdown_recovery"
! exists "$events_recovery"
! exists "$markdown_failed_new"
! exists "$events_failed_new"
phase=recovery_markdown
if exists "$markdown_target"; then
    test -d "$markdown_target" && test ! -L "$markdown_target"
    markdown_existed=1
    cp -a -- "$markdown_target" "$markdown_recovery"
    validate_shape "$markdown_recovery" markdown-recovery
    trees_equal "$markdown_target" "$markdown_recovery"
else
    mkdir -- "$markdown_recovery"
    validate_empty "$markdown_recovery" markdown-recovery-empty
fi
markdown_recovery_ready=1
phase=recovery_operator_events
if exists "$events_target"; then
    test -d "$events_target" && test ! -L "$events_target"
    events_existed=1
    cp -a -- "$events_target" "$events_recovery"
    validate_shape "$events_recovery" events-recovery
    trees_equal "$events_target" "$events_recovery"
else
    mkdir -- "$events_recovery"
    validate_empty "$events_recovery" events-recovery-empty
fi
events_recovery_ready=1
swap_started=1
{swap_commands}
phase=verify_install
{validate_markdown_target}
{validate_events_target}
test -d "$markdown_recovery"
test -d "$events_recovery"
phase=committed
(umask 077; printf '%s %s\\n' "$transaction_id" "$run_id" > "$lock_commit_pending")
mv -- "$lock_commit_pending" "$lock_commit"
committed=1
"""


def _replace_tree_with_fresh_empty(root: Path) -> None:
    resolved = root.resolve(strict=False)
    if resolved == Path(resolved.anchor) or root.is_symlink():
        raise RuntimeError("unsafe empty OperatorEvent root")
    parent = root.parent
    parent.mkdir(parents=True, exist_ok=True)
    token = uuid4().hex
    staging = parent / f".{root.name}.staging-{token}"
    backup = parent / f".{root.name}.backup-{token}"
    staging.mkdir(parents=False, exist_ok=False)
    moved_old = False
    installed = False
    try:
        if root.exists():
            if not root.is_dir():
                raise RuntimeError("unsafe empty OperatorEvent root")
            root.replace(backup)
            moved_old = True
        staging.replace(root)
        installed = True
        if backup.exists():
            rmtree(backup)
    except Exception:
        if staging.exists():
            rmtree(staging)
        if installed and root.exists():
            rmtree(root)
        if moved_old and backup.exists() and not root.exists():
            backup.replace(root)
        raise


def _ssh_args(host: str) -> list[str]:
    return ["ssh", *_SSH_OPTIONS, host, "sh", "-s"]


def _scp_args(local_root: Path, host: str, remote_target: str) -> list[str]:
    return ["scp", *_SSH_OPTIONS, "-r", str(local_root), f"{host}:{remote_target}"]


def _run_remote_script(
    host: str,
    script: str,
    *,
    failure_code: str,
    lock_busy_exit: int | None = None,
    exit_reason_codes: dict[int, str] | None = None,
) -> None:
    _run_checked(
        _ssh_args(host),
        failure_code=failure_code,
        lock_busy_exit=lock_busy_exit,
        exit_reason_codes=exit_reason_codes,
        input_bytes=_remote_script_bytes(script, failure_code=failure_code),
    )


def _remote_script_bytes(script: str, *, failure_code: str) -> bytes:
    """Encode a remote shell payload without platform newline translation."""

    if "\x00" in script:
        raise VpsSyncError(failure_code)
    return script.replace("\r\n", "\n").replace("\r", "\n").encode("utf-8")


def _cleanup_remote_transaction(host: str, remote: dict[str, str]) -> None:
    transaction_id = shlex.quote(remote["transaction_id"])
    script = f"""set -u
transaction_id={transaction_id}
lock={shlex.quote(remote['lock'])}
lock_owner={shlex.quote(remote['lock_owner'])}
lock_safe={shlex.quote(remote['lock_safe'])}
markdown_staging={shlex.quote(remote['markdown_staging'])}
events_staging={shlex.quote(remote['events_staging'])}
exists() {{ [ -e "$1" ] || [ -L "$1" ]; }}
rm -rf -- "$markdown_staging" "$events_staging"
if ! exists "$lock"; then
    exit 0
fi
owner_value=
safe_value=
if [ -f "$lock_owner" ]; then owner_value=$(cat -- "$lock_owner" 2>/dev/null); fi
if [ -f "$lock_safe" ]; then safe_value=$(cat -- "$lock_safe" 2>/dev/null); fi
if [ -n "$owner_value" ] && [ "$owner_value" != "$transaction_id" ]; then
    exit 0
fi
if [ "$owner_value" = "$transaction_id" ] && [ "$safe_value" = "$transaction_id" ]; then
    rm -f -- "$lock_safe"
    rm -f -- "$lock_owner"
    rmdir -- "$lock"
fi
    """
    try:
        _run_remote_script(
            host,
            script,
            failure_code="vps_sync_remote_cleanup_failed",
        )
    except VpsSyncError:
        pass


def _run_checked(
    args: list[str],
    *,
    failure_code: str,
    lock_busy_exit: int | None = None,
    exit_reason_codes: dict[int, str] | None = None,
    input_text: str | None = None,
    input_bytes: bytes | None = None,
) -> None:
    if input_text is not None and input_bytes is not None:
        raise ValueError("subprocess input must be text or bytes, not both")
    kwargs: dict[str, object] = {
        "capture_output": True,
        "check": False,
        "timeout": _REMOTE_COMMAND_TIMEOUT_SECONDS,
    }
    if input_bytes is not None:
        # Keep generated POSIX scripts in binary mode: Windows text mode turns
        # LF into CRLF before ssh forwards stdin to `sh -s`.
        kwargs["input"] = input_bytes
    else:
        kwargs["text"] = True
        kwargs["encoding"] = "utf-8"
        kwargs["errors"] = "replace"
    if input_text is None and input_bytes is None:
        kwargs["stdin"] = subprocess.DEVNULL
    elif input_text is not None:
        kwargs["input"] = input_text
    try:
        result = subprocess.run(args, **kwargs)
    except subprocess.TimeoutExpired:
        raise VpsSyncError("vps_sync_remote_timeout") from None
    except OSError:
        raise VpsSyncError("vps_sync_remote_unavailable") from None
    if lock_busy_exit is not None and result.returncode == lock_busy_exit:
        raise VpsSyncError("vps_sync_remote_lock_busy")
    if exit_reason_codes and result.returncode in exit_reason_codes:
        raise VpsSyncError(exit_reason_codes[result.returncode])
    if result.returncode != 0:
        raise VpsSyncError(failure_code)


def _content_projects(config: dict) -> list[dict]:
    """Include dynamically discovered Codex project folders in content runs."""
    projects = configured_projects(config)
    known_ai_dirs = {str(Path(project["ai_logs_dir"]).resolve()).casefold() for project in projects}
    central_root = Path(config.get("ai_logs_dir") or "ai-logs")
    if not central_root.exists():
        return projects

    for folder in sorted(
        (path for path in central_root.iterdir() if path.is_dir() and not path.is_symlink()),
        key=lambda path: path.name.casefold(),
    ):
        key = str(folder.resolve()).casefold()
        if key in known_ai_dirs:
            continue
        projects.append(
            {
                "name": folder.name,
                "repo_path": folder.name,
                "notes_dir": str(folder / ".no-manual-notes"),
                "ai_logs_dir": str(folder),
                "terminal_logs_dir": str(folder / ".no-terminal-logs"),
            }
        )
        known_ai_dirs.add(key)
    return projects


def _unique_paths(paths: list[Path]) -> list[Path]:
    seen = set()
    unique = []
    for path in paths:
        resolved = str(path.resolve())
        if resolved in seen:
            continue
        seen.add(resolved)
        unique.append(path)
    return unique


def build_live_payload(config: dict, target_date: str) -> dict:
    projects = _content_projects(config)
    project_payloads = []
    for project in projects:
        result = _build_pack_for_project(project, config, target_date)
        pack = result["pack"]
        if not _pack_has_real_events(pack):
            continue
        project_payloads.append(
            {
                "name": result["project"]["name"],
                "path": result["project"]["repo_path"],
                "terminal_logs": pack["raw_context"]["terminal_logs"],
                "events": pack["events"],
                "hooks": pack["hooks"],
                "best_format": pack["best_format"],
                "do_not_publish": pack["do_not_publish"],
            }
        )
    return {
        "updated_at": now_local_iso(),
        "date": target_date,
        "projects": project_payloads,
    }


def _default_telegram_message(kind: str, target_date: str) -> str:
    if kind == "daily":
        return f"Готов дневной content pack за {target_date}."
    if kind == "live":
        return f"Свежая live-летопись за {target_date}."
    return "Отчет от Agent Content."


def _build_pack_for_project(project: dict, config: dict, target_date: str) -> dict:
    privacy = PrivacyScanner()
    notes = NotesCollector(project["notes_dir"]).collect(target_date)
    ai_logs = AiLogsCollector(project["ai_logs_dir"]).collect(target_date)
    terminal_logs = TerminalCollector(project["terminal_logs_dir"]).collect(target_date)

    notes = _apply_aliases_notes(notes, config.get("project_aliases", {}))
    ai_logs = _apply_aliases_notes(ai_logs, config.get("project_aliases", {}))
    terminal_logs = _apply_aliases_terminal_logs(terminal_logs, config.get("project_aliases", {}))

    notes, note_findings = _mask_notes(notes, privacy)
    ai_logs, log_findings = _mask_notes(ai_logs, privacy)
    terminal_logs, terminal_findings = _mask_terminal_logs(terminal_logs, privacy)
    findings = note_findings + log_findings + terminal_findings

    events = EventAnalyzer().analyze(notes, ai_logs, terminal_logs)
    scorer = ContentPotentialScorer()
    tone_selector = ToneSelector(config.get("recent_tones", []))
    scored_events = []
    for event in events:
        event = scorer.score(event)
        event.tone = tone_selector.select(event)
        scored_events.append(event)

    pack = ContentPackGenerator(tone_selector, int(config["story_count"])).generate(
        target_date=target_date,
        notes=notes,
        ai_logs=ai_logs,
        terminal_logs=terminal_logs,
        events=scored_events,
        privacy_findings=findings,
    )
    pack["project"] = {"name": project["name"], "path": project["repo_path"]}
    return {"project": project, "pack": pack}


def _select_daily_pack(target_date: str, project_results: list[dict]) -> dict | None:
    meaningful = [
        result
        for result in project_results
        if _pack_has_real_events(result["pack"])
    ]
    if not meaningful:
        return None
    if len(meaningful) == 1:
        return meaningful[0]["pack"]
    return _combine_daily_packs(target_date, meaningful)


def _pack_has_real_events(pack: dict) -> bool:
    return any(
        not str(event.get("source", "")).startswith("system:")
        for event in pack.get("events", [])
    )


def _combine_daily_packs(target_date: str, project_results: list[dict]) -> dict:
    packs = [item["pack"] for item in project_results]
    all_events = [event for pack in packs for event in pack["events"]]
    meaningful_events = [event for event in all_events if not str(event.get("source", "")).startswith("system:")]
    visible_events = meaningful_events or all_events
    primary = max(
        visible_events,
        key=lambda event: event["score"],
    )
    primary_pack = next(
        (pack for pack in packs if any(event == primary for event in pack["events"])),
        packs[0],
    )
    safety_items: list[dict] = []
    safety_seen: set[tuple] = set()
    for item in (entry for pack in packs for entry in pack["do_not_publish"]):
        key = tuple(sorted((str(name), str(value)) for name, value in item.items()))
        if key in safety_seen:
            continue
        safety_seen.add(key)
        safety_items.append(item)

    return {
        "date": target_date,
        "recap": {
            "what_happened": f"Собрано проектов: {len(packs)}. Главный сигнал: {primary['title']}.",
            "main_story": primary["summary"],
            "main_thought": "Несколько рабочих окон можно свести в одну редакторскую картину дня.",
        },
        "best_format": primary_pack["best_format"],
        "events": visible_events,
        "stories": primary_pack["stories"],
        "reels": primary_pack["reels"],
        "post": primary_pack["post"],
        "hooks": primary_pack["hooks"],
        "do_not_publish": safety_items,
        "raw_context": {
            "projects": [
                {
                    "name": pack["project"]["name"],
                    "path": pack["project"]["path"],
                    "notes": pack["raw_context"]["notes"],
                    "ai_logs": pack["raw_context"]["ai_logs"],
                    "terminal_logs": pack["raw_context"]["terminal_logs"],
                }
                for pack in packs
            ]
        },
    }


def _mask_notes(notes: list[Note], privacy: PrivacyScanner) -> tuple[list[Note], list]:
    masked_notes: list[Note] = []
    all_findings = []
    for note in notes:
        masked_text, findings = privacy.scan_and_mask(note.text, note.path)
        masked_title, title_findings = privacy.scan_and_mask(note.title, note.path)
        masked_notes.append(Note(path=note.path, title=masked_title, text=masked_text))
        all_findings.extend(findings)
        all_findings.extend(title_findings)
    return masked_notes, all_findings


def _mask_terminal_logs(logs: list[TerminalLog], privacy: PrivacyScanner) -> tuple[list[TerminalLog], list]:
    masked_logs: list[TerminalLog] = []
    all_findings = []
    for log in logs:
        masked_text, findings = privacy.scan_and_mask(log.text, log.path)
        masked_title, title_findings = privacy.scan_and_mask(log.title, log.path)
        masked_logs.append(TerminalLog(path=log.path, title=masked_title, text=masked_text))
        all_findings.extend(findings)
        all_findings.extend(title_findings)
    return masked_logs, all_findings


def _apply_aliases_notes(notes: list[Note], aliases: dict[str, str]) -> list[Note]:
    if not aliases:
        return notes
    return [
        Note(
            path=note.path,
            title=_replace_aliases(note.title, aliases),
            text=_replace_aliases(note.text, aliases),
        )
        for note in notes
    ]


def _apply_aliases_terminal_logs(logs: list[TerminalLog], aliases: dict[str, str]) -> list[TerminalLog]:
    if not aliases:
        return logs
    return [
        TerminalLog(
            path=log.path,
            title=_replace_aliases(log.title, aliases),
            text=_replace_aliases(log.text, aliases),
        )
        for log in logs
    ]


def _replace_aliases(value, aliases: dict[str, str]):
    if isinstance(value, str):
        result = value
        for secret, public in aliases.items():
            result = result.replace(secret, public)
        return result
    if isinstance(value, list):
        return [_replace_aliases(item, aliases) for item in value]
    if isinstance(value, dict):
        return {key: _replace_aliases(item, aliases) for key, item in value.items()}
    return value
