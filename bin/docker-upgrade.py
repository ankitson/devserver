#!/usr/bin/env python3
# /// script
# requires-python = ">=3.11"
# dependencies = []
# ///
"""
Plan and optionally upgrade Docker Compose services in the homeserver/devserver stacks.

Dry-run is the default. The script uses Compose files as the source of truth and
Docker labels only to determine which services are currently running.
"""

from __future__ import annotations

import argparse
from datetime import UTC, datetime
import json
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any


HROOT = Path("/home/ankit/hroot")
BUILD_SCRIPT = HROOT / "devserver" / "bin" / "docker-build-committed-image.py"

PROJECTS = {
    "homeserver": [
        {"cwd": HROOT / "homeserver", "files": ["docker-compose.yaml"]},
    ],
    "devserver": [
        {"cwd": HROOT / "devserver", "files": ["docker-compose.yml"]},
        {"cwd": HROOT / "devserver", "files": ["docker-compose.pipelines.yml"]},
    ],
}

STATEFUL_SERVICES = {
    ("homeserver", "actual-budget"),
    ("homeserver", "gitea"),
    ("homeserver", "homeassistant"),
    ("homeserver", "keeper-upstream"),
    ("homeserver", "ollama"),
    ("homeserver", "open-webui"),
    ("homeserver", "postgres"),
    ("homeserver", "readest"),
    ("homeserver", "wealthfolio"),
    ("devserver", "litellm-db"),
    ("devserver", "phoenix"),
}

MOVING_TAGS = {
    "",
    "latest",
    "stable",
    "main",
    "main-latest",
    "master",
    "edge",
    "nightly",
    "server-cuda",
    "latest-cuda",
}


@dataclass(frozen=True)
class ComposeProject:
    name: str
    cwd: Path
    files: list[str]


@dataclass
class ServicePlan:
    project: str
    service: str
    action: str
    reason: str
    commands: list[list[str]]
    cwd: Path
    image: str | None
    running: bool
    local_build: bool
    stateful: bool
    config_files: list[str]

    def to_json(self) -> dict[str, Any]:
        return {
            "project": self.project,
            "service": self.service,
            "action": self.action,
            "reason": self.reason,
            "commands": self.commands,
            "cwd": str(self.cwd),
            "image": self.image,
            "running": self.running,
            "local_build": self.local_build,
            "stateful": self.stateful,
            "config_files": self.config_files,
        }


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


def compose_cmd(project: ComposeProject) -> list[str]:
    cmd = ["docker", "compose"]
    for compose_file in project.files:
        cmd.extend(["-f", compose_file])
    return cmd


def load_compose(project: ComposeProject) -> dict[str, Any]:
    result = run(compose_cmd(project) + ["config", "--format", "json"], cwd=project.cwd)
    return json.loads(result.stdout)


def load_running_services() -> dict[tuple[str, str], dict[str, str | None]]:
    result = run(["docker", "ps", "-q"])
    running: dict[tuple[str, str], dict[str, str | None]] = {}
    ids = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    for container_id in ids:
        inspect = run(["docker", "inspect", container_id])
        data = json.loads(inspect.stdout)[0]
        labels = data.get("Config", {}).get("Labels") or {}
        project = labels.get("com.docker.compose.project")
        service = labels.get("com.docker.compose.service")
        if not project or not service:
            continue
        running[(project, service)] = {
            "container": data.get("Name", "").lstrip("/"),
            "image": data.get("Config", {}).get("Image"),
            "working_dir": labels.get("com.docker.compose.project.working_dir"),
            "config_files": labels.get("com.docker.compose.project.config_files"),
        }
    return running


def image_tag(image: str) -> str:
    if "@sha256:" in image:
        return ""
    last_slash = image.rfind("/")
    last_colon = image.rfind(":")
    if last_colon > last_slash:
        return image[last_colon + 1 :]
    return ""


def is_registry_image_pinned(image: str) -> bool:
    if "@sha256:" in image:
        return True
    tag = image_tag(image)
    if tag in MOVING_TAGS:
        return False
    if not tag:
        return False
    if re.search(r"\d", tag):
        return True
    return tag not in MOVING_TAGS


def service_build(cfg: dict[str, Any]) -> bool:
    return bool(cfg.get("build"))


def is_local_context(context: str | None) -> bool:
    if not context:
        return False
    return not (
        "://" in context
        or context.startswith("git@")
        or context.startswith("ssh:")
        or context.startswith("http:")
        or context.startswith("https:")
    )


def build_context_path(project: ComposeProject, context: str) -> Path:
    path = Path(context)
    if not path.is_absolute():
        path = project.cwd / path
    return path.resolve()


def image_repo(image: str | None) -> str | None:
    if not image:
        return None
    if "@" in image:
        image = image.split("@", 1)[0]
    last_slash = image.rfind("/")
    last_colon = image.rfind(":")
    if last_colon > last_slash:
        return image[:last_colon]
    return image


def local_build_plan_file(args: argparse.Namespace, project: ComposeProject, service: str) -> Path:
    plan_path = args.plan_file
    if not plan_path.is_absolute():
        plan_path = Path.cwd() / plan_path
    build_dir = plan_path.parent / f"{plan_path.stem}-build-plans"
    return build_dir / f"{project.name}-{service}.json"


def create_committed_build_plan(
    args: argparse.Namespace,
    project: ComposeProject,
    service: str,
    image: str,
    build_cfg: dict[str, Any],
) -> Path | None:
    context = build_cfg.get("context")
    dockerfile = build_cfg.get("dockerfile") or "Dockerfile"
    additional_contexts = build_cfg.get("additional_contexts") or {}
    if additional_contexts or not is_local_context(context):
        return None

    repo = image_repo(image)
    if not repo:
        return None

    plan_file = local_build_plan_file(args, project, service)
    cmd = [
        str(BUILD_SCRIPT),
        "--context",
        str(build_context_path(project, context)),
        "--image",
        repo,
        "--dockerfile",
        dockerfile,
        "--plan-file",
        str(plan_file),
    ]
    for key, value in sorted((build_cfg.get("args") or {}).items()):
        cmd.extend(["--build-arg", f"{key}={value}"])
    if args.force_build:
        cmd.append("--force")

    result = run(cmd, cwd=project.cwd, check=False)
    if result.returncode != 0:
        raise RuntimeError(result.stderr.strip() or result.stdout.strip() or "committed build planning failed")
    return plan_file


def selected_projects(value: str) -> list[ComposeProject]:
    names = list(PROJECTS) if value == "all" else [value]
    projects: list[ComposeProject] = []
    for name in names:
        for cfg in PROJECTS[name]:
            projects.append(ComposeProject(name=name, cwd=cfg["cwd"], files=cfg["files"]))
    return projects


def plan_service(
    project: ComposeProject,
    service: str,
    cfg: dict[str, Any],
    running: dict[tuple[str, str], dict[str, str | None]],
    args: argparse.Namespace,
) -> ServicePlan:
    key = (project.name, service)
    image = cfg.get("image")
    local_build = service_build(cfg)
    is_running = key in running
    stateful = key in STATEFUL_SERVICES
    base = compose_cmd(project)

    def skip(reason: str) -> ServicePlan:
        return ServicePlan(project.name, service, "skip", reason, [], project.cwd, image, is_running, local_build, stateful, project.files)

    if args.running_only and not is_running:
        return skip("not currently running")
    if args.service and service not in args.service:
        return skip("not selected")
    if stateful and not args.include_stateful:
        return skip("stateful; pass --include-stateful to touch it")
    if image and "@sha256:" in image and not args.include_pinned:
        return skip("digest pinned; pass --include-pinned to recreate it")
    if local_build and not args.rebuild_local:
        return skip("local build skipped by --skip-local-build")
    if image and not local_build and is_registry_image_pinned(image) and not args.include_pinned:
        return skip(f"version/tag pinned ({image}); pass --include-pinned to pull/recreate")

    if local_build:
        build_cfg = cfg.get("build") if isinstance(cfg.get("build"), dict) else {"context": cfg.get("build")}
        committed_plan = create_committed_build_plan(args, project, service, image, build_cfg) if image else None
        if committed_plan:
            commands = [
                [str(BUILD_SCRIPT), "--apply", "--plan-file", str(committed_plan)],
                base + ["up", "-d", "--no-deps", service],
            ]
            return ServicePlan(project.name, service, "rebuild", f"committed local build plan {committed_plan}", commands, project.cwd, image, is_running, local_build, stateful, project.files)
        commands = [base + ["build", "--pull", "--no-cache", service], base + ["up", "-d", "--no-deps", service]]
        return ServicePlan(project.name, service, "rebuild", "compose build fallback", commands, project.cwd, image, is_running, local_build, stateful, project.files)

    if image:
        commands = [base + ["pull", service], base + ["up", "-d", "--no-deps", service]]
        return ServicePlan(project.name, service, "pull", "registry image", commands, project.cwd, image, is_running, local_build, stateful, project.files)

    return skip("no image or build configuration found")


def format_command(cmd: list[str]) -> str:
    return " ".join(cmd)


def print_plan(
    plans: list[ServicePlan],
    running: dict[tuple[str, str], dict[str, str | None]],
    project_names: set[str],
    errors: list[dict[str, Any]],
) -> None:
    for error in errors:
        print(
            f"ERROR    {error['project']}/{','.join(error['config_files'])} "
            f"{error['message']}"
        )

    for plan in plans:
        label = plan.action.upper()
        state = "running" if plan.running else "defined"
        image = f" image={plan.image}" if plan.image else ""
        print(f"{label:8} {plan.project}/{plan.service:30} {state:7} {plan.reason}{image}")
        for command in plan.commands:
            print(f"         cd {plan.cwd} && {format_command(command)}")

    defined = {(plan.project, plan.service) for plan in plans}
    extras = sorted(key for key in running if key[0] in project_names and key not in defined)
    if extras:
        print()
        print("Running Compose services not found in the selected Compose config:")
        for project, service in extras:
            info = running[(project, service)]
            print(
                f"WARN     {project}/{service:30} container={info.get('container')} "
                f"image={info.get('image')} config_files={info.get('config_files')}"
            )


def write_plan_file(
    plan_file: Path,
    args: argparse.Namespace,
    projects: list[ComposeProject],
    plans: list[ServicePlan],
    errors: list[dict[str, Any]],
) -> None:
    payload = {
        "version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "cwd": str(Path.cwd()),
        "args": {
            "project": args.project,
            "service": args.service or [],
            "running_only": args.running_only,
            "include_stateful": args.include_stateful,
            "include_pinned": args.include_pinned,
            "rebuild_local": args.rebuild_local,
            "force_build": args.force_build,
        },
        "compose_configs": [
            {"project": project.name, "cwd": str(project.cwd), "files": project.files}
            for project in projects
        ],
        "errors": errors,
        "plans": [plan.to_json() for plan in plans],
    }
    plan_file.write_text(json.dumps(payload, indent=2, sort_keys=True) + "\n")


def execute_plan_file(plan_file: Path) -> int:
    if not plan_file.exists():
        print(f"ERROR: plan file does not exist: {plan_file}", file=sys.stderr)
        return 2

    payload = json.loads(plan_file.read_text())
    errors = payload.get("errors") or []
    if errors:
        print(f"Plan has {len(errors)} recorded config error(s); applying executable entries anyway.")
        for error in errors:
            print(f"ERROR    {error['project']}/{','.join(error['config_files'])} {error['message']}")

    for plan in payload.get("plans") or []:
        if plan.get("action") == "skip":
            continue
        cwd = Path(plan["cwd"])
        print(f"\n==> {plan['project']}/{plan['service']}: {plan['action']}")
        for command in plan.get("commands") or []:
            print(f"+ cd {cwd} && {format_command(command)}")
            result = run(command, cwd=cwd, check=False, capture=False)
            if result.returncode != 0:
                print(f"ERROR: command failed with exit code {result.returncode}", file=sys.stderr)
                return result.returncode
    return 0


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--project", choices=["homeserver", "devserver", "all"], default="all")
    parser.add_argument("--service", action="append", help="Only plan/apply this service. Repeatable.")
    parser.add_argument("--apply", action="store_true", help="Execute the plan. Default is dry-run.")
    parser.add_argument("--plan-file", type=Path, default=Path("docker-upgrade-plan.json"), help="JSON plan path. Default: ./docker-upgrade-plan.json")
    parser.add_argument("--all-defined", dest="running_only", action="store_false", help="Include services that are defined but not running.")
    parser.add_argument("--include-stateful", action="store_true", help="Allow stateful services to be upgraded/recreated.")
    parser.add_argument("--include-pinned", action="store_true", help="Allow exact-version or digest-pinned images to be pulled/recreated.")
    parser.add_argument("--skip-local-build", dest="rebuild_local", action="store_false", help="Do not rebuild local/custom build-context services.")
    parser.add_argument("--force-build", action="store_true", help="Build committed local images even if the commit tag exists.")
    parser.set_defaults(running_only=True, rebuild_local=True, force_build=False)
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    plan_file = args.plan_file
    if args.apply:
        return execute_plan_file(plan_file)

    projects = selected_projects(args.project)
    running = load_running_services()
    plans: list[ServicePlan] = []
    errors: list[dict[str, Any]] = []

    for project in projects:
        try:
            data = load_compose(project)
        except subprocess.CalledProcessError as exc:
            message = (exc.stderr or exc.stdout or str(exc)).strip()
            errors.append(
                {
                    "project": project.name,
                    "cwd": str(project.cwd),
                    "config_files": project.files,
                    "message": message,
                    "returncode": exc.returncode,
                }
            )
            continue
        for service, cfg in sorted((data.get("services") or {}).items()):
            try:
                plans.append(plan_service(project, service, cfg, running, args))
            except RuntimeError as exc:
                errors.append(
                    {
                        "project": project.name,
                        "cwd": str(project.cwd),
                        "config_files": project.files,
                        "service": service,
                        "message": str(exc),
                        "returncode": 1,
                    }
                )

    print_plan(plans, running, {project.name for project in projects}, errors)
    write_plan_file(plan_file, args, projects, plans, errors)
    print()
    print(f"Wrote JSON plan: {plan_file}")
    print(f"Dry run only. Re-run with --apply --plan-file {plan_file} to execute this saved plan.")
    return 0


if __name__ == "__main__":
    try:
        raise SystemExit(main())
    except KeyboardInterrupt:
        print("Interrupted.", file=sys.stderr)
        raise SystemExit(130)
