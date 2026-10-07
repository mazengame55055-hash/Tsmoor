from __future__ import annotations

import asyncio
import ipaddress
import re
import secrets
import socket
from pathlib import Path
from urllib.parse import urlparse

import logging

import discord
from discord import app_commands

_log = logging.getLogger("renderbot")
UI_ERROR_MESSAGE = "❌ حصل خطأ غير متوقع. جرّب تاني بعد شوية."


async def report_ui_error(interaction: discord.Interaction, error: Exception, where: str) -> None:
    """Modals and Views do not go through the command tree's error handler: log + tell the user."""
    _log.error("%s failed: %s", where, type(error).__name__, exc_info=error)
    try:
        if interaction.response.is_done():
            await interaction.followup.send(UI_ERROR_MESSAGE, ephemeral=True)
        else:
            await interaction.response.send_message(UI_ERROR_MESSAGE, ephemeral=True)
    except discord.HTTPException:
        pass


async def member_gate(interaction: discord.Interaction) -> bool:
    """Membership check for buttons / menus / modals (slash commands are checked by the command tree)."""
    return await interaction.client.tree.gate(interaction)


def cooldown(seconds: float):
    """One use per `seconds` per user. Protects the host from spam (every command spawns Kaggle work)."""
    return app_commands.checks.cooldown(1, seconds, key=lambda i: i.user.id)


def sanitize_filename(name: str) -> str:
    """Keep only safe characters; never allow path traversal."""
    name = Path(name).name
    stem, dot, ext = name.rpartition(".")
    if not dot:
        stem, ext = name, ""
    stem = re.sub(r"[^A-Za-z0-9._-]+", "_", stem).strip("._") or "file"
    ext = re.sub(r"[^A-Za-z0-9]+", "", ext).lower()
    return f"{stem[:80]}.{ext}" if ext else stem[:80]


def make_slug(stem: str) -> str:
    """Kaggle dataset slug: 3-50 chars, lowercase letters, digits, hyphens. Random suffix = always unique."""
    base = re.sub(r"[^a-z0-9]+", "-", stem.lower()).strip("-")[:30] or "file"
    return f"rb-{base}-{secrets.token_hex(3)}"


def make_title(stem: str) -> str:
    """Kaggle dataset title must be 6-50 chars."""
    title = f"RenderBot {re.sub(r'[^A-Za-z0-9 _-]+', ' ', stem).strip()}"[:50].strip()
    return title if len(title) >= 6 else "RenderBot file"


def free_disk_bytes(path: Path) -> int:
    import shutil
    try:
        return shutil.disk_usage(path).free
    except OSError:
        return 1 << 60  # unknown: do not block


def chunk_lines(lines: list[str], limit: int = 1800) -> list[str]:
    """Joins lines into messages that stay under Discord's 2000-character limit."""
    chunks, cur = [], ""
    for line in lines:
        line = line[:limit]
        if cur and len(cur) + 1 + len(line) > limit:
            chunks.append(cur)
            cur = line
        else:
            cur = f"{cur}\n{line}" if cur else line
    return chunks + [cur] if cur else chunks or [""]


def human_size(n: int) -> str:
    size = float(n)
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024 or unit == "GB":
            return f"{size:.0f} {unit}" if unit == "B" else f"{size:.1f} {unit}"
        size /= 1024
    return f"{n} B"


def looks_like_blend(path: Path) -> bool:
    """Plain .blend starts with 'BLENDER'; compressed ones are gzip or zstd."""
    with open(path, "rb") as f:
        head = f.read(7)
    return head == b"BLENDER" or head[:2] == b"\x1f\x8b" or head[:4] == b"\x28\xb5\x2f\xfd"


def is_public_ip(text: str) -> bool:
    try:
        addr = ipaddress.ip_address(text.split("%")[0])  # strip IPv6 scope id
    except ValueError:
        return False
    if addr.version == 6 and addr.ipv4_mapped:
        addr = addr.ipv4_mapped
    return addr.is_global


async def assert_public_url(url: str) -> None:
    """Blocks localhost / LAN / cloud-metadata targets (the bot may run on the owner's PC)."""
    parsed = urlparse(url)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError("الرابط لازم يبدأ بـ http:// أو https://")
    port = parsed.port or (443 if parsed.scheme == "https" else 80)
    try:
        infos = await asyncio.get_running_loop().getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError("مش قادر أوصل للرابط ده.")
    for info in infos:
        if not is_public_ip(info[4][0]):
            raise ValueError("الرابط ده بيشاور على عنوان داخلي، مرفوض.")
