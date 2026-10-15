"""Noninteractive health journal commands with consistent JSON envelopes."""

import argparse
import json
import sys
from pathlib import Path

from . import __version__
from .core.apple import import_apple
from .core.common import LogError, failure, settings, success, today
from .core.store import Store


class Parser(argparse.ArgumentParser):
    def error(self, message):
        raise LogError("INVALID_ARGUMENT", message)


def parser():
    root = Parser(prog="thl", description="Private offline travel health journal")
    root.add_argument("--data-dir")
    root.add_argument("--config")
    for flag in ["json", "quiet", "yes", "progress"]:
        root.add_argument("--" + flag, action="store_true")
    root.add_argument("--version", action="version", version=__version__)
    commands = root.add_subparsers(dest="command", required=True, parser_class=Parser)
    log = commands.add_parser("log")
    log.add_argument("values", nargs="+")
    log.add_argument("--date")
    log.add_argument("--source", default="manual")
    log.add_argument("--note", default="")
    days = commands.add_parser("day")
    days.add_argument("status", choices=["home", "trip", "transit"])
    days.add_argument("--date")
    days.add_argument("--city", default="")
    entries = commands.add_parser("entries")
    entries.add_argument("--start")
    entries.add_argument("--end")
    habits = commands.add_parser("habit").add_subparsers(
        dest="action", required=True, parser_class=Parser
    )
    add = habits.add_parser("add")
    add.add_argument("name")
    add.add_argument("--metric")
    add.add_argument("--op", choices=["gte", "lte", "eq", "before"], default="gte")
    add.add_argument("--target")
    add.add_argument("--start")
    check = habits.add_parser("check")
    check.add_argument("name")
    check.add_argument("--date")
    check.add_argument("--undone", action="store_true")
    listing = habits.add_parser("list")
    listing.add_argument("--days", type=int, default=30)
    listing.add_argument("--as-of")
    commands.add_parser("templates")
    meds = commands.add_parser("med").add_subparsers(
        dest="action", required=True, parser_class=Parser
    )
    add = meds.add_parser("add")
    add.add_argument("name")
    add.add_argument("--schedule", required=True)
    add.add_argument("--start", required=True)
    add.add_argument("--end")
    take = meds.add_parser("taken")
    take.add_argument("name")
    take.add_argument("--date")
    take.add_argument("--time", required=True)
    reminders = commands.add_parser("reminder").add_subparsers(
        dest="action", required=True, parser_class=Parser
    )
    add = reminders.add_parser("add")
    add.add_argument("title")
    add.add_argument("--date", required=True)
    done = reminders.add_parser("done")
    done.add_argument("id", type=int)
    for name in ["stats", "compare"]:
        command = commands.add_parser(name)
        command.add_argument("--days", type=int, default=30)
        command.add_argument("--as-of")
    due = commands.add_parser("due")
    due.add_argument("--as-of")
    due.add_argument("--at")
    importing = commands.add_parser("import")
    importing.add_argument("items", nargs="+", help="PATH, apple-health XML, or trips CSV")
    exporting = commands.add_parser("export")
    exporting.add_argument("--format", choices=["json", "csv"], required=True)
    exporting.add_argument("--kind", choices=["archive", "entries"], default="archive")
    exporting.add_argument("--out", required=True)
    exporting.add_argument("--no-bom", action="store_true")
    backup = commands.add_parser("backup")
    backup.add_argument("--out")
    report = commands.add_parser("report")
    report.add_argument("--week", required=True)
    report.add_argument("--out")
    serving = commands.add_parser("serve")
    serving.add_argument("--host", default="127.0.0.1")
    serving.add_argument("--port", type=int, default=8915)
    serving.add_argument("--allow-lan", action="store_true")
    commands.add_parser("mcp")
    return root


def globals_first(argv):
    front, rest = [], []
    index = 0
    while index < len(argv):
        arg = argv[index]
        if arg == "--":
            rest.extend(argv[index:])
            break
        if arg in {"--json", "--quiet", "--progress", "--yes", "--version"}:
            front.append(arg)
        elif arg in {"--data-dir", "--config"}:
            if index + 1 >= len(argv):
                raise LogError("INVALID_ARGUMENT", f"{arg} needs a value")
            front.extend(argv[index : index + 2])
            index += 1
        elif arg.startswith(("--data-dir=", "--config=")):
            front.append(arg)
        else:
            rest.append(arg)
        index += 1
    return front + rest


TEMPLATES = [
    {"name": "Example step goal", "rule": {"metric": "steps", "op": "gte", "target": 8000}},
    {"name": "Example bedtime", "rule": {"metric": "bedtime", "op": "before", "target": "23:30"}},
    {"name": "Alcohol-free check-in", "rule": {"metric": "alcohol", "op": "eq", "target": 0}},
    {"name": "Stretching check-in", "rule": {"type": "manual"}, "minutes": 10},
    {
        "name": "Hotel room movement example",
        "rule": {"type": "manual"},
        "minutes": 15,
        "plan": [
            {"activity": "Easy warm-up", "minutes": 3},
            {"activity": "Gentle mobility", "minutes": 4},
            {"activity": "Comfortable bodyweight movements", "minutes": 5},
            {"activity": "Relaxed stretch", "minutes": 3},
        ],
        "note": "Optional journal template; adapt to your ability. Not an exercise prescription.",
    },
]


def dispatch(args, store):
    current = today(store.config)
    if args.command == "log":
        values = {}
        for pair in args.values:
            if "=" not in pair:
                raise LogError("INVALID_ARGUMENT", "Metrics use key=value")
            key, value = pair.split("=", 1)
            if key in values:
                raise LogError("INVALID_ARGUMENT", "Repeated metric key")
            values[key] = value
        return store.log(args.date or current, values, args.source, args.note)
    if args.command == "day":
        return store.set_day(args.date or current, args.status, args.city)
    if args.command == "entries":
        return store.records(args.start, args.end)
    if args.command == "habit":
        if args.action == "add":
            rule = (
                {"metric": args.metric, "op": args.op, "target": args.target}
                if args.metric
                else None
            )
            if args.target is not None and not args.metric:
                raise LogError("INVALID_RULE", "--target requires --metric")
            return store.add_habit(args.name, rule, args.start)
        if args.action == "check":
            return store.check_habit(args.name, args.date or current, not args.undone)
        return store.habit_status(args.days, args.as_of)
    if args.command == "templates":
        return TEMPLATES
    if args.command == "med":
        if args.action == "add":
            return store.add_med(args.name, args.schedule.split(","), args.start, args.end)
        return store.take_med(args.name, args.date or current, args.time)
    if args.command == "reminder":
        return (
            store.add_reminder(args.title, args.date)
            if args.action == "add"
            else store.complete_reminder(args.id)
        )
    if args.command in {"stats", "compare"}:
        return getattr(store, args.command)(args.days, args.as_of)
    if args.command == "due":
        return store.due(args.as_of, args.at)
    if args.command == "import":
        if len(args.items) == 1:
            return store.import_file(args.items[0])
        if len(args.items) == 2 and args.items[0] == "apple-health":
            progress = (
                (lambda count: print(f"Parsed {count} Apple records", file=sys.stderr))
                if args.progress and not args.quiet
                else None
            )
            return import_apple(store, args.items[1], progress)
        if len(args.items) == 2 and args.items[0] == "trips":
            return store.import_trips(args.items[1])
        raise LogError(
            "INVALID_ARGUMENT", "Use import PATH, import apple-health XML or import trips CSV"
        )
    if args.command == "export":
        return store.export(args.format, args.out, args.kind, args.yes, not args.no_bom)
    if args.command == "backup":
        return store.backup(args.out)
    if args.command == "report":
        result = store.report(args.week)
        if args.out:
            target = Path(args.out)
            store.protect_output(target)
            if target.exists() and not args.yes:
                raise LogError("EXISTS", "Report exists; use --yes to replace it")
            target.parent.mkdir(parents=True, exist_ok=True)
            with target.open("w" if args.yes else "x", encoding="utf-8") as file:
                file.write(result["markdown"])
            if store.config["private_permissions"]:
                target.chmod(0o600)
            result["path"] = str(target.absolute())
        return result
    raise LogError("INVALID_ARGUMENT", "Unknown command")


def main(argv=None):
    args, store = None, None
    try:
        args = parser().parse_args(globals_first(list(sys.argv[1:] if argv is None else argv)))
        root, config = settings(args.data_dir, args.config)
        if args.command == "mcp":
            from .mcp_server import create_server

            create_server(root, config).run(transport="stdio")
            return 0
        if args.command == "serve":
            if args.host not in {"127.0.0.1", "localhost", "::1"} and not args.allow_lan:
                raise LogError(
                    "LAN_DISABLED",
                    "Non-loopback binding requires --allow-lan and explicit allowed_hosts",
                )
            import uvicorn

            from .web.server import create_app

            uvicorn.run(
                create_app(root, config),
                host=args.host,
                port=args.port,
                log_level="warning" if args.quiet else "info",
                access_log=False,
            )
            return 0
        store = Store(root, config)
        result, status = success(dispatch(args, store)), 0
    except Exception as exc:
        result, status = failure(exc)
    finally:
        if store:
            store.close()
    print(json.dumps(result, ensure_ascii=False, indent=None if args and args.json else 2))
    return status


if __name__ == "__main__":
    raise SystemExit(main())
