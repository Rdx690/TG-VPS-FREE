"""Telegram interface for SOURAJITVFX VPS management."""
from __future__ import annotations

import asyncio
import html
import json
import logging
import os
import re
import shutil
import shlex
import signal
import subprocess
import tempfile
import threading
import time
from datetime import datetime, timezone
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.error import TelegramError
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

from config_helper import get_config, initialize_config, load_dotenv, mutate_config

import root

VERSION = "1.0.0"
BASE_DIR = Path(__file__).resolve().parent
LOGGER = logging.getLogger("sourajitvfx_vps")
STATE: dict[int, dict[str, Any]] = {}
INSTALLING_USERS: set[int] = set()
JOIN_CACHE: dict[int, tuple[bool, float]] = {}
SSHX_CACHE: dict[int, tuple[str, float]] = {}
SSHX_PROCESSES: dict[int, subprocess.Popen[bytes]] = {}
SSHX_LOGS: dict[int, Path] = {}
SSHX_LOCKS: dict[int, asyncio.Lock] = {}
SSHX_INSTALL_LOCK = threading.Lock()
JOIN_CACHE_SECONDS = 300
SSHX_CACHE_SECONDS = 600
STATE_TTL_SECONDS = 600
TELEGRAM_TEXT_LIMIT = 3900
SSHX_LINK_RE = re.compile(r"https://sshx\.io/s/[A-Za-z0-9#]+")


class _HealthHandler(BaseHTTPRequestHandler):
    """Serve minimal health responses for Render's web-service port check."""

    def _respond(self, include_body: bool = True) -> None:
        if self.path not in {"/", "/healthz"}:
            self.send_error(404)
            return
        body = b'{"status":"ok"}\n'
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        if include_body:
            self.wfile.write(body)

    def do_GET(self) -> None:
        self._respond()

    def do_HEAD(self) -> None:
        self._respond(include_body=False)

    def log_message(self, format: str, *args: Any) -> None:
        del format, args


def _start_health_server() -> ThreadingHTTPServer:
    """Bind the HTTP port Render requires while Telegram polling runs."""
    port = _env_int("PORT", 10000)
    server = ThreadingHTTPServer(("0.0.0.0", port), _HealthHandler)
    thread = threading.Thread(target=server.serve_forever, name="health-server", daemon=True)
    thread.start()
    LOGGER.info("Health endpoint listening on port %s", port)
    return server


def _env_int(name: str, default: int = 0) -> int:
    """Read an integer environment variable with safe fallback."""
    try:
        return int(os.environ.get(name, str(default)).strip())
    except (TypeError, ValueError):
        LOGGER.error("Environment variable %s must be an integer", name)
        return default


def owner_id() -> int:
    """Return the configured owner ID, where zero disables owner privileges."""
    return _env_int("OWNER_ID", 0)


def is_owner(user_id: int) -> bool:
    """Return whether a Telegram user is the configured active owner."""
    configured_owner = owner_id()
    return configured_owner != 0 and user_id == configured_owner


def allowed_user_ids() -> set[int]:
    """Parse optional comma-separated Telegram user allowlist."""
    raw = os.environ.get("ALLOWED_USERS", "").strip()
    if not raw:
        return set()
    result: set[int] = set()
    for token in raw.split(","):
        try:
            result.add(int(token.strip()))
        except ValueError:
            LOGGER.error("Ignoring invalid ALLOWED_USERS entry: %s", token)
    return result


def _clean_stale_state() -> None:
    """Drop expired conversations and caches to keep process state bounded."""
    now = time.monotonic()
    for uid, state in list(STATE.items()):
        if now - float(state.get("updated", now)) > STATE_TTL_SECONDS:
            STATE.pop(uid, None)
    for uid, (_, expires) in list(JOIN_CACHE.items()):
        if now >= expires:
            JOIN_CACHE.pop(uid, None)
    for uid, (_, expires) in list(SSHX_CACHE.items()):
        if now >= expires:
            SSHX_CACHE.pop(uid, None)


def _set_state(user_id: int, **values: Any) -> None:
    """Set a user's conversation state and refresh its expiration timer."""
    STATE[user_id] = {**values, "updated": time.monotonic()}


def _main_keyboard(user_id: int) -> InlineKeyboardMarkup:
    """Build the main inline navigation menu."""
    rows = [
        [InlineKeyboardButton("➕ Install VPS", callback_data="menu:install")],
        [InlineKeyboardButton("💻 Enter VPS (SSHX)", callback_data="menu:enter")],
        [InlineKeyboardButton("📋 List VPS", callback_data="menu:list")],
        [InlineKeyboardButton("🗑️ Delete VPS", callback_data="menu:delete")],
        [InlineKeyboardButton("🚀 SSHX Terminal", callback_data="menu:sshx")],
        [InlineKeyboardButton("📊 System Info", callback_data="menu:system")],
    ]
    if is_owner(user_id):
        rows.append([InlineKeyboardButton("👑 Owner Panel", callback_data="owner:menu")])
    return InlineKeyboardMarkup(rows)


def _owner_keyboard() -> InlineKeyboardMarkup:
    """Build the owner-only management menu."""
    return InlineKeyboardMarkup([
        [InlineKeyboardButton("👥 All Users", callback_data="owner:users")],
        [InlineKeyboardButton("📊 Global Stats", callback_data="owner:stats")],
        [InlineKeyboardButton("🗑️ Force Delete Any VPS", callback_data="owner:delete")],
        [InlineKeyboardButton("📢 Broadcast Message", callback_data="owner:broadcast")],
        [InlineKeyboardButton("🔄 Restart SSHX", callback_data="owner:sshx")],
        [InlineKeyboardButton("🧹 Cleanup Orphaned", callback_data="owner:cleanup")],
        [InlineKeyboardButton("⬅️ Main Menu", callback_data="menu:home")],
    ])


def _truncate(text: str, limit: int = TELEGRAM_TEXT_LIMIT) -> str:
    """Keep a Telegram message within a safe character limit."""
    if len(text) <= limit:
        return text
    return text[: limit - 40] + "\n\n[Output truncated to fit Telegram.]"


def _force_channel() -> str:
    """Return a normalized channel identifier for get_chat_member."""
    return os.environ.get("FORCE_CHANNEL", "").strip().removeprefix("@").strip()


def _join_url(channel: str) -> str:
    """Return the configured join URL or a public-channel t.me URL."""
    configured = os.environ.get("FORCE_CHANNEL_URL", "").strip()
    if configured:
        return configured
    if channel and not channel.startswith("-"):
        return f"https://t.me/{channel}"
    return ""


async def _send_join_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE, channel: str) -> None:
    """Explain the channel requirement and offer a join/recheck button."""
    keyboard_rows: list[list[InlineKeyboardButton]] = []
    url = _join_url(channel)
    if url:
        keyboard_rows.append([InlineKeyboardButton("Join Channel", url=url)])
    keyboard_rows.append([InlineKeyboardButton("✅ I Joined", callback_data="join:check")])
    markup = InlineKeyboardMarkup(keyboard_rows)
    text = "Please join the required channel, then tap ‘I Joined’ to continue."
    if update.callback_query and update.callback_query.message:
        await update.callback_query.message.reply_text(text, reply_markup=markup)
    elif update.effective_message:
        await update.effective_message.reply_text(text, reply_markup=markup)


async def _authorized(update: Update, context: ContextTypes.DEFAULT_TYPE) -> bool:
    """Apply the user allowlist and force-channel gate before bot actions."""
    _clean_stale_state()
    user = update.effective_user
    if user is None:
        return False
    uid = user.id
    allowlist = allowed_user_ids()
    if allowlist and uid not in allowlist and not is_owner(uid):
        if update.effective_message:
            await update.effective_message.reply_text("Access is restricted. Please contact the bot owner.")
        elif update.callback_query and update.callback_query.message:
            await update.callback_query.message.reply_text("Access is restricted. Please contact the bot owner.")
        return False
    channel = _force_channel()
    if channel and not is_owner(uid):
        cached = JOIN_CACHE.get(uid)
        joined = cached is not None and cached[1] > time.monotonic() and cached[0]
        if not joined:
            try:
                member = await context.bot.get_chat_member(chat_id=channel, user_id=uid)
                status = str(member.status)
                joined = status in {"member", "administrator", "creator"} or bool(
                    getattr(member, "is_member", False)
                )
            except TelegramError:
                LOGGER.exception("Could not verify force-channel membership for user %s", uid)
                if update.effective_message:
                    await update.effective_message.reply_text(
                        "I could not verify channel membership. Please ask the bot owner to check that the bot is an administrator in the required channel."
                    )
                elif update.callback_query and update.callback_query.message:
                    await update.callback_query.message.reply_text(
                        "I could not verify channel membership. Please ask the bot owner to check the bot's channel permissions."
                    )
                return False
            JOIN_CACHE[uid] = (joined, time.monotonic() + JOIN_CACHE_SECONDS)
        if not joined:
            await _send_join_prompt(update, context, channel)
            return False
    config = await get_config()
    if uid not in config.setdefault("users", []):
        def add_user(data: dict[str, Any]) -> None:
            """Record a newly seen and authorized Telegram user ID."""
            users = data.setdefault("users", [])
            if uid not in users:
                users.append(uid)
        try:
            await mutate_config(add_user)
        except OSError:
            LOGGER.exception("Could not record user %s", uid)
    return True


async def _reply(update: Update, text: str, markup: InlineKeyboardMarkup | None = None) -> None:
    """Reply to the current interaction, using a callback edit when possible."""
    message = update.effective_message
    query = update.callback_query
    if query and query.message:
        try:
            await query.message.edit_text(_truncate(text), reply_markup=markup)
            return
        except TelegramError:
            LOGGER.exception("Could not edit callback message")
    if message:
        await message.reply_text(_truncate(text), reply_markup=markup)


async def start_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /start and show the main menu."""
    if not await _authorized(update, context):
        return
    uid = update.effective_user.id  # type: ignore[union-attr]
    await update.effective_message.reply_text(
        "Welcome to SOURAJITVFX VPS. Select an action below.", reply_markup=_main_keyboard(uid)
    )


async def cancel_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Cancel an active name/broadcast input flow."""
    if not await _authorized(update, context):
        return
    uid = update.effective_user.id  # type: ignore[union-attr]
    if uid in INSTALLING_USERS:
        await update.effective_message.reply_text(
            "An installation is already in progress and cannot be cancelled from chat. Please wait for it to finish."
        )
        return
    STATE.pop(uid, None)
    await update.effective_message.reply_text("Current action cancelled.", reply_markup=_main_keyboard(uid))


async def _user_vps(uid: int, include_all: bool = False) -> list[tuple[str, dict[str, Any]]]:
    """Return VPS entries visible to a user, sorted oldest-first."""
    config = await get_config()
    entries = config.get("vps", {})
    result: list[tuple[str, dict[str, Any]]] = []
    for name, item in entries.items():
        if not isinstance(item, dict):
            continue
        if include_all or int(item.get("owner_id", -1)) == uid:
            result.append((name, item))
    result.sort(key=lambda pair: str(pair[1].get("created_at", "")))
    return result


def _format_created(value: str) -> str:
    """Format an ISO timestamp in compact UTC form."""
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
        return parsed.astimezone(timezone.utc).strftime("%Y-%m-%d")
    except (ValueError, TypeError):
        return "Unknown"


def _format_size(path: Path) -> str:
    """Format a directory size in MiB."""
    return f"{root.directory_size_bytes(path) / (1024 * 1024):.1f} MiB"


async def _show_vps_list(update: Update, uid: int) -> None:
    """Render the caller's VPS list, or all VPS for the owner."""
    rows = await _user_vps(uid, include_all=is_owner(uid))
    if not rows:
        await _reply(update, "No VPS instances found.", _main_keyboard(uid))
        return
    lines = ["VPS instances (oldest first):"]
    for name, info in rows:
        path = root.INSTANCES_DIR / name
        exists = path.is_dir()
        state = "🟢" if exists else "🔴"
        size = await asyncio.to_thread(_format_size, path)
        details = (
            f"{state} {name} | {info.get('os_label', info.get('os_key', 'Unknown'))} | "
            f"{_format_created(str(info.get('created_at', '')))} | {size}"
        )
        if is_owner(uid):
            details += f" | owner {info.get('owner_id', 'unknown')}"
        lines.append(details)
    await _reply(update, "\n".join(lines), _main_keyboard(uid))


def _vps_picker(items: list[tuple[str, dict[str, Any]]], prefix: str) -> InlineKeyboardMarkup:
    """Build a compact button list for a set of VPS records."""
    rows = [[InlineKeyboardButton(name, callback_data=f"{prefix}:{name}")] for name, _ in items]
    rows.append([InlineKeyboardButton("⬅️ Back", callback_data="menu:home")])
    return InlineKeyboardMarkup(rows)


async def _start_install(update: Update, uid: int) -> None:
    """Check quota and show supported operating systems."""
    entries = await _user_vps(uid)
    max_count = max(0, _env_int("MAX_VPS_PER_USER", 1))
    if not is_owner(uid) and len(entries) >= max_count:
        await _reply(update, f"You have reached your VPS limit ({max_count}). Delete an existing VPS before installing another.", _main_keyboard(uid))
        return
    if STATE.get(uid, {}).get("installing") or uid in INSTALLING_USERS:
        await _reply(update, "An installation is already running for your account.", _main_keyboard(uid))
        return
    keyboard = InlineKeyboardMarkup([
        [InlineKeyboardButton("Ubuntu 24.04 LTS", callback_data="os:ubuntu2404")],
        [InlineKeyboardButton("Ubuntu 22.04 LTS", callback_data="os:ubuntu2204")],
        [InlineKeyboardButton("Debian 12 Bookworm", callback_data="os:debian12")],
        [InlineKeyboardButton("Debian 13 Trixie", callback_data="os:debian13")],
        [InlineKeyboardButton("Alpine Linux", callback_data="os:alpine")],
        [InlineKeyboardButton("Cancel", callback_data="menu:home")],
    ])
    _set_state(uid, action="choose_os")
    await _reply(update, "Choose a root filesystem to install:", keyboard)


async def _install_vps(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, name_input: str) -> None:
    """Install the selected VPS with asynchronous progress updates."""
    current = STATE.get(uid, {})
    os_key = str(current.get("os_key", ""))
    raw_name = re.sub(r"[^A-Za-z0-9_.-]+", "_", name_input.strip()).strip("._-")
    if not raw_name:
        await update.effective_message.reply_text("Please send a name using letters and numbers.")
        return
    if not re.match(r"^[A-Za-z0-9]", raw_name):
        raw_name = "vps_" + raw_name
    prefix = "" if is_owner(uid) else f"u{uid}_"
    name = prefix + raw_name
    name = name[:40]
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}", name):
        await update.effective_message.reply_text("That VPS name is not valid. Use 1–40 letters, numbers, dots, dashes, or underscores.")
        return
    entries = await _user_vps(uid)
    max_count = max(0, _env_int("MAX_VPS_PER_USER", 1))
    if not is_owner(uid) and len(entries) >= max_count:
        STATE.pop(uid, None)
        await update.effective_message.reply_text("Your VPS limit was reached before installation could start.")
        return
    if (await get_config()).get("vps", {}).get(name):
        await update.effective_message.reply_text("That VPS name is already in use. Send another name, or /cancel.")
        return
    _set_state(uid, action="install", installing=True, os_key=os_key, name=name)
    status = await update.effective_message.reply_text(f"Starting installation of {name}…")
    loop = asyncio.get_running_loop()
    queue: asyncio.Queue[str] = asyncio.Queue()

    def progress(message: str) -> None:
        """Transfer progress updates from worker thread to the event loop."""
        loop.call_soon_threadsafe(queue.put_nowait, message)

    install_task = asyncio.create_task(asyncio.to_thread(
        root.create_vps, name, os_key, uid, is_owner(uid), progress
    ))
    latest = "Preparing rootfs installation…"
    last_edit = time.monotonic()
    try:
        while not install_task.done():
            wait_seconds = max(0.0, 3.0 - (time.monotonic() - last_edit))
            try:
                latest = await asyncio.wait_for(queue.get(), timeout=wait_seconds)
            except asyncio.TimeoutError:
                pass
            if time.monotonic() - last_edit >= 3.0:
                try:
                    await status.edit_text(f"Installing {name}\n{latest}")
                except TelegramError:
                    LOGGER.exception("Could not update installation progress for %s", name)
                last_edit = time.monotonic()
        success, result = await install_task
        if success:
            os_label = root.OS_CATALOG.get(os_key, {}).get("label", os_key)
            metadata = {
                "owner_id": uid,
                "os_key": os_key,
                "os_label": os_label,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            def add_vps(config: dict[str, Any]) -> None:
                """Register the newly created VPS metadata by its unique name."""
                config.setdefault("vps", {})[name] = metadata
            try:
                await mutate_config(add_vps)
            except OSError:
                LOGGER.exception("Could not save config record for VPS %s", name)
                await asyncio.to_thread(root.delete_vps, name)
                final = "❌ The rootfs was installed but its config could not be saved. The partial instance was removed; check storage permissions and try again."
            else:
                final = (
                    f"✅ {result}\n\nName: {name}\nOS: {os_label}\n"
                    f"Created: {_format_created(metadata['created_at'])}\n"
                    "Use ‘Enter VPS (SSHX)’ to get an interactive terminal workflow."
                )
        else:
            final = f"❌ {result}"
        await status.edit_text(_truncate(final), reply_markup=_main_keyboard(uid))
    except asyncio.CancelledError:
        LOGGER.info("Installation flow cancelled for user %s", uid)
        raise
    except (OSError, TelegramError):
        LOGGER.exception("Installation flow failed for user %s", uid)
        try:
            await status.edit_text("Installation could not be completed. Check the server logs and try again.", reply_markup=_main_keyboard(uid))
        except TelegramError:
            LOGGER.exception("Could not report installation failure")
    finally:
        STATE.pop(uid, None)


def _terminate_sshx(uid: int) -> None:
    """Stop this bot process's SSHX child for one user, if running."""
    process = SSHX_PROCESSES.pop(uid, None)
    if process is not None and process.poll() is None:
        try:
            process.terminate()
            process.wait(timeout=5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=5)
        except OSError:
            LOGGER.exception("Could not stop SSHX child for user %s", uid)
    SSHX_CACHE.pop(uid, None)


def _install_sshx_binary() -> str:
    """Install SSHX into the bot user's home directory without requiring sudo."""
    with SSHX_INSTALL_LOCK:
        existing = shutil.which("sshx")
        if existing:
            return existing
        curl = shutil.which("curl")
        if curl is None:
            raise RuntimeError("curl is not installed on the host")
        install_dir = Path.home() / ".local" / "bin"
        install_dir.mkdir(parents=True, exist_ok=True)
        result = subprocess.run(
            ["sh", "-c", f"{curl} -sSf https://sshx.io/get | sh -s download"],
            cwd=install_dir, capture_output=True, text=True, timeout=180,
        )
        if result.returncode != 0:
            raise RuntimeError(f"SSHX installer failed: {result.stderr[-1000:]}")
        candidate = install_dir / "sshx"
        if not candidate.is_file():
            raise RuntimeError("SSHX installer completed, but the sshx executable was not found")
        return str(candidate)


def _spawn_sshx(uid: int) -> tuple[str, str]:
    """Create one SSHX terminal for a user and wait briefly for its sharing link."""
    _terminate_sshx(uid)
    binary = _install_sshx_binary()
    file_descriptor, raw_log_path = tempfile.mkstemp(
        prefix=f"sourajitvfx-sshx-{uid}-", suffix=".log", dir="/tmp"
    )
    log_path = Path(raw_log_path)
    log_handle = os.fdopen(file_descriptor, "ab", buffering=0)
    try:
        process = subprocess.Popen(
            [binary], cwd=BASE_DIR, stdin=subprocess.DEVNULL,
            stdout=log_handle, stderr=subprocess.STDOUT,
            start_new_session=True, env={**os.environ, "PROOT_NO_SECCOMP": "1"},
        )
    finally:
        log_handle.close()
    SSHX_PROCESSES[uid] = process
    SSHX_LOGS[uid] = log_path
    deadline = time.monotonic() + 25
    while time.monotonic() < deadline:
        if process.poll() is not None:
            output = log_path.read_text(encoding="utf-8", errors="replace")[-1200:]
            raise RuntimeError(f"SSHX exited before creating a link. {output}")
        output = log_path.read_text(encoding="utf-8", errors="replace")
        match = SSHX_LINK_RE.search(output)
        if match:
            return match.group(0), str(log_path)
        time.sleep(0.5)
    raise RuntimeError("Timed out waiting for SSHX to print a terminal link")


async def _sshx_for_user(uid: int, restart: bool = False) -> tuple[bool, str]:
    """Return a cached SSHX link or start a fresh per-user terminal process."""
    lock = SSHX_LOCKS.setdefault(uid, asyncio.Lock())
    async with lock:
        cached = SSHX_CACHE.get(uid)
        if not restart and cached and cached[1] > time.monotonic():
            process = SSHX_PROCESSES.get(uid)
            if process and process.poll() is None:
                return True, cached[0]
        try:
            link, _ = await asyncio.to_thread(_spawn_sshx, uid)
            SSHX_CACHE[uid] = (link, time.monotonic() + SSHX_CACHE_SECONDS)
            return True, link
        except (OSError, RuntimeError, subprocess.TimeoutExpired) as exc:
            LOGGER.exception("Could not start SSHX for user %s", uid)
            return False, str(exc)


def _stop_all_sshx() -> None:
    """Stop every SSHX subprocess managed by this bot process and clear link caches."""
    for uid in list(SSHX_PROCESSES):
        _terminate_sshx(uid)
    for path in list(SSHX_LOGS.values()):
        try:
            path.unlink(missing_ok=True)
        except OSError:
            LOGGER.exception("Could not remove SSHX log %s", path)
    SSHX_LOGS.clear()


async def _send_sshx_link(update: Update, uid: int, vps_name: str | None = None, restart: bool = False) -> None:
    """Start SSHX and provide safe, explicit instructions for PRoot access."""
    await _reply(update, "Starting your private SSHX terminal. This can take a few seconds…")
    success, result = await _sshx_for_user(uid, restart=restart)
    if not success:
        await _reply(update, f"SSHX could not be started: {result}", _main_keyboard(uid))
        return
    if vps_name:
        visible = dict(await _user_vps(uid, include_all=is_owner(uid)))
        info = visible.get(vps_name, {})
        os_label = info.get("os_label", "Unknown")
        shell = "/bin/sh" if info.get("os_key") == "alpine" else "/bin/bash"
        command = shlex.join([
            "proot", "-0", "-r", str(root.INSTANCES_DIR / vps_name),
            "-w", "/root", shell,
        ])
        instructions = f"Selected VPS: {vps_name} ({os_label})\nAfter opening the terminal, run:\n{command}"
    else:
        example_root = shlex.quote(str(root.INSTANCES_DIR / "<name>"))
        instructions = f"Open the terminal link, then run the PRoot command for one of your VPS instances, for example:\nproot -0 -r {example_root} -w /root /bin/bash"
    text = (
        f"Your SSHX terminal link (cached for up to 10 minutes):\n{result}\n\n"
        f"{instructions}\n\n"
        "Security notice: PRoot is not a security boundary. The shared host process and files may be reachable from this terminal. Do not use this service for untrusted users or store secrets in instances."
    )
    await _reply(update, text, _main_keyboard(uid))


async def _system_info() -> str:
    """Collect basic host status and total VPS storage usage."""
    outputs: list[str] = []
    for label, command in (
        ("Kernel", ["uname", "-a"]),
        ("Disk", ["df", "-h", str(BASE_DIR)]),
        ("Memory", ["free", "-m"]),
    ):
        try:
            result = await asyncio.to_thread(
                subprocess.run, command, capture_output=True, text=True,
                timeout=10, check=False,
            )
            outputs.append(f"{label}:\n{result.stdout.strip() or result.stderr.strip()}")
        except (OSError, subprocess.TimeoutExpired):
            LOGGER.exception("System information command failed: %s", label)
            outputs.append(f"{label}: unavailable")
    entries = (await get_config()).get("vps", {})
    total_size = await asyncio.to_thread(root.directory_size_bytes, root.INSTANCES_DIR)
    outputs.append(f"PRoot: {'installed' if shutil.which('proot') else 'not installed'}")
    outputs.append(f"Configured VPS count: {len(entries)}")
    outputs.append(f"Instance storage: {total_size / (1024 * 1024):.1f} MiB")
    return "\n\n".join(outputs)


async def _owner_stats() -> str:
    """Build global VPS and storage statistics for the owner."""
    config = await get_config()
    entries = config.get("vps", {})
    per_user: dict[int, int] = {}
    for info in entries.values():
        if isinstance(info, dict):
            uid = int(info.get("owner_id", 0))
            per_user[uid] = per_user.get(uid, 0) + 1
    total_bytes = await asyncio.to_thread(root.directory_size_bytes, root.INSTANCES_DIR)
    lines = [
        f"Total VPS: {len(entries)}",
        f"Total instance storage: {total_bytes / (1024 * 1024):.1f} MiB",
        "Per-user breakdown:",
    ]
    lines.extend(f"{uid}: {count} VPS" for uid, count in sorted(per_user.items()))
    return "\n".join(lines)


async def _owner_users() -> str:
    """List known Telegram user IDs with at least one VPS."""
    config = await get_config()
    user_ids = sorted({int(item.get("owner_id", 0)) for item in config.get("vps", {}).values() if isinstance(item, dict)})
    return "Users with VPS:\n" + ("\n".join(str(uid) for uid in user_ids) if user_ids else "No users have VPS instances.")


async def _delete_vps(name: str) -> tuple[bool, str]:
    """Remove a VPS directory and its config record as one user-facing operation."""
    config = await get_config()
    if name not in config.get("vps", {}):
        return False, "VPS record was not found."
    success, result = await asyncio.to_thread(root.delete_vps, name)
    if not success:
        return False, result
    def remove_record(data: dict[str, Any]) -> None:
        """Remove the deleted VPS metadata entry from persistent config."""
        data.setdefault("vps", {}).pop(name, None)
    await mutate_config(remove_record)
    return True, result


async def _cleanup_orphans() -> str:
    """Remove instance directories without records and records without directories."""
    config = await get_config()
    records: dict[str, Any] = config.get("vps", {})
    directory_names = {
        path.name for path in root.INSTANCES_DIR.iterdir()
        if path.is_dir() and not path.name.startswith(".")
    } if root.INSTANCES_DIR.exists() else set()
    record_names = set(records)
    orphan_dirs = sorted(directory_names - record_names)
    missing_dirs = sorted(record_names - directory_names)
    for name in orphan_dirs:
        ok, message = await asyncio.to_thread(root.delete_vps, name)
        if not ok:
            LOGGER.error("Orphan cleanup failed for %s: %s", name, message)
    def cleanup_records(data: dict[str, Any]) -> None:
        """Remove VPS records whose instance directories no longer exist."""
        for name in missing_dirs:
            data.setdefault("vps", {}).pop(name, None)
    if missing_dirs:
        await mutate_config(cleanup_records)
    return (
        f"Cleanup complete.\nRemoved orphan directories: {len(orphan_dirs)}"
        f"\nRemoved missing-directory records: {len(missing_dirs)}"
    )


async def _broadcast(update: Update, context: ContextTypes.DEFAULT_TYPE, uid: int, text: str) -> None:
    """Send an owner-supplied message to all stored known user IDs."""
    config = await get_config()
    recipients = sorted({int(value) for value in config.get("users", []) if str(value).lstrip("-").isdigit()})
    delivered = 0
    failed = 0
    for recipient in recipients:
        if recipient == uid:
            continue
        try:
            await context.bot.send_message(chat_id=recipient, text=_truncate(text))
            delivered += 1
            await asyncio.sleep(0.05)
        except TelegramError:
            LOGGER.exception("Broadcast delivery failed for user %s", recipient)
            failed += 1
    STATE.pop(uid, None)
    await update.effective_message.reply_text(
        f"Broadcast finished. Delivered: {delivered}. Failed: {failed}.", reply_markup=_owner_keyboard()
    )


async def callback_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Route all inline buttons after immediately acknowledging the callback."""
    query = update.callback_query
    if query is None:
        return
    try:
        await query.answer()
    except TelegramError:
        LOGGER.exception("Could not answer callback query")
    if query.data == "join:check" and update.effective_user:
        JOIN_CACHE.pop(update.effective_user.id, None)
    if not await _authorized(update, context):
        return
    uid = update.effective_user.id  # type: ignore[union-attr]
    data = query.data or ""
    if data == "join:check":
        await _reply(update, "Membership verified. Welcome!", _main_keyboard(uid))
        return
    if data == "menu:home":
        STATE.pop(uid, None)
        await _reply(update, "Main menu:", _main_keyboard(uid))
        return
    if data == "menu:install":
        await _start_install(update, uid)
        return
    if data.startswith("os:"):
        os_key = data.split(":", 1)[1]
        if os_key not in root.OS_CATALOG:
            await _reply(update, "That operating system selection is not available.", _main_keyboard(uid))
            return
        _set_state(uid, action="name", os_key=os_key)
        await _reply(update, "Send a short VPS name (letters, numbers, dot, dash, underscore). Use /cancel to stop.")
        return
    if data == "menu:list":
        await _show_vps_list(update, uid)
        return
    if data == "menu:delete":
        items = await _user_vps(uid, include_all=is_owner(uid))
        if not items:
            await _reply(update, "No VPS instances are available to delete.", _main_keyboard(uid))
        else:
            await _reply(update, "Choose a VPS to delete:", _vps_picker(items, "delete:ask"))
        return
    if data.startswith("delete:ask:"):
        name = data.split(":", 2)[2]
        items = dict(await _user_vps(uid, include_all=is_owner(uid)))
        if name not in items:
            await _reply(update, "That VPS is not available to your account.", _main_keyboard(uid))
            return
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("✅ Yes, delete", callback_data=f"delete:confirm:{name}"),
            InlineKeyboardButton("❌ Cancel", callback_data="menu:delete"),
        ]])
        await _reply(update, f"Permanently delete VPS ‘{name}’? This cannot be undone.", markup)
        return
    if data.startswith("delete:confirm:"):
        name = data.split(":", 2)[2]
        items = dict(await _user_vps(uid, include_all=is_owner(uid)))
        info = items.get(name)
        if not info or (not is_owner(uid) and int(info.get("owner_id", -1)) != uid):
            await _reply(update, "That VPS is not available to your account.", _main_keyboard(uid))
            return
        success, result = await _delete_vps(name)
        await _reply(update, ("✅ " if success else "❌ ") + result, _main_keyboard(uid))
        return
    if data == "menu:enter":
        items = await _user_vps(uid, include_all=is_owner(uid))
        if not items:
            await _reply(update, "You do not have any VPS instances yet.", _main_keyboard(uid))
        else:
            await _reply(update, "Choose a VPS to open in SSHX:", _vps_picker(items, "enter"))
        return
    if data.startswith("enter:"):
        name = data.split(":", 1)[1]
        items = dict(await _user_vps(uid, include_all=is_owner(uid)))
        if name not in items:
            await _reply(update, "That VPS is not available to your account.", _main_keyboard(uid))
            return
        await _send_sshx_link(update, uid, name)
        return
    if data == "menu:sshx":
        await _send_sshx_link(update, uid)
        return
    if data == "menu:system":
        await _reply(update, await _system_info(), _main_keyboard(uid))
        return
    if data == "owner:menu":
        if not is_owner(uid):
            await _reply(update, "Owner access only.", _main_keyboard(uid))
            return
        await _reply(update, "Owner panel:", _owner_keyboard())
        return
    if data.startswith("owner:") and not is_owner(uid):
        await _reply(update, "Owner access only.", _main_keyboard(uid))
        return
    if data == "owner:users":
        await _reply(update, await _owner_users(), _owner_keyboard())
        return
    if data == "owner:stats":
        await _reply(update, await _owner_stats(), _owner_keyboard())
        return
    if data == "owner:delete":
        items = await _user_vps(uid, include_all=True)
        await _reply(update, "Choose any VPS to delete:", _vps_picker(items, "delete:ask") if items else _owner_keyboard())
        return
    if data == "owner:broadcast":
        _set_state(uid, action="broadcast")
        await _reply(update, "Send the broadcast message as your next text message. Use /cancel to stop.")
        return
    if data == "owner:sshx":
        await _reply(update, "Restarting all bot-managed SSHX sessions…")
        await asyncio.to_thread(_stop_all_sshx)
        success, result = await _sshx_for_user(uid)
        await _reply(update, f"SSHX restarted: {result}" if success else f"SSHX restart failed: {result}", _owner_keyboard())
        return
    if data == "owner:cleanup":
        markup = InlineKeyboardMarkup([[
            InlineKeyboardButton("Confirm cleanup", callback_data="owner:cleanup-confirm"),
            InlineKeyboardButton("Cancel", callback_data="owner:menu"),
        ]])
        await _reply(update, "Cleanup deletes instance folders with no config record and removes config records whose folders are missing. Continue?", markup)
        return
    if data == "owner:cleanup-confirm":
        await _reply(update, await _cleanup_orphans(), _owner_keyboard())
        return
    await _reply(update, "This button is no longer available.", _main_keyboard(uid))


async def text_handler(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle text input only when a supported stateful flow is active."""
    if not await _authorized(update, context):
        return
    message = update.effective_message
    if message is None or message.text is None:
        return
    uid = update.effective_user.id  # type: ignore[union-attr]
    if message.text.startswith("/"):
        await message.reply_text("Unknown command. Use /start to open the menu.", reply_markup=_main_keyboard(uid))
        return
    current = STATE.get(uid, {})
    action = current.get("action")
    if action == "name":
        if uid in INSTALLING_USERS:
            await message.reply_text("An installation is already running for your account.")
            return
        INSTALLING_USERS.add(uid)
        try:
            await _install_vps(update, context, uid, message.text)
        finally:
            INSTALLING_USERS.discard(uid)
        return
    if action == "broadcast" and is_owner(uid):
        await _broadcast(update, context, uid, message.text)
        return
    await message.reply_text("Use the menu buttons to choose an action.", reply_markup=_main_keyboard(uid))


async def _shutdown(application: Application) -> None:
    """Terminate SSHX child processes and remove temporary link logs on shutdown."""
    del application
    _stop_all_sshx()


def _handle_signal(signum: int, frame: Any) -> None:
    """Log process termination signals; PTB's event loop handles its own shutdown."""
    del frame
    LOGGER.info("Received signal %s; shutting down", signum)
    raise KeyboardInterrupt


def main() -> None:
    """Initialize storage and start the Telegram polling application."""
    load_dotenv()
    logging.basicConfig(
        level=os.environ.get("LOG_LEVEL", "INFO").upper(),
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("httpcore").setLevel(logging.WARNING)
    token = os.environ.get("BOT_TOKEN", "").strip()
    if not token:
        raise SystemExit("BOT_TOKEN is required. Copy .env.example to .env and configure it.")
    root.initialize()
    initialize_config()
    if os.environ.get("PROOT_NO_SECCOMP", "1") == "1":
        os.environ["PROOT_NO_SECCOMP"] = "1"
    signal.signal(signal.SIGTERM, _handle_signal)
    application: Application = ApplicationBuilder().token(token).post_shutdown(_shutdown).build()
    application.add_handler(CommandHandler("start", start_command))
    application.add_handler(CommandHandler("cancel", cancel_command))
    application.add_handler(CallbackQueryHandler(callback_handler))
    application.add_handler(MessageHandler(filters.ALL, text_handler))
    application.add_error_handler(error_handler)
    LOGGER.info("Starting SOURAJITVFX VPS bot version %s", VERSION)
    health_server = _start_health_server()
    try:
        application.run_polling(allowed_updates=Update.ALL_TYPES, close_loop=False)
    except KeyboardInterrupt:
        LOGGER.info("Bot stopped by signal or keyboard interrupt")
    finally:
        health_server.shutdown()
        health_server.server_close()


async def error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log uncaught Telegram update errors and notify the affected user safely."""
    LOGGER.error("Unhandled Telegram error", exc_info=context.error)
    if isinstance(update, Update) and update.effective_message:
        try:
            await update.effective_message.reply_text("An unexpected error occurred. Please try again later.")
        except TelegramError:
            LOGGER.exception("Could not send error notice")


if __name__ == "__main__":
    main()
