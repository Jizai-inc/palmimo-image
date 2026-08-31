#!/usr/bin/env -S uv run --script
# /// script
# requires-python = ">=3.12"
# dependencies = []
# ///
"""One-shot pi-gen build: produce the shipped Palmimo .img.xz from a clean
checkout, with no manual pi-gen wrangling.

Owns a gitignored workspace at pigen/.workspace/ (see that directory's own
.gitignore -- the self-ignoring `*` / `!.gitignore` pattern is the only
ignore mechanism; do not also add `.workspace` to the root .gitignore, which
would stop git from descending into it and break the inner negation). This
script automates exactly what pigen/README.md's manual recipe describes:

  1. preflight: a reachable Docker daemon, no stray `pigen_work` container
     already running
  2. clone RPi-Distro/pi-gen into .workspace/pi-gen if absent, and pin it to
     PIGEN_REF (a fixed commit -- see the constant below)
  3. sync pigen/stage-palmimo and config into the pi-gen checkout, every run
     (so edits here are never stale)
  4. run pi-gen's build-docker.sh with PIGEN_DOCKER_OPTS bind-mounting this
     repository in, streaming output live and into .workspace/build.log
  5. copy the resulting image into dist/ and print its sha256

Usage:
    uv run tools/make_image.py
    uv run tools/make_image.py --portal-tag v0.1.0-rc1
    uv run tools/make_image.py --pigen-ref <sha-or-branch>
    uv run tools/make_image.py --dry-run
    uv run tools/make_image.py --clean

All paths are derived from this script's own location (nothing depends on
the working directory the script is invoked from). Every step is designed
to be idempotent and to fail loud rather than silently paper over a bad
state -- see MakeImageError below.
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import Any


# RPi-Distro/pi-gen's `arm64` branch HEAD, resolved 2026-08-21 via
# `git ls-remote https://github.com/RPi-Distro/pi-gen.git refs/heads/arm64`.
# Pinned (rather than tracking the branch) so a build today and a build next
# month use identical upstream pi-gen logic; bump deliberately, in a PR
# whose point is the bump. --pigen-ref overrides this for one-off builds.
PIGEN_REF = "ca8aeed0ae300c2a89f55ce9617d5f96a27e99e5"

PIGEN_REPO_URL = "https://github.com/RPi-Distro/pi-gen.git"
CONTAINER_NAME = "pigen_work"
DEPLOY_IMAGE_GLOB = "image_*-palmimo.img.xz"

SHA256_CHUNK_SIZE = 1024 * 1024  # 1 MiB


class MakeImageError(Exception):
    """A user-facing, fatal error. Caught in main() and printed without a
    traceback."""


# ---------------------------------------------------------------------------
# Path resolution -- everything derives from the script's own location.
# ---------------------------------------------------------------------------


def resolve_repo_root(script_path: Path) -> Path:
    """tools/make_image.py -> repo root is parents[1]:
    tools/ (0) -> repo root (1)."""
    resolved = script_path.resolve()
    return resolved.parents[1]


def image_dir(repo_root: Path) -> Path:
    """The image directory IS the repo root now -- kept as a named function
    since every other path helper below is expressed in terms of it."""
    return repo_root


def pigen_source_dir(repo_root: Path) -> Path:
    return image_dir(repo_root) / "pigen"


def workspace_dir(repo_root: Path) -> Path:
    return pigen_source_dir(repo_root) / ".workspace"


def pigen_checkout_dir(repo_root: Path) -> Path:
    return workspace_dir(repo_root) / "pi-gen"


def build_log_path(repo_root: Path) -> Path:
    return workspace_dir(repo_root) / "build.log"


def dist_dir(repo_root: Path) -> Path:
    return image_dir(repo_root) / "dist"


# ---------------------------------------------------------------------------
# Container-state decision -- pure, parses `docker ps -a --filter
# name=^/pigen_work$ --format '{{.Status}}'` output.
# ---------------------------------------------------------------------------


def decide_container_action(ps_status_output: str) -> str:
    """Return "proceed" (no container -- nothing to do), "remove" (a stopped
    container from a previous run, safe to `docker rm`), or "abort" (a
    container is currently running -- never kill it silently)."""
    status = ps_status_output.strip()
    if not status:
        return "proceed"
    if status.startswith("Up "):
        return "abort"
    return "remove"


# ---------------------------------------------------------------------------
# PIGEN_DOCKER_OPTS assembly -- pure.
# ---------------------------------------------------------------------------


def assemble_docker_opts(repo_root: Path, portal_tag: str | None) -> str:
    opts = f"--volume {repo_root}:/palmimo-image:ro"
    if portal_tag:
        opts += f" -e PALMIMO_PORTAL_TAG={portal_tag}"
    return opts


# ---------------------------------------------------------------------------
# Deploy-image selection -- pure over an already-globbed file list.
# ---------------------------------------------------------------------------


def find_deploy_images(deploy_dir: Path) -> list[Path]:
    if not deploy_dir.is_dir():
        return []
    return sorted(deploy_dir.glob(DEPLOY_IMAGE_GLOB))


def select_newest_deploy_image(images: list[Path]) -> Path:
    if not images:
        raise MakeImageError(
            f"build reported success but produced no image (no {DEPLOY_IMAGE_GLOB!r} in pi-gen's deploy/ directory)"
        )
    return max(images, key=lambda p: p.stat().st_mtime)


# ---------------------------------------------------------------------------
# Plan construction -- pure. Used by both --dry-run and the real run.
# ---------------------------------------------------------------------------


class Plan:
    def __init__(
        self,
        *,
        repo_root: Path,
        pigen_ref: str,
        portal_tag: str | None,
    ) -> None:
        self.repo_root = repo_root
        self.pigen_ref = pigen_ref
        self.portal_tag = portal_tag
        self.image_dir = image_dir(repo_root)
        self.pigen_source_dir = pigen_source_dir(repo_root)
        self.workspace_dir = workspace_dir(repo_root)
        self.pigen_checkout_dir = pigen_checkout_dir(repo_root)
        self.build_log_path = build_log_path(repo_root)
        self.dist_dir = dist_dir(repo_root)
        self.docker_opts = assemble_docker_opts(repo_root, portal_tag)


def build_plan(repo_root: Path, *, pigen_ref: str, portal_tag: str | None) -> Plan:
    return Plan(repo_root=repo_root, pigen_ref=pigen_ref, portal_tag=portal_tag)


def render_plan(plan: Plan) -> str:
    lines = [
        "make_image.py plan (--dry-run -- nothing will be touched):",
        f"  repo root:        {plan.repo_root}",
        f"  pigen source:     {plan.pigen_source_dir}",
        f"  workspace:        {plan.workspace_dir}",
        f"  pi-gen checkout:  {plan.pigen_checkout_dir}",
        f"  pigen ref:        {plan.pigen_ref}",
        f"  portal tag:       {plan.portal_tag or '<pi-gen config default>'}",
        f"  PIGEN_DOCKER_OPTS: {plan.docker_opts}",
        f"  build log:        {plan.build_log_path}",
        f"  dist dir:         {plan.dist_dir}",
        "  steps that would run: preflight docker -> clone/checkout pi-gen ->",
        "                        sync stage-palmimo + config -> touch stage2/SKIP_IMAGES ->",
        "                        ./build-docker.sh -> copy newest deploy image to dist/ + sha256",
    ]
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


def preflight_docker() -> None:
    result = subprocess.run(["docker", "info"], capture_output=True, text=True, check=False)
    if result.returncode != 0:
        raise MakeImageError(
            f"`docker info` failed -- Docker does not appear to be running/reachable. stderr: {result.stderr.strip()}"
        )


def handle_stray_container() -> None:
    result = subprocess.run(
        ["docker", "ps", "-a", "--filter", f"name=^/{CONTAINER_NAME}$", "--format", "{{.Status}}"],
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise MakeImageError(f"`docker ps` failed: {result.stderr.strip()}")
    action = decide_container_action(result.stdout)
    if action == "abort":
        raise MakeImageError(
            f"container {CONTAINER_NAME!r} is currently running -- another build appears to be "
            "running. Refusing to touch it; wait for it to finish or investigate by hand."
        )
    if action == "remove":
        print(f"==> removing stopped container {CONTAINER_NAME!r} from a previous run")
        rm = subprocess.run(["docker", "rm", CONTAINER_NAME], capture_output=True, text=True, check=False)
        if rm.returncode != 0:
            raise MakeImageError(f"`docker rm {CONTAINER_NAME}` failed: {rm.stderr.strip()}")


# ---------------------------------------------------------------------------
# Workspace: clone + pin pi-gen
# ---------------------------------------------------------------------------


def _run_git(args: list[str], **kwargs: Any) -> subprocess.CompletedProcess[str]:
    return subprocess.run(["git", *args], capture_output=True, text=True, check=False, **kwargs)


def ensure_pigen_checkout(plan: Plan) -> None:
    plan.workspace_dir.mkdir(parents=True, exist_ok=True)
    if not plan.pigen_checkout_dir.exists():
        print(f"==> cloning {PIGEN_REPO_URL} into {plan.pigen_checkout_dir}")
        result = _run_git(["clone", PIGEN_REPO_URL, str(plan.pigen_checkout_dir)])
        if result.returncode != 0:
            raise MakeImageError(f"git clone failed: {result.stderr.strip()}")

    # Make sure the ref exists locally before checking it out -- an
    # unrecognized --pigen-ref (a branch/tag not fetched yet) needs a fetch
    # first; the default PIGEN_REF is a full SHA that a fresh clone already
    # has.
    probe = _run_git(["-C", str(plan.pigen_checkout_dir), "cat-file", "-e", f"{plan.pigen_ref}^{{commit}}"])
    if probe.returncode != 0:
        print(f"==> fetching (ref {plan.pigen_ref!r} not found locally)")
        fetch = _run_git(["-C", str(plan.pigen_checkout_dir), "fetch", "--all", "--tags"])
        if fetch.returncode != 0:
            raise MakeImageError(f"git fetch failed: {fetch.stderr.strip()}")

    print(f"==> checking out pi-gen at {plan.pigen_ref}")
    checkout = _run_git(["-C", str(plan.pigen_checkout_dir), "checkout", "--detach", plan.pigen_ref])
    if checkout.returncode != 0:
        raise MakeImageError(f"git checkout {plan.pigen_ref!r} failed: {checkout.stderr.strip()}")


def sync_stage(plan: Plan) -> None:
    stage_src = plan.pigen_source_dir / "stage-palmimo"
    stage_dst = plan.pigen_checkout_dir / "stage-palmimo"
    config_src = plan.pigen_source_dir / "config"
    config_dst = plan.pigen_checkout_dir / "config"
    stage2_skip_images = plan.pigen_checkout_dir / "stage2" / "SKIP_IMAGES"

    print(f"==> syncing stage-palmimo -> {stage_dst}")
    if stage_dst.exists():
        shutil.rmtree(stage_dst)
    shutil.copytree(stage_src, stage_dst)

    print(f"==> syncing config -> {config_dst}")
    shutil.copy2(config_src, config_dst)

    stage2_skip_images.parent.mkdir(parents=True, exist_ok=True)
    stage2_skip_images.touch()


# ---------------------------------------------------------------------------
# Build
# ---------------------------------------------------------------------------


def run_build(plan: Plan) -> None:
    env = os.environ.copy()
    env["PIGEN_DOCKER_OPTS"] = plan.docker_opts

    plan.build_log_path.parent.mkdir(parents=True, exist_ok=True)
    print(f"==> running ./build-docker.sh (cwd={plan.pigen_checkout_dir})")
    print(f"    PIGEN_DOCKER_OPTS={plan.docker_opts}")
    print(f"    streaming to {plan.build_log_path}")

    with plan.build_log_path.open("w", encoding="utf-8") as log_file:
        process = subprocess.Popen(
            ["./build-docker.sh"],
            cwd=plan.pigen_checkout_dir,
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
            bufsize=1,
        )
        assert process.stdout is not None
        for line in process.stdout:
            sys.stdout.write(line)
            log_file.write(line)
        returncode = process.wait()

    if returncode != 0:
        raise MakeImageError(
            f"build-docker.sh exited {returncode}. See {plan.build_log_path} for the full log. "
            f"The container {CONTAINER_NAME!r} (if any) was left in place for inspection -- "
            f"`docker rm {CONTAINER_NAME}` before re-running once you've looked at it."
        )


def sha256_of(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as f:
        while True:
            chunk = f.read(SHA256_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
    return digest.hexdigest()


def collect_output_image(plan: Plan) -> Path:
    deploy_dir = plan.pigen_checkout_dir / "deploy"
    images = find_deploy_images(deploy_dir)
    selected = select_newest_deploy_image(images)

    plan.dist_dir.mkdir(parents=True, exist_ok=True)
    dest = plan.dist_dir / selected.name
    shutil.copy2(selected, dest)
    return dest


# ---------------------------------------------------------------------------
# --clean
# ---------------------------------------------------------------------------


def clean_workspace(workspace: Path) -> None:
    if not workspace.exists():
        print(f"==> {workspace} does not exist, nothing to clean")
        return
    for entry in workspace.iterdir():
        if entry.name == ".gitignore":
            continue
        print(f"    removing {entry}")
        if entry.is_dir() and not entry.is_symlink():
            shutil.rmtree(entry)
        else:
            entry.unlink()
    print(f"==> cleaned {workspace} (kept .gitignore)")


# ---------------------------------------------------------------------------
# argparse / main
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="make_image.py",
        description=__doc__,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument(
        "--portal-tag",
        default=None,
        help="palmimo-portal tag to bake in (default: pi-gen config's own default, currently v0.1.0).",
    )
    parser.add_argument(
        "--pigen-ref",
        default=None,
        help=f"pi-gen ref (SHA/branch/tag) to build from (default: pinned {PIGEN_REF}).",
    )
    parser.add_argument(
        "--clean",
        action="store_true",
        help="Remove everything under .workspace/ except .gitignore, then exit.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="Print the full plan (resolved paths, docker opts) and touch nothing.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)

    repo_root = resolve_repo_root(Path(__file__))

    if args.clean:
        clean_workspace(workspace_dir(repo_root))
        return 0

    plan = build_plan(repo_root, pigen_ref=args.pigen_ref or PIGEN_REF, portal_tag=args.portal_tag)

    if args.dry_run:
        print(render_plan(plan))
        return 0

    try:
        preflight_docker()
        handle_stray_container()
        ensure_pigen_checkout(plan)
        sync_stage(plan)
        run_build(plan)
        dest = collect_output_image(plan)
        digest = sha256_of(dest)
        print()
        print("=" * 60)
        print("Build complete.")
        print(f"  image:  {dest}")
        print(f"  sha256: {digest}")
        print("=" * 60)
        return 0
    except MakeImageError as exc:
        print(f"error: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\naborted", file=sys.stderr)
        return 130


if __name__ == "__main__":
    raise SystemExit(main())
