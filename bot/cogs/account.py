from __future__ import annotations

from typing import Optional

import discord
from discord import app_commands
from discord.ext import commands

from .. import config, crypto, kaggle_client
from ..accounts import load_credentials
from ..kaggle_client import AuthError, Credentials, KaggleError
from ..utils import cooldown, member_gate, report_ui_error


def accounts_text(rows: list, active_id: int) -> str:
    lines = [f"{'✅' if r['id'] == active_id else '▫️'} **{r['kaggle_username']}**"
             f"{'  ← النشط' if r['id'] == active_id else ''}" for r in rows]
    return (f"**حسابات Kaggle بتاعتك ({len(rows)}/{config.MAX_ACCOUNTS_PER_USER}):**\n" + "\n".join(lines) +
            "\n\nكل أوامر `/upload` `/files` `/render` بتشتغل على **الحساب النشط**، ولكل حساب ملفاته ورندراته.\n"
            "• ضيف حساب: `/login` • امسح حساب: `/logout`")


class AccountSelect(discord.ui.Select):
    def __init__(self, rows: list, active_id: int):
        opts = [discord.SelectOption(label=r["kaggle_username"][:100], value=str(r["id"]), default=(r["id"] == active_id))
                for r in rows[:25]]
        super().__init__(placeholder="🔁 بدّل الحساب النشط", options=opts, min_values=1, max_values=1)

    async def callback(self, interaction: discord.Interaction) -> None:
        db = interaction.client.db
        if not await db.set_active_account(interaction.user.id, int(self.values[0])):
            await interaction.response.send_message("الحساب ده مبقاش موجود.", ephemeral=True)
            return
        rows = await db.list_accounts(interaction.user.id)
        active_id = int(self.values[0])
        await interaction.response.edit_message(content=accounts_text(rows, active_id),
                                                view=AccountsView(interaction.user.id, rows, active_id))


class AccountsView(discord.ui.View):
    def __init__(self, owner_id: int, rows: list, active_id: int):
        super().__init__(timeout=840)
        self.owner_id = owner_id
        self.add_item(AccountSelect(rows, active_id))

    async def on_error(self, interaction: discord.Interaction, error: Exception, item: discord.ui.Item) -> None:
        await report_ui_error(interaction, error, "accounts view")

    async def interaction_check(self, interaction: discord.Interaction) -> bool:
        if interaction.user.id != self.owner_id:
            await interaction.response.send_message("القايمة دي مش بتاعتك.", ephemeral=True)
            return False
        return await member_gate(interaction)


class LoginModal(discord.ui.Modal, title="إضافة حساب Kaggle"):
    secret = discord.ui.TextInput(label="Kaggle API Token", placeholder="KGAT_...",
                                  min_length=10, max_length=300)
    username = discord.ui.TextInput(label="Username (للمفتاح القديم kaggle.json بس)", required=False,
                                    min_length=2, max_length=60)

    def __init__(self, bot: commands.Bot):
        super().__init__()
        self.bot = bot

    async def on_error(self, interaction: discord.Interaction, error: Exception) -> None:
        await report_ui_error(interaction, error, "login modal")

    async def on_submit(self, interaction: discord.Interaction) -> None:
        if not await member_gate(interaction):  # the user may have left the server while the form was open
            return
        await interaction.response.defer(ephemeral=True, thinking=True)
        typed_name = self.username.value.strip().lstrip("@")
        secret = self.secret.value.strip()
        kind = "token" if secret.startswith("KGAT_") else "legacy"
        if kind == "legacy" and not typed_name:
            await interaction.followup.send(
                "المفتاح ده من النوع القديم (kaggle.json)، فلازم تكتب الـ Username كمان.", ephemeral=True)
            return
        creds = Credentials(typed_name, secret, kind)
        try:
            username = await kaggle_client.verify(creds)
        except AuthError:
            await interaction.followup.send(
                "❌ Kaggle رفض البيانات دي. اتأكد من التوكين وجرّب تاني.", ephemeral=True)
            return
        except KaggleError as e:
            await interaction.followup.send(f"⚠️ معرفتش أتواصل مع Kaggle دلوقتي:\n```{e}```", ephemeral=True)
            return
        existing = await self.bot.db.list_accounts(interaction.user.id)
        known = {r["kaggle_username"].lower() for r in existing}
        if username.lower() not in known and len(existing) >= config.MAX_ACCOUNTS_PER_USER:
            await interaction.followup.send(
                f"وصلت للحد الأقصى ({config.MAX_ACCOUNTS_PER_USER} حسابات). امسح حساب بـ `/logout` الأول.", ephemeral=True)
            return
        # Same Kaggle account again = token refresh (keeps its files), not a duplicate.
        name_in_db = next((r["kaggle_username"] for r in existing if r["kaggle_username"].lower() == username.lower()),
                          username)
        await self.bot.db.save_account(interaction.user.id, name_in_db, crypto.encrypt(secret), kind)
        total = len(await self.bot.db.list_accounts(interaction.user.id))
        await interaction.followup.send(
            f"✅ تم تسجيل الدخول كـ **{name_in_db}** وبقى هو **الحساب النشط**.\n"
            f"عندك {total} حساب. بدّل بينهم بـ `/accounts`.\n"
            "التوكين متخزّن **مشفّر** عند صاحب البوت. تقدر تمسحه بـ `/logout`، "
            "وتقدر تلغيه في أي وقت من إعدادات Kaggle.\n"
            "الخطوة الجاية: `/upload` لرفع ملف الـ Blend (على الحساب ده).",
            ephemeral=True)


class Account(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="login", description="ضيف حساب Kaggle (تقدر تضيف أكتر من حساب)")
    @cooldown(15)
    async def login(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_modal(LoginModal(self.bot))

    @app_commands.command(name="accounts", description="اعرض حسابات Kaggle بتاعتك وبدّل بينهم")
    @cooldown(3)
    async def accounts(self, interaction: discord.Interaction) -> None:
        rows = await self.bot.db.list_accounts(interaction.user.id)
        if not rows:
            await interaction.response.send_message("مفيش حسابات. استخدم `/login`.", ephemeral=True)
            return
        active = await self.bot.db.get_active_account(interaction.user.id)
        active_id = active["id"] if active else rows[0]["id"]
        await interaction.response.send_message(accounts_text(rows, active_id),
                                                view=AccountsView(interaction.user.id, rows, active_id), ephemeral=True)

    @app_commands.command(name="logout", description="امسح حساب Kaggle من البوت (النشط، أو اختار حساب، أو الكل)")
    @app_commands.describe(account="الحساب اللي عايز تمسحه (فاضي = الحساب النشط)", all="امسح كل حساباتك")
    @cooldown(5)
    async def logout(self, interaction: discord.Interaction, account: Optional[str] = None, all: bool = False) -> None:
        uid = interaction.user.id
        rows = await self.bot.db.list_accounts(uid)
        if not rows:
            await interaction.response.send_message("مفيش حسابات مسجّلة.", ephemeral=True)
            return
        revoke = "\nلو عايز تلغي التوكين نفسه، امسحه من إعدادات Kaggle."
        if all:
            await self.bot.db.delete_all_accounts(uid)
            await interaction.response.send_message(
                f"🗑️ اتمسحت **{len(rows)}** حسابات وكل ملفاتها ورندراتها من البوت." + revoke, ephemeral=True)
            return
        if account is None:
            target = await self.bot.db.get_active_account(uid)
        else:
            target = next((r for r in rows if str(r["id"]) == account or r["kaggle_username"].lower() == account.lower()), None)
        if not target:
            await interaction.response.send_message("مش لاقي الحساب ده. اختاره من القايمة.", ephemeral=True)
            return
        await self.bot.db.delete_account(uid, target["id"])
        nxt = await self.bot.db.get_active_account(uid)
        tail = (f"\nالحساب النشط دلوقتي: **{nxt['kaggle_username']}**." if nxt else "\nمفيش حسابات متبقية، استخدم `/login`.")
        await interaction.response.send_message(
            f"🗑️ اتمسح **{target['kaggle_username']}** وملفاته ورندراته من البوت." + tail + revoke, ephemeral=True)

    @logout.autocomplete("account")
    async def logout_autocomplete(self, interaction: discord.Interaction, current: str):
        rows = await self.bot.db.list_accounts(interaction.user.id)
        needle = current.lower().strip()
        return [app_commands.Choice(name=r["kaggle_username"][:100], value=str(r["id"]))
                for r in rows if not needle or needle in r["kaggle_username"].lower()][:25]

    @app_commands.command(name="quota", description="تقدير حصة GPU المتبقية لحساباتك على Kaggle")
    @cooldown(5)
    async def quota(self, interaction: discord.Interaction) -> None:
        uid = interaction.user.id
        rows = await self.bot.db.list_accounts(uid)
        if not rows:
            await interaction.response.send_message("مفيش حسابات. استخدم `/login`.", ephemeral=True)
            return
        active = await self.bot.db.get_active_account(uid)
        limit = config.QUOTA_GPU_HOURS * 3600
        lines = []
        for r in rows:
            used, running = await self.bot.db.gpu_usage(r["id"], int(config.QUOTA_WINDOW_DAYS * 86400))
            left = max(0.0, limit - used)
            filled = min(10, round(10 * used / limit)) if limit else 0
            bar = "🟥" * filled + "⬜" * (10 - filled) if used >= limit else "🟩" * (10 - filled) + "⬜" * filled
            mark = " ← النشط" if active and r["id"] == active["id"] else ""
            lines.append(f"**{r['kaggle_username']}**{mark}\n{bar}\n"
                         f"استُخدم ≈ **{used / 3600:.1f}h** • متبقي ≈ **{left / 3600:.1f}h** من {config.QUOTA_GPU_HOURS:g}h"
                         + ("  🔥 فيه رندر شغال دلوقتي" if running else ""))
        await interaction.response.send_message(
            "**🎮 تقدير حصة GPU (آخر "
            f"{config.QUOTA_WINDOW_DAYS:g} أيام):**\n\n" + "\n\n".join(lines) +
            "\n\n⚠️ ده **تقدير** من الرندرات اللي اتعملت عن طريق البوت بس. Kaggle مش بيدّي API للحصة، "
            "فأي استخدام تاني للـ GPU على حسابك (نوتبوك يدوي) مش محسوب هنا. "
            "الرقم الدقيق بيظهر لك في **Your Kaggle Quota** جوه Kaggle.", ephemeral=True)

    @app_commands.command(name="whoami", description="اعرف أنت مسجّل بأنهي حساب")
    @cooldown(5)
    async def whoami(self, interaction: discord.Interaction) -> None:
        creds = await load_credentials(self.bot.db, interaction.user.id)
        if not creds:
            await interaction.response.send_message("مش مسجّل. استخدم `/login`.", ephemeral=True)
            return
        files = await self.bot.db.list_files(creds.account_id, limit=1000)
        total = len(await self.bot.db.list_accounts(interaction.user.id))
        await interaction.response.send_message(
            f"👤 الحساب النشط: **{creds.username}**\n📂 عدد الملفات المرفوعة عليه: **{len(files)}**\n"
            f"🔑 إجمالي حساباتك: {total} (`/accounts` للتبديل)", ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(Account(bot))
