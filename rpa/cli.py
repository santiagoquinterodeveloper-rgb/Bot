"""Línea de comandos:  python -m rpa <comando>"""
from __future__ import annotations

import argparse
import getpass
import sys
import time
from pathlib import Path

from .audit_export import export_audit
from .config import load_config
from .db import AuditDB
from .mailer import build_mailer
from .orchestrator import Orchestrator
from .reports import ReportsDir
from .secrets_store import set_secret
from .types import RunAborted


def _log(msg: str) -> None:
    print(f"{time.strftime('%H:%M:%S')} {msg}", flush=True)


def _screenshot(path: Path) -> None:
    from PIL import ImageGrab

    ImageGrab.grab().save(path)


def _build(args, need_driver: bool, need_mailer: bool):
    overrides = {"mail": {"dry_run": False}} if getattr(args, "real", False) else None
    cfg = load_config(args.config, overrides)
    db = AuditDB(cfg.path("paths.db_path"))
    driver = None
    if need_driver:
        from .hosvital.driver import HosvitalDriver  # solo Windows

        reports = ReportsDir(cfg.path("paths.reports_dir"), cfg.path("paths.work_dir") / "quarantine", cfg.prefix_map())
        driver = HosvitalDriver(cfg, reports, log=_log)
    mailer = build_mailer(cfg) if need_mailer else None
    return cfg, db, Orchestrator(cfg, db, driver, mailer, screenshot=_screenshot, log=_log)


def _excel(args, cfg) -> Path:
    return Path(args.excel) if getattr(args, "excel", None) else cfg.path("paths.input_excel")


def _run_id(args, db) -> int:
    rid = getattr(args, "run", None) or db.last_run_id()
    if rid is None:
        raise SystemExit("No hay corridas. Ejecuta primero: python -m rpa prepare --excel <archivo>")
    return int(rid)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="rpa", description="RPA Hosvital: descarga y envío de documentos clínicos")
    p.add_argument("--config", default="config.yaml")
    sub = p.add_subparsers(dest="cmd", required=True)
    for name, helptext in [("prepare", "lee el Excel y crea la corrida"),
                           ("download", "descarga los PDF desde Hosvital"),
                           ("send", "cifra y envía por Graph"),
                           ("run", "prepare + download + send + auditoría + limpieza")]:
        sp = sub.add_parser(name, help=helptext)
        sp.add_argument("--excel")
        sp.add_argument("--run", type=int)
        sp.add_argument("--real", action="store_true", help="envío real (ignora mail.dry_run=true)")
    for name in ("audit", "cleanup"):
        sp = sub.add_parser(name)
        sp.add_argument("--run", type=int)
    sp = sub.add_parser("set-secret", help="guarda un secreto en el Administrador de credenciales de Windows")
    sp.add_argument("name", help="p. ej. hosvital_password o graph_client_secret")
    args = p.parse_args(argv)

    if args.cmd == "set-secret":
        set_secret(args.name, getpass.getpass(f"Valor de {args.name}: "))
        print("Guardado.")
        return 0

    try:
        if args.cmd == "prepare":
            cfg, db, orch = _build(args, False, False)
            orch.prepare(_excel(args, cfg))
        elif args.cmd == "download":
            cfg, db, orch = _build(args, True, False)
            orch.download(_run_id(args, db))
        elif args.cmd == "send":
            cfg, db, orch = _build(args, False, True)
            orch.send(_run_id(args, db))
        elif args.cmd == "audit":
            cfg, db, _ = _build(args, False, False)
            print(export_audit(db, _run_id(args, db), cfg.path("paths.audit_dir")))
        elif args.cmd == "cleanup":
            cfg, db, orch = _build(args, False, False)
            print(orch.cleanup(_run_id(args, db)))
        elif args.cmd == "run":
            cfg, db, orch = _build(args, True, True)
            run_id = orch.prepare(_excel(args, cfg))
            try:
                orch.download(run_id)
                orch.send(run_id)
            finally:
                print("Auditoría:", export_audit(db, run_id, cfg.path("paths.audit_dir")))
            if not orch.mailer.dry_run:
                orch.cleanup(run_id)
                db.finish_run(run_id)
    except RunAborted as e:
        _log(f"CORRIDA DETENIDA: {e}")
        return 2
    return 0


if __name__ == "__main__":
    sys.exit(main())
