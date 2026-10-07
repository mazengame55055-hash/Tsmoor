"""
Per-user Kaggle access.

Isolation rule: every Kaggle call runs in its OWN subprocess with the caller's credentials
in that subprocess's environment only. Nothing is ever written to ~/.kaggle or set in the
bot's own os.environ, so two users running commands at the same time can never mix tokens.
"""
from __future__ import annotations

import asyncio
import json
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path

from . import config

# Run the official Kaggle CLI from the installed package (no PATH dependency, works on Windows).
CLI_SNIPPET = "import sys; sys.argv[0] = 'kaggle'; from kaggle.cli import main; main()"


# Caps how many Kaggle CLI processes run at once on the host (each one is a Python process).
_LIGHT = asyncio.Semaphore(config.KAGGLE_LIGHT_SLOTS)   # status, verify, push, delete
_HEAVY = asyncio.Semaphore(config.KAGGLE_HEAVY_SLOTS)   # dataset upload, output download


_ENV_KEEP = {"PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "TMPDIR",
             "LANG", "LC_ALL", "LC_CTYPE", "SSL_CERT_FILE", "SSL_CERT_DIR", "REQUESTS_CA_BUNDLE",
             "CURL_CA_BUNDLE", "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "ALL_PROXY"}

# The CLI echoes the uploaded file names, so a file called "error.blend" must not look like a failure.
_PROGRESS_LINE = re.compile(r"^\s*(starting upload|uploading|upload successful|skipping)|%\|", re.I)


_URL = re.compile(r"https?://\S+")


def _output_has_error(out: str) -> bool:
    # URLs are removed first: they contain the dataset/kernel slug (made from the FILE NAME) and the
    # username, so "error.blend" or a user called "errorfan" used to make a successful upload look failed.
    lines = (_URL.sub("", l) for l in out.splitlines() if not _PROGRESS_LINE.search(l))
    return "error" in "\n".join(lines).lower()


class KaggleError(Exception):
    pass


class OutputTooLarge(KaggleError):
    """The render result is bigger than we are willing to store on this host."""


class AuthError(KaggleError):
    pass


@dataclass(frozen=True)
class Credentials:
    username: str  # may be "" for KGAT_ tokens: Kaggle tells us who owns the token
    secret: str
    kind: str  # 'token' -> KGAT_... access token | 'legacy' -> old kaggle.json key
    account_id: int = 0  # row id in the bot's `accounts` table (0 = not stored yet, e.g. while verifying a login)


def build_env(creds: Credentials) -> dict[str, str]:
    # Minimal environment: the bot's own secrets (DISCORD_TOKEN, ENCRYPTION_KEY...) must never reach the CLI.
    env = {k: v for k, v in os.environ.items() if k.upper() in _ENV_KEEP}
    if creds.kind == "token":
        env["KAGGLE_API_TOKEN"] = creds.secret
    else:
        env["KAGGLE_USERNAME"] = creds.username
        env["KAGGLE_KEY"] = creds.secret
    # SECURITY: the Kaggle CLI also reads ~/.kaggle/access_token and cached OAuth logins from the
    # real home directory. If a user's token is invalid it would silently fall back to the BOT
    # OWNER's own account. KAGGLE_CONFIG_DIR alone does not prevent that (tested), so the
    # subprocess gets a fake, empty HOME as well.
    home = str(config.CLI_HOME_DIR)
    env["HOME"] = home
    env["USERPROFILE"] = home
    env["XDG_CONFIG_HOME"] = home
    env.pop("HOMEDRIVE", None)
    env.pop("HOMEPATH", None)
    env["KAGGLE_CONFIG_DIR"] = str(config.CLI_CONFIG_DIR)
    env["PYTHONIOENCODING"] = "utf-8"
    return env


def _dir_size(path: Path) -> int:
    total = 0
    for p in path.rglob("*"):
        try:
            if p.is_file():
                total += p.stat().st_size
        except OSError:
            pass
    return total


async def _watch_dir_size(proc, path: Path, limit: int, flag: list) -> None:
    """Kills the CLI as soon as the download folder grows past `limit` (protects a tiny disk)."""
    while proc.returncode is None:
        await asyncio.sleep(1)
        if await asyncio.to_thread(_dir_size, path) > limit:
            flag.append(True)
            proc.kill()
            return


async def run_cli(creds: Credentials, args: list[str], cwd: Path | None = None,
                  timeout: int = 0, heavy: bool = False,
                  watch_dir: Path | None = None, watch_limit: int = 0) -> tuple[int, str]:
    """timeout <= 0 means UNLIMITED: wait for the Kaggle CLI as long as it needs.
    (The old short timeouts killed the CLI mid-call and the bot then wrongly reported
    renders as failed. Kaggle itself enforces the real session limits.)"""
    too_big: list = []
    async with (_HEAVY if heavy else _LIGHT):
        proc = await asyncio.create_subprocess_exec(
            sys.executable, "-c", CLI_SNIPPET, *args,
            env=build_env(creds), cwd=str(cwd) if cwd else None,
            stdout=asyncio.subprocess.PIPE, stderr=asyncio.subprocess.STDOUT,
        )
        watcher = (asyncio.create_task(_watch_dir_size(proc, watch_dir, watch_limit, too_big))
                   if watch_dir and watch_limit else None)
        try:
            if timeout and timeout > 0:
                raw, _ = await asyncio.wait_for(proc.communicate(), timeout=timeout)
            else:
                raw, _ = await proc.communicate()
        except asyncio.TimeoutError:
            proc.kill()
            await proc.wait()
            raise KaggleError(f"العملية أخدت وقت أطول من المسموح ({timeout} ثانية).")
        finally:
            if watcher:
                watcher.cancel()
    if too_big:
        raise OutputTooLarge("result bigger than the host limit")
    text = raw.decode("utf-8", errors="replace").replace(creds.secret, "***")
    return proc.returncode or 0, text.strip()


async def verify(creds: Credentials) -> str:
    """Authenticated call to Kaggle. Returns the REAL username that owns these credentials."""
    rc, out = await run_cli(creds, ["datasets", "list", "--mine"], timeout=0)
    if rc != 0:
        low = out.lower()
        if any(w in low for w in ("401", "403", "unauthorized", "forbidden", "invalid", "authentication required")):
            raise AuthError(out[-300:])
        raise KaggleError(out[-300:])
    rc, out = await run_cli(creds, ["config", "view"], timeout=0)
    m = re.search(r"^- username:\s*(\S+)", out, re.MULTILINE)
    if rc != 0 or not m or m.group(1) == "None":
        raise AuthError("معرفتش أحدد اسم المستخدم من التوكين.")
    return m.group(1)


async def create_dataset(creds: Credentials, folder: Path, slug: str, title: str) -> str:
    """Uploads everything in `folder` as a new PRIVATE dataset and returns its URL."""
    meta = {"title": title, "id": f"{creds.username}/{slug}", "licenses": [{"name": "CC0-1.0"}]}
    (folder / "dataset-metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    rc, out = await run_cli(creds, ["datasets", "create", "-p", str(folder)],
                            timeout=config.KAGGLE_TIMEOUT_SEC, heavy=True)
    # Some CLI versions print errors but still exit 0, so check the text too.
    if rc != 0 or _output_has_error(out):
        raise KaggleError(out[-400:] or "فشل إنشاء الـ Dataset.")
    return f"https://www.kaggle.com/datasets/{creds.username}/{slug}"


# --------------------------------------------------------------------------- kernels (renders)
_STATUS_RE = re.compile(r'has status "(?:[A-Za-z]+\.)?([A-Za-z_]+)"')
_FAIL_RE = re.compile(r'Failure message: "(.*)"', re.DOTALL)
_STATUS_MAP = {"queued": "queued", "newscript": "queued", "running": "running", "complete": "complete",
               "error": "error", "cancelrequested": "cancelled", "cancelacknowledged": "cancelled"}


def kernel_url(username: str, slug: str) -> str:
    return f"https://www.kaggle.com/code/{username}/{slug}"


async def push_kernel(creds: Credentials, folder: Path, slug: str, dataset_slugs: list[str],
                      accelerator: str = "") -> str:
    """Creates + starts a PRIVATE script kernel with GPU and internet. Returns its URL."""
    meta = {
        "id": f"{creds.username}/{slug}",
        "title": slug.replace("-", " "),  # slugifies back to exactly `slug`
        "code_file": "render.py",
        "language": "python",
        "kernel_type": "script",
        "is_private": True,
        "enable_gpu": True,
        "enable_tpu": False,
        "enable_internet": True,  # needed to download Blender
        "dataset_sources": [f"{creds.username}/{d}" for d in dataset_slugs],
        "competition_sources": [],
        "kernel_sources": [],
    }
    (folder / "kernel-metadata.json").write_text(json.dumps(meta), encoding="utf-8")
    args = ["kernels", "push", "-p", str(folder)]
    if accelerator:
        args += ["--accelerator", accelerator]
    rc, out = await run_cli(creds, args, timeout=0)  # unlimited: never abort a push mid-flight
    low = out.lower()
    if rc != 0 or _output_has_error(out) or "does not resolve" in low or "successfully pushed" not in low:
        raise KaggleError(out[-500:] or "فشل تشغيل الـ Kernel.")
    return kernel_url(creds.username, slug)


async def kernel_status(creds: Credentials, ref: str) -> tuple[str, str]:
    """Returns (queued|running|complete|error|cancelled, failure_message)."""
    rc, out = await run_cli(creds, ["kernels", "status", ref], timeout=config.KAGGLE_STATUS_TIMEOUT_SEC)
    m = _STATUS_RE.search(out)
    if rc != 0 or not m:
        raise KaggleError(out[-300:] or "معرفتش أجيب حالة الرندر.")
    raw = m.group(1).lower().replace("_", "")
    f = _FAIL_RE.search(out)
    return _STATUS_MAP.get(raw, raw), (f.group(1).strip() if f else "")


async def download_output(creds: Credentials, ref: str, dest: Path) -> list[Path]:
    dest.mkdir(parents=True, exist_ok=True)
    # Hard cap on what we store: result files above the Discord limit are never sent anyway.
    cap = (config.DISCORD_UPLOAD_LIMIT_MB + 40) * 1024 * 1024
    rc, out = await run_cli(creds, ["kernels", "output", ref, "-p", str(dest), "-o", "-q"],
                            timeout=config.KAGGLE_TIMEOUT_SEC, heavy=True, watch_dir=dest, watch_limit=cap)
    if rc != 0:
        raise KaggleError(out[-300:] or "فشل تنزيل نتيجة الرندر.")
    return [p for p in dest.rglob("*") if p.is_file()]


# --------------------------------------------------------------------------- datasets: ready / delete
async def wait_dataset_ready(creds: Credentials, slug: str, timeout: int = 120) -> bool:
    """Polls Kaggle until the new dataset is usable. False = still processing when we gave up."""
    deadline = asyncio.get_running_loop().time() + timeout
    while True:
        try:
            rc, out = await run_cli(creds, ["datasets", "status", f"{creds.username}/{slug}"], timeout=90)
        except KaggleError:  # one slow poll must not turn a finished upload into a "failure"
            rc, out = 1, ""
        low = out.lower()
        if rc == 0 and "ready" in low:
            return True
        if rc == 0 and ("error" in low or "fail" in low):
            raise KaggleError("Kaggle فشل في معالجة الملف: " + out[-200:])
        if asyncio.get_running_loop().time() >= deadline:
            return False
        await asyncio.sleep(10)


async def delete_dataset(creds: Credentials, slug: str) -> None:
    rc, out = await run_cli(creds, ["datasets", "delete", "-y", f"{creds.username}/{slug}"], timeout=120)
    low = out.lower()
    if rc != 0 and not any(w in low for w in ("not found", "404", "does not exist")):
        raise KaggleError(out[-300:] or "فشل مسح الـ Dataset.")
