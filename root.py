"""PRoot VPS filesystem management; this module never opens an interactive shell."""
from __future__ import annotations

import json
import logging
import os
import platform
import posixpath
import re
import shutil
import subprocess
import tarfile
import time
import urllib.request
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Callable
from urllib.parse import quote, unquote, urljoin

VERSION = "1.0.0"
BASE_DIR = Path(__file__).resolve().parent
STORAGE_DIR = (
    Path(os.environ["DATA_DIR"]).expanduser().resolve()
    if os.environ.get("DATA_DIR", "").strip()
    else BASE_DIR
)
BIN_DIR = STORAGE_DIR / "bin"
INSTANCES_DIR = STORAGE_DIR / "instances"
DOWNLOADS_DIR = STORAGE_DIR / "downloads"
MAX_ROOTFS_DOWNLOAD_BYTES = 1_500_000_000
MAX_ROOTFS_UNPACKED_BYTES = 8_000_000_000
MAX_TAR_MEMBERS = 500_000
DOWNLOAD_TIMEOUT_SECONDS = 45
USER_AGENT = f"SOURAJITVFX-VPS/{VERSION}"
LOGGER = logging.getLogger(__name__)

OS_CATALOG: dict[str, dict[str, str]] = {
    "ubuntu2404": {"label": "Ubuntu 24.04 LTS", "image": "ubuntu/noble"},
    "ubuntu2204": {"label": "Ubuntu 22.04 LTS", "image": "ubuntu/jammy"},
    "debian12": {"label": "Debian 12 Bookworm", "image": "debian/bookworm"},
    "debian13": {"label": "Debian 13 Trixie", "image": "debian/trixie"},
    "alpine": {"label": "Alpine Linux", "image": "alpine/latest"},
}


def initialize() -> None:
    """Create the project's persistent directories; safe to call repeatedly."""
    for path in (BIN_DIR, INSTANCES_DIR, DOWNLOADS_DIR):
        path.mkdir(parents=True, exist_ok=True)


def _architecture() -> str:
    """Map the host CPU architecture to the Linux Containers image naming scheme."""
    machine = platform.machine().lower()
    if machine in {"x86_64", "amd64"}:
        return "amd64"
    if machine in {"aarch64", "arm64"}:
        return "arm64"
    raise RuntimeError(f"Unsupported CPU architecture: {machine}")


def _http_text(url: str) -> str:
    """Fetch a small UTF-8 directory-index page with a bounded timeout."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        return response.read(4_000_000).decode("utf-8", errors="replace")


def _latest_alpine_image() -> str:
    """Find the newest stable Alpine image family exposed by the image index."""
    index_url = "https://images.linuxcontainers.org/images/alpine/"
    page = _http_text(index_url)
    versions = re.findall(r'href=["\'](3\.\d+)/["\']', page)
    if not versions:
        raise RuntimeError("Could not find a stable Alpine rootfs in the image index")
    latest = max(set(versions), key=lambda version: tuple(int(part) for part in version.split(".")))
    return f"alpine/{latest}"


def _latest_rootfs_url(image: str, architecture: str) -> str:
    """Resolve a current Linux Containers rootfs archive for a distro image."""
    if image == "alpine/latest":
        image = _latest_alpine_image()
    directory_url = f"https://images.linuxcontainers.org/images/{image}/{architecture}/default/"
    page = _http_text(directory_url)
    build_links = re.findall(r"href=['\"]([^/'\"]+)/['\"]", page)
    timestamps = sorted(
        {
            unquote(link)
            for link in build_links
            if re.fullmatch(r"\d{8}_\d{2}:?\d{2}", unquote(link))
        }
    )
    if not timestamps:
        raise RuntimeError(f"No image builds are listed for {image} on {architecture}")
    latest = timestamps[-1]
    return urljoin(directory_url, f"{quote(latest, safe='_')}/rootfs.tar.xz")


def _download(url: str, destination: Path, progress_cb: Callable[[str], None] | None) -> None:
    """Stream a rootfs archive to disk while enforcing a maximum download size."""
    request = urllib.request.Request(url, headers={"User-Agent": USER_AGENT})
    total = 0
    last_report = 0.0
    with urllib.request.urlopen(request, timeout=DOWNLOAD_TIMEOUT_SECONDS) as response:
        declared_size = response.headers.get("Content-Length")
        if declared_size and int(declared_size) > MAX_ROOTFS_DOWNLOAD_BYTES:
            raise RuntimeError("Rootfs archive exceeds the configured download size limit")
        with destination.open("wb") as output:
            while True:
                chunk = response.read(1024 * 1024)
                if not chunk:
                    break
                total += len(chunk)
                if total > MAX_ROOTFS_DOWNLOAD_BYTES:
                    raise RuntimeError("Rootfs archive exceeded the configured download size limit")
                output.write(chunk)
                now = time.monotonic()
                if progress_cb and now - last_report >= 2.5:
                    progress_cb(f"Downloaded {total / (1024 * 1024):.0f} MiB of rootfs data…")
                    last_report = now
    if total == 0:
        raise RuntimeError("The downloaded rootfs archive was empty")


def _inside_root(root: Path, candidate: Path) -> bool:
    """Return whether a resolved candidate remains within the extraction root."""
    try:
        candidate.resolve(strict=False).relative_to(root.resolve())
        return True
    except (OSError, ValueError):
        return False


def _fallback_data_filter(member: tarfile.TarInfo, destination: str) -> tarfile.TarInfo | None:
    """Python 3.11-compatible conservative equivalent of tarfile.data_filter."""
    root = Path(destination).resolve()
    member_path = PurePosixPath(member.name)
    if member_path.is_absolute() or ".." in member_path.parts:
        raise tarfile.FilterError(f"Unsafe archive path: {member.name}")
    target = root.joinpath(*member_path.parts)
    if not _inside_root(root, target):
        raise tarfile.FilterError(f"Archive path escapes extraction root: {member.name}")
    if member.isdev() or member.isfifo():
        return None
    if member.issym():
        link = PurePosixPath(member.linkname)
        if link.is_absolute():
            resolved = root.joinpath(*link.parts[1:])
            if not _inside_root(root, resolved):
                raise tarfile.FilterError(f"Unsafe symbolic link: {member.name}")
            member.linkname = posixpath.relpath(
                resolved.relative_to(root).as_posix(),
                target.parent.relative_to(root).as_posix() or ".",
            )
        else:
            resolved = target.parent.joinpath(*link.parts)
        if not _inside_root(root, resolved):
            raise tarfile.FilterError(f"Unsafe symbolic link: {member.name}")
    elif member.islnk():
        link = PurePosixPath(member.linkname)
        resolved = root.joinpath(*(link.parts[1:] if link.is_absolute() else link.parts))
        if ".." in link.parts or not _inside_root(root, resolved):
            raise tarfile.FilterError(f"Unsafe hard link: {member.name}")
        if link.is_absolute():
            member.linkname = resolved.relative_to(root).as_posix()
    member.mode &= 0o777
    member.uid = 0
    member.gid = 0
    return member


def _safe_extract_legacy(archive: tarfile.TarFile, destination: Path) -> None:
    """Safely extract archives on Python versions without extractall(filter=...)."""
    root = destination.resolve()
    members = archive.getmembers()
    if len(members) > MAX_TAR_MEMBERS:
        raise RuntimeError("Rootfs archive contains too many entries")
    unpacked = sum(max(0, item.size) for item in members if item.isfile())
    if unpacked > MAX_ROOTFS_UNPACKED_BYTES:
        raise RuntimeError("Rootfs archive exceeds the unpacked size limit")
    checked: list[tuple[tarfile.TarInfo, Path]] = []
    for member in members:
        filtered = _fallback_data_filter(member, str(root))
        if filtered is None:
            continue
        relative = PurePosixPath(filtered.name)
        target = root.joinpath(*relative.parts)
        checked.append((filtered, target))
    for member, target in checked:
        if member.isdir():
            target.mkdir(parents=True, exist_ok=True)
        elif member.isfile():
            target.parent.mkdir(parents=True, exist_ok=True)
            source = archive.extractfile(member)
            if source is None:
                continue
            with source, target.open("wb") as output:
                shutil.copyfileobj(source, output, length=1024 * 1024)
            target.chmod(member.mode & 0o777)
    for member, target in checked:
        if member.islnk():
            link = root.joinpath(*PurePosixPath(member.linkname).parts)
            target.parent.mkdir(parents=True, exist_ok=True)
            if link.is_file() and not target.exists():
                os.link(link, target)
    for member, target in checked:
        if member.issym():
            target.parent.mkdir(parents=True, exist_ok=True)
            if not target.exists() and not target.is_symlink():
                os.symlink(member.linkname, target)


def safe_extract_rootfs(archive_path: Path, destination: Path) -> None:
    """Extract a rootfs archive while rejecting path traversal and unsafe entries."""
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive_path, mode="r:*") as archive:
        if len(archive.getmembers()) > MAX_TAR_MEMBERS:
            raise RuntimeError("Rootfs archive contains too many entries")
        unpacked_size = sum(
            max(0, member.size) for member in archive.getmembers() if member.isfile()
        )
        if unpacked_size > MAX_ROOTFS_UNPACKED_BYTES:
            raise RuntimeError("Rootfs archive exceeds the unpacked size limit")
        data_filter = getattr(tarfile, "data_filter", None)
        if data_filter is not None:
            def safe_filter(member: tarfile.TarInfo, path: str) -> tarfile.TarInfo | None:
                """Normalize safe absolute in-root links before applying Python's data filter."""
                filtered = _fallback_data_filter(member, path)
                if filtered is None:
                    return None
                return data_filter(filtered, path)
            archive.extractall(path=destination, filter=safe_filter)
        else:
            _safe_extract_legacy(archive, destination)


def _validate_name(name: str) -> str:
    """Validate an instance name as a single safe directory component."""
    if not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,39}", name):
        raise ValueError("VPS name must be 1–40 characters using letters, numbers, dot, dash, or underscore")
    return name


def create_vps(
    name: str,
    os_key: str,
    owner_id: int,
    is_owner: bool,
    progress_cb: Callable[[str], None] | None = None,
) -> tuple[bool, str]:
    """Download and extract a rootfs into an instance directory without launching it."""
    del is_owner  # Kept in the public interface for caller-level policy decisions.
    initialize()
    try:
        name = _validate_name(name)
        if os_key not in OS_CATALOG:
            return False, "Unsupported operating system selection."
        target = INSTANCES_DIR / name
        if target.exists():
            return False, "A VPS with that name already exists."
        staging = INSTANCES_DIR / f".{name}.installing"
        archive_path = DOWNLOADS_DIR / f"{name}.rootfs.tar.xz"
        shutil.rmtree(staging, ignore_errors=True)
        archive_path.unlink(missing_ok=True)
        staging.mkdir(parents=True, exist_ok=True)
        if progress_cb:
            progress_cb("Resolving the latest rootfs image…")
        os_info = OS_CATALOG[os_key]
        url = _latest_rootfs_url(os_info["image"], _architecture())
        if progress_cb:
            progress_cb("Downloading rootfs archive…")
        _download(url, archive_path, progress_cb)
        if progress_cb:
            progress_cb("Safely extracting rootfs. This may take several minutes…")
        safe_extract_rootfs(archive_path, staging)
        etc_dir = staging / "etc"
        if etc_dir.is_symlink():
            etc_dir.unlink()
        etc_dir.mkdir(parents=True, exist_ok=True)
        resolv_path = etc_dir / "resolv.conf"
        if resolv_path.is_symlink() or resolv_path.exists():
            if resolv_path.is_dir() and not resolv_path.is_symlink():
                shutil.rmtree(resolv_path)
            else:
                resolv_path.unlink()
        resolv_path.write_text(
            "nameserver 1.1.1.1\nnameserver 8.8.8.8\n", encoding="utf-8"
        )
        tmp_dir = staging / "tmp"
        if tmp_dir.is_symlink():
            tmp_dir.unlink()
        tmp_dir.mkdir(parents=True, exist_ok=True)
        tmp_dir.chmod(0o1777)
        metadata = {
            "name": name,
            "os_key": os_key,
            "os_label": os_info["label"],
            "owner_id": int(owner_id),
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        metadata_path = staging / ".vps-info.json"
        if metadata_path.is_symlink() or metadata_path.exists():
            if metadata_path.is_dir() and not metadata_path.is_symlink():
                shutil.rmtree(metadata_path)
            else:
                metadata_path.unlink()
        metadata_path.write_text(
            json.dumps(metadata, indent=2) + "\n", encoding="utf-8"
        )
        staging.replace(target)
        archive_path.unlink(missing_ok=True)
        if progress_cb:
            progress_cb("Rootfs is ready; registering VPS metadata…")
        return True, f"VPS '{name}' ({os_info['label']}) was installed successfully."
    except Exception as exc:
        LOGGER.exception("Failed to create VPS %s", name)
        try:
            shutil.rmtree(INSTANCES_DIR / f".{name}.installing", ignore_errors=True)
            (DOWNLOADS_DIR / f"{name}.rootfs.tar.xz").unlink(missing_ok=True)
        except OSError:
            LOGGER.exception("Failed to clean up partial VPS install")
        return False, f"VPS installation failed: {exc}"


def delete_vps(name: str) -> tuple[bool, str]:
    """Delete a VPS directory safely using a validated instance name."""
    try:
        name = _validate_name(name)
        target = (INSTANCES_DIR / name).resolve()
        if target.parent != INSTANCES_DIR.resolve():
            return False, "Invalid VPS directory."
        if not target.exists():
            return False, "VPS directory was not found."
        shutil.rmtree(target)
        return True, f"VPS '{name}' was deleted."
    except (OSError, ValueError) as exc:
        LOGGER.exception("Failed to delete VPS %s", name)
        return False, f"Could not delete VPS: {exc}"


def list_vps(owner_id: int, is_owner: bool) -> str:
    """Return a compact inventory filtered to the caller unless they are the owner."""
    if not INSTANCES_DIR.exists():
        return "No VPS instances found."
    records: list[tuple[str, int, str]] = []
    for path in INSTANCES_DIR.iterdir():
        if not path.is_dir() or path.name.startswith("."):
            continue
        try:
            info = json.loads((path / ".vps-info.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            LOGGER.warning("Could not read metadata for %s", path.name)
            continue
        instance_owner = int(info.get("owner_id", -1))
        if is_owner or instance_owner == owner_id:
            records.append((path.name, instance_owner, str(info.get("os_label", "Unknown"))))
    records.sort(key=lambda item: item[0])
    return "\n".join(f"{name} | {label} | owner {uid}" for name, uid, label in records) or "No VPS instances found."


def get_proot_command(name: str) -> tuple[bool, list[str]]:
    """Return a safe command list to start an interactive PRoot shell."""
    try:
        name = _validate_name(name)
    except ValueError:
        return False, []
    rootfs = INSTANCES_DIR / name
    if not rootfs.is_dir():
        return False, []
    proot_path = shutil.which("proot") or str(BIN_DIR / "proot")
    if not Path(proot_path).is_file() and shutil.which(proot_path) is None:
        return False, []
    shell = "/bin/bash"
    try:
        metadata = json.loads((rootfs / ".vps-info.json").read_text(encoding="utf-8"))
        if metadata.get("os_key") == "alpine" or not (rootfs / "bin" / "bash").exists():
            shell = "/bin/sh"
    except (OSError, json.JSONDecodeError):
        LOGGER.warning("Could not read VPS metadata for shell selection: %s", name)
    return True, [proot_path, "-0", "-r", str(rootfs), "-w", "/root", shell]


def install_proot() -> bool:
    """Install PRoot from the host package manager when available and not installed."""
    if shutil.which("proot"):
        return True
    if os.name != "posix" or shutil.which("apt-get") is None:
        LOGGER.error("PRoot is missing and apt-get is unavailable; install it on the host")
        return False
    try:
        update = subprocess.run(
            ["apt-get", "update"], check=False, capture_output=True, text=True, timeout=300
        )
        if update.returncode != 0:
            LOGGER.error("apt-get update failed: %s", update.stderr[-2000:])
            return False
        install = subprocess.run(
            ["apt-get", "install", "-y", "--no-install-recommends", "proot"],
            check=False, capture_output=True, text=True, timeout=300,
        )
        if install.returncode != 0:
            LOGGER.error("apt-get install proot failed: %s", install.stderr[-2000:])
            return False
        return shutil.which("proot") is not None
    except (OSError, subprocess.TimeoutExpired):
        LOGGER.exception("PRoot installation failed")
        return False


def directory_size_bytes(path: Path) -> int:
    """Calculate a directory's size while tolerating transient filesystem errors."""
    total = 0
    if not path.exists():
        return total
    for item in path.rglob("*"):
        try:
            if item.is_file() and not item.is_symlink():
                total += item.stat().st_size
        except OSError:
            LOGGER.warning("Could not stat %s", item)
    return total
