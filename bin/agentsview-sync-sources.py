#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# ///
"""Mirror external agent transcript roots and index them into AgentsView.

Remote mirrors are replaced only after a complete archive/extract succeeds. If a
machine is offline, the previous mirror is left intact and can still be indexed.
"""

from __future__ import annotations

import argparse
import os
import shutil
import sqlite3
import subprocess
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path


DEVSERVER_ROOT = Path(__file__).resolve().parents[1]
DEFAULT_SOURCES_ROOT = DEVSERVER_ROOT / "volumes" / "agentsview-sources"
DEFAULT_DB = DEVSERVER_ROOT / "volumes" / "agentsview" / "sessions.db"
DEFAULT_CONTAINER = os.environ.get("AGENTSVIEW_CONTAINER", "agentsview")
CONTAINER_SOURCE_ROOT = "/agent-sources"
DISABLED_DIR = "/tmp/agentsview-disabled"


@dataclass(frozen=True)
class RemoteSource:
    machine: str
    host: str
    platform: str
    agent: str
    parent: str
    child: str

    @property
    def remote_path(self) -> str:
        sep = "\\" if self.platform == "windows" else "/"
        return self.parent.rstrip("\\/") + sep + self.child

    def local_dir(self, root: Path) -> Path:
        return root / self.machine / self.agent

    @property
    def container_dir(self) -> str:
        return f"{CONTAINER_SOURCE_ROOT}/{self.machine}/{self.agent}"


REMOTE_SOURCES = [
    RemoteSource("desktop-win", "desktop-win", "windows", "codex", r"C:\Users\ankit\.codex", "sessions"),
    RemoteSource("desktop-win", "desktop-win", "windows", "claude", r"C:\Users\ankit\.claude", "projects"),
    RemoteSource("desktop-win", "desktop-win", "windows", "opencode", r"C:\Users\ankit\.local\share", "opencode"),
    RemoteSource("m2book", "m2book", "posix", "codex", "/Users/ankit/.codex", "sessions"),
    RemoteSource("m2book", "m2book", "posix", "claude", "/Users/ankit/.claude", "projects"),
]

AGENT_ENV = {
    "claude": "CLAUDE_PROJECTS_DIR",
    "codex": "CODEX_SESSIONS_DIR",
    "openclaw": "OPENCLAW_DIR",
    "opencode": "OPENCODE_DIR",
}
ALL_AGENT_ENVS = [
    "CLAUDE_PROJECTS_DIR",
    "CODEX_SESSIONS_DIR",
    "COPILOT_DIR",
    "CURSOR_PROJECTS_DIR",
    "OPENCODE_DIR",
    "GEMINI_DIR",
    "PI_DIR",
    "HERMES_SESSIONS_DIR",
    "OPENCLAW_DIR",
    "AMP_DIR",
    "IFLOW_DIR",
    "QWEN_PROJECTS_DIR",
    "QCLAW_DIR",
    "WORKBUDDY_PROJECTS_DIR",
    "PIEBALD_DIR",
    "KIRO_IDE_DIR",
    "FORGE_DIR",
    "WARP_DIR",
    "POSITRON_DIR",
    "ANTIGRAVITY_CLI_DIR",
]


def log(message: str) -> None:
    print(f"[agentsview-sync] {message}", flush=True)


def run(cmd: list[str], *, timeout: int | None = None, check: bool = False) -> subprocess.CompletedProcess[str]:
    cp = subprocess.run(cmd, text=True, capture_output=True, timeout=timeout)
    if check and cp.returncode != 0:
        raise RuntimeError(
            f"command failed ({cp.returncode}): {subprocess.list2cmdline(cmd)}\n"
            f"stdout: {cp.stdout[-2000:]}\n"
            f"stderr: {cp.stderr[-2000:]}"
        )
    return cp


def ssh_base(src: RemoteSource, connect_timeout: int) -> list[str]:
    return ["ssh", "-o", "BatchMode=yes", "-o", f"ConnectTimeout={connect_timeout}", src.host]


def powershell_command(script: str) -> str:
    # The OpenSSH server on desktop-win currently starts cmd.exe, so pass one
    # quoted PowerShell command string rather than relying on POSIX shell syntax.
    return 'powershell -NoProfile -Command "' + script.replace('"', '\\"') + '"'


def remote_exists(src: RemoteSource, connect_timeout: int) -> tuple[bool, str]:
    if src.platform == "windows":
        ps = f"if (Test-Path -LiteralPath '{src.remote_path}' -PathType Container) {{ exit 0 }} else {{ exit 1 }}"
        cmd = ssh_base(src, connect_timeout) + [powershell_command(ps)]
    else:
        test_path = src.remote_path.replace("'", "'\"'\"'")
        cmd = ssh_base(src, connect_timeout) + ["sh", "-lc", f"test -d '{test_path}'"]
    cp = run(cmd, timeout=connect_timeout + 10)
    return cp.returncode == 0, (cp.stderr or cp.stdout).strip()


def remote_tar_command(src: RemoteSource, connect_timeout: int) -> list[str]:
    if src.platform == "windows":
        ps = (
            f"$parent = '{src.parent}'; "
            f"$child = '{src.child}'; "
            "$ErrorActionPreference = 'Stop'; "
            "& tar -C $parent -cf - $child; "
            "exit $LASTEXITCODE"
        )
        return ssh_base(src, connect_timeout) + [powershell_command(ps)]

    parent = src.parent.replace("'", "'\"'\"'")
    child = src.child.replace("'", "'\"'\"'")
    return ssh_base(src, connect_timeout) + ["sh", "-lc", f"exec tar -C '{parent}' -cf - '{child}'"]


def replace_tree(new_tree: Path, dest: Path) -> None:
    dest.parent.mkdir(parents=True, exist_ok=True)
    backup = dest.parent / f".{dest.name}.previous-{int(time.time())}"
    if backup.exists():
        shutil.rmtree(backup)

    moved_old = False
    try:
        if dest.exists():
            dest.rename(backup)
            moved_old = True
        new_tree.rename(dest)
    except Exception:
        if moved_old and backup.exists() and not dest.exists():
            backup.rename(dest)
        raise
    else:
        if backup.exists():
            shutil.rmtree(backup)


def mirror_source(src: RemoteSource, root: Path, connect_timeout: int, archive_timeout: int) -> bool:
    exists, detail = remote_exists(src, connect_timeout)
    if not exists:
        suffix = f": {detail}" if detail else ""
        log(f"skip {src.machine}/{src.agent}: remote path unavailable ({src.remote_path}){suffix}")
        return False

    dest = src.local_dir(root)
    stage = dest.parent / f".{dest.name}.tmp-{os.getpid()}-{int(time.time())}"
    archive = stage / "source.tar"
    if stage.exists():
        shutil.rmtree(stage)
    stage.mkdir(parents=True)

    try:
        log(f"pull {src.machine}/{src.agent}: {src.remote_path}")
        with archive.open("wb") as out:
            cp = subprocess.run(
                remote_tar_command(src, connect_timeout),
                stdout=out,
                stderr=subprocess.PIPE,
                text=False,
                timeout=archive_timeout,
            )
        if cp.returncode != 0:
            stderr = cp.stderr.decode("utf-8", "replace")[-2000:]
            raise RuntimeError(f"remote tar failed for {src.machine}/{src.agent}: {stderr}")

        run(["tar", "-xf", str(archive), "-C", str(stage)], check=True)
        archive.unlink(missing_ok=True)
        extracted = stage / src.child
        if not extracted.is_dir():
            raise RuntimeError(f"archive for {src.machine}/{src.agent} did not contain {src.child}/")
        replace_tree(extracted, dest)
        file_count = sum(1 for p in dest.rglob("*") if p.is_file())
        log(f"mirrored {src.machine}/{src.agent}: {file_count} files -> {dest}")
        return True
    finally:
        shutil.rmtree(stage, ignore_errors=True)


def backup_db(db_path: Path, keep: int) -> Path | None:
    if not db_path.exists():
        log(f"skip db backup: {db_path} does not exist yet")
        return None
    backup_dir = db_path.parent / "backups"
    backup_dir.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")
    backup_path = backup_dir / f"sessions.db.backup-{stamp}.sqlite"
    src = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    dst = sqlite3.connect(str(backup_path))
    try:
        src.backup(dst)
    finally:
        dst.close()
        src.close()

    backups = sorted(backup_dir.glob("sessions.db.backup-*.sqlite"), key=lambda p: p.stat().st_mtime, reverse=True)
    for old in backups[max(0, keep):]:
        old.unlink(missing_ok=True)
    log(f"backed up AgentsView DB -> {backup_path}")
    return backup_path


def docker_exec(
    container: str,
    args: list[str],
    env: dict[str, str] | None = None,
    timeout: int | None = None,
    user: str | None = None,
) -> None:
    cmd = ["docker", "exec"]
    if user:
        cmd += ["-u", user]
    for key, value in (env or {}).items():
        cmd += ["-e", f"{key}={value}"]
    cmd += [container] + args
    cp = run(cmd, timeout=timeout)
    if cp.stdout:
        print(cp.stdout, end="")
    if cp.stderr:
        print(cp.stderr, end="", file=sys.stderr)
    if cp.returncode != 0:
        raise RuntimeError(f"docker exec failed ({cp.returncode}): {subprocess.list2cmdline(cmd)}")


def prepare_disabled_dirs(container: str) -> None:
    paths = [f"{DISABLED_DIR}/{name.lower()}" for name in ALL_AGENT_ENVS]
    docker_exec(container, ["mkdir", "-p", *paths], user="0")


def ensure_db_writable(container: str, db_path: Path) -> None:
    if os.access(db_path, os.W_OK) and os.access(db_path.parent, os.W_OK):
        return
    uid_gid = f"{os.getuid()}:{os.getgid()}"
    log(f"making AgentsView data writable by host user {uid_gid}")
    docker_exec(container, ["chown", "-R", uid_gid, "/data"], user="0")


def sync_agent_root(container: str, agent: str, container_dir: str, timeout: int) -> None:
    env = {name: f"{DISABLED_DIR}/{name.lower()}" for name in ALL_AGENT_ENVS}
    env[AGENT_ENV[agent]] = container_dir
    log(f"sync AgentsView {agent}: {container_dir}")
    docker_exec(container, ["agentsview", "sync"], env=env, timeout=timeout)


def tag_remote_sessions(db_path: Path, sources: list[RemoteSource]) -> int:
    if not db_path.exists():
        raise RuntimeError(f"AgentsView DB not found: {db_path}")
    conn = sqlite3.connect(str(db_path))
    total = 0
    try:
        for src in sources:
            prefix = src.container_dir.rstrip("/") + "/"
            cur = conn.execute(
                "UPDATE sessions SET machine = ? WHERE substr(file_path, 1, ?) = ?",
                (src.machine, len(prefix), prefix),
            )
            total += cur.rowcount
        conn.commit()
    finally:
        conn.close()
    log(f"tagged {total} remote session rows by machine")
    return total


def acquire_lock(lock_path: Path) -> int:
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    fd = os.open(lock_path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    os.write(fd, str(os.getpid()).encode("ascii"))
    return fd


def release_lock(fd: int, lock_path: Path) -> None:
    os.close(fd)
    lock_path.unlink(missing_ok=True)


def select_sources(machines: set[str], agents: set[str]) -> list[RemoteSource]:
    return [s for s in REMOTE_SOURCES if s.machine in machines and s.agent in agents]


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--sources-root", type=Path, default=DEFAULT_SOURCES_ROOT)
    parser.add_argument("--db", type=Path, default=DEFAULT_DB)
    parser.add_argument("--container", default=DEFAULT_CONTAINER)
    parser.add_argument("--machines", default="desktop-win,m2book", help="Comma-separated remote machines to pull.")
    parser.add_argument("--agents", default="codex,claude,opencode", help="Comma-separated remote agent roots to pull.")
    parser.add_argument("--connect-timeout", type=int, default=8)
    parser.add_argument("--archive-timeout", type=int, default=900)
    parser.add_argument("--sync-timeout", type=int, default=1800)
    parser.add_argument("--pull-only", action="store_true")
    parser.add_argument("--sync-only", action="store_true")
    parser.add_argument("--skip-openclaw", action="store_true", help="Do not run the local OpenClaw sync.")
    parser.add_argument("--no-db-backup", action="store_true")
    parser.add_argument("--backup-keep", type=int, default=6)
    parser.add_argument("--strict", action="store_true", help="Fail if any selected remote source cannot be pulled.")
    args = parser.parse_args(argv)

    machines = {m.strip() for m in args.machines.split(",") if m.strip()}
    agents = {a.strip() for a in args.agents.split(",") if a.strip()}
    selected = select_sources(machines, agents)
    if not selected and not args.skip_openclaw:
        log("no remote sources selected; only OpenClaw will be synced")
    elif not selected:
        raise SystemExit("no sources selected")

    lock_path = args.sources_root / ".sync.lock"
    try:
        lock_fd = acquire_lock(lock_path)
    except FileExistsError:
        raise SystemExit(f"sync already running: {lock_path}")

    pulled: list[RemoteSource] = []
    failed: list[RemoteSource] = []
    try:
        args.sources_root.mkdir(parents=True, exist_ok=True)
        if not args.sync_only:
            for src in selected:
                try:
                    if mirror_source(src, args.sources_root, args.connect_timeout, args.archive_timeout):
                        pulled.append(src)
                    else:
                        failed.append(src)
                except Exception as exc:
                    failed.append(src)
                    log(f"warning: {src.machine}/{src.agent} pull failed: {exc}")
                    if args.strict:
                        raise

        if args.pull_only:
            return 0 if not (args.strict and failed) else 1

        if not args.no_db_backup:
            backup_db(args.db, args.backup_keep)

        synced_sources: list[RemoteSource] = []
        prepare_disabled_dirs(args.container)
        for src in selected:
            if src.local_dir(args.sources_root).is_dir():
                sync_agent_root(args.container, src.agent, src.container_dir, args.sync_timeout)
                synced_sources.append(src)
            else:
                log(f"skip AgentsView sync for {src.machine}/{src.agent}: no local mirror yet")

        if not args.skip_openclaw:
            sync_agent_root(args.container, "openclaw", "/agents/openclaw", args.sync_timeout)

        if synced_sources:
            ensure_db_writable(args.container, args.db)
            tag_remote_sessions(args.db, synced_sources)

        if failed:
            log("completed with unavailable sources: " + ", ".join(f"{s.machine}/{s.agent}" for s in failed))
        else:
            log("completed successfully")
        return 0
    finally:
        release_lock(lock_fd, lock_path)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv[1:]))
