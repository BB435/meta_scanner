"""Small persistent index for exact-content deduplication and change detection."""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timezone
from pathlib import Path


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


class Repository:
    def __init__(self, database: Path):
        database.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(database)
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA foreign_keys=ON")
        self.db.executescript(
            """
            CREATE TABLE IF NOT EXISTS runs (
                id INTEGER PRIMARY KEY, started_at TEXT NOT NULL, finished_at TEXT,
                status TEXT NOT NULL, scanned INTEGER NOT NULL DEFAULT 0,
                extracted INTEGER NOT NULL DEFAULT 0, reused INTEGER NOT NULL DEFAULT 0,
                errors INTEGER NOT NULL DEFAULT 0
            );
            CREATE TABLE IF NOT EXISTS contents (
                sha256 TEXT PRIMARY KEY, extraction_version TEXT NOT NULL,
                metadata_version TEXT, status TEXT NOT NULL, record_json TEXT, blocks_path TEXT,
                extraction_warnings TEXT,
                error_code TEXT, error_detail TEXT, updated_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS paths (
                path_key TEXT PRIMARY KEY, display_path TEXT NOT NULL, root TEXT NOT NULL,
                sha256 TEXT REFERENCES contents(sha256), size_bytes INTEGER NOT NULL,
                last_seen_run INTEGER NOT NULL, last_verified_at TEXT NOT NULL,
                error_code TEXT
            );
            CREATE INDEX IF NOT EXISTS paths_sha ON paths(sha256);
            CREATE TABLE IF NOT EXISTS errors (
                id INTEGER PRIMARY KEY, run_id INTEGER NOT NULL, path TEXT NOT NULL,
                code TEXT NOT NULL, detail TEXT NOT NULL, occurred_at TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS llm_chunks (
                sha256 TEXT NOT NULL, metadata_version TEXT NOT NULL,
                chunk_index INTEGER NOT NULL, result_json TEXT NOT NULL,
                PRIMARY KEY(sha256, metadata_version, chunk_index)
            );
            CREATE TABLE IF NOT EXISTS ocr_units (
                sha256 TEXT NOT NULL, extraction_version TEXT NOT NULL,
                locator TEXT NOT NULL, text TEXT NOT NULL, updated_at TEXT NOT NULL,
                PRIMARY KEY(sha256, extraction_version, locator)
            );
            """
        )
        columns = {row["name"] for row in self.db.execute("PRAGMA table_info(contents)")}
        if "metadata_version" not in columns:
            self.db.execute("ALTER TABLE contents ADD COLUMN metadata_version TEXT")
        if "extraction_warnings" not in columns:
            self.db.execute("ALTER TABLE contents ADD COLUMN extraction_warnings TEXT")
        if "metadata_version" not in columns or "extraction_warnings" not in columns:
            self.db.commit()

    def close(self) -> None:
        self.db.close()

    def start_run(self) -> int:
        cursor = self.db.execute("INSERT INTO runs(started_at,status) VALUES (?, 'running')", (utc_now(),))
        self.db.commit()
        return cursor.lastrowid

    def finish_run(self, run_id: int, status: str, counts: dict[str, int]) -> None:
        self.db.execute(
            "UPDATE runs SET finished_at=?, status=?, scanned=?, extracted=?, reused=?, errors=? WHERE id=?",
            (utc_now(), status, counts["scanned"], counts["extracted"], counts["reused"], counts["errors"], run_id),
        )
        self.db.commit()

    def content(self, digest: str, version: str) -> sqlite3.Row | None:
        return self.db.execute(
            "SELECT * FROM contents WHERE sha256=? AND extraction_version=?", (digest, version)
        ).fetchone()

    def save_content(self, digest: str, version: str, status: str, record: dict | None = None,
                     blocks_path: Path | None = None, error_code: str | None = None, detail: str | None = None,
                     metadata_version: str | None = None, extraction_warnings: list[str] | None = None) -> None:
        self.db.execute(
            """INSERT INTO contents(sha256,extraction_version,metadata_version,status,record_json,blocks_path,
                                    extraction_warnings,error_code,error_detail,updated_at)
               VALUES(?,?,?,?,?,?,?,?,?,?) ON CONFLICT(sha256) DO UPDATE SET
               extraction_version=excluded.extraction_version,metadata_version=excluded.metadata_version,
               status=excluded.status,
               record_json=excluded.record_json,blocks_path=excluded.blocks_path,
               extraction_warnings=excluded.extraction_warnings,
               error_code=excluded.error_code,error_detail=excluded.error_detail,
               updated_at=excluded.updated_at""",
            (digest, version, metadata_version, status, json.dumps(record, ensure_ascii=False) if record else None,
             str(blocks_path) if blocks_path else None,
             json.dumps(extraction_warnings, ensure_ascii=False) if extraction_warnings is not None else None,
             error_code, detail, utc_now()),
        )
        self.db.commit()

    def mark_path(self, run_id: int, root: Path, path: Path, digest: str | None, size: int,
                  error_code: str | None = None) -> None:
        self.db.execute(
            """INSERT INTO paths(path_key,display_path,root,sha256,size_bytes,last_seen_run,last_verified_at,error_code)
               VALUES(?,?,?,?,?,?,?,?) ON CONFLICT(path_key) DO UPDATE SET
               display_path=excluded.display_path,root=excluded.root,sha256=excluded.sha256,
               size_bytes=excluded.size_bytes,last_seen_run=excluded.last_seen_run,
               last_verified_at=excluded.last_verified_at,error_code=excluded.error_code""",
            (str(path).casefold(), str(path), str(root).casefold(), digest, size, run_id, utc_now(), error_code),
        )
        self.db.commit()

    def record_error(self, run_id: int, path: Path, code: str, detail: str) -> None:
        self.db.execute(
            "INSERT INTO errors(run_id,path,code,detail,occurred_at) VALUES (?,?,?,?,?)",
            (run_id, str(path), code, detail[:1000], utc_now()),
        )
        self.db.commit()

    def prune_root(self, root: Path, run_id: int) -> None:
        self.db.execute("DELETE FROM paths WHERE root=? AND last_seen_run<>?", (str(root).casefold(), run_id))
        self.db.commit()

    def catalog_rows(self):
        return self.db.execute(
            """SELECT c.sha256,c.extraction_version,c.metadata_version,c.record_json,
                      p.display_path,p.size_bytes,p.last_verified_at
               FROM contents c JOIN paths p ON p.sha256=c.sha256
               WHERE c.status IN ('review_required','complete') AND c.record_json IS NOT NULL
               ORDER BY c.sha256,p.display_path"""
        )

    def chunk(self, digest: str, version: str, index: int) -> dict | None:
        row = self.db.execute(
            "SELECT result_json FROM llm_chunks WHERE sha256=? AND metadata_version=? AND chunk_index=?",
            (digest, version, index),
        ).fetchone()
        return json.loads(row[0]) if row else None

    def save_chunk(self, digest: str, version: str, index: int, result: dict) -> None:
        self.db.execute(
            "INSERT OR REPLACE INTO llm_chunks(sha256,metadata_version,chunk_index,result_json) VALUES(?,?,?,?)",
            (digest, version, index, json.dumps(result, ensure_ascii=False)),
        )
        self.db.commit()

    def ocr_text(self, digest: str, version: str, locator: str) -> str | None:
        row = self.db.execute(
            "SELECT text FROM ocr_units WHERE sha256=? AND extraction_version=? AND locator=?",
            (digest, version, locator),
        ).fetchone()
        return row[0] if row is not None else None

    def save_ocr_text(self, digest: str, version: str, locator: str, value: str) -> None:
        self.db.execute(
            """INSERT OR REPLACE INTO ocr_units(sha256,extraction_version,locator,text,updated_at)
               VALUES(?,?,?,?,?)""",
            (digest, version, locator, value, utc_now()),
        )
        self.db.commit()

    def latest_run(self) -> dict | None:
        row = self.db.execute("SELECT * FROM runs ORDER BY id DESC LIMIT 1").fetchone()
        return dict(row) if row else None

    def errors_for_run(self, run_id: int):
        return self.db.execute(
            "SELECT path,code,detail,occurred_at FROM errors WHERE run_id=? ORDER BY id", (run_id,)
        )


class OcrCheckpoint:
    """Read and commit OCR results for one immutable content/version pair."""

    def __init__(self, repository: Repository, digest: str, version: str):
        self.repository = repository
        self.digest = digest
        self.version = version

    def get(self, locator: str) -> str | None:
        return self.repository.ocr_text(self.digest, self.version, locator)

    def save(self, locator: str, value: str) -> None:
        self.repository.save_ocr_text(self.digest, self.version, locator, value)
