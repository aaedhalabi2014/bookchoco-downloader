from __future__ import annotations

import sqlite3
import threading
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path

from .config import DB_PATH


_DB_LOCK = threading.Lock()


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat()


@dataclass
class Analysis:
    id: str
    source_url: str
    platform: str
    title: str
    formats_json: str
    created_at: str


@dataclass
class Job:
    id: str
    status: str
    progress: int
    platform: str
    title: str | None
    filename: str | None
    file_path: str | None
    error: str | None
    created_at: str
    updated_at: str
    downloaded_bytes: int = 0
    total_bytes: int = 0
    speed_bps: float | None = None
    eta_seconds: int | None = None
    phase: str = "queued"


def _connect() -> sqlite3.Connection:
    conn = sqlite3.connect(DB_PATH, timeout=10)
    conn.row_factory = sqlite3.Row
    return conn


def init_db() -> None:
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS jobs (
                id TEXT PRIMARY KEY,
                status TEXT NOT NULL,
                progress INTEGER NOT NULL DEFAULT 0,
                platform TEXT NOT NULL,
                title TEXT,
                filename TEXT,
                file_path TEXT,
                error TEXT,
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL,
                downloaded_bytes INTEGER NOT NULL DEFAULT 0,
                total_bytes INTEGER NOT NULL DEFAULT 0,
                speed_bps REAL,
                eta_seconds INTEGER,
                phase TEXT NOT NULL DEFAULT 'queued'
            )
            """
        )

        # Additive migration for existing Render instances that already have
        # the earlier jobs table.
        existing = {row["name"] for row in conn.execute("PRAGMA table_info(jobs)").fetchall()}
        migrations = {
            "downloaded_bytes": "INTEGER NOT NULL DEFAULT 0",
            "total_bytes": "INTEGER NOT NULL DEFAULT 0",
            "speed_bps": "REAL",
            "eta_seconds": "INTEGER",
            "phase": "TEXT NOT NULL DEFAULT 'queued'",
        }
        for name, definition in migrations.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE jobs ADD COLUMN {name} {definition}")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS analyses (
                id TEXT PRIMARY KEY,
                source_url TEXT NOT NULL,
                platform TEXT NOT NULL,
                title TEXT NOT NULL,
                formats_json TEXT NOT NULL,
                created_at TEXT NOT NULL
            )
            """
        )
        conn.commit()


def create_analysis(analysis_id: str, source_url: str, platform: str, title: str, formats_json: str) -> Analysis:
    created_at = utc_now()
    analysis = Analysis(analysis_id, source_url, platform, title, formats_json, created_at)
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            "INSERT INTO analyses (id, source_url, platform, title, formats_json, created_at) VALUES (?, ?, ?, ?, ?, ?)",
            (analysis.id, analysis.source_url, analysis.platform, analysis.title, analysis.formats_json, analysis.created_at),
        )
        conn.commit()
    return analysis


def get_analysis(analysis_id: str) -> Analysis | None:
    with _DB_LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM analyses WHERE id = ?", (analysis_id,)).fetchone()
    return Analysis(**dict(row)) if row else None


def delete_analysis(analysis_id: str) -> None:
    with _DB_LOCK, _connect() as conn:
        conn.execute("DELETE FROM analyses WHERE id = ?", (analysis_id,))
        conn.commit()


def list_analyses() -> list[Analysis]:
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute("SELECT * FROM analyses").fetchall()
    return [Analysis(**dict(row)) for row in rows]


def create_job(job_id: str, platform: str) -> Job:
    now = utc_now()
    job = Job(
        id=job_id,
        status="queued",
        progress=0,
        platform=platform,
        title=None,
        filename=None,
        file_path=None,
        error=None,
        created_at=now,
        updated_at=now,
        phase="queued",
    )
    data = asdict(job)
    columns = ", ".join(data.keys())
    placeholders = ", ".join("?" for _ in data)
    with _DB_LOCK, _connect() as conn:
        conn.execute(
            f"INSERT INTO jobs ({columns}) VALUES ({placeholders})",
            tuple(data.values()),
        )
        conn.commit()
    return job


def get_job(job_id: str) -> Job | None:
    with _DB_LOCK, _connect() as conn:
        row = conn.execute("SELECT * FROM jobs WHERE id = ?", (job_id,)).fetchone()
    return Job(**dict(row)) if row else None


def update_job(job_id: str, **fields: object) -> None:
    if not fields:
        return
    allowed = {
        "status",
        "progress",
        "platform",
        "title",
        "filename",
        "file_path",
        "error",
        "downloaded_bytes",
        "total_bytes",
        "speed_bps",
        "eta_seconds",
        "phase",
    }
    clean = {k: v for k, v in fields.items() if k in allowed}
    if not clean:
        return
    clean["updated_at"] = utc_now()
    assignments = ", ".join(f"{key} = ?" for key in clean)
    values = list(clean.values()) + [job_id]
    with _DB_LOCK, _connect() as conn:
        conn.execute(f"UPDATE jobs SET {assignments} WHERE id = ?", values)
        conn.commit()


def delete_job(job_id: str) -> None:
    with _DB_LOCK, _connect() as conn:
        conn.execute("DELETE FROM jobs WHERE id = ?", (job_id,))
        conn.commit()


def list_jobs() -> list[Job]:
    with _DB_LOCK, _connect() as conn:
        rows = conn.execute("SELECT * FROM jobs").fetchall()
    return [Job(**dict(row)) for row in rows]


def remove_job_file(job: Job) -> None:
    if not job.file_path:
        return
    path = Path(job.file_path)
    try:
        if path.exists() and path.is_file():
            path.unlink()
        parent = path.parent
        if parent.exists() and parent.name == job.id:
            for child in parent.iterdir():
                if child.is_file():
                    child.unlink(missing_ok=True)
            parent.rmdir()
    except OSError:
        pass
