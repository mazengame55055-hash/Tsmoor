from __future__ import annotations

import asyncio
import shutil
import uuid
from pathlib import Path
from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .. import config, kaggle_client
from ..accounts import load_credentials
from ..downloader import DownloadError, download_url
from ..kaggle_client import KaggleError
from ..utils import (chunk_lines, cooldown, free_disk_bytes, human_size, looks_like_blend, make_slug, make_title,
                     sanitize_filename)

EXT_KIND = {
    ".blend": "blend",
    ".mp3": "audio", ".wav": "audio", ".flac": "audio", ".ogg": "audio", ".m4a": "audio",
}
KIND_LABEL = {"blend": "🧊 Blender", "audio": "🎵 صوت"}


class UserError(Exception):
    """Message is safe to show to the user as-is."""


class Files(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._locks: dict[int, asyncio.Lock] = {}
        self._slots = asyncio.Semaphore(config.UPLOAD_SLOTS)  # simultaneous uploads (each is staged on disk)

    async def _notify(self, interaction: discord.Interaction, text: str) -> None:
        try:
            await interaction.edit_original_response(content=text)
        except discord.HTTPException:
            # Interaction token expires after 15 min; large uploads can outlive it.
            try:
                await interaction.user.send(text)
            except discord.HTTPException:
                pass

    @app_commands.command(name="upload", description="ارفع ملف .blend أو صوت على حساب Kaggle بتاعك")
    @cooldown(20)
    @app_commands.describe(file="الملف (حد Discord للمرفقات صغير)", url="أو رابط تحميل مباشر للملفات الكبيرة")
    async def upload(self, interaction: discord.Interaction,
                     file: Optional[discord.Attachment] = None, url: Optional[str] = None) -> None:
        if bool(file) == bool(url):
            await interaction.response.send_message("اختار **ملف** أو **رابط** (واحد بس).", ephemeral=True)
            return
        creds = await load_credentials(self.bot.db, interaction.user.id)
        if not creds:
            await interaction.response.send_message("اعمل `/login` الأول.", ephemeral=True)
            return
        if len(await self.bot.db.list_files(creds.account_id, limit=1000)) >= config.MAX_FILES_PER_USER:
            await interaction.response.send_message(
                f"وصلت للحد الأقصى ({config.MAX_FILES_PER_USER} ملف). امسح ملفات قديمة بـ `/delete`.", ephemeral=True)
            return
        lock = self._locks.setdefault(interaction.user.id, asyncio.Lock())
        if lock.locked():
            await interaction.response.send_message("لسه فيه رفع شغال عندك، استنى يخلص.", ephemeral=True)
            return

        await interaction.response.defer(ephemeral=True, thinking=True)
        async with lock, self._slots:
            stage = config.STAGING_DIR / str(interaction.user.id) / uuid.uuid4().hex
            stage.mkdir(parents=True)
            try:
                msg = await self._process(interaction, creds, stage, file, url)
            except (UserError, DownloadError) as e:
                msg = f"❌ {e}"
            except KaggleError as e:
                msg = f"❌ Kaggle رفض العملية:\n```{e}```"
            finally:
                await asyncio.to_thread(shutil.rmtree, stage, True)
        await self._notify(interaction, msg)

    @staticmethod
    def _check_disk(size: int) -> None:
        need = size + config.MIN_FREE_DISK_MB * 1024 * 1024
        if free_disk_bytes(config.STAGING_DIR) < need:
            raise UserError("مساحة السيرفر مش كفاية للملف ده دلوقتي. جرّب ملف أصغر أو استنى شوية.")

    async def _process(self, interaction, creds, stage: Path,
                       file: Optional[discord.Attachment], url: Optional[str]) -> str:
        await self._notify(interaction, "⏳ جاري تنزيل الملف...")
        if file:
            if file.size > config.MAX_FILE_BYTES:
                raise UserError(f"الملف أكبر من {config.MAX_FILE_MB} MB.")
            self._check_disk(file.size)
            path = stage / sanitize_filename(file.filename)
            await file.save(path)
        else:
            budget = min(config.MAX_FILE_BYTES, max(0, free_disk_bytes(stage) - config.MIN_FREE_DISK_MB * 1024 * 1024))
            if budget < 1024 * 1024:
                raise UserError("مساحة السيرفر ممتلئة دلوقتي، جرّب بعد شوية.")
            path = await download_url(url.strip(), stage, budget)

        kind = EXT_KIND.get(path.suffix.lower())
        if not kind:
            raise UserError("النوع ده مش مدعوم. المسموح: `.blend` و `.mp3 .wav .flac .ogg .m4a`.")
        size = path.stat().st_size
        if size == 0:
            raise UserError("الملف فاضي.")
        if kind == "blend" and not looks_like_blend(path):
            raise UserError("الملف ده مش ملف Blender سليم (الهيدر غلط أو الملف ناقص).")

        await self._notify(interaction, f"☁️ جاري الرفع على Kaggle ({human_size(size)})... ممكن ياخد وقت.")
        # The name the user sees keeps their own spelling (Arabic etc.); the name inside Kaggle stays ASCII-safe.
        display = Path(file.filename).name[:100] if file and file.filename else path.name
        slug = make_slug(path.stem)
        link = await kaggle_client.create_dataset(creds, stage, slug, make_title(path.stem))
        file_id = await self.bot.db.add_file(interaction.user.id, creds.account_id, kind, display, slug, size, path.name)
        await self._notify(interaction, "🔄 Kaggle بيجهّز الملف...")
        try:
            ready = await kaggle_client.wait_dataset_ready(creds, slug, timeout=120)
        except KaggleError as e:  # Kaggle itself failed to process the file: do not keep a dead entry
            await self.bot.db.delete_file(creds.account_id, file_id)
            try:
                await kaggle_client.delete_dataset(creds, slug)
            except KaggleError:
                pass
            raise e
        state = ("✅ الملف جاهز للرندر." if ready else
                 "⏳ Kaggle لسه بيعالج الملف. لو `/render` قالك إنه مش جاهز، استنى دقيقتين وجرّب.")
        return (f"✅ اترفع **{display}** ({human_size(size)}) كـ Dataset **خاص** على حساب **{creds.username}**.\n"
                f"🆔 رقم الملف: `{file_id}`\n🔗 {link}\n{state}")

    @app_commands.command(name="files", description="اعرض ملفاتك المرفوعة")
    @cooldown(5)
    async def files(self, interaction: discord.Interaction) -> None:
        creds = await load_credentials(self.bot.db, interaction.user.id)
        if not creds:
            await interaction.response.send_message("اعمل `/login` الأول.", ephemeral=True)
            return
        rows = await self.bot.db.list_files(creds.account_id)
        if not rows:
            await interaction.response.send_message("مفيش ملفات لسه. استخدم `/upload`.", ephemeral=True)
            return
        lines = [
            f"`#{r['id']}` {KIND_LABEL.get(r['kind'], r['kind'])} • `{r['original_name']}` • "
            f"{human_size(r['size_bytes'])} • "
            f"[Kaggle](https://www.kaggle.com/datasets/{creds.username}/{r['dataset_slug']})"
            for r in rows
        ]
        # Discord rejects messages above 2000 characters, and 25 lines with links easily pass that.
        chunks = chunk_lines(lines)
        await interaction.response.send_message(f"**ملفات حساب {creds.username} (آخر 25):**\n" + chunks[0], ephemeral=True)
        for extra in chunks[1:]:
            await interaction.followup.send(extra, ephemeral=True)


    @app_commands.command(name="delete", description="امسح ملف من Kaggle ومن قايمتك")
    @app_commands.describe(file="الملف اللي عايز تمسحه")
    @cooldown(10)
    async def delete(self, interaction: discord.Interaction, file: int) -> None:
        uid = interaction.user.id
        creds = await load_credentials(self.bot.db, uid)
        if not creds:
            await interaction.response.send_message("اعمل `/login` الأول.", ephemeral=True)
            return
        row = await self.bot.db.get_file(creds.account_id, file)
        if not row:
            await interaction.response.send_message("مفيش ملف بالرقم ده عندك.", ephemeral=True)
            return
        if await self.bot.db.active_renders(creds.account_id):
            await interaction.response.send_message("عندك رندر شغال، استنى يخلص قبل ما تمسح ملفات.", ephemeral=True)
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        try:
            await kaggle_client.delete_dataset(creds, row["dataset_slug"])
        except KaggleError as e:
            await interaction.followup.send(f"❌ Kaggle رفض المسح:\n```{e}```", ephemeral=True)
            return
        await self.bot.db.delete_file(creds.account_id, file)
        await interaction.followup.send(f"🗑️ اتمسح `{row['original_name']}` من Kaggle ومن قايمتك.", ephemeral=True)

    @delete.autocomplete("file")
    async def delete_autocomplete(self, interaction: discord.Interaction, current: str):
        acc = await self.bot.db.get_active_account(interaction.user.id)
        if not acc:
            return []
        rows = await self.bot.db.list_files(acc["id"], limit=100)
        needle = current.lower().strip()
        return [app_commands.Choice(name=f"#{r['id']} {r['original_name']}"[:100], value=r["id"])
                for r in rows if not needle or needle in r["original_name"].lower()][:25]


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Files(bot))
