import argparse
import hashlib
import json
import re
from pathlib import Path

from sqlalchemy import delete

from .channels import CHANNELS, set_source, source_rules
from .connectors import watch
from .db import Event, Job, Reservation, make_sessions
from .demo import seed
from .documents import read_stock
from .ingest import EXPECTED, amount, date, parse_file, preview, write_rows
from .monitoring import DEFAULT_INTERVAL, MARKETPLACES, save_watch, set_enabled
from .replay import replay
from .runtime import bootstrap, run_local
from .store import canonical, digest
from .value import dec
from .worker import run


def mode_flag(command, default="demo"):
    command.add_argument("--mode", choices=("demo", "real"), default=default)


def parser():
    command = argparse.ArgumentParser(prog="tracker")
    cmds = command.add_subparsers(dest="command", required=True)
    cmds.add_parser("bootstrap")
    local = cmds.add_parser("run-local", help="Avvia il tracker e il worker dalla stessa configurazione")
    local.add_argument("--port", type=int, default=8000)
    local.add_argument("--env-file", type=Path)
    restore = cmds.add_parser("restore-sqlite", help="Trasferisci un archivio SQLite in un PostgreSQL vuoto")
    restore.add_argument("file", type=Path)
    cmds.add_parser("seed-demo")
    reset = cmds.add_parser("reset-demo")
    reset.add_argument("--yes", action="store_true", required=True)
    work = cmds.add_parser("worker")
    work.add_argument("--mode", choices=("demo", "real", "both"), default="both")
    work.add_argument("--once", action="store_true")
    collect = cmds.add_parser("collect")
    mode_flag(collect, "real")

    source = cmds.add_parser("source")
    mode_flag(source, "real")
    source.add_argument("name")
    source.add_argument("--allow-history", action="store_true")
    source.add_argument("--allow-training", action="store_true")
    source.add_argument("--note", default="")
    search = cmds.add_parser("watch")
    mode_flag(search, "real")
    search.add_argument("source")
    search.add_argument("query")
    search.add_argument("--minutes", type=int, default=60)
    monitor = cmds.add_parser("monitor")
    mode_flag(monitor, "real")
    monitor.add_argument("product")
    monitor.add_argument("--source", choices=MARKETPLACES, action="append")
    monitor.add_argument("--minutes", type=int, default=DEFAULT_INTERVAL)
    monitor.add_argument("--query", default="")
    monitor.add_argument("--max-cost", default="")
    monitor.add_argument("--min-profit", default="")
    monitor.add_argument("--signals", action="store_true")
    monitor.add_argument("--pause", action="store_true")

    importing = cmds.add_parser("import")
    mode_flag(importing, "real")
    importing.add_argument("kind", choices=("offer", "sale", "stock"))
    importing.add_argument("source")
    importing.add_argument("file", type=Path)
    importing.add_argument("--columns", default="{}")
    importing.add_argument("--historical", action="store_true")
    importing.add_argument("--preview", action="store_true")
    for kind in ("dataset", "archive"):
        export = cmds.add_parser(kind)
        mode_flag(export)
        export.add_argument("file", type=Path)
        if kind == "dataset":
            export.add_argument("--cutoff", type=date)
    simulation = cmds.add_parser("replay")
    mode_flag(simulation)
    simulation.add_argument("--start", type=date, required=True)
    simulation.add_argument("--end", type=date, required=True)
    simulation.add_argument("--budget", default="420")
    simulation.add_argument("--latency", type=int, default=15)
    simulation.add_argument("--days", type=int, default=14)
    simulation.add_argument("--assume-availability", action="store_true")
    return command


def source_key(value):
    value = value.casefold()
    if not re.fullmatch(r"[a-z][a-z0-9-]{0,61}", value):
        raise ValueError("Fonte non valida")
    return value


def imported(session, args):
    source = source_key(args.source)
    raw = args.file.read_bytes()
    mapping = json.loads(args.columns)
    if not isinstance(mapping, dict) or not set(mapping) <= EXPECTED or not all(isinstance(v, str) for v in mapping.values()):
        raise ValueError("Mappatura colonne non valida")
    rows = read_stock(raw, args.file.name) if args.kind == "stock" else parse_file(raw, args.file.name)
    if not args.historical:
        column = mapping.get("observed_at", "observed_at").strip().casefold()
        rows = [(index, {key: value for key, value in row.items() if str(key).strip().casefold() != column})
                for index, row in rows]
    valid, errors = preview(rows, mapping, args.kind)
    if args.preview:
        return {"valid": len(valid), "rejected": len(errors), "errors": errors}
    if not source_rules(session, args.mode).get(source, {}).get("history"):
        if source != "local":
            raise ValueError("Abilita lo storico per la fonte con tracker source --allow-history")
        set_source(session, args.mode, source, history=True)
    batch = digest(f"{args.kind}:{canonical(mapping)}:{args.historical}:" + hashlib.sha256(raw).hexdigest())
    count = write_rows(session, args.mode, source, args.kind, valid, errors, batch)
    return {"new_events": count, "valid": len(valid), "rejected": len(errors), "errors": errors}


def main():
    root = parser()
    args = root.parse_args()
    action = args.command
    if action == "run-local":
        try:
            raise SystemExit(run_local(args.port, args.env_file))
        except (OSError, ValueError) as error:
            root.error(str(error))
    sessions = make_sessions()
    if action == "bootstrap":
        print(json.dumps(bootstrap(sessions), ensure_ascii=False, indent=2))
        return
    if action in {"worker", "collect"}:
        modes = ("demo", "real") if args.mode == "both" else (args.mode,)
        run(sessions, modes, once=action == "collect" or args.once)
        return
    result = None
    try:
        with sessions.begin() as session:
            if action in {"seed-demo", "reset-demo"}:
                if action == "reset-demo":
                    for table in (Job, Reservation, Event):
                        session.execute(delete(table).where(table.mode == "demo"))
                result = {"demo_seeded": seed(session)}
            elif action == "source":
                set_source(session, args.mode, source_key(args.name), history=args.allow_history,
                           training=args.allow_training, note=args.note)
            elif action == "watch":
                if not 15 <= args.minutes <= 10080 or not 2 <= len(args.query.strip()) <= 120:
                    raise ValueError("Termine o frequenza non validi")
                result = {"watch": watch(session, args.mode, args.query.strip(), source_key(args.source), args.minutes)}
            elif action == "monitor":
                if args.pause:
                    set_enabled(session, args.mode, args.product, False)
                    result = {"product": args.product, "enabled": False}
                else:
                    result = {"watch": save_watch(session, args.mode, args.product, args.source or MARKETPLACES,
                                                  args.minutes, query=args.query, max_cost=args.max_cost,
                                                  min_profit=args.min_profit, signals=args.signals)}
            elif action == "import":
                result = imported(session, args)
            elif action == "restore-sqlite":
                from .restore import restore_sqlite
                result = restore_sqlite(session, args.file)
            elif action in {"dataset", "archive"}:
                from .dataset import archive_events, export_parquet
                if action == "dataset":
                    result = export_parquet(session, args.mode, args.file, args.cutoff)
                else:
                    args.file.parent.mkdir(parents=True, exist_ok=True)
                    archive_events(session, args.mode, args.file)
                    result = {"file": str(args.file)}
            elif action == "replay":
                from .db import now
                if not args.start or not args.end or not args.start <= args.end <= now():
                    raise ValueError("Periodo passato non valido")
                if not 0 <= args.latency <= 1440 or not 1 <= args.days <= 365:
                    raise ValueError("Latenza o durata non valida")
                budget = amount(args.budget)
                if budget is None:
                    raise ValueError("Budget richiesto")
                result = [replay(session, args.mode, args.start, args.end, dec(budget), strategy,
                                 args.latency, args.assume_availability, args.days) for strategy in ("margin", "liquidity")]
    except (OSError, ValueError) as error:
        root.error(str(error))
    if result is not None:
        print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
