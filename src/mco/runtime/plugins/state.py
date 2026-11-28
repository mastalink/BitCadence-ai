"""SQLite durability for schedule cursors, nonce consumption, and action audit."""
from __future__ import annotations

from contextlib import contextmanager
import hashlib
from pathlib import Path
import sqlite3
import uuid

from .trust import canonical


class PluginState:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(path, check_same_thread=False, isolation_level=None)
        mode, = self.db.execute("PRAGMA journal_mode = WAL").fetchone()
        if mode != "wal":
            self.db.close()
            raise RuntimeError("plugin state requires SQLite WAL mode")
        self.db.execute("PRAGMA synchronous = FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS metadata (key TEXT PRIMARY KEY, value TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS schedules (
                plugin TEXT NOT NULL, name TEXT NOT NULL, period INTEGER NOT NULL,
                next_fire REAL NOT NULL, PRIMARY KEY (plugin, name)
            );
            CREATE TABLE IF NOT EXISTS actions (
                run TEXT PRIMARY KEY, nonce TEXT NOT NULL UNIQUE, plugin TEXT NOT NULL,
                action TEXT NOT NULL, owner TEXT NOT NULL, outcome TEXT NOT NULL
            );
            CREATE TABLE IF NOT EXISTS audit (
                sequence INTEGER PRIMARY KEY, previous TEXT NOT NULL, digest TEXT NOT NULL,
                record TEXT NOT NULL
            );
        """)
        self.db.execute("INSERT OR IGNORE INTO metadata VALUES ('audience', ?)", (uuid.uuid4().hex,))
        self.audience: str = self.db.execute("SELECT value FROM metadata WHERE key = 'audience'").fetchone()[0]

    @contextmanager
    def transaction(self):
        self.db.execute("BEGIN IMMEDIATE")
        try:
            yield
            self.db.commit()
        except BaseException:
            self.db.rollback()
            raise

    def schedule_due(self, plugin: str, name: str, period: int, now: float) -> bool:
        with self.transaction():
            row = self.db.execute(
                "SELECT period, next_fire FROM schedules WHERE plugin = ? AND name = ?", (plugin, name)
            ).fetchone()
            if row is None or row[0] != period:
                self.db.execute(
                    "INSERT OR REPLACE INTO schedules VALUES (?, ?, ?, ?)", (plugin, name, period, now + period)
                )
                return False
            _, due = row
            if due > now:
                return False
            # Coalesce missed intervals. Keep the original phase and advance before spawn.
            next_fire = due + (int((now - due) // period) + 1) * period
            self.db.execute(
                "UPDATE schedules SET next_fire = ? WHERE plugin = ? AND name = ?", (next_fire, plugin, name)
            )
            return True

    def prune_schedules(self, plugin: str, active: set[str]) -> None:
        with self.transaction():
            existing = self.db.execute("SELECT name FROM schedules WHERE plugin = ?", (plugin,)).fetchall()
            self.db.executemany("DELETE FROM schedules WHERE plugin = ? AND name = ?",
                                [(plugin, name) for name, in existing if name not in active])

    def audit(self, record: dict) -> None:
        row = self.db.execute("SELECT digest FROM audit ORDER BY sequence DESC LIMIT 1").fetchone()
        previous = row[0] if row else "0" * 64
        serialized = canonical(record)
        digest = hashlib.sha256(bytes.fromhex(previous) + serialized).hexdigest()
        self.db.execute("INSERT INTO audit(previous, digest, record) VALUES (?, ?, ?)",
                        (previous, digest, serialized.decode("ascii")))

    def admit(self, run: str, nonce: str, plugin: str, action: str, owner: str, now: float) -> bool:
        with self.transaction():
            result = self.db.execute(
                "INSERT OR IGNORE INTO actions VALUES (?, ?, ?, ?, ?, 'admitted')",
                (run, nonce, plugin, action, owner),
            )
            if not result.rowcount:
                return False
            self.audit({"time": now, "run": run, "plugin": plugin, "action": action, "owner": owner,
                        "outcome": "admitted"})
            return True

    def finish(self, run: str, outcome: str, now: float) -> None:
        with self.transaction():
            cursor = self.db.execute("UPDATE actions SET outcome = ? WHERE run = ? AND outcome = 'admitted'",
                                     (outcome, run))
            if cursor.rowcount:
                self.audit({"time": now, "run": run, "outcome": outcome})

    def interrupted(self, now: float) -> None:
        for run, in self.db.execute("SELECT run FROM actions WHERE outcome = 'admitted'").fetchall():
            self.finish(run, "interrupted", now)

    def action_result(self, run: str) -> dict | None:
        row = self.db.execute("SELECT plugin, action, outcome FROM actions WHERE run = ?", (run,)).fetchone()
        return dict(zip(("plugin", "action", "outcome"), row)) if row else None

    def audit_valid(self) -> bool:
        previous = "0" * 64
        for link, digest, record in self.db.execute("SELECT previous, digest, record FROM audit ORDER BY sequence"):
            if link != previous or digest != hashlib.sha256(bytes.fromhex(previous) + record.encode("ascii")).hexdigest():
                return False
            previous = digest
        return True

    def close(self) -> None:
        self.db.close()
