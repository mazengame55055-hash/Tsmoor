# Changelog

## 1.3.0
- **New owner-only `/restore`:** upload a backup (`.tar.gz` made by `/backup`, or a raw `.sqlite3`) and the bot restores it live. Requires `confirm: True`. Safe by design: the file is validated first (archive structure, SQLite integrity, expected tables), old-format backups are migrated, the saved tokens must decrypt with the CURRENT encryption key (otherwise it refuses and changes nothing), and a safety backup of the current data is written to `data/backups` before the swap.

## 1.2.0
- **Backups.** Owner-only `/backup` (a consistent snapshot of the live database, `.tar.gz`; `include_key` optionally adds the encryption key). Automatic backup every `BACKUP_INTERVAL_HOURS` (default 24): saved in `data/backups` (last `BACKUP_KEEP`, default 7) and sent to the owner's DM **without** the encryption key (at most once per interval, not on every restart).
- Restore: stop the bot, put `bot.sqlite3` from the archive into `data/` next to the same encryption key.

## 1.1.1
- **Fix (access gate):** a user who joined the server, used the bot and then LEFT kept access. Causes: (1) the "is a member" answer was cached for 5 minutes; (2) only slash commands were checked, so an already-open `/render` menu, the `/accounts` menu and the `/login` / numbers forms kept working. Now the membership check runs on every command, button, menu and form submit, and the cache is off by default (`MEMBER_CACHE_SEC=0`).
- A render that was already started before the user left is still delivered to their DM.

## 1.1.0
- **New `/quota`:** estimated remaining Kaggle GPU quota per account (default 30h per rolling 7 days; `QUOTA_GPU_HOURS`, `QUOTA_WINDOW_DAYS`). Kaggle has no API for the real balance, so this is computed from the renders run through the bot (real run time reported by the Kaggle script when available). GPU use outside the bot is not counted; the exact figure stays on Kaggle ("Your Kaggle Quota").
- Database: `renders.started_at` / `renders.run_seconds` columns (added automatically).

## 1.0.9
- **New: several Kaggle accounts per Discord user** (up to `MAX_ACCOUNTS_PER_USER`, default 5).
  - `/login` adds an account (same account again = token refresh) and makes it the ACTIVE one.
  - New `/accounts`: list + switch the active account. `/logout` removes the active account, a chosen one (autocomplete) or `all:True`. `/whoami` shows the active account.
  - Files (`/upload` `/files` `/delete`) and renders (`/render` `/renders`) always work on the active account; each account has its own files, its own limit of files (`MAX_FILES_PER_USER`) and its own "one active render" slot, so you can render on two accounts at the same time.
  - A `/render` menu stays bound to the account it was opened for, even if you switch accounts before pressing Start; progress/results are tracked with that account's token.
  - Old databases are migrated automatically on the first start (existing login, files and renders move to the first account; the old `users` table is emptied so no stale token copy remains).
  - Removing an account deletes its files/renders from the bot's list (not from Kaggle) and the token from the bot.

## 1.0.8
- **New: works everywhere.** Commands are now registered for servers, DMs and group DMs (`guild + dm_channel + private_channel`) and for both install types, so the **Add to My Apps** (User Install) button lets you use the bot in any server, even ones the bot is not in.
- Removed the "DM only" lock (`DMOnlyTree` -> `EverywhereTree`).
- **Privacy:** in a server the `/render` menu is ephemeral (only you see it) and the result/progress messages are always delivered to your **DM**, never posted in the public channel (the bot often can't post there anyway for user installs).
- **Access gate:** only members of `REQUIRED_GUILD_ID` can use any command; `OWNER_ID` bypasses it. The bot must be in that server. Membership is checked via REST and cached for `MEMBER_CACHE_SEC`.
- `DATA_DIR` setting; Railway volumes are detected via `RAILWAY_VOLUME_MOUNT_PATH`. Removed the Dockerfile `VOLUME` line (Railway uses its own volumes).
- Docs updated (README, USER_GUIDE, Railway guide).

## 1.0.7
- **Fix:** an upload/render could be reported as FAILED although Kaggle accepted it, whenever the file name (or the Kaggle username) contained the word "error": the check looked at the URL Kaggle prints. URLs are now ignored. (This left an orphan dataset on Kaggle and no entry in `/files`.)
- **Fix:** `/files` crashed ("content must be 2000 or fewer") for users with roughly 15+ files; the list is now split over several messages.
- **Fix:** a render that finished but whose output was not downloadable yet was reported as "failed" and closed. It is now retried, and if the output still can't be fetched the user gets the Kaggle link with a "finished" message.
- **Fix:** a slow status poll while checking a freshly uploaded file no longer turns a successful `/upload` into an error; if Kaggle really fails to process the file, the dead entry and dataset are removed.
- **Fix (Kaggle script):** Blender output with invalid UTF-8 (Blender cuts names at 63 bytes, which breaks Arabic names) silently killed the log reader and the render then hung until Kaggle's 12h limit. Output is now decoded with replacement and the reader can never leave the loop waiting.
- **Fix (Kaggle script):** an audio file with embedded cover art could be picked by ffmpeg as the "video"; streams are now mapped explicitly (`0:v:0` + `1:a:0`).
- **Fix (Kaggle script):** GPU device refresh also works on Blender versions where `get_devices()` was renamed (`refresh_devices()`), so the GPU is not silently skipped.
- **Fix:** 🎯 Original no longer inherits codec / CRF / samples chosen earlier on the Custom screen (Custom -> Back -> Original).
- **Fix:** a file whose name contains a placeholder such as `@@OUT_DIR@@` could break the generated Blender script (single-pass replacement now).
- **Robustness:** the monitor loop can no longer die from one unexpected error (a dead loop silently stops all result delivery); the "render sent" message falls back to a DM if the 15-minute interaction token expired; buttons are removed after early `/render` errors; the user is told when a render can't be followed because the token was removed.
- Cleanup: unused imports, version strings in README.

## 1.0.6
- **Fix:** one slow Kaggle answer no longer kills a running render. Status polls were capped at 90s and a single
  slow poll got a >20-minute-old render declared as failed ("العملية أخدت وقت أطول من المسموح") and un-monitored.
- **No-limits mode (new defaults):** every Kaggle CLI call waits indefinitely (`KAGGLE_TIMEOUT_SEC=0`,
  `KAGGLE_STATUS_TIMEOUT_SEC=0`, push/verify uncapped), the monitor never stops following a render
  (`RENDER_MAX_HOURS=0`), never declares it un-followable (`STATUS_MAX_FAILURES=0`) and retries delivery
  forever (`DELIVERY_MAX_ATTEMPTS=0`). Set positive values in `.env` to cap anything again.
  (Kaggle's own 12h GPU session limit still applies on Kaggle's side and cannot be changed from the bot.)

## 1.0.5
- New `/info` command: what the bot does + developer card (Mazen / Discord `tsmoor` / Instagram `tsmoor_t`).
- Small-host mode (512 MB disk): default max upload 100 MB, 1 simultaneous upload, free-disk check before staging, render-output download is capped and aborted early (user gets the Kaggle link instead), fewer parallel Kaggle CLI processes (all configurable in `.env`).
- Added root `main.py` for hosting panels that run `python main.py`.

## 1.0.4
- Errors inside modals (`/login`, numbers) and buttons/menus are logged and answered ("unexpected error") instead of silently failing.
- Abandoned `/render` menus are replaced by an "expired" note instead of leaving dead buttons; "back" now builds a fresh menu.
- Files keep their real (e.g. Arabic) name in `/files`, `/render` and `/renders`; only the copy inside Kaggle uses an ASCII-safe name. Old databases are upgraded automatically.
- The Kaggle script no longer picks "any file with the same name": it uses the attached dataset, or fails if the choice would be ambiguous.

## 1.0.3
- **Fix:** a Python error inside Blender (e.g. CUDA out of memory mid-render) now fails the render (`--python-exit-code 1`) and an animation with missing frames is never delivered as a success.
- **Fix:** the "one active render per user" limit can no longer be bypassed while a render is still being sent to Kaggle (slot is reserved first; stale reservations are cleared on startup).
- **Fix:** files whose name contains "error" no longer make uploads look failed.
- **Security:** Kaggle CLI subprocesses get a minimal environment (no `DISCORD_TOKEN` / `ENCRYPTION_KEY`); URL downloads validate the addresses actually connected to (DNS-rebinding), including IPv4-mapped IPv6.
- Results are marked delivered only after Discord accepted them (up to 5 retries); push retries only for "dataset not ready" errors.
- Bad/missing `DISCORD_TOKEN` no longer causes an endless restart loop in `run.sh` / `run.bat`.

## 1.0.2
- Every received command is logged with its delivery delay (`age=`) and the gateway latency; warnings for late delivery and for a frozen event loop.
- A reply that arrives after Discord's 3-second window is now one clean warning line instead of a long traceback.

## 1.0.1
- Startup log now lists the registered commands, and every received command is logged (name only) to diagnose "The application did not respond".
- README: use a dedicated Discord application for this bot.

## 1.0.0
- `/login`, `/logout`, `/whoami`: per-user Kaggle tokens, encrypted at rest, isolated per command.
- `/upload`, `/files`, `/delete`: .blend and audio files as private Kaggle datasets (attachment or direct link).
- `/render`: **Original** (file as saved) and **Custom** (frames, samples, FPS, resolution, engine, color, codec, denoise), optional audio, optional EEVEE→Cycles.
- `/renders`, automatic status tracking and result delivery in DMs, survives bot restarts.
- `/help`, cooldowns, per-user limits, DM-only commands, rotating log file.
- Fixes found while testing: Cycles on an EEVEE-saved file now correctly uses the GPU; stuck-EEVEE watchdog; odd-resolution video padding; audio typo from the original notebook.
