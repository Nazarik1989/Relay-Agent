from __future__ import annotations

import argparse
import json
import os
import re
import subprocess
import time
from dataclasses import asdict
from datetime import datetime
from pathlib import Path

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
        return 0

    print(f"Restoring dates from Codex chat summaries and notes: {', '.join(dates)}")
    for target_date in dates:
        packs = [_build_pack_for_project(project, config, target_date) for project in _content_projects(config)]
        pack = _select_daily_pack(target_date, packs)
        if pack is None:
            print(f"skipped {target_date}: no real source material")
            continue
        md_path = outputs_dir / f"{target_date}-content-pack.md"
        json_path = outputs_dir / f"{target_date}-content-pack.json"
        pick_path = outputs_dir / f"{target_date}-today-pick.md"
        MarkdownWriter().write(md_path, pack)
        JsonWriter().write(json_path, pack)
        TodayPickWriter().write(pick_path, pack)
        print(f"rebuilt {target_date}: {md_path}")

    if args.sync_vps:
        result, topics = _write_topic_inbox(summaries)
        host = args.vps_host or os.getenv("NAZAI_VPS_HOST")
        if not host:
            raise RuntimeError("VPS sync requires --vps-host or NAZAI_VPS_HOST")
        path = args.vps_path or os.getenv("NAZAI_VPS_PATH") or "/opt/naz-ai-bot"
        _sync_nazai_inbox_to_vps(Path(result["inbox_dir"]), host, path)
        _sync_operator_events_result_to_vps(result, host, path)
        print(f"synced {len(topics)} topics: {host}:{path}/content_inbox/agent_content")
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
        return 0
    result, topics = _write_topic_inbox(summaries)
    selected = [topic for topic in topics if topic.date == args.date]
    print(f"Тематический архив передан в Naz_Ai_Bot inbox: {result['inbox_dir']}")
    print(
        f"Проектов: {result['project_count']}; дат: {result['date_count']}; "
        f"рассказов: {result['document_count']} (за {args.date}: {len(selected)})"
    )
    if args.sync_vps:
        host = args.vps_host or os.getenv("NAZAI_VPS_HOST")
        if not host:
            raise RuntimeError("VPS sync requires --vps-host or NAZAI_VPS_HOST")
        path = args.vps_path or os.getenv("NAZAI_VPS_PATH") or "/opt/naz-ai-bot"
        _sync_nazai_inbox_to_vps(Path(result["inbox_dir"]), host, path)
        _sync_operator_events_result_to_vps(result, host, path)
        print(f"Тематический inbox скопирован на VPS: {host}:{path}/content_inbox/agent_content")
    return 0


def run_export_nazai_inbox_all(args: argparse.Namespace) -> int:
    config = load_config(args.config)
    summaries = _refresh_codex_summaries(config, args)
    if not summaries:
        print("Не найдено пользовательской истории Codex для экспорта.")
        return 0

    result, topics = _write_topic_inbox(summaries)
    print(f"Полная тематическая история передана в Naz_Ai_Bot inbox: {result['inbox_dir']}")
    print(
        f"Проектов: {result['project_count']}; дат: {result['date_count']}; "
        f"текстовых рассказов: {result['document_count']}"
    )

    if args.sync_vps:
        host = args.vps_host or os.getenv("NAZAI_VPS_HOST")
        if not host:
            raise RuntimeError("VPS sync requires --vps-host or NAZAI_VPS_HOST")
        path = args.vps_path or os.getenv("NAZAI_VPS_PATH") or "/opt/naz-ai-bot"
        _sync_nazai_inbox_to_vps(Path(result["inbox_dir"]), host, path)
        _sync_operator_events_result_to_vps(result, host, path)
        print(f"Тематический inbox скопирован на VPS: {host}:{path}/content_inbox/agent_content")
    return 0


def _write_topic_inbox(summaries: list) -> tuple[dict[str, object], list]:
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
    if event_documents is not None:
        try:
            event_result = operator_events.write_documents(event_documents)
        except Exception:
            pass
        else:
            result["operator_events_status"] = "ready"
            result["operator_events_reason_code"] = None
            result["operator_event_count"] = event_result["document_count"]
    return result, stories


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


def _sync_nazai_inbox_to_vps(inbox_dir: Path, host: str, vps_path: str) -> None:
    if not inbox_dir.exists() or not inbox_dir.is_dir():
        raise RuntimeError(f"Inbox directory not found: {inbox_dir}")
    files = [path for path in inbox_dir.rglob("*") if path.is_file()]
    if not files or any(path.suffix.casefold() != ".md" or path.is_symlink() for path in files):
        raise RuntimeError("Refusing VPS sync: inbox must contain regular Markdown documents only")

    remote_parent = f"{vps_path.rstrip('/')}/content_inbox"
    remote_target = f"{remote_parent}/agent_content"
    remote_staging = f"{remote_parent}/.agent_content.staging"
    remote_backup = f"{remote_parent}/.agent_content.backup"
    _run_checked(["ssh", host, f"mkdir -p {remote_parent} && rm -rf {remote_staging} {remote_backup}"])
    _run_checked(["scp", "-r", str(inbox_dir), f"{host}:{remote_staging}"])
    _run_checked(
        [
            "ssh",
            host,
            (
                f"test -d {remote_staging} && "
                f"if test -d {remote_target}; then mv {remote_target} {remote_backup}; fi && "
                f"mv {remote_staging} {remote_target} && rm -rf {remote_backup}"
            ),
        ]
    )


def _sync_operator_events_result_to_vps(result: dict[str, object], host: str, vps_path: str) -> None:
    if result.get("operator_events_status") != "ready":
        return
    raw_root = result.get("operator_events_dir")
    if raw_root is None:
        raise RuntimeError("OperatorEvent export is ready but its root is missing")
    _sync_operator_events_to_vps(Path(raw_root), host, vps_path)


def _sync_operator_events_to_vps(events_dir: Path, host: str, vps_path: str) -> None:
    if not events_dir.exists() or not events_dir.is_dir():
        raise RuntimeError(f"OperatorEvent directory not found: {events_dir}")
    entries = list(events_dir.rglob("*"))
    if any(path.is_symlink() for path in entries):
        raise RuntimeError("Refusing VPS sync: OperatorEvent tree must not contain symlinks")
    files = [path for path in entries if path.is_file()]
    if not files or any(path.suffix.casefold() != ".json" for path in files):
        raise RuntimeError("Refusing VPS sync: OperatorEvent tree must contain regular JSON documents only")

    for path in files:
        relative = path.relative_to(events_dir)
        if len(relative.parts) != 3:
            raise RuntimeError(f"Refusing VPS sync: unsafe OperatorEvent path {relative.as_posix()}")
        project, target_date, filename = relative.parts
        if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target_date):
            raise RuntimeError(f"Refusing VPS sync: unsafe OperatorEvent date {target_date}")
        match = re.fullmatch(r"t-([0-9a-f]{12})\.json", filename)
        if not match:
            raise RuntimeError(f"Refusing VPS sync: unsafe OperatorEvent filename {filename}")
        try:
            payload = json.loads(path.read_text(encoding="utf-8", errors="strict"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            raise RuntimeError(f"Refusing VPS sync: invalid OperatorEvent JSON {filename}") from exc
        if not isinstance(payload, dict) or payload.get("contract_version") != "operator-event-set.v1":
            raise RuntimeError(f"Refusing VPS sync: invalid OperatorEvent contract {filename}")
        if (
            payload.get("project") != project
            or payload.get("date") != target_date
            or payload.get("topic_id") != match.group(1)
            or not re.fullmatch(r"[0-9a-f]{64}", str(payload.get("source_hash") or ""))
            or not isinstance(payload.get("events"), list)
            or len(payload["events"]) != 1
        ):
            raise RuntimeError(f"Refusing VPS sync: mismatched OperatorEvent metadata {filename}")

    remote_parent = f"{vps_path.rstrip('/')}/content_inbox"
    remote_target = f"{remote_parent}/operator_events"
    remote_staging = f"{remote_parent}/.operator_events.staging"
    remote_backup = f"{remote_parent}/.operator_events.backup"
    _run_checked(["ssh", host, f"mkdir -p {remote_parent} && rm -rf {remote_staging} {remote_backup}"])
    _run_checked(["scp", "-r", str(events_dir), f"{host}:{remote_staging}"])
    _run_checked(
        [
            "ssh",
            host,
            (
                f"test -d {remote_staging} && "
                f"if test -d {remote_target}; then mv {remote_target} {remote_backup}; fi && "
                f"mv {remote_staging} {remote_target} && rm -rf {remote_backup}"
            ),
        ]
    )


def _sync_nazai_inbox_day_to_vps(day_dir: Path, target_date: str, host: str, vps_path: str) -> None:
    if not day_dir.exists() or not day_dir.is_dir():
        raise RuntimeError(f"Inbox day directory not found: {day_dir}")
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", target_date):
        raise RuntimeError(f"Unsafe inbox date for VPS sync: {target_date}")
    remote_parent = f"{vps_path.rstrip('/')}/content_inbox/agent_content"
    remote_day = f"{remote_parent}/{target_date}"
    _run_checked(["ssh", host, f"mkdir -p {remote_parent} && rm -rf {remote_day}"])
    _run_checked(["scp", "-r", str(day_dir), f"{host}:{remote_parent}/"])


def _run_checked(args: list[str]) -> None:
    result = subprocess.run(args, text=True, encoding="utf-8", errors="replace", capture_output=True, check=False)
    if result.returncode != 0:
        raise RuntimeError(
            f"Command failed: {' '.join(args)}\n"
            f"stdout:\n{result.stdout}\n"
            f"stderr:\n{result.stderr}"
        )


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
