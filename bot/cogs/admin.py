"""Owner-only tools: /backup and the automatic daily backup of the database."""
from __future__ import annotations

import asyncio
import io
import logging
import sqlite3
import tarfile
import time
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import config, crypto
from ..db import Database
from ..utils import cooldown, human_size

log = logging.getLogger("renderbot")
PREFIX = "renderbot-backup-"
SAFETY_PREFIX = "renderbot-before-restore-"  # never removed by the daily rotation of automatic backups


def _write_archive(db_copy: Path, out: Path, key: Optional[bytes]) -> None:
    with tarfile.open(out, "w:gz") as tar:
        tar.add(db_copy, arcname="bot.sqlite3")
        if key:
            info = tarfile.TarInfo("encryption.key")
            info.size, info.mtime, info.mode = len(key), int(time.time()), 0o600
            tar.addfile(info, io.BytesIO(key))


async def make_backup(db, dest_dir: Path, include_key: bool = False, prefix: str = PREFIX) -> Path:
    """Snapshot of the database packed as .tar.gz (optionally with the encryption key)."""
    dest_dir.mkdir(parents=True, exist_ok=True)
    now = time.time()
    stamp = time.strftime("%Y%m%d-%H%M%S", time.gmtime(now)) + f"-{int(now * 1000) % 1000:03d}"  # ms: no name clashes
    snap = dest_dir / f"snapshot-{stamp}.sqlite3"
    archive = dest_dir / f"{prefix}{stamp}{'-WITH-KEY' if include_key else ''}.tar.gz"
    try:
        await db.backup_to(snap)
        await asyncio.to_thread(_write_archive, snap, archive, config.ENCRYPTION_KEY if include_key else None)
    finally:
        snap.unlink(missing_ok=True)
    return archive


def rotate(dest_dir: Path, keep: int, prefix: str = PREFIX) -> None:
    old = sorted(dest_dir.glob(f"{prefix}*.tar.gz"), key=lambda p: p.stat().st_mtime, reverse=True)
    for p in old[keep:]:
        p.unlink(missing_ok=True)


MAX_RESTORE_BYTES = 50 * 1024 * 1024


class RestoreError(Exception):
    """Message is safe to show to the owner as-is."""


def _extract_db(data: bytes) -> tuple[bytes, Optional[bytes]]:
    """(database bytes, encryption key bytes or None) from a .tar.gz made by /backup or a raw .sqlite3 file."""
    if data[:2] == b"\x1f\x8b":
        try:
            with tarfile.open(fileobj=io.BytesIO(data), mode="r:gz") as tar:
                names = set(tar.getnames())
                if "bot.sqlite3" not in names:
                    raise RestoreError("الأرشيف مفيهوش `bot.sqlite3`. ده مش ملف نسخة احتياطية من البوت.")
                # Only these two members are ever read, and never written by name: no path tricks possible.
                db_member = tar.extractfile("bot.sqlite3")
                key_member = tar.extractfile("encryption.key") if "encryption.key" in names else None
                return db_member.read(MAX_RESTORE_BYTES + 1), (key_member.read(1024) if key_member else None)
        except (tarfile.TarError, OSError, EOFError):
            raise RestoreError("مقدرتش أفتح الأرشيف (تالف أو مش `.tar.gz`).")
    if data[:16] == b"SQLite format 3\x00":
        return data, None
    raise RestoreError("نوع الملف مش مفهوم. ابعت ملف `.tar.gz` من `/backup` أو ملف `.sqlite3`.")


def _validate(path: Path) -> None:
    try:
        c = sqlite3.connect(str(path))
        try:
            ok = c.execute("PRAGMA integrity_check").fetchone()[0]
            tables = {r[0] for r in c.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        finally:
            c.close()
    except sqlite3.DatabaseError:
        raise RestoreError("الملف ده مش قاعدة بيانات سليمة.")
    if ok != "ok":
        raise RestoreError("قاعدة البيانات في الملف تالفة (فشل فحص السلامة).")
    if not ({"accounts", "users"} & tables) or "files" not in tables or "renders" not in tables:
        raise RestoreError("القاعدة دي مش بتاعة البوت ده (جداول ناقصة).")


async def restore_from_bytes(db, data: bytes, work_dir: Path) -> str:
    """Validates a backup and, only if everything is fine, replaces the live database with it."""
    if len(data) > MAX_RESTORE_BYTES:
        raise RestoreError("الملف كبير أوي.")
    raw, key_in_archive = await asyncio.to_thread(_extract_db, data)
    if len(raw) > MAX_RESTORE_BYTES:
        raise RestoreError("قاعدة البيانات اللي في الملف كبيرة أوي.")
    work_dir.mkdir(parents=True, exist_ok=True)
    tmp = work_dir / f"restore-{int(time.time())}.sqlite3"
    try:
        tmp.write_bytes(raw)
        await asyncio.to_thread(_validate, tmp)
        staged = Database(tmp)  # runs the same migrations as a normal start, so OLD backups restore fine too
        accounts = await staged.list_all_accounts()
        # Tokens are only readable with the SAME key. Refuse before touching anything if they are not.
        for row in accounts[:3]:
            try:
                crypto.decrypt(row["token_enc"])
            except crypto.InvalidToken:
                same = key_in_archive is not None and key_in_archive.strip() == config.ENCRYPTION_KEY
                hint = ("المفتاح اللي جوه النسخة مختلف عن مفتاح البوت الحالي. حطه في متغير `ENCRYPTION_KEY` "
                        "وأعد تشغيل البوت، وبعدين كرر الاسترجاع." if key_in_archive and not same else
                        "التوكينات في النسخة مشفّرة بمفتاح مختلف عن مفتاح البوت الحالي. حط المفتاح الأصلي في "
                        "`ENCRYPTION_KEY` وأعد تشغيل البوت، وبعدين كرر الاسترجاع.")
                raise RestoreError("⛔ " + hint + "\n(ماتغيّرش أي حاجة.)")
        counts = await staged.counts()
        staged.close()
        safety = await make_backup(db, config.BACKUP_DIR, prefix=SAFETY_PREFIX)  # undo point for the CURRENT data
        await asyncio.to_thread(rotate, config.BACKUP_DIR, 3, SAFETY_PREFIX)  # keep the last 3 undo points
        await db.restore_from(tmp)
        await db.fail_preparing()
        return (f"✅ اترجّعت النسخة: **{counts['accounts']}** حساب • **{counts['files']}** ملف • "
                f"**{counts['renders']}** رندر.\n💾 نسخة أمان من البيانات اللي كانت قبل الاسترجاع: `{safety.name}` "
                "(على الـ Volume في `data/backups`).")
    finally:
        tmp.unlink(missing_ok=True)


class Admin(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._marker = config.BACKUP_DIR / ".last_dm"

    async def cog_load(self) -> None:
        if config.BACKUP_INTERVAL_HOURS > 0:
            self.auto_backup.change_interval(hours=config.BACKUP_INTERVAL_HOURS)
            self.auto_backup.start()

    async def cog_unload(self) -> None:
        self.auto_backup.cancel()

    # ------------------------------------------------------------ automatic
    @tasks.loop(hours=24)
    async def auto_backup(self) -> None:
        try:
            archive = await make_backup(self.bot.db, config.BACKUP_DIR)
            await asyncio.to_thread(rotate, config.BACKUP_DIR, config.BACKUP_KEEP)
            log.info("Automatic backup saved: %s (%s)", archive.name, human_size(archive.stat().st_size))
            if config.BACKUP_DM_OWNER and config.OWNER_ID:
                await self._dm_owner(archive)
        except Exception:  # noqa: BLE001  a failed backup must never stop the loop
            log.exception("Automatic backup failed")

    @auto_backup.before_loop
    async def _before(self) -> None:
        await self.bot.wait_until_ready()

    async def _dm_owner(self, archive: Path) -> None:
        # Restarts/deploys also trigger a backup: only DM about once per interval, not on every restart.
        try:
            last = float(self._marker.read_text())
        except (OSError, ValueError):
            last = 0.0
        if time.time() - last < config.BACKUP_INTERVAL_HOURS * 3600 * 0.9:
            return
        size = archive.stat().st_size
        if size > config.DISCORD_UPLOAD_LIMIT_MB * 1024 * 1024:
            log.warning("Backup is %s: too big to DM (limit %d MB). It is still saved on disk.",
                        human_size(size), config.DISCORD_UPLOAD_LIMIT_MB)
            return
        try:
            user = await self.bot.fetch_user(config.OWNER_ID)
            await user.send(
                f"🗄️ نسخة احتياطية تلقائية ({human_size(size)}).\n"
                "فيها قاعدة البيانات **بدون مفتاح التشفير**؛ احتفظ بالمفتاح (`ENCRYPTION_KEY`) في مكان تاني.",
                file=discord.File(archive))
            self._marker.write_text(str(time.time()))
        except discord.HTTPException:
            log.warning("Could not DM the backup to the owner (DMs closed or no shared server).")

    # ------------------------------------------------------------ /backup
    @app_commands.command(name="backup", description="(للمالك فقط) ابعتلي نسخة احتياطية من قاعدة البيانات")
    @app_commands.describe(include_key="ضيف مفتاح التشفير في الملف (حساس جداً: بيفتح كل التوكينات)")
    @cooldown(30)
    async def backup(self, interaction: discord.Interaction, include_key: bool = False) -> None:
        if interaction.user.id != config.OWNER_ID:
            await interaction.response.send_message("🔒 الأمر ده لصاحب البوت بس.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        tmp = config.STAGING_DIR / "backup"
        archive: Optional[Path] = None
        try:
            archive = await make_backup(self.bot.db, tmp, include_key)
            size = archive.stat().st_size
            if size > config.DISCORD_UPLOAD_LIMIT_MB * 1024 * 1024:
                await interaction.followup.send(
                    f"❌ النسخة ({human_size(size)}) أكبر من حد الرفع ({config.DISCORD_UPLOAD_LIMIT_MB} MB). "
                    "حمّلها من الـ Volume بدل كده.", ephemeral=True)
                return
            warn = ("\n⚠️ **الملف فيه مفتاح التشفير**: أي حد يوصله يفك كل التوكينات. احذفه بعد ما تحفظه."
                    if include_key else "\nالملف **من غير** مفتاح التشفير؛ احتفظ بـ `ENCRYPTION_KEY` منفصل.")
            await interaction.followup.send(f"🗄️ نسخة احتياطية ({human_size(size)}).{warn}",
                                            file=discord.File(archive), ephemeral=True)
        finally:
            await asyncio.to_thread(lambda: [p.unlink(missing_ok=True) for p in tmp.glob("*")])


    # ------------------------------------------------------------ /restore
    @app_commands.command(name="restore", description="(للمالك فقط) استرجع نسخة احتياطية من ملف")
    @app_commands.describe(file="ملف النسخة (.tar.gz من /backup أو .sqlite3)",
                           confirm="لازم تختار True: الاسترجاع بيستبدل كل البيانات الحالية")
    @cooldown(30)
    async def restore(self, interaction: discord.Interaction, file: discord.Attachment, confirm: bool = False) -> None:
        if interaction.user.id != config.OWNER_ID:
            await interaction.response.send_message("🔒 الأمر ده لصاحب البوت بس.", ephemeral=True)
            return
        if not confirm:
            await interaction.response.send_message(
                "⚠️ الاسترجاع **بيستبدل كل البيانات الحالية** (الحسابات والملفات والرندرات). "
                "اعمل الأمر تاني واختار `confirm: True`. (هتتعمل نسخة أمان تلقائي قبل الاستبدال.)", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            if file.size > MAX_RESTORE_BYTES:
                raise RestoreError("الملف كبير أوي.")
            msg = await restore_from_bytes(self.bot.db, await file.read(), config.STAGING_DIR / "restore")
            log.warning("Database restored from %s by the owner", file.filename)
        except RestoreError as e:
            msg = f"❌ {e}"
        except Exception:  # noqa: BLE001
            log.exception("restore failed")
            msg = "❌ حصل خطأ غير متوقع أثناء الاسترجاع. البيانات الحالية لسه زي ما هي."
        await interaction.followup.send(msg, ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Admin(bot))
