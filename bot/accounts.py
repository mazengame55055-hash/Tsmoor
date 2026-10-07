from __future__ import annotations

from typing import Optional

from . import crypto
from .db import Database
from .kaggle_client import Credentials


async def load_credentials(db: Database, discord_id: int, account_id: Optional[int] = None) -> Credentials | None:
    """Credentials of one of the user's Kaggle accounts: `account_id`, or the ACTIVE one when omitted."""
    row = await (db.get_active_account(discord_id) if account_id is None else db.get_account(discord_id, account_id))
    if not row:
        return None
    try:
        secret = crypto.decrypt(row["token_enc"])
    except crypto.InvalidToken:
        return None  # key changed or data corrupted -> user must /login again
    return Credentials(row["kaggle_username"], secret, row["token_kind"], row["id"])
