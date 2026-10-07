from __future__ import annotations

import discord
from discord import app_commands
from discord.ext import commands

from .. import __version__, config
from ..utils import cooldown


def build_help_embed() -> discord.Embed:
    e = discord.Embed(
        title="🎬 Kaggle Render Bot",
        description="بترندر ملفات Blender على **GPU مجاني من حساب Kaggle بتاعك** وتستلم النتيجة هنا في الخاص.",
        color=0x20BEFF,
    )
    e.add_field(
        name="🚀 البداية (٤ خطوات)",
        value=(
            "١) على Kaggle: **Settings ← API ← Generate New Token** وانسخ التوكين (بيبدأ بـ `KGAT_`).\n"
            "٢) وثّق حسابك بالموبايل (**Phone Verification**) وإلا الـ GPU والإنترنت مش هيشتغلوا.\n"
            "٣) `/login` والصق التوكين، ثم `/upload` وارفع الـ `.blend`.\n"
            "٤) `/render` واختار **Original** أو **Custom**."),
        inline=False)
    e.add_field(
        name="📋 الأوامر",
        value=(
            "`/login` `/accounts` `/logout` `/whoami` — الحسابات (تقدر تضيف أكتر من حساب Kaggle وتبدّل بينهم)\n"
            "`/upload` `/files` `/delete` — ملفاتك (.blend وصوت)\n"
            "`/render` `/renders` — الرندر وحالته • `/quota` — تقدير حصة GPU المتبقية\n"
            "`/help` — الشرح ده • `/info` — عن البوت والمطوّر"),
        inline=False)
    e.add_field(
        name="🎯 Original و 🎛️ Custom",
        value=(
            "**Original**: الملف زي ما هو متسيّف، بدون أي تعديل في المشهد.\n"
            "**Custom**: الفريمات، Samples، FPS، Resolution%، Engine، الألوان، الكوديك، Denoise.\n"
            "لو ملفك **EEVEE** فعّل **EEVEE→Cycles**، لأن EEVEE بيعلّق على Kaggle كتير."),
        inline=False)
    e.add_field(
        name="⚠️ لازم تعرف",
        value=(
            f"• رندر واحد شغال في نفس الوقت **لكل حساب Kaggle**، وحد أقصى {config.MAX_FILES_PER_USER} ملف.\n"
            f"• النتيجة الأكبر من {config.DISCORD_UPLOAD_LIMIT_MB} MB بتيجيلك كرابط Kaggle (تبويب Output).\n"
            "• مفيش نسبة تقدم لحظية ولا زرار إيقاف؛ للإيقاف افتح رابط الرندر على Kaggle واضغط Stop.\n"
            "• الرندر بيخصم من حصة الـ GPU الأسبوعية بتاعتك على Kaggle (حوالي ٣٠ ساعة حسب Kaggle).\n"
            "• Kaggle ممكن يقيّد استخدام GPU لأغراض غير تعلّم الآلة، فراجع شروطهم. استخدامك على مسؤوليتك."),
        inline=False)
    e.add_field(
        name="🔒 الخصوصية",
        value=(
            "التوكين بيتخزن **مشفّر** عند صاحب البوت، وبيتستخدم لحسابك إنت بس. "
            "ملفاتك بتتخزن كـ Datasets **خاصة** في حسابك على Kaggle. "
            "`/logout` بيمسح التوكين من البوت، وتقدر تلغيه من إعدادات Kaggle في أي وقت."),
        inline=False)
    e.set_footer(text=f"v{__version__}")
    return e


DEVELOPER_NAME = "Mazen"
DEVELOPER_DISCORD = "tsmoor"
DEVELOPER_INSTAGRAM = "tsmoor_t"


def build_info_embed() -> discord.Embed:
    e = discord.Embed(
        title="ℹ️ عن البوت",
        description=(
            "**Kaggle Render Bot** بيرندر ملفات **Blender** على **GPU مجاني من حساب Kaggle بتاعك** "
            "وبيبعتلك النتيجة (فيديو أو صورة) هنا في الخاص، من غير ما تحتاج كمبيوتر قوي."),
        color=0x20BEFF,
    )
    e.add_field(
        name="⚙️ بيعمل إيه؟",
        value=(
            "• `/login` تسجّل توكين Kaggle بتاعك (بيتخزن مشفّر)، وتقدر تضيف **أكتر من حساب** وتبدّل بـ `/accounts`.\n"
            "• `/upload` ترفع ملف `.blend` (وصوت لو عايز) كـ Dataset خاص في حسابك.\n"
            "• `/render` ترندر بوضع **Original** (الملف زي ما هو) أو **Custom** "
            "(فريمات، Samples، FPS، Resolution، Engine، ألوان، كوديك، Denoise).\n"
            "• `/renders` تتابع حالة الرندر، والبوت يبعتلك النتيجة أول ما تخلص."),
        inline=False)
    e.add_field(name="👨‍💻 المطوّر", value=f"**{DEVELOPER_NAME}**", inline=False)
    e.add_field(name="💬 ديسكورد", value=f"`{DEVELOPER_DISCORD}`", inline=True)
    e.add_field(name="📸 انستجرام",
                value=f"[@{DEVELOPER_INSTAGRAM}](https://instagram.com/{DEVELOPER_INSTAGRAM})", inline=True)
    e.set_footer(text=f"v{__version__} • اكتب /help للشرح الكامل")
    return e


class General(commands.Cog):
    def __init__(self, bot: commands.Bot):
        self.bot = bot

    @app_commands.command(name="info", description="معلومات عن البوت والمطوّر")
    @cooldown(5)
    async def info(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=build_info_embed(), ephemeral=True)

    @app_commands.command(name="help", description="شرح استخدام البوت")
    @cooldown(5)
    async def help(self, interaction: discord.Interaction) -> None:
        await interaction.response.send_message(embed=build_help_embed(), ephemeral=True)


async def setup(bot: commands.Bot) -> None:
    await bot.add_cog(General(bot))
