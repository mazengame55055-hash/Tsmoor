"""Safe streaming download from a direct URL (manual redirects, every hop re-validated)."""
from __future__ import annotations

import socket
from pathlib import Path
from urllib.parse import unquote, urljoin, urlparse

import aiohttp

from .utils import assert_public_url, is_public_ip, sanitize_filename


class DownloadError(Exception):
    pass


class PublicOnlyResolver(aiohttp.ThreadedResolver):
    """Validates the addresses aiohttp is ACTUALLY about to connect to (closes the DNS-rebinding gap
    between assert_public_url's lookup and the real connection)."""

    async def resolve(self, host, port=0, family=socket.AF_INET):
        result = await super().resolve(host, port, family)
        if not result or not all(is_public_ip(r["host"]) for r in result):
            raise OSError("blocked: non-public address")
        return result


async def download_url(url: str, dest_dir: Path, max_bytes: int, max_redirects: int = 5) -> Path:
    timeout = aiohttp.ClientTimeout(total=None, sock_connect=30, sock_read=120)
    connector = aiohttp.TCPConnector(resolver=PublicOnlyResolver())
    async with aiohttp.ClientSession(timeout=timeout, connector=connector) as session:
        current = url
        for _ in range(max_redirects + 1):
            try:
                await assert_public_url(current)
            except ValueError as e:
                raise DownloadError(str(e))
            try:
                async with session.get(current, allow_redirects=False) as resp:
                    if resp.status in (301, 302, 303, 307, 308):
                        loc = resp.headers.get("Location")
                        if not loc:
                            raise DownloadError("الرابط عمل redirect من غير وجهة.")
                        current = urljoin(current, loc)
                        continue
                    if resp.status != 200:
                        raise DownloadError(f"السيرفر رد بـ HTTP {resp.status}.")
                    if "text/html" in resp.headers.get("Content-Type", "").lower():
                        raise DownloadError(
                            "الرابط ده بيفتح صفحة ويب مش ملف. استخدم رابط تحميل مباشر "
                            "(Google Drive للملفات الكبيرة بيرجّع صفحة تأكيد)."
                        )
                    if resp.content_length and resp.content_length > max_bytes:
                        raise DownloadError("الملف أكبر من الحد المسموح.")
                    cd = resp.content_disposition
                    raw = (cd.filename if cd and cd.filename else None) or unquote(Path(urlparse(current).path).name)
                    dest = dest_dir / sanitize_filename(raw or "download")
                    written = 0
                    with open(dest, "wb") as f:
                        async for chunk in resp.content.iter_chunked(1 << 20):
                            written += len(chunk)
                            if written > max_bytes:
                                raise DownloadError("الملف أكبر من الحد المسموح.")
                            f.write(chunk)
                    return dest
            except (aiohttp.ClientError, OSError) as e:
                raise DownloadError("مش قادر أوصل للرابط ده أو الاتصال اتقطع.") from e
        raise DownloadError("redirects كتير أوي.")
