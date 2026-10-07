"""Central configuration. Secrets live in .env, never in code."""
from __future__ import annotations

import os
import sys
from pathlib import Path

from cryptography.fernet import Fernet
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
ENV_PATH = ROOT / ".env"
load_dotenv(ENV_PATH)

# Where the database, encryption key and logs live. DATA_DIR wins; on Railway a mounted volume is picked up
# automatically (RAILWAY_VOLUME_MOUNT_PATH); otherwise ./data next to the code.
DATA_DIR = Path(os.getenv("DATA_DIR") or os.getenv("RAILWAY_VOLUME_MOUNT_PATH") or ROOT / "data")
STAGING_DIR = DATA_DIR / "staging"
CLI_HOME_DIR = DATA_DIR / "kaggle_home"  # deliberately empty fake HOME, see kaggle_client.build_env
CLI_CONFIG_DIR = CLI_HOME_DIR / ".kaggle"
DB_PATH = DATA_DIR / "bot.sqlite3"
for _d in (DATA_DIR, STAGING_DIR, CLI_HOME_DIR, CLI_CONFIG_DIR):
    _d.mkdir(parents=True, exist_ok=True)

DISCORD_TOKEN = os.getenv("DISCORD_TOKEN", "").strip()
if not DISCORD_TOKEN:
    print("DISCORD_TOKEN is missing. Copy .env.example to .env and fill it in.")
    sys.exit(78)  # run.sh / run.bat do not restart on this code


def _load_or_create_key() -> bytes:
    """ENCRYPTION_KEY env var wins (older installs keep working); otherwise data/encryption.key."""
    key = os.getenv("ENCRYPTION_KEY", "").strip()
    if key:
        return key.encode()
    key_file = DATA_DIR / "encryption.key"
    if key_file.exists():
        return key_file.read_text(encoding="utf-8").strip().encode()
    key = Fernet.generate_key().decode()
    key_file.write_text(key, encoding="utf-8")
    try:
        key_file.chmod(0o600)
    except OSError:
        pass
    print(f"[config] Generated a new encryption key at {key_file}. BACK UP the whole data/ folder: "
          "without this key saved tokens cannot be decrypted.")
    return key.encode()


ENCRYPTION_KEY = _load_or_create_key()

# Access gate: only members of REQUIRED_GUILD_ID can use the bot (the bot must be a member of that server).
# OWNER_ID always bypasses the check. REQUIRED_GUILD_ID=0 turns the gate off.
OWNER_ID = int(os.getenv("OWNER_ID") or 820408813790167041)
REQUIRED_GUILD_ID = int(os.getenv("REQUIRED_GUILD_ID") or 1483113341160259806)
REQUIRED_GUILD_INVITE = os.getenv("REQUIRED_GUILD_INVITE", "https://discord.gg/DSSe44DgV3").strip()  # optional invite link shown to non-members
MEMBER_CACHE_SEC = int(os.getenv("MEMBER_CACHE_SEC", "0"))  # 0 = check on EVERY action (leaving the server cuts access at once)

# Backups of the database (owner-only /backup + automatic).
BACKUP_DIR = DATA_DIR / "backups"
BACKUP_INTERVAL_HOURS = float(os.getenv("BACKUP_INTERVAL_HOURS", "24"))  # 0 = no automatic backups
BACKUP_KEEP = max(1, int(os.getenv("BACKUP_KEEP", "7")))  # how many automatic backups stay on disk
BACKUP_DM_OWNER = os.getenv("BACKUP_DM_OWNER", "true").strip().lower() not in ("0", "false", "no")

MAX_FILE_MB = int(os.getenv("MAX_FILE_MB", "100"))  # small hosts (512 MB disk): keep it low
MAX_FILE_BYTES = MAX_FILE_MB * 1024 * 1024
DEV_GUILD_ID = int(os.getenv("DEV_GUILD_ID") or 0) or None
# Max seconds for one Kaggle upload/download command. 0 = UNLIMITED (wait as long as Kaggle needs).
# Kaggle itself enforces the real limits (GPU session max 12h), so waiting here is always safe.
KAGGLE_TIMEOUT_SEC = int(os.getenv("KAGGLE_TIMEOUT_SEC", "0"))
# Max seconds for one `kaggle kernels status` poll. 0 = UNLIMITED.
KAGGLE_STATUS_TIMEOUT_SEC = int(os.getenv("KAGGLE_STATUS_TIMEOUT_SEC", "0"))

# Original mode never touches scene settings. This only decides whether the GPU device gets switched on
# (a file saved on CPU would otherwise render on CPU even though Kaggle gives us a GPU).
ORIGINAL_FORCE_GPU = os.getenv("ORIGINAL_FORCE_GPU", "true").strip().lower() not in ("0", "false", "no")
KAGGLE_ACCELERATOR = os.getenv("KAGGLE_ACCELERATOR", "NvidiaTeslaT4").strip()  # empty = Kaggle default GPU
DISCORD_UPLOAD_LIMIT_MB = int(os.getenv("DISCORD_UPLOAD_LIMIT_MB", "10"))
MAX_ACTIVE_RENDERS_PER_USER = 1  # per Kaggle ACCOUNT (each account has its own GPU quota)
# Kaggle gives no API for the remaining GPU quota, so /quota ESTIMATES it from the renders this bot ran.
QUOTA_GPU_HOURS = float(os.getenv("QUOTA_GPU_HOURS", "30"))
QUOTA_WINDOW_DAYS = float(os.getenv("QUOTA_WINDOW_DAYS", "7"))
MAX_ACCOUNTS_PER_USER = max(1, int(os.getenv("MAX_ACCOUNTS_PER_USER", "5")))
MAX_FILES_PER_USER = int(os.getenv("MAX_FILES_PER_USER", "50"))
# Consecutive failed status polls before a render is declared un-followable. 0 = NEVER give up.
STATUS_MAX_FAILURES = int(os.getenv("STATUS_MAX_FAILURES", "0"))
# Delivery attempts (result/failure message) before giving up. 0 = retry FOREVER.
DELIVERY_MAX_ATTEMPTS = int(os.getenv("DELIVERY_MAX_ATTEMPTS", "0"))

# --- small-host protection (512 MB disk / little RAM) ---
# Simultaneous uploads (each one is staged on disk first) and simultaneous Kaggle CLI processes
# (each one is a Python process of ~60-100 MB RAM).
UPLOAD_SLOTS = max(1, int(os.getenv("UPLOAD_SLOTS", "1")))
KAGGLE_LIGHT_SLOTS = max(1, int(os.getenv("KAGGLE_LIGHT_SLOTS", "3")))
KAGGLE_HEAVY_SLOTS = max(1, int(os.getenv("KAGGLE_HEAVY_SLOTS", "1")))
# Always keep this much disk free (MB) for the database, logs and the Kaggle CLI.
MIN_FREE_DISK_MB = int(os.getenv("MIN_FREE_DISK_MB", "60"))
# Stop monitoring a render after this many hours. 0 = NEVER stop monitoring.
# (Kaggle GPU sessions are killed by Kaggle itself after 12h anyway — that limit is on Kaggle's side
# and cannot be changed from here.)
RENDER_MAX_HOURS = int(os.getenv("RENDER_MAX_HOURS", "0"))
