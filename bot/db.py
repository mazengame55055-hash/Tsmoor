"""Tiny SQLite layer. Blocking sqlite3 calls run in a thread so the event loop never stalls."""
from __future__ import annotations

import asyncio
import sqlite3
import threading
import time
from pathlib import Path
from typing import Any, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    discord_id      INTEGER PRIMARY KEY,
    kaggle_username TEXT    NOT NULL,
    token_enc       BLOB    NOT NULL,
    token_kind      TEXT    NOT NULL,          -- 'token' (KGAT_...) or 'legacy' (username + key)
    created_at      INTEGER NOT NULL
);
CREATE TABLE IF NOT EXISTS files (
    id            INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id    INTEGER NOT NULL,
    kind          TEXT    NOT NULL,            -- 'blend' or 'audio'
    original_name TEXT    NOT NULL,
    dataset_slug  TEXT    NOT NULL,
    size_bytes    INTEGER NOT NULL,
    created_at    INTEGER NOT NULL,
    stored_name   TEXT                         -- ASCII name inside the Kaggle dataset (NULL on old rows = original_name)
);
CREATE INDEX IF NOT EXISTS idx_files_user ON files(discord_id);
CREATE TABLE IF NOT EXISTS renders (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id  INTEGER NOT NULL,
    channel_id  INTEGER,
    kernel_slug TEXT    NOT NULL,
    mode        TEXT    NOT NULL,
    blend_name  TEXT    NOT NULL,
    config_json TEXT    NOT NULL,
    status      TEXT    NOT NULL,              -- queued | running | complete | error | cancelled | timeout
    created_at  INTEGER NOT NULL,
    finished_at INTEGER
);
CREATE INDEX IF NOT EXISTS idx_renders_user ON renders(discord_id);
-- v1.0.9: several Kaggle accounts per Discord user. `users` is only read once, to migrate old installs.
CREATE TABLE IF NOT EXISTS accounts (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    discord_id      INTEGER NOT NULL,
    kaggle_username TEXT    NOT NULL,
    token_enc       BLOB    NOT NULL,
    token_kind      TEXT    NOT NULL,
    created_at      INTEGER NOT NULL,
    UNIQUE(discord_id, kaggle_username)
);
CREATE TABLE IF NOT EXISTS active_account (
    discord_id INTEGER PRIMARY KEY,
    account_id INTEGER NOT NULL
);
"""


class Database:
    def __init__(self, path: Path):
        self._conn = sqlite3.connect(str(path), check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._lock = threading.Lock()
        with self._lock:
            self._conn.executescript(SCHEMA)
            cols = {r[1] for r in self._conn.execute("PRAGMA table_info(files)")}
            if "stored_name" not in cols:  # database created by v1.0.0-1.0.3
                self._conn.execute("ALTER TABLE files ADD COLUMN stored_name TEXT")
            for table in ("files", "renders"):  # database created by v1.0.0-1.0.8: rows belong to the single account
                if "account_id" not in {r[1] for r in self._conn.execute(f"PRAGMA table_info({table})")}:
                    self._conn.execute(f"ALTER TABLE {table} ADD COLUMN account_id INTEGER")
            rcols = {r[1] for r in self._conn.execute("PRAGMA table_info(renders)")}
            for col in ("started_at", "run_seconds"):  # v1.1.0: needed by /quota
                if col not in rcols:
                    self._conn.execute(f"ALTER TABLE renders ADD COLUMN {col} INTEGER")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_files_account ON files(account_id)")
            self._conn.execute("CREATE INDEX IF NOT EXISTS idx_renders_account ON renders(account_id)")
            # One-time migration of the old single-account table (it is emptied afterwards so a later /logout
            # can never leave an old copy of a token behind).
            self._conn.execute(
                "INSERT OR IGNORE INTO accounts(discord_id, kaggle_username, token_enc, token_kind, created_at) "
                "SELECT discord_id, kaggle_username, token_enc, token_kind, created_at FROM users")
            self._conn.execute("DELETE FROM users")
            self._conn.execute("INSERT OR IGNORE INTO active_account(discord_id, account_id) "
                               "SELECT discord_id, MIN(id) FROM accounts GROUP BY discord_id")
            for table in ("files", "renders"):
                self._conn.execute(
                    f"UPDATE {table} SET account_id=(SELECT MIN(id) FROM accounts a WHERE a.discord_id={table}.discord_id) "
                    f"WHERE account_id IS NULL")
            self._conn.commit()

    def _run(self, sql: str, params: tuple = (), fetch: Optional[str] = None) -> Any:
        with self._lock:
            cur = self._conn.execute(sql, params)
            self._conn.commit()
            if fetch == "one":
                return cur.fetchone()
            if fetch == "all":
                return cur.fetchall()
            return cur.lastrowid

    async def _exec(self, sql: str, params: tuple = (), fetch: Optional[str] = None) -> Any:
        return await asyncio.to_thread(self._run, sql, params, fetch)

    async def backup_to(self, dest: Path) -> None:
        """Consistent snapshot of the live database (safe while the bot is running)."""
        def run():
            with self._lock:
                out = sqlite3.connect(str(dest))
                try:
                    self._conn.backup(out)
                finally:
                    out.close()
        await asyncio.to_thread(run)

    async def restore_from(self, src_path: Path) -> None:
        """Replaces EVERYTHING in the live database with the content of `src_path` (already validated)."""
        def run():
            with self._lock:
                src = sqlite3.connect(str(src_path))
                try:
                    src.backup(self._conn)
                finally:
                    src.close()
                self._conn.commit()
        await asyncio.to_thread(run)

    async def _tx(self, fn):
        """Runs several statements as one transaction (in a thread, under the DB lock)."""
        def run():
            with self._lock:
                try:
                    result = fn(self._conn)
                    self._conn.commit()
                    return result
                except Exception:
                    self._conn.rollback()
                    raise
        return await asyncio.to_thread(run)

    # ---- accounts (several Kaggle accounts per Discord user) ----
    async def save_account(self, discord_id: int, username: str, token_enc: bytes, kind: str) -> int:
        """Adds (or refreshes the token of) an account and makes it the ACTIVE one. Returns the account id."""
        def work(c):
            c.execute(
                "INSERT INTO accounts(discord_id, kaggle_username, token_enc, token_kind, created_at) VALUES (?,?,?,?,?) "
                "ON CONFLICT(discord_id, kaggle_username) DO UPDATE SET token_enc=excluded.token_enc, "
                "token_kind=excluded.token_kind, created_at=excluded.created_at",
                (discord_id, username, token_enc, kind, int(time.time())))
            acc_id = c.execute("SELECT id FROM accounts WHERE discord_id=? AND kaggle_username=?",
                               (discord_id, username)).fetchone()[0]
            c.execute("INSERT INTO active_account(discord_id, account_id) VALUES (?,?) "
                      "ON CONFLICT(discord_id) DO UPDATE SET account_id=excluded.account_id", (discord_id, acc_id))
            return acc_id
        return await self._tx(work)

    async def list_all_accounts(self) -> list[sqlite3.Row]:
        return await self._exec("SELECT * FROM accounts ORDER BY id", (), "all")

    async def counts(self) -> dict:
        out = {}
        for t in ("accounts", "files", "renders"):
            out[t] = (await self._exec(f"SELECT COUNT(*) FROM {t}", (), "one"))[0]
        return out

    def close(self) -> None:
        with self._lock:
            self._conn.close()

    async def list_accounts(self, discord_id: int) -> list[sqlite3.Row]:
        return await self._exec("SELECT * FROM accounts WHERE discord_id=? ORDER BY id", (discord_id,), "all")

    async def get_account(self, discord_id: int, account_id: int) -> Optional[sqlite3.Row]:
        return await self._exec("SELECT * FROM accounts WHERE id=? AND discord_id=?", (account_id, discord_id), "one")

    async def get_active_account(self, discord_id: int) -> Optional[sqlite3.Row]:
        def work(c):
            row = c.execute("SELECT a.* FROM accounts a JOIN active_account s ON s.account_id=a.id "
                            "WHERE s.discord_id=?", (discord_id,)).fetchone()
            if row is None:  # pointer missing: fall back to the oldest account and remember it
                row = c.execute("SELECT * FROM accounts WHERE discord_id=? ORDER BY id LIMIT 1", (discord_id,)).fetchone()
                if row is not None:
                    c.execute("INSERT OR REPLACE INTO active_account(discord_id, account_id) VALUES (?,?)",
                              (discord_id, row["id"]))
            return row
        return await self._tx(work)

    async def set_active_account(self, discord_id: int, account_id: int) -> bool:
        def work(c):
            if not c.execute("SELECT 1 FROM accounts WHERE id=? AND discord_id=?", (account_id, discord_id)).fetchone():
                return False
            c.execute("INSERT INTO active_account(discord_id, account_id) VALUES (?,?) "
                      "ON CONFLICT(discord_id) DO UPDATE SET account_id=excluded.account_id", (discord_id, account_id))
            return True
        return await self._tx(work)

    async def delete_account(self, discord_id: int, account_id: int) -> bool:
        """Removes one account with its files and renders from the bot. The next oldest account becomes active."""
        def work(c):
            if not c.execute("SELECT 1 FROM accounts WHERE id=? AND discord_id=?", (account_id, discord_id)).fetchone():
                return False
            c.execute("DELETE FROM files WHERE account_id=?", (account_id,))
            c.execute("DELETE FROM renders WHERE account_id=?", (account_id,))
            c.execute("DELETE FROM accounts WHERE id=?", (account_id,))
            c.execute("DELETE FROM active_account WHERE discord_id=? AND account_id=?", (discord_id, account_id))
            nxt = c.execute("SELECT id FROM accounts WHERE discord_id=? ORDER BY id LIMIT 1", (discord_id,)).fetchone()
            if nxt is not None:
                c.execute("INSERT OR IGNORE INTO active_account(discord_id, account_id) VALUES (?,?)", (discord_id, nxt[0]))
            return True
        return await self._tx(work)

    async def delete_all_accounts(self, discord_id: int) -> None:
        def work(c):
            for table in ("files", "renders", "accounts", "active_account"):
                c.execute(f"DELETE FROM {table} WHERE discord_id=?", (discord_id,))
        await self._tx(work)

    # ---- files (always scoped to ONE Kaggle account: the datasets live in that account) ----
    async def add_file(self, discord_id: int, account_id: int, kind: str, name: str, slug: str, size: int,
                       stored_name: Optional[str] = None) -> int:
        return await self._exec(
            "INSERT INTO files(discord_id, account_id, kind, original_name, dataset_slug, size_bytes, created_at, stored_name) "
            "VALUES (?,?,?,?,?,?,?,?)",
            (discord_id, account_id, kind, name, slug, size, int(time.time()), stored_name or name),
        )

    async def list_files(self, account_id: int, limit: int = 25) -> list[sqlite3.Row]:
        return await self._exec(
            "SELECT * FROM files WHERE account_id=? ORDER BY id DESC LIMIT ?",
            (account_id, limit),
            "all",
        )

    async def get_file(self, account_id: int, file_id: int) -> Optional[sqlite3.Row]:
        return await self._exec("SELECT * FROM files WHERE id=? AND account_id=?", (file_id, account_id), "one")

    async def delete_file(self, account_id: int, file_id: int) -> None:
        await self._exec("DELETE FROM files WHERE id=? AND account_id=?", (file_id, account_id))

    # ---- renders ----
    async def add_render(self, discord_id: int, account_id: int, channel_id: Optional[int], slug: str, mode: str,
                         blend_name: str, config_json: str, status: str = "queued") -> int:
        return await self._exec(
            "INSERT INTO renders(discord_id, account_id, channel_id, kernel_slug, mode, blend_name, config_json, status, created_at) "
            "VALUES (?,?,?,?,?,?,?,?,?)",
            (discord_id, account_id, channel_id, slug, mode, blend_name, config_json, status, int(time.time())),
        )

    async def fail_preparing(self) -> None:
        """A crash while a render was being sent to Kaggle leaves 'preparing' rows nobody will finish."""
        await self._exec("UPDATE renders SET status='error', finished_at=? WHERE status='preparing'",
                         (int(time.time()),))

    async def monitored_renders(self) -> list[sqlite3.Row]:
        return await self._exec("SELECT * FROM renders WHERE status IN ('queued','running') ORDER BY id", (), "all")

    async def set_render_status(self, render_id: int, status: str, finished: bool = False) -> None:
        now = int(time.time())
        await self._exec("UPDATE renders SET status=?, finished_at=?, "
                         "started_at=CASE WHEN ?='running' THEN COALESCE(started_at, ?) ELSE started_at END WHERE id=?",
                         (status, now if finished else None, status, now, render_id))

    async def set_render_seconds(self, render_id: int, seconds: int) -> None:
        """Real run time reported by the Kaggle script (render_status.json): the best number for /quota."""
        await self._exec("UPDATE renders SET run_seconds=? WHERE id=?", (int(seconds), render_id))

    async def gpu_usage(self, account_id: int, window_sec: int) -> tuple[int, bool]:
        """(seconds of this account's bot renders inside the last `window_sec`, is one running now)."""
        now = int(time.time())
        rows = await self._exec(
            "SELECT status, created_at, started_at, finished_at, run_seconds FROM renders "
            "WHERE account_id=? AND status!='preparing' AND (finished_at IS NULL OR finished_at>=?)",
            (account_id, now - window_sec), "all")
        total, running = 0, False
        for r in rows:
            end = r["finished_at"] or now
            if r["run_seconds"]:
                start = end - r["run_seconds"]
            elif r["started_at"]:
                start = r["started_at"]
            elif r["status"] in ("running", "complete", "timeout"):
                start = r["created_at"]  # old row / very short render: includes the queue time (slight overestimate)
            else:
                continue  # never ran (failed to start, still queued, cancelled before start)
            if r["status"] == "running":
                running = True
            total += max(0, end - max(start, now - window_sec))
        return total, running

    async def active_renders(self, account_id: Optional[int] = None) -> list[sqlite3.Row]:
        if account_id is None:
            return await self._exec("SELECT * FROM renders WHERE status IN ('preparing','queued','running') ORDER BY id", (), "all")
        return await self._exec("SELECT * FROM renders WHERE account_id=? AND status IN ('preparing','queued','running')",
                                (account_id,), "all")

    async def list_renders(self, discord_id: int, limit: int = 8) -> list[sqlite3.Row]:
        return await self._exec(
            "SELECT r.*, a.kaggle_username FROM renders r LEFT JOIN accounts a ON a.id=r.account_id "
            "WHERE r.discord_id=? ORDER BY r.id DESC LIMIT ?", (discord_id, limit), "all")
