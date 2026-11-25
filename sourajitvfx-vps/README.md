# SOURAJITVFX VPS

SOURAJITVFX VPS is an asynchronous Telegram bot that downloads selected Linux root filesystems and manages them as PRoot instance directories. It includes per-user quotas, optional channel membership checks, VPS inventory/deletion, system information, SSHX terminal links, and owner controls.

> **Critical security warning:** PRoot is not a security boundary. A PRoot process runs with the bot's host-user permissions and may access host files and processes. SSHX exposes an interactive terminal to the host user; a user may leave the PRoot rootfs. Do not offer this bot to untrusted users, do not store credentials or secrets on the host, and do not treat PRoot as multi-tenant VPS isolation. For hostile or public users, use real VM/container isolation (separate non-root identities, namespaces/seccomp/cgroups, and per-instance SSH access) rather than this project.

## Features

- Async Telegram Bot API using `python-telegram-bot` 20.7.
- Ubuntu 24.04/22.04, Debian 12/13, and latest stable Alpine rootfs selection from Linux Containers image indexes.
- Download progress updates, bounded download/unpacked sizes, and safe archive extraction.
- Per-user VPS quota, owner bypass, private/public force-channel checks, and 5-minute membership cache.
- Per-user SSHX terminal sessions with a 10-minute link cache.
- VPS inventory, owner statistics, broadcast, forced deletion, SSHX restart, and orphan cleanup.
- JSON configuration with atomic writes and no bot tokens or credentials persisted in the config.

## Requirements

- Python 3.11 or newer.
- Linux host with `proot`, `curl`, `ca-certificates`, and `xz-utils` available. The Docker image installs these packages.
- A Telegram bot token.
- Persistent storage for `/app/instances`, `/app/downloads`, and `/app/config.json`.
- Network access to Telegram, `images.linuxcontainers.org`, and `sshx.io`.

## Environment variables

| Variable | Required | Description |
|---|---:|---|
| `BOT_TOKEN` | Yes | Token created with `@BotFather`. |
| `OWNER_ID` | No | Numeric Telegram user ID with owner privileges. `0` disables the owner panel. |
| `ALLOWED_USERS` | No | Comma-separated numeric IDs. Empty means allow all users. The owner bypasses this list. |
| `FORCE_CHANNEL` | No | Channel username without `@`, or numeric channel chat ID. Empty disables membership checks. |
| `FORCE_CHANNEL_URL` | No | Channel join URL; needed for private channels and recommended for invite-only channels. |
| `MAX_VPS_PER_USER` | No | Non-owner VPS quota; defaults to `1`. |
| `PROOT_NO_SECCOMP` | No | Keep set to `1` for compatibility. |
| `DATA_DIR` | No | Persistent runtime storage path. Blank/unset defaults to the project directory; Docker sets `/data`. |
| `LOG_LEVEL` | No | Python logging level; defaults to `INFO`. |

If `FORCE_CHANNEL` is configured, the bot must be an administrator in that channel so it can call `get_chat_member`. For a private channel, configure a valid invite link in `FORCE_CHANNEL_URL`.

## Get a bot token and owner ID

1. In Telegram, open `@BotFather`, create a bot with `/newbot`, and copy its token into `BOT_TOKEN`.
2. Obtain your numeric Telegram user ID through a trusted ID lookup bot or your Telegram client, then set `OWNER_ID`.
3. Never commit `.env` or disclose the bot token. Rotate it with BotFather immediately if it leaks.

## Run locally

```bash
python3 -m venv .venv
. .venv/bin/activate
pip install -r requirements.txt
cp .env.example .env
# Edit .env, then run:
python -u bot.py
```

By default, the application stores runtime data beside the source files in `config.json`, `instances/`, and `downloads/`. If `DATA_DIR` is set in the process environment, those files and directories are placed under that persistent data directory instead.

## Deploy with Docker

Build and run the image as a long-running worker. The bot uses Telegram polling and does not open an HTTP port.

```bash
docker build -t sourajitvfx-vps .
docker run -d --name sourajitvfx-vps --restart unless-stopped \
  --env-file .env \
  -v sourajitvfx-data:/data \
  sourajitvfx-vps
```

The Docker image sets `DATA_DIR=/data`, so one persistent volume contains `config.json`, `instances/`, `downloads/`, and `bin/`. Ensure the `vpsbot` UID 10001 can write to the mounted path.

## Railway

1. Create a project from this repository and use the included `Dockerfile`.
2. Add the environment variables in the service's Variables settings.
3. Deploy it as a **Background Worker** (or worker service), not as a public web service. This app does not bind a port.
4. Attach a persistent volume and mount it at `/data` so instances and `config.json` survive redeploys.
5. Review the platform's CPU, memory, and disk quotas; rootfs installs can consume substantial storage.

## Koyeb

1. Create an application from the repository and select the included Dockerfile build.
2. Configure all required environment variables.
3. Run it as a worker/background process with command `python -u bot.py`; do not configure an HTTP port.
4. Attach persistent storage at `/data` if available in your chosen plan/region. Verify that the mounted volume is writable by UID 10001.

## Render

1. Create a **Background Worker**, not a Web Service.
2. Connect the repository and choose Docker as the runtime.
3. Set `BOT_TOKEN` and other environment variables in the Render dashboard.
4. Attach a persistent disk and mount it at `/data` if the selected plan supports it. Without one, VPS data is lost when the container is replaced.

## Platform limitations and warnings

- **PRoot/ptrace:** PRoot uses `ptrace`; some container hosts restrict it or do not support it reliably. A working Docker build does not guarantee that PRoot can run on the selected host.
- **Ephemeral storage:** Instances and config live on local disk. Add persistent storage or all VPS data can be lost during a restart/redeploy.
- **Resource exhaustion:** This is not a production multi-tenant virtualization platform. Resource limits and isolation are not equivalent to VM hosting.
- **SSHX:** Sharing links are access credentials. Treat them as sensitive, don't post them publicly, and stop sharing when finished.
- **Rootfs availability:** The bot selects the newest image directory exposed by Linux Containers. Upstream indexes, architecture support, and network access can change.

## Troubleshooting

- **Bot exits immediately:** Check `BOT_TOKEN`, platform logs, and that the service is a worker rather than a web service.
- **Force-channel verification fails:** Add the bot as a channel administrator and verify `FORCE_CHANNEL` (without `@`) or the numeric ID. Private channels also need a valid `FORCE_CHANNEL_URL` invite link.
- **Rootfs download fails:** Check outbound network access to `images.linuxcontainers.org`, available disk, supported architecture (`amd64` or `arm64`), and upstream image-index availability.
- **Extraction fails:** Check disk space and that the archive download was not truncated. Remove abandoned hidden `.installing` directories only after stopping the bot.
- **PRoot does not start:** Confirm `proot` is installed and that the platform allows its ptrace behavior. `PROOT_NO_SECCOMP=1` is enabled in the Docker image.
- **SSHX link does not appear:** Check access to `sshx.io`, `curl`, DNS, process permissions, and the bot logs. Links are cached for up to 10 minutes per user.
- **Missing VPS after deployment:** Attach and mount persistent storage at `/data`; ephemeral container storage is not durable.
- **Telegram does not deliver broadcasts:** Users must have started the bot and not blocked it; Telegram can reject messages to unknown or blocked users.

## Data and operational notes

`config.json` stores user IDs and VPS metadata, not Telegram tokens. Rootfs archives are removed after successful installation. The `downloads/` directory may contain a partial archive after abrupt process termination; inspect before deleting. Take backups of persistent storage before upgrades or manual cleanup.
