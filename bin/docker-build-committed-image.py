#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Plan and apply a Docker image build from the committed git tree for a directory.

Planning writes a JSON file. Applying reads that JSON file and builds from the
recorded commit, ignoring uncommitted working-tree changes.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
from pathlib import Path, PurePosixPath
import subprocess
import sys
import tarfile
import tempfile
from typing import Any


def run(
    args: list[str],
    *,
    cwd: Path | None = None,
    check: bool = True,
    capture: bool = True,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        args,
        cwd=str(cwd) if cwd else None,
        text=True,
        capture_output=capture,
        check=check,
    )


def git(cwd: Path, *args: str) -> str:
    return run(["git", "-C", str(cwd), *args]).stdout.strip()


def image_ref_exists(ref: str) -> bool:
    return run(["docker", "image", "inspect", ref], check=False).returncode == 0


def image_id(ref: str) -> str | None:
    result = run(["docker", "image", "inspect", ref, "--format", "{{.Id}}"], check=False)
    if result.returncode != 0:
        return None
    return result.stdout.strip()


def split_image_ref(image: str) -> str:
    if "@" in image:
        image = image.split("@", 1)[0]
    last_slash = image.rfind("/")
    last_colon = image.rfind(":")
    if last_colon > last_slash:
        return image[:last_colon]
    return image


def clean_relpath(path: Path) -> str:
    rel = path.as_posix()
    if rel == ".":
        return "."
    parts = PurePosixPath(rel).parts
    if any(part == ".." for part in parts):
        raise ValueError(f"path escapes build context: {path}")
    return rel


def dockerfile_in_context(dockerfile: str) -> str:
    path = PurePosixPath(dockerfile)
    if path.is_absolute() or any(part == ".." for part in path.parts):
        raise ValueError(f"dockerfile must be relative to context: {dockerfile}")
    return path.as_posix()


def build_plan(args: argparse.Namespace) -> dict[str, Any]:
    context = args.context.resolve()
    repo_root = Path(git(context, "rev-parse", "--show-toplevel")).resolve()
    commit = git(context, "rev-parse", "HEAD")
    short = git(context, "rev-parse", "--short=12", "HEAD")
    status = git(context, "status", "--porcelain")
    rel_context = clean_relpath(context.relative_to(repo_root))
    dockerfile = dockerfile_in_context(args.dockerfile)
    image_repo = split_image_ref(args.image)
    tags = [f"{image_repo}:{short}", f"{image_repo}:latest"]
    if args.version:
        tags.append(f"{image_repo}:{args.version}")

    tag_ids = {tag: img_id for tag in tags if (img_id := image_id(tag))}
    action = "build"
    reason = "committed image tag missing"
    commit_tag = f"{image_repo}:{short}"
    commit_image_id = tag_ids.get(commit_tag)
    if commit_image_id and not args.force:
        missing_tags = [tag for tag in tags if tag_ids.get(tag) != commit_image_id]
        if missing_tags:
            action = "tag"
            reason = "commit image exists; missing alias tag(s)"
        else:
            action = "skip"
            reason = "all requested tags already exist"

    return {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "action": action,
        "reason": reason,
        "force": args.force,
        "context": str(context),
        "repo_root": str(repo_root),
        "context_subdir": rel_context,
        "dockerfile": dockerfile,
        "commit": commit,
        "commit_short": short,
        "dirty": bool(status),
        "status_porcelain": status.splitlines(),
        "image_repo": image_repo,
        "tags": tags,
        "existing_tags_at_plan": sorted(tag_ids),
        "image_ids_at_plan": tag_ids,
        "build_args": args.build_arg or [],
    }


def archive_context(plan: dict[str, Any], dest: Path) -> None:
    repo_root = Path(plan["repo_root"])
    subdir = plan["context_subdir"]
    treeish = plan["commit"] if subdir == "." else f"{plan['commit']}:{subdir}"
    archive = subprocess.run(
        ["git", "-C", str(repo_root), "archive", "--format=tar", treeish],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        check=True,
    )
    tar_path = dest / "context.tar"
    tar_path.write_bytes(archive.stdout)
    with tarfile.open(tar_path) as tar:
        tar.extractall(dest / "context")
    tar_path.unlink()


def apply_plan(plan_file: Path) -> int:
    plan = json.loads(plan_file.read_text())
    tags = plan["tags"]
    commit_tag = f"{plan['image_repo']}:{plan['commit_short']}"

    if plan["action"] == "skip":
        print(f"SKIP {commit_tag}: {plan['reason']}")
        return 0

    if plan["action"] == "tag":
        commit_id = image_id(commit_tag)
        for tag in tags:
            if tag == commit_tag or image_id(tag) == commit_id:
                continue
            print(f"+ docker tag {commit_tag} {tag}")
            result = run(["docker", "tag", commit_tag, tag], check=False, capture=False)
            if result.returncode != 0:
                return result.returncode
        return 0

    with tempfile.TemporaryDirectory(prefix="docker-build-committed-") as tmp:
        tmp_path = Path(tmp)
        archive_context(plan, tmp_path)
        context = tmp_path / "context"
        dockerfile = context / plan["dockerfile"]
        if not dockerfile.exists():
            print(f"ERROR: dockerfile not found in committed context: {plan['dockerfile']}", file=sys.stderr)
            return 2

        cmd = ["docker", "build", "--pull", "-f", str(dockerfile)]
        for tag in tags:
            cmd.extend(["-t", tag])
        for build_arg in plan.get("build_args") or []:
            cmd.extend(["--build-arg", build_arg])
        cmd.append(str(context))

        print(f"Building {plan['image_repo']} from {plan['commit_short']} ({plan['context']})")
        if plan.get("dirty"):
            print("Working tree had uncommitted changes when planned; build uses committed tree only.")
        print("+ " + " ".join(cmd))
        result = run(cmd, check=False, capture=False)
        return result.returncode


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--context", type=Path, help="Docker build context directory in a git repo.")
    parser.add_argument("--image", help="Image repository or image ref. Tags are replaced with commit/latest.")
    parser.add_argument("--dockerfile", default="Dockerfile", help="Dockerfile path relative to context.")
    parser.add_argument("--build-arg", action="append", help="Build arg KEY=VALUE. Repeatable.")
    parser.add_argument("--version", help="Optional extra tag to apply.")
    parser.add_argument("--force", action="store_true", help="Build even if the commit tag already exists.")
    parser.add_argument("--plan-file", type=Path, default=Path("docker-build-plan.json"))
    parser.add_argument("--apply", action="store_true", help="Apply an existing JSON plan.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    if args.apply:
        return apply_plan(args.plan_file)
    if not args.context or not args.image:
        print("ERROR: --context and --image are required when planning.", file=sys.stderr)
        return 2
    plan = build_plan(args)
    args.plan_file.parent.mkdir(parents=True, exist_ok=True)
    args.plan_file.write_text(json.dumps(plan, indent=2, sort_keys=True) + "\n")
    print(f"{plan['action'].upper()} {plan['image_repo']} from {plan['commit_short']}: {plan['reason']}")
    if plan["dirty"]:
        print("NOTE working tree is dirty; build plan uses committed HEAD only.")
    print(f"Wrote JSON plan: {args.plan_file}")
    print(f"Apply with: {Path(__file__).resolve()} --apply --plan-file {args.plan_file}")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
