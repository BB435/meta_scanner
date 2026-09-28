"""Command-line entry point for local document metadata extraction."""

from __future__ import annotations

import argparse
import json
import msvcrt
import sqlite3
import sys
from contextlib import contextmanager
from pathlib import Path

from .config import ConfigurationError, load_settings
from .local_llm import LlmError, verify_model
from .repository import Repository
from .scanner import run_scan


class AlreadyRunning(RuntimeError):
    pass


@contextmanager
def _single_instance(database: Path):
    database.parent.mkdir(parents=True, exist_ok=True)
    lock_path = database.with_suffix(database.suffix + ".lock")
    with lock_path.open("a+b") as stream:
        stream.seek(0, 2)
        if stream.tell() == 0:
            stream.write(b"\0")
            stream.flush()
        stream.seek(0)
        try:
            msvcrt.locking(stream.fileno(), msvcrt.LK_NBLCK, 1)
        except OSError as exc:
            raise AlreadyRunning("Another scanner run is active") from exc
        try:
            yield
        finally:
            stream.seek(0)
            msvcrt.locking(stream.fileno(), msvcrt.LK_UNLCK, 1)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="meta-scanner", description="Local document catalog extraction"
    )
    commands = parser.add_subparsers(dest="command", required=True)
    for name in ("doctor", "scan", "run", "status"):
        command = commands.add_parser(name)
        command.add_argument("--config", type=Path, required=True)
        if name == "scan":
            command.add_argument("--dry-run", action="store_true", required=True)
        if name == "run":
            command.add_argument("--ignore-window", action="store_true")
            command.add_argument(
                "--extract-only",
                action="store_true",
                help="write an extractive preview without starting the local AI",
            )
    return parser


def main() -> None:
    arguments = _parser().parse_args()
    try:
        settings = load_settings(arguments.config)
        if arguments.command == "doctor":
            model_error = None
            if settings.llm_enabled:
                try:
                    verify_model(settings)
                except (LlmError, OSError) as exc:
                    model_error = str(exc)
            report = {
                "python": sys.version.split()[0],
                "roots": [str(root) for root in settings.roots],
                "tesseract": settings.tesseract.is_file()
                if settings.ocr_enabled
                else "disabled",
                "tessdata": {
                    lang: (settings.tessdata_dir / f"{lang}.traineddata").is_file()
                    for lang in (*settings.ocr_languages, settings.vertical_language)
                }
                if settings.ocr_enabled
                else {},
                "local_llm": "disabled"
                if not settings.llm_enabled
                else "ready"
                if model_error is None
                else model_error,
                "legacy_office": "not implemented",
            }
            code = (
                0
                if report["tesseract"] is not False
                and all(report["tessdata"].values())
                and model_error is None
                else 20
            )
        elif arguments.command == "scan":
            code, report = run_scan(settings, ignore_window=True, dry_run=True)
        elif arguments.command == "status":
            if settings.database.exists():
                repository = Repository(settings.database)
                try:
                    report = repository.latest_run() or {"status": "no_runs"}
                finally:
                    repository.close()
            else:
                report = {"status": "no_runs"}
            code = 0
        else:
            with _single_instance(settings.database):
                code, report = run_scan(
                    settings,
                    ignore_window=arguments.ignore_window,
                    extract_only=arguments.extract_only,
                )
    except ConfigurationError as exc:
        code, report = 20, {"error": str(exc)}
    except AlreadyRunning as exc:
        code, report = 21, {"error": str(exc)}
    except (OSError, RuntimeError, sqlite3.Error) as exc:
        code, report = 30, {"error": f"{type(exc).__name__}: {exc}"}
    print(json.dumps(report, ensure_ascii=False, indent=2))
    raise SystemExit(code)
