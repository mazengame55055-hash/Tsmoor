from __future__ import annotations

import asyncio
import json
import logging
import secrets
import shutil
import time
import uuid
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Awaitable, Callable, Optional

import discord
from discord import app_commands
from discord.ext import commands, tasks

from .. import config, kaggle_client, kernel_builder
from ..accounts import load_credentials
from ..kaggle_client import Credentials, KaggleError, OutputTooLarge
from ..render_config import (CODECS, ConfigError, RenderConfig, parse_frames, parse_int)
from ..utils import cooldown, human_size, member_gate, report_ui_error

log = logging.getLogger("renderbot.render")

STATUS_LABEL = {"queued": "⏳ في الانتظار", "running": "🔥 شغال", "complete": "✅ خلص",
                "error": "❌ فشل", "preparing": "📤 بيتبعت لـ Kaggle", "cancelled": "🛑 اتلغى", "timeout": "⌛ انتهى الوقت"}
ENGINE_LABEL = {"KEEP_ORIGINAL": "من الملف (زي ما هو)", "CYCLES": "Cycles", "EEVEE": "EEVEE"}
COLOR_LABEL = {"KEEP_ORIGINAL": "من الملف (زي ما هو)", "AgX": "AgX", "Filmic": "Filmic", "Standard": "Standard"}


# =========================================================================== draft + views
@dataclass
class Draft:
    blends: list
    audios: list
    blend_id: int
    audio_id: int = 0
    account_id: int = 0  # the Kaggle account this menu was opened for (fixed: switching accounts later must not change it)
    account_name: str = ""
    custom: RenderConfig = field(default_factory=lambda: RenderConfig(mode="custom", frame_start=1, frame_end=96))

    def _row(self, rows: list, rid: int):
        return next((r for r in rows if r["id"] == rid), None)

    def build(self, mode: str) -> RenderConfig:
        b = self._row(self.blends, self.blend_id)
        a = self._row(self.audios, self.audio_id)
        # Original = the file as saved: never inherit values the user tried in the Custom screen
        # (codec/CRF would otherwise leak into an "Original" render after Custom -> Back -> Original).
        base = self.custom if mode == "custom" else RenderConfig(mode="original",
                                                                  eevee_to_cycles=self.custom.eevee_to_cycles)
        return replace(
            base, mode=mode,
            blend_slug=b["dataset_slug"] if b else "", blend_name=(b["stored_name"] or b["original_name"]) if b else "",
            blend_label=b["original_name"] if b else "",
            audio_slug=a["dataset_slug"] if a else "", audio_name=(a["stored_name"] or a["original_name"]) if a else "",
            audio_label=a["original_name"] if a else "",
            force_gpu=config.ORIGINAL_FORCE_GPU if mode == "original" else True,
        )

    def files_text(self) -> str:
        b = self._row(self.blends, self.blend_id)
        a = self._row(self.audios, self.audio_id)
        who = f"👤 حساب Kaggle: **{self.account_name}**\n" if self.account_name else ""
        return who + f"🧊 **{b['original_name'] if b else '—'}**\n🎵 {a['original_name'] if a else 'بدون صوت'}"

    def start_text(self) -> str:
        return ("**اختار الملف ونوع الرندر:**\n" + self.files_text() + "\n\n"
                "🎯 **Original**: الملف زي ما هو متسيّف، بدون أي تعديل.\n"
                "🎛️ **Custom**: تتحكم في Samples و FPS والفريمات والكوديك وغيره.\n"
                "🔁 **EEVEE→Cycles**: لو ملفك EEVEE، شغّلها (EEVEE بيعلّق على Kaggle كتير). "
                "بتغيّر شكل الرندر لأن المحرك بيتغيّر."
                + ("\n✅ **مفعّلة**" if self.custom.eevee_to_cycles else ""))

    def custom_text(self) -> str:
        return ("**🎛️ Custom — ظبّط الإعدادات:**\n" + self.files_text() + "\n\n"
                + self.build("custom").summary()
                + f"\n🧹 Denoise: {'شغال' if self.custom.denoise else 'مقفول'}")


EXPIRED_TEXT = "⌛ القايمة دي انتهت. اكتب `/render` من جديد."


class OwnerView(discord.ui.View):
    def __init__(self, owner_id: int, timeout: float = 840):
        super().__init__(timeout=timeout)
        self.owner_id = owner_id
        self.message: Optional[discord.PartialMessage] = None  # edited with the bot token, so it outlives the 15-min interaction token

    async def on_timeout(self) -> None:
        # Not called for views that were stop()ped (switched screen / render launched), so this only
        # fires for a menu that was really abandoned.
        if self.message:
            try:
                await self.message.edit(content=EXPIRED_TEXT, view=None)
            except discord.HTTPException:
                pass

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await report_ui_error(interaction, error, "view")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("القايمة دي مش بتاعتك.", ephemeral=True)
            return False
        return await member_gate(interaction)


class ChoiceSelect(discord.ui.Select):
    def __init__(self, placeholder: str, options: list[tuple[str, str, str]], current: str,
                 on_pick: Callable[[discord.Interaction, str], Awaitable[None]], row: int):
        opts = [discord.SelectOption(label=label[:100], value=value, description=(desc or None)[:100] if desc else None,
                                     default=(value == current)) for value, label, desc in options]
        super().__init__(placeholder=placeholder, options=opts, min_values=1, max_values=1, row=row)
        self._on_pick = on_pick

    async def callback(self, interaction: discord.Interaction) -> None:
        value = self.values[0]
        for o in self.options:
            o.default = (o.value == value)
        await self._on_pick(interaction, value)


class StartView(OwnerView):
    def __init__(self, cog: "Render", draft: Draft, owner_id: int):
        super().__init__(owner_id)
        self.cog, self.draft = cog, draft
        self.add_item(ChoiceSelect(
            "🧊 اختار ملف الـ Blend",
            [(str(r["id"]), r["original_name"], human_size(r["size_bytes"])) for r in draft.blends[:25]],
            str(draft.blend_id), self._pick_blend, row=0))
        if draft.audios:
            self.add_item(ChoiceSelect(
                "🎵 صوت (اختياري)",
                [("0", "بدون صوت", "")] + [(str(r["id"]), r["original_name"], human_size(r["size_bytes"]))
                                           for r in draft.audios[:24]],
                str(draft.audio_id), self._pick_audio, row=1))
        on = draft.custom.eevee_to_cycles
        self.eevee_toggle.label = f"🔁 EEVEE→Cycles: {'ON' if on else 'OFF'}"
        self.eevee_toggle.style = discord.ButtonStyle.success if on else discord.ButtonStyle.secondary

    async def _pick_blend(self, interaction: discord.Interaction, value: str) -> None:
        self.draft.blend_id = int(value)
        await interaction.response.edit_message(content=self.draft.start_text(), view=self)

    async def _pick_audio(self, interaction: discord.Interaction, value: str) -> None:
        self.draft.audio_id = int(value)
        await interaction.response.edit_message(content=self.draft.start_text(), view=self)

    @discord.ui.button(label="🔁 EEVEE→Cycles: OFF", style=discord.ButtonStyle.secondary, row=3)
    async def eevee_toggle(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        on = not self.draft.custom.eevee_to_cycles
        self.draft.custom = replace(self.draft.custom, eevee_to_cycles=on)
        button.label = f"🔁 EEVEE→Cycles: {'ON' if on else 'OFF'}"
        button.style = discord.ButtonStyle.success if on else discord.ButtonStyle.secondary
        await interaction.response.edit_message(content=self.draft.start_text(), view=self)

    @discord.ui.button(label="🎯 Original", style=discord.ButtonStyle.success, row=2)
    async def original(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await self.cog.launch(interaction, self.draft.build("original"), self.draft.account_id)

    @discord.ui.button(label="🎛️ Custom", style=discord.ButtonStyle.primary, row=2)
    async def custom(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        view = CustomView(self.cog, self.draft, self.owner_id)
        view.message = self.message
        await interaction.response.edit_message(content=self.draft.custom_text(), view=view)


class NumbersModal(discord.ui.Modal, title="أرقام الرندر"):
    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_ui_error(interaction, error, "numbers modal")

    def __init__(self, view: "CustomView"):
        super().__init__()
        self.parent_view = view
        c = view.draft.custom
        frames = (f"{c.frame_start}" if c.frame_start == c.frame_end else
                  f"{c.frame_start}-{c.frame_end}" + (f"-{c.frame_step}" if c.frame_step > 1 else ""))
        self.frames = discord.ui.TextInput(label="الفريمات (1-96 أو 40 أو 1-96-2)", default=frames, max_length=24)
        self.samples = discord.ui.TextInput(label="Samples", default=str(c.samples), max_length=5)
        self.fps = discord.ui.TextInput(label="FPS", default=str(c.fps), max_length=3)
        self.res = discord.ui.TextInput(label="Resolution %", default=str(c.resolution_percentage), max_length=3)
        self.crf = discord.ui.TextInput(label="جودة الفيديو CRF (الأقل = أحسن، 18 ممتاز)", default=str(c.crf), max_length=2)
        for item in (self.frames, self.samples, self.fps, self.res, self.crf):
            self.add_item(item)

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await member_gate(interaction):
            return
        try:
            start, end, step = parse_frames(self.frames.value)
            samples = parse_int(self.samples.value, "Samples", 1, 20000)
            fps = parse_int(self.fps.value, "FPS", 1, 240)
            res = parse_int(self.res.value, "Resolution %", 1, 200)
            crf = parse_int(self.crf.value, "CRF", 0, 51)
        except ConfigError as e:
            await interaction.response.send_message(f"❌ {e}", ephemeral=True)
            return
        self.parent_view.draft.custom = replace(
            self.parent_view.draft.custom, frame_start=start, frame_end=end, frame_step=step,
            samples=samples, fps=fps, resolution_percentage=res, crf=crf)
        await interaction.response.edit_message(content=self.parent_view.draft.custom_text(), view=self.parent_view)


class CustomView(OwnerView):
    def __init__(self, cog: "Render", draft: Draft, owner_id: int):
        super().__init__(owner_id)
        self.cog, self.draft = cog, draft
        c = draft.custom
        self.add_item(ChoiceSelect("🎞️ المحرك (Engine)", [(k, v, "") for k, v in ENGINE_LABEL.items()],
                                   c.engine, self._pick_engine, row=0))
        self.add_item(ChoiceSelect("🎨 تحويل الألوان", [(k, v, "") for k, v in COLOR_LABEL.items()],
                                   c.color_transform, self._pick_color, row=1))
        self.add_item(ChoiceSelect("🎬 الكوديك", [(k, v, "") for k, v in CODECS.items()],
                                   c.codec, self._pick_codec, row=2))
        self._sync_denoise_label()

    def _sync_denoise_label(self) -> None:
        self.denoise_btn.label = f"🧹 Denoise: {'ON' if self.draft.custom.denoise else 'OFF'}"
        self.denoise_btn.style = discord.ButtonStyle.success if self.draft.custom.denoise else discord.ButtonStyle.secondary

    async def _refresh(self, interaction: discord.Interaction) -> None:
        await interaction.response.edit_message(content=self.draft.custom_text(), view=self)

    async def _pick_engine(self, interaction, value):
        self.draft.custom = replace(self.draft.custom, engine=value)
        await self._refresh(interaction)

    async def _pick_color(self, interaction, value):
        self.draft.custom = replace(self.draft.custom, color_transform=value)
        await self._refresh(interaction)

    async def _pick_codec(self, interaction, value):
        self.draft.custom = replace(self.draft.custom, codec=value)
        await self._refresh(interaction)

    @discord.ui.button(label="⚙️ الأرقام", style=discord.ButtonStyle.primary, row=3)
    async def numbers(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        await interaction.response.send_modal(NumbersModal(self))

    @discord.ui.button(label="🧹 Denoise", style=discord.ButtonStyle.success, row=3)
    async def denoise_btn(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.draft.custom = replace(self.draft.custom, denoise=not self.draft.custom.denoise)
        self._sync_denoise_label()
        await self._refresh(interaction)

    @discord.ui.button(label="▶️ ابدأ الرندر", style=discord.ButtonStyle.success, row=3)
    async def start(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        await self.cog.launch(interaction, self.draft.build("custom"), self.draft.account_id)

    @discord.ui.button(label="◀️ رجوع", style=discord.ButtonStyle.secondary, row=3)
    async def back(self, interaction: discord.Interaction, button: discord.ui.Button) -> None:
        self.stop()
        view = StartView(self.cog, self.draft, self.owner_id)  # fresh view: an old one may already be timed out
        view.message = self.message
        await interaction.response.edit_message(content=self.draft.start_text(), view=view)


# =========================================================================== cog
class Render(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot
        self._launch_locks: dict[int, asyncio.Lock] = {}
        self._attempts: dict[int, int] = {}  # failed delivery attempts per render id
        self._status_fails: dict[int, int] = {}  # consecutive failed status polls per render id

    async def cog_load(self) -> None:
        self.monitor.start()

    async def cog_unload(self) -> None:
        self.monitor.cancel()

    # ---------------------------------------------------------------- /render
    @app_commands.command(name="render", description="ابدأ رندر Blender على Kaggle")
    @cooldown(20)
    async def render(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        creds = await load_credentials(self.bot.db, uid)
        if not creds:
            await interaction.response.send_message("اعمل `/login` الأول.", ephemeral=True)
            return
        if len(await self.bot.db.active_renders(creds.account_id)) >= config.MAX_ACTIVE_RENDERS_PER_USER:
            await interaction.response.send_message(
                f"عندك رندر شغال دلوقتي على حساب **{creds.username}**. شوفه بـ `/renders` واستنى يخلص، "
                "أو بدّل لحساب تاني بـ `/accounts`.", ephemeral=True)
            return
        files = await self.bot.db.list_files(creds.account_id, limit=200)
        blends = [r for r in files if r["kind"] == "blend"]
        audios = [r for r in files if r["kind"] == "audio"]
        if not blends:
            await interaction.response.send_message(
                f"مفيش ملف `.blend` على حساب **{creds.username}**. ارفع واحد بـ `/upload` (أو بدّل الحساب بـ `/accounts`).",
                ephemeral=True)
            return
        draft = Draft(blends=blends, audios=audios, blend_id=blends[0]["id"],
                      account_id=creds.account_id, account_name=creds.username)
        view = StartView(self, draft, uid)
        in_server = interaction.guild_id is not None
        # In servers the menu is private (ephemeral) so the render settings are not shown to everyone.
        await interaction.response.send_message(draft.start_text(), view=view, ephemeral=in_server)
        try:
            msg = await interaction.original_response()
            if in_server:
                view.message = msg  # ephemeral messages can only be edited through the interaction
            else:
                view.message = self.bot.get_partial_messageable(interaction.channel_id).get_partial_message(msg.id)
        except discord.HTTPException:
            pass

    async def _edit(self, interaction: discord.Interaction, text: str) -> bool:
        try:
            await interaction.edit_original_response(content=text, view=None)
            return True
        except discord.HTTPException:
            return False

    async def launch(self, interaction: discord.Interaction, cfg: RenderConfig, account_id: int) -> None:
        uid = interaction.user.id
        try:
            cfg.validate()
        except ConfigError as e:
            await interaction.response.edit_message(content=f"❌ {e}", view=None)
            return
        creds = await load_credentials(self.bot.db, uid, account_id)
        if not creds:
            await interaction.response.edit_message(content="الحساب ده اتمسح من البوت. اعمل `/login` وابدأ `/render` من جديد.", view=None)
            return
        slug = f"rb-render-{secrets.token_hex(3)}"
        # Check + reserve under a per-user lock: the row exists (as 'preparing') BEFORE the slow Kaggle push,
        # so a second /render cannot slip through while the first is still being sent.
        async with self._launch_locks.setdefault(account_id, asyncio.Lock()):
            if len(await self.bot.db.active_renders(account_id)) >= config.MAX_ACTIVE_RENDERS_PER_USER:
                await interaction.response.edit_message(content=f"عندك رندر شغال دلوقتي على حساب **{creds.username}**.", view=None)
                return
            # Results are always sent to the user's DM when the command was used in a server (or in a
            # server where the bot is not a member): never post a private result in a public channel.
            channel_id = interaction.channel_id if interaction.guild_id is None else None
            render_id = await self.bot.db.add_render(uid, account_id, channel_id, slug, cfg.mode,
                                                     cfg.blend_label or cfg.blend_name,
                                                     json.dumps(cfg.to_json_dict()), status="preparing")
        await interaction.response.edit_message(content="⏳ جاري تجهيز الرندر وإرساله لـ Kaggle...", view=None)

        folder = config.STAGING_DIR / str(uid) / uuid.uuid4().hex
        try:
            kernel_builder.write_script(folder, cfg)
            url = await self._push_with_retry(interaction, creds, folder, slug, kernel_builder.dataset_slugs(cfg))
            await self.bot.db.set_render_status(render_id, "queued")
        except KaggleError as e:
            await self.bot.db.set_render_status(render_id, "error", True)
            await self._edit(interaction, f"❌ Kaggle رفض تشغيل الرندر:\n```{e}```")
            return
        except Exception:
            await self.bot.db.set_render_status(render_id, "error", True)  # never leave a slot reserved forever
            raise
        finally:
            await asyncio.to_thread(shutil.rmtree, folder, True)

        text = (f"🚀 **الرندر #{render_id} اتبعت لـ Kaggle** (حساب **{creds.username}**)\n🧊 `{cfg.blend_label or cfg.blend_name}`\n{cfg.summary()}\n\n"
                f"🔗 {url}\n"
                "هبعتلك رسالة لما يبدأ، ولما يخلص هبعتلك النتيجة هنا.\n"
                "_Kaggle مش بيدّي نسبة تقدم لحظية، وبتابع حالة الرندر كل شوية._")
        if not await self._edit(interaction, text):  # push + retries can outlive the interaction token
            try:
                await interaction.user.send(text)
            except discord.HTTPException:
                pass

    async def _push_with_retry(self, interaction, creds: Credentials, folder: Path, slug: str,
                               datasets: list[str]) -> str:
        last: Optional[KaggleError] = None
        for attempt in range(1, 13):
            try:
                return await kaggle_client.push_kernel(creds, folder, slug, datasets, config.KAGGLE_ACCELERATOR)
            except KaggleError as e:
                last = e
                low = str(e).lower()
                # a freshly uploaded dataset takes a while to be usable
                if not any(w in low for w in ("not found", "does not exist", "does not resolve", "processing",
                                              "not ready", "not yet", "still being")):
                    raise
                await self._edit(interaction, f"⏳ Kaggle لسه بيجهّز ملفاتك... (محاولة {attempt}/12)")
                await asyncio.sleep(30)
        raise last or KaggleError("الملفات لسه مش جاهزة على Kaggle، جرّب بعد شوية.")

    # ---------------------------------------------------------------- /renders
    @app_commands.command(name="renders", description="اعرض آخر عمليات الرندر بتاعتك وحالتها")
    @cooldown(5)
    async def renders(self, interaction: discord.Interaction) -> None:
        if not await self.bot.db.list_accounts(interaction.user.id):
            await interaction.response.send_message("اعمل `/login` الأول.", ephemeral=True)
            return
        rows = await self.bot.db.list_renders(interaction.user.id)
        if not rows:
            await interaction.response.send_message("مفيش رندرات لسه. استخدم `/render`.", ephemeral=True)
            return
        lines = []
        for r in rows:
            end = r["finished_at"] or int(time.time())
            mins = max(0, (end - r["created_at"]) // 60)
            lines.append(f"`#{r['id']}` {STATUS_LABEL.get(r['status'], r['status'])} • `{r['blend_name']}` • "
                         f"{'🎯' if r['mode'] == 'original' else '🎛️'} • {mins} د • 👤 {r['kaggle_username'] or '?'} • "
                         f"[Kaggle]({kaggle_client.kernel_url(r['kaggle_username'] or '', r['kernel_slug'])})")
        await interaction.response.send_message(
            "**آخر الرندرات:**\n" + "\n".join(lines) +
            "\n\n_لإيقاف رندر شغال: افتح رابطه على Kaggle واضغط Stop (الـ API الرسمي مفيهوش إيقاف)._",
            ephemeral=True)

    # ---------------------------------------------------------------- background monitor
    @tasks.loop(seconds=45)
    async def monitor(self) -> None:
        slots = asyncio.Semaphore(4)  # check several users' renders in parallel, but not unbounded

        async def guarded(row) -> None:
            async with slots:
                try:
                    await self._check(row)
                except Exception:  # noqa: BLE001  one broken render must never stop the loop
                    log.exception("monitor failed for render %s", row["id"])

        try:
            rows = await self.bot.db.monitored_renders()
            await asyncio.gather(*(guarded(r) for r in rows))
        except Exception:  # noqa: BLE001
            log.exception("monitor cycle failed")

    @monitor.before_loop
    async def _before_monitor(self) -> None:
        await self.bot.wait_until_ready()

    async def _check(self, row) -> None:
        db = self.bot.db
        creds = await load_credentials(db, row["discord_id"], row["account_id"])
        if not creds:
            await db.set_render_status(row["id"], "error", True)
            await self._notify(row, f"⚠️ مقدرتش أكمّل متابعة الرندر #{row['id']}: التوكين اتمسح أو اتغيّر. "
                                    "اعمل `/login` من جديد، والرندر ممكن يكون كمّل على Kaggle في حسابك.")
            return
        ref = f"{creds.username}/{row['kernel_slug']}"
        age = int(time.time()) - row["created_at"]
        if config.RENDER_MAX_HOURS > 0 and age > config.RENDER_MAX_HOURS * 3600:
            await db.set_render_status(row["id"], "timeout", True)
            await self._notify(row, f"⌛ الرندر #{row['id']} عدّى {config.RENDER_MAX_HOURS} ساعة، بوقّف المتابعة.\n"
                                    f"🔗 {kaggle_client.kernel_url(creds.username, row['kernel_slug'])}")
            return
        try:
            status, failure = await kaggle_client.kernel_status(creds, ref)
            self._status_fails.pop(row["id"], None)  # a good poll resets the failure counter
        except KaggleError as e:
            n = self._status_fails.get(row["id"], 0) + 1
            self._status_fails[row["id"]] = n
            log.warning("status check %d failed for render %s: %s", n, row["id"], str(e)[:120])
            # one slow Kaggle answer must never kill a running render: only give up after
            # several consecutive failures. STATUS_MAX_FAILURES = 0 means NEVER give up.
            if config.STATUS_MAX_FAILURES > 0 and n >= config.STATUS_MAX_FAILURES and age > 20 * 60:
                self._status_fails.pop(row["id"], None)
                await db.set_render_status(row["id"], "error", True)
                await self._notify(row, f"❌ معرفتش أتابع الرندر #{row['id']} على Kaggle بعد "
                                        f"{config.STATUS_MAX_FAILURES} محاولات متتالية:\n```{str(e)[:300]}```")
            return
        if status == row["status"]:
            return
        if status == "running":
            await db.set_render_status(row["id"], "running")
            await self._notify(row, f"🔥 الرندر #{row['id']} بدأ على Kaggle.")
        elif status == "queued":
            await db.set_render_status(row["id"], "queued")
        elif status in ("complete", "error"):
            sent = await self._deliver(row, creds, ref, status, failure)
            n = self._attempts.get(row["id"], 0) + 1
            give_up = config.DELIVERY_MAX_ATTEMPTS > 0 and n >= config.DELIVERY_MAX_ATTEMPTS
            if sent or give_up:
                self._attempts.pop(row["id"], None)
                if not sent:
                    log.warning("giving up delivering render %s after %d attempts", row["id"], n)
                await db.set_render_status(row["id"], status, True)
            else:
                self._attempts[row["id"]] = n  # stays queued/running in the DB -> retried on the next cycle
        elif status == "cancelled":
            await db.set_render_status(row["id"], "cancelled", True)
            await self._notify(row, f"🛑 الرندر #{row['id']} اتلغى.")

    async def _notify(self, row, text: str, file_path: Optional[Path] = None) -> bool:
        def make_file():
            return discord.File(file_path) if file_path else None
        if row["channel_id"]:
            try:
                await self.bot.get_partial_messageable(row["channel_id"]).send(text, file=make_file())
                return True
            except discord.HTTPException:
                pass
        try:
            user = await self.bot.fetch_user(row["discord_id"])
            await user.send(text, file=make_file())
            return True
        except discord.HTTPException:
            log.warning("could not notify user %s about render %s", row["discord_id"], row["id"])
            return False

    async def _deliver(self, row, creds: Credentials, ref: str, status: str, failure: str) -> bool:
        """True when the user was told (result or failure report). False -> the monitor retries later."""
        link = kaggle_client.kernel_url(creds.username, row["kernel_slug"])
        dest = config.STAGING_DIR / "out" / str(row["id"])
        try:
            try:
                files = await kaggle_client.download_output(creds, ref, dest)
            except OutputTooLarge:
                if status == "complete":
                    return await self._notify(
                        row, f"✅ **الرندر #{row['id']} خلص**، بس النتيجة كبيرة ومينفعش تتبعت هنا.\n"
                             f"نزّلها من تبويب **Output** هنا:\n🔗 {link}")
                files = []
            except KaggleError as e:
                files = []
                failure = failure or str(e)
                if status == "complete" and self._attempts.get(row["id"], 0) < 4:
                    log.warning("output download failed for render %s, will retry: %s", row["id"], str(e)[:120])
                    return False
            by_name = {p.name: p for p in files}
            info = {}
            if "render_status.json" in by_name:
                try:
                    info = json.loads(by_name["render_status.json"].read_text(encoding="utf-8"))
                except ValueError:
                    pass
            if info.get("seconds"):
                await self.bot.db.set_render_seconds(row["id"], int(info["seconds"]))
            result = next((by_name[n] for n in ("render_output.mp4", "render_output.mov", "single_frame.png")
                           if n in by_name), None)
            if status == "complete" and not result and info.get("ok", True):
                # Kaggle can report COMPLETE a bit before the output files are downloadable.
                if self._attempts.get(row["id"], 0) < 4:
                    log.warning("render %s complete but no result file yet, will retry", row["id"])
                    return False
                return await self._notify(
                    row, f"✅ **الرندر #{row['id']} خلص** على Kaggle، بس معرفتش أنزّل النتيجة تلقائياً.\n"
                         f"نزّلها من تبويب **Output** هنا:\n🔗 {link}")
            if status == "complete" and result and info.get("ok", True):
                size = result.stat().st_size
                mins = info.get("seconds", 0) // 60
                head = f"✅ **الرندر #{row['id']} خلص** ({mins} دقيقة) — `{result.name}` ({human_size(size)})"
                if size <= config.DISCORD_UPLOAD_LIMIT_MB * 1024 * 1024:
                    return await self._notify(row, head, result)
                return await self._notify(row, head + f"\nالملف أكبر من حد Discord ({config.DISCORD_UPLOAD_LIMIT_MB} MB).\n"
                                                      f"نزّله من تبويب **Output** هنا:\n🔗 {link}")
            reason = info.get("reason") or failure or "السبب مش معروف."
            tail = ""
            if "render_log.txt" in by_name:
                lines = by_name["render_log.txt"].read_text(encoding="utf-8", errors="replace").strip().splitlines()
                tail = "\n".join(lines[-12:])[-1200:]
            return await self._notify(row, f"❌ **الرندر #{row['id']} فشل**\nالسبب: {reason[:400]}"
                                    + (f"\n```\n{tail}\n```" if tail else "") + f"\n🔗 {link}")
        finally:
            await asyncio.to_thread(shutil.rmtree, dest, True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Render(bot))
