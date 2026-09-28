"""Strict content hashing, direct extraction, and a resumable file index."""

from __future__ import annotations

import fnmatch
import hashlib
import importlib.metadata
import json
import os
import tempfile
import time
from collections import defaultdict
from datetime import datetime, timedelta
from datetime import time as clock_time
from pathlib import Path

from .config import Settings
from .extractors import (
    DocumentError,
    Extraction,
    ProcessingDeferred,
    TextBlock,
    extract_document,
)
from .llm_metadata import generate_metadata, metadata_version
from .local_llm import LlmDeferred, LlmError, LocalModel, verify_model
from .metadata import preview_metadata
from .repository import OcrCheckpoint, Repository, utc_now

IMPLEMENTATION_VERSION = "native-extraction-v3"
METADATA_VERSION = hashlib.sha256(b"extractive-preview-v1-sudachi-c").hexdigest()


class SourceChanged(RuntimeError):
    pass


def _stamp(path: Path) -> tuple[int, int, int]:
    info = path.stat()
    return info.st_size, info.st_mtime_ns, info.st_ino


def hash_file(path: Path, max_bytes: int | None = None) -> str:
    before = _stamp(path)
    digest = hashlib.sha256()
    total = 0
    with path.open("rb") as source:
        while chunk := source.read(1024 * 1024):
            total += len(chunk)
            if max_bytes is not None and total > max_bytes:
                raise DocumentError(
                    "LIMIT_EXCEEDED", "File grew beyond the configured size limit"
                )
            digest.update(chunk)
    if _stamp(path) != before:
        raise SourceChanged("File changed while it was being read")
    return digest.hexdigest()


def extraction_version(settings: Settings) -> str:
    ocr_info = None
    if settings.ocr_enabled:
        executable = settings.tesseract.stat()
        language_files = []
        for language in (*settings.ocr_languages, settings.vertical_language):
            target = settings.tessdata_dir / f"{language}.traineddata"
            item = target.stat()
            language_files.append((language, item.st_size, item.st_mtime_ns))
        ocr_info = (
            str(settings.tesseract),
            executable.st_size,
            executable.st_mtime_ns,
            language_files,
            settings.dpi,
            settings.max_page_pixels,
        )
    values = {
        "version": IMPLEMENTATION_VERSION,
        "libraries": {
            name: importlib.metadata.version(name)
            for name in (
                "python-docx",
                "python-pptx",
                "openpyxl",
                "pypdf",
                "pypdfium2",
                "sudachipy",
                "sudachidict-full",
            )
        },
        "ocr": ocr_info,
        "notes": settings.include_notes,
        "hidden_sheets": settings.include_hidden_sheets,
        "max_file": settings.max_file_bytes,
        "max_uncompressed": settings.max_uncompressed_bytes,
        "max_pages": settings.max_pages,
        "max_cells": settings.max_cells,
        "max_chars": settings.max_chars,
        "allow_cp932": settings.allow_cp932,
    }
    return hashlib.sha256(json.dumps(values, sort_keys=True).encode()).hexdigest()


def _excluded(root: Path, path: Path, settings: Settings) -> bool:
    if path.name.startswith("~$"):
        return True
    if any(
        path == area or area in path.parents
        for area in (settings.database.parent, settings.cache_dir, settings.export_dir)
    ):
        return True
    relative = path.relative_to(root).as_posix().casefold()
    for pattern in settings.exclude_globs:
        pattern = pattern.casefold()
        if fnmatch.fnmatchcase(relative, pattern):
            return True
        if pattern.startswith("**/") and pattern.endswith("/**"):
            directory = pattern[3:-3]
            if relative == directory or relative.endswith("/" + directory):
                return True
    return False


def candidates(root: Path, settings: Settings, on_error) -> list[Path]:
    selected = []
    for current, directories, files in os.walk(
        root, followlinks=False, onerror=on_error
    ):
        folder = Path(current)
        directories[:] = [
            name
            for name in directories
            if not (folder / name).is_symlink()
            and not _excluded(root, folder / name, settings)
        ]
        for filename in files:
            path = folder / filename
            if (
                path.suffix.lower() in settings.extensions
                and not path.is_symlink()
                and not _excluded(root, path, settings)
            ):
                selected.append(path)
    return sorted(selected, key=lambda item: str(item).casefold())


def _copy_snapshot(path: Path, settings: Settings, expected_hash: str) -> Path:
    settings.cache_dir.mkdir(parents=True, exist_ok=True)
    fd, name = tempfile.mkstemp(
        prefix="source-", suffix=path.suffix.lower(), dir=settings.cache_dir
    )
    os.close(fd)
    snapshot = Path(name)
    try:
        with path.open("rb") as source, snapshot.open("wb") as target:
            total = 0
            while chunk := source.read(1024 * 1024):
                total += len(chunk)
                if total > settings.max_file_bytes:
                    raise DocumentError(
                        "LIMIT_EXCEEDED", "Snapshot exceeds the configured size limit"
                    )
                target.write(chunk)
        if hash_file(snapshot, settings.max_file_bytes) != expected_hash:
            raise SourceChanged("Source changed during snapshot copy")
        return snapshot
    except Exception:
        snapshot.unlink(missing_ok=True)
        raise


def _cache_blocks(settings: Settings, digest: str, version: str, blocks) -> Path:
    target = settings.cache_dir / f"{digest}-{version}.jsonl"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        prefix="blocks-",
        suffix=".tmp",
        dir=settings.cache_dir,
    ) as stream:
        temporary = Path(stream.name)
        for block in blocks:
            stream.write(json.dumps(block.to_dict(), ensure_ascii=False) + "\n")
    os.replace(temporary, target)
    return target


def _load_blocks(path: Path) -> list[TextBlock]:
    blocks = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            item = json.loads(line)
            blocks.append(TextBlock(item["locator"], item["text"], item["method"]))
    return blocks


def _cached_warnings(existing) -> list[str]:
    if existing["extraction_warnings"] is not None:
        return json.loads(existing["extraction_warnings"])
    if existing["record_json"]:
        record = json.loads(existing["record_json"])
        return [
            item
            for item in record.get("warnings", [])
            if item
            not in {
                "LOCAL_LLM_PENDING",
                "EXTRACTIVE_PREVIEW",
                "UNVERIFIED_LLM_OUTPUT",
                "UNANCHORED_KEYWORDS_REMOVED",
            }
        ]
    return []


def _window_deadline(settings: Settings) -> datetime | None:
    now = datetime.now().astimezone()
    start = clock_time.fromisoformat(settings.start)
    end = clock_time.fromisoformat(settings.end)
    current = now.time().replace(tzinfo=None)
    if start < end:
        if not start <= current < end:
            return None
        return now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    if current >= start:
        return (now + timedelta(days=1)).replace(
            hour=end.hour, minute=end.minute, second=0, microsecond=0
        )
    if current < end:
        return now.replace(hour=end.hour, minute=end.minute, second=0, microsecond=0)
    return None


def export_catalog(
    repository: Repository,
    settings: Settings,
    run_id: int,
    counts: dict,
    complete_roots: list[str],
) -> Path:
    settings.export_dir.mkdir(parents=True, exist_ok=True)
    grouped = defaultdict(list)
    data = {}
    for row in repository.catalog_rows():
        grouped[row["sha256"]].append(
            {
                "path": row["display_path"],
                "size_bytes": row["size_bytes"],
                "last_verified_at": row["last_verified_at"],
            }
        )
        data[row["sha256"]] = (
            row["extraction_version"],
            row["metadata_version"],
            json.loads(row["record_json"]),
        )
    target = settings.export_dir / "catalog.jsonl"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        prefix="catalog-",
        suffix=".tmp",
        dir=settings.export_dir,
    ) as stream:
        temporary = Path(stream.name)
        for digest, paths in grouped.items():
            extraction_version_value, metadata_version_value, preview = data[digest]
            paths.sort(key=lambda item: item["path"].casefold())
            if preview["title_source"] == "filename":
                preview["title"] = Path(paths[0]["path"]).stem[:80] or "無題"
            record = {
                "schema_version": 1,
                "document_id": f"sha256:{digest}",
                "representative_path": paths[0]["path"],
                "paths": paths,
                **preview,
                "extraction_version": extraction_version_value,
                "metadata_version": metadata_version_value or METADATA_VERSION,
            }
            stream.write(json.dumps(record, ensure_ascii=False) + "\n")
    os.replace(temporary, target)
    errors_path = settings.export_dir / "errors.jsonl"
    with tempfile.NamedTemporaryFile(
        mode="w",
        encoding="utf-8",
        newline="\n",
        delete=False,
        prefix="errors-",
        suffix=".tmp",
        dir=settings.export_dir,
    ) as stream:
        errors_temporary = Path(stream.name)
        for row in repository.errors_for_run(run_id):
            stream.write(json.dumps(dict(row), ensure_ascii=False) + "\n")
    os.replace(errors_temporary, errors_path)
    manifest = settings.export_dir / "run.json"
    manifest.write_text(
        json.dumps(
            {
                "run_id": run_id,
                "generated_at": utc_now(),
                "counts": counts,
                "complete_roots": complete_roots,
                "catalog_path": str(target),
            },
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    return target


def run_scan(
    settings: Settings,
    *,
    ignore_window: bool = False,
    dry_run: bool = False,
    extract_only: bool = False,
) -> tuple[int, dict]:
    deadline = None if ignore_window else _window_deadline(settings)
    if not ignore_window and deadline is None:
        return 0, {"reason": "outside_processing_window"}
    if dry_run:
        summary = {"files": 0, "bytes": 0, "errors": []}
        for root in settings.roots:
            for path in candidates(
                root, settings, lambda exc: summary["errors"].append(str(exc))
            ):
                try:
                    summary["bytes"] += path.stat().st_size
                    summary["files"] += 1
                except OSError as exc:
                    summary["errors"].append(str(exc))
        return (10 if summary["errors"] else 0), summary

    if settings.ocr_enabled and (
        not settings.tesseract.is_file() or not settings.tessdata_dir.is_dir()
    ):
        return 20, {"error": "Tesseract executable or tessdata directory is missing"}
    try:
        version = extraction_version(settings)
    except OSError as exc:
        return 20, {"error": f"OCR language data is missing: {exc}"}

    use_llm = settings.llm_enabled and not extract_only
    if use_llm:
        try:
            verify_model(settings)
            generated_version = metadata_version(settings, version)
        except (LlmError, OSError) as exc:
            return 20, {"error": str(exc)}
    else:
        generated_version = METADATA_VERSION

    repository = Repository(settings.database)
    run_id = repository.start_run()
    counts = {"scanned": 0, "extracted": 0, "reused": 0, "errors": 0}
    complete_roots: list[str] = []
    deferred = False
    attempted_metadata: set[str] = set()

    def check_deadline() -> None:
        if deadline and datetime.now().astimezone() >= deadline - timedelta(minutes=5):
            raise ProcessingDeferred("PROCESSING_WINDOW_END: work can resume next run")

    try:
        with LocalModel(
            settings,
            stop_at=deadline.timestamp() - 300 if deadline else None,
            model_verified=use_llm,
        ) as model:
            for root in settings.roots:
                root_errors = []
                deadline_hit = False
                for path in candidates(root, settings, root_errors.append):
                    if deadline and datetime.now().astimezone() >= deadline - timedelta(
                        minutes=5
                    ):
                        deferred = True
                        deadline_hit = True
                        break
                    counts["scanned"] += 1
                    size = 0
                    digest = None
                    try:
                        before = _stamp(path)
                        size = before[0]
                        if size > settings.max_file_bytes:
                            raise DocumentError(
                                "LIMIT_EXCEEDED",
                                "File exceeds the configured size limit",
                            )
                        if (
                            time.time_ns() - before[1]
                            < settings.stable_age_seconds * 1_000_000_000
                        ):
                            raise SourceChanged("File may still be in use")
                        digest = hash_file(path, settings.max_file_bytes)
                        existing = repository.content(digest, version)
                        if (
                            existing
                            and existing["status"] in ("review_required", "complete")
                            and existing["metadata_version"] == generated_version
                        ):
                            repository.mark_path(run_id, root, path, digest, size)
                            counts["reused"] += 1
                            continue
                        if (
                            existing
                            and existing["status"] == "extracted"
                            and digest in attempted_metadata
                        ):
                            repository.mark_path(run_id, root, path, digest, size)
                            counts["reused"] += 1
                            continue
                        if existing and existing["status"] == "failed":  # noqa: SIM102
                            # Identical bytes with a different extension may still be valid.
                            if existing["error_code"] == "UNSUPPORTED_CONTENT":
                                existing = None
                        if existing and existing["status"] == "failed":
                            repository.mark_path(
                                run_id, root, path, None, size, existing["error_code"]
                            )
                            repository.record_error(
                                run_id,
                                path,
                                existing["error_code"],
                                existing["error_detail"] or "Previously failed",
                            )
                            counts["errors"] += 1
                            continue
                        blocks_path = (
                            Path(existing["blocks_path"])
                            if existing and existing["blocks_path"]
                            else None
                        )
                        extraction = None
                        if blocks_path and blocks_path.is_file():
                            try:
                                extraction = Extraction(
                                    _load_blocks(blocks_path),
                                    _cached_warnings(existing),
                                )
                            except OSError, ValueError, KeyError, TypeError:
                                extraction = None
                        if extraction is None:
                            snapshot = _copy_snapshot(path, settings, digest)
                            try:
                                extraction = extract_document(
                                    snapshot,
                                    settings,
                                    checkpoint=OcrCheckpoint(
                                        repository, digest, version
                                    ),
                                    check_deadline=check_deadline,
                                )
                                if hash_file(path, settings.max_file_bytes) != digest:
                                    raise SourceChanged(
                                        "Source changed during extraction"
                                    )
                                blocks_path = _cache_blocks(
                                    settings, digest, version, extraction.blocks
                                )
                                counts["extracted"] += 1
                            finally:
                                snapshot.unlink(missing_ok=True)
                        repository.save_content(
                            digest,
                            version,
                            "extracted",
                            blocks_path=blocks_path,
                            extraction_warnings=extraction.warnings,
                        )
                        repository.mark_path(run_id, root, path, digest, size)
                        if use_llm and extraction.blocks:
                            record = generate_metadata(
                                extraction,
                                digest,
                                generated_version,
                                settings,
                                repository,
                                model,
                                check_deadline,
                            )
                        else:
                            record = preview_metadata(extraction, path.stem)
                            if use_llm and not extraction.blocks:
                                record["warnings"] = [
                                    warning
                                    for warning in record["warnings"]
                                    if warning != "LOCAL_LLM_PENDING"
                                ]
                        if hash_file(path, settings.max_file_bytes) != digest:
                            raise SourceChanged(
                                "Source changed during metadata generation"
                            )
                        repository.save_content(
                            digest,
                            version,
                            "review_required",
                            record,
                            blocks_path,
                            metadata_version=generated_version,
                            extraction_warnings=extraction.warnings,
                        )
                    except ProcessingDeferred, LlmDeferred:
                        deferred = True
                        deadline_hit = True
                        break
                    except LlmError as exc:
                        attempted_metadata.add(digest)  # pyright: ignore[reportArgumentType]
                        code = str(exc).split(":", 1)[0]
                        repository.record_error(run_id, path, code, str(exc))
                        counts["errors"] += 1
                        if code in {
                            "LLM_ENDPOINT_BUSY",
                            "LLM_START_FAILED",
                            "LLM_START_TIMEOUT",
                            "LLM_CONNECTION_ERROR",
                        }:
                            deferred = True
                            deadline_hit = True
                            break
                    except SourceChanged as exc:
                        repository.mark_path(
                            run_id, root, path, None, size, "SOURCE_CHANGED"
                        )
                        repository.record_error(
                            run_id, path, "SOURCE_CHANGED", str(exc)
                        )
                        counts["errors"] += 1
                        deferred = True
                    except DocumentError as exc:
                        if digest is not None and exc.code != "UNSUPPORTED_CONTENT":
                            repository.save_content(
                                digest,
                                version,
                                "failed",
                                error_code=exc.code,
                                detail=str(exc),
                            )
                        repository.mark_path(run_id, root, path, None, size, exc.code)
                        repository.record_error(run_id, path, exc.code, str(exc))
                        counts["errors"] += 1
                    except (OSError, ValueError) as exc:
                        repository.mark_path(run_id, root, path, None, size, "IO_ERROR")
                        repository.record_error(run_id, path, "IO_ERROR", str(exc))
                        counts["errors"] += 1
                if root_errors:
                    for error in root_errors:
                        repository.record_error(run_id, root, "IO_ERROR", str(error))
                        counts["errors"] += 1
                if deadline_hit:
                    break
                if not root_errors:
                    repository.prune_root(root, run_id)
                    complete_roots.append(str(root))
        status = (
            "deferred"
            if deferred
            else "complete_with_errors"
            if counts["errors"]
            else "complete"
        )
        if not deferred and len(complete_roots) == len(settings.roots):
            export_catalog(repository, settings, run_id, counts, complete_roots)
        repository.finish_run(run_id, status, counts)
        return 11 if deferred else 10 if counts["errors"] else 0, {
            "run_id": run_id,
            "status": status,
            **counts,
        }
    except Exception:
        repository.finish_run(run_id, "failed", counts)
        raise
    finally:
        repository.close()
