"""Contracts for tools/make_image.py -- the one-shot pi-gen
build script.

This module pins the parts a static/unit check can verify without a Docker
daemon or a real pi-gen checkout: repo-root resolution from the script's own
path, the container-state decision matrix parsed from `docker ps` output
shape, PIGEN_DOCKER_OPTS assembly with and without --portal-tag, deploy-image
selection (newest wins, empty errors loud), and --dry-run producing a plan
with zero side effects. Anything that actually calls docker/git/build-docker.sh
is out of scope here by design -- --dry-run never calls those, which is
exactly what lets this module test it.
"""

from __future__ import annotations

import importlib.util
import os
import subprocess
import sys
import time
from pathlib import Path
from types import ModuleType

import pytest


REPO_ROOT = Path(__file__).resolve().parents[1]
MAKE_IMAGE_SCRIPT = REPO_ROOT / "tools" / "make_image.py"


def _load_module() -> ModuleType:
    spec = importlib.util.spec_from_file_location("make_image", MAKE_IMAGE_SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


make_image = _load_module()


# ---------------------------------------------------------------------------
# Repo-root resolution
# ---------------------------------------------------------------------------


def test_resolve_repo_root_from_the_real_script_path() -> None:
    assert make_image.resolve_repo_root(MAKE_IMAGE_SCRIPT) == REPO_ROOT


def test_resolve_repo_root_is_independent_of_cwd(tmp_path: Path) -> None:
    # tools/make_image.py -> tools(0) -> repo root(1).
    fake_script = tmp_path / "some-repo" / "tools" / "make_image.py"
    fake_script.parent.mkdir(parents=True)
    fake_script.write_text("# stub\n", encoding="utf-8")
    assert make_image.resolve_repo_root(fake_script) == tmp_path / "some-repo"


# ---------------------------------------------------------------------------
# Container-state decision matrix
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("ps_output", "expected"),
    [
        ("", "proceed"),
        ("\n", "proceed"),
        ("Up 5 minutes\n", "abort"),
        ("Up About an hour\n", "abort"),
        ("Exited (0) 2 minutes ago\n", "remove"),
        ("Exited (137) 10 seconds ago\n", "remove"),
        ("Created\n", "remove"),
    ],
)
def test_decide_container_action(ps_output: str, expected: str) -> None:
    assert make_image.decide_container_action(ps_output) == expected


# ---------------------------------------------------------------------------
# PIGEN_DOCKER_OPTS assembly
# ---------------------------------------------------------------------------


def test_assemble_docker_opts_without_portal_tag(tmp_path: Path) -> None:
    opts = make_image.assemble_docker_opts(tmp_path, None)
    assert opts == f"--volume {tmp_path}:/palmimo-image:ro"
    assert "PALMIMO_PORTAL_TAG" not in opts


def test_assemble_docker_opts_with_portal_tag(tmp_path: Path) -> None:
    opts = make_image.assemble_docker_opts(tmp_path, "v0.1.0-rc1")
    assert opts == f"--volume {tmp_path}:/palmimo-image:ro -e PALMIMO_PORTAL_TAG=v0.1.0-rc1"


def test_assemble_docker_opts_without_skip_corresponding_source_by_default(tmp_path: Path) -> None:
    opts = make_image.assemble_docker_opts(tmp_path, None)
    assert "PALMIMO_SKIP_CORRESPONDING_SOURCE" not in opts


def test_assemble_docker_opts_with_skip_corresponding_source(tmp_path: Path) -> None:
    opts = make_image.assemble_docker_opts(tmp_path, None, skip_corresponding_source=True)
    assert opts == f"--volume {tmp_path}:/palmimo-image:ro -e PALMIMO_SKIP_CORRESPONDING_SOURCE=1"


def test_assemble_docker_opts_with_portal_tag_and_skip_corresponding_source(tmp_path: Path) -> None:
    opts = make_image.assemble_docker_opts(tmp_path, "v0.1.0-rc1", skip_corresponding_source=True)
    assert opts == (
        f"--volume {tmp_path}:/palmimo-image:ro -e PALMIMO_PORTAL_TAG=v0.1.0-rc1 -e PALMIMO_SKIP_CORRESPONDING_SOURCE=1"
    )


def test_build_plan_defaults_skip_corresponding_source_to_false(tmp_path: Path) -> None:
    plan = make_image.build_plan(tmp_path, pigen_ref=make_image.PIGEN_REF, portal_tag=None)
    assert plan.skip_corresponding_source is False
    assert "PALMIMO_SKIP_CORRESPONDING_SOURCE" not in plan.docker_opts


def test_build_plan_propagates_skip_corresponding_source(tmp_path: Path) -> None:
    plan = make_image.build_plan(
        tmp_path, pigen_ref=make_image.PIGEN_REF, portal_tag=None, skip_corresponding_source=True
    )
    assert plan.skip_corresponding_source is True
    assert "PALMIMO_SKIP_CORRESPONDING_SOURCE=1" in plan.docker_opts


def test_cli_skip_corresponding_source_flag_reaches_the_plan(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, str(MAKE_IMAGE_SCRIPT), "--dry-run", "--skip-corresponding-source"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert "PALMIMO_SKIP_CORRESPONDING_SOURCE=1" in result.stdout
    assert "skip corresponding source: True" in result.stdout


# ---------------------------------------------------------------------------
# Deploy-image selection
# ---------------------------------------------------------------------------


def test_select_newest_deploy_image_picks_the_newest(tmp_path: Path) -> None:
    older = tmp_path / "image_2026-01-01-palmimo.img.xz"
    newer = tmp_path / "image_2026-02-01-palmimo.img.xz"
    older.write_bytes(b"old")
    time.sleep(0.01)
    newer.write_bytes(b"new")
    os.utime(older, (time.time() - 100, time.time() - 100))
    os.utime(newer, (time.time(), time.time()))

    images = make_image.find_deploy_images(tmp_path)
    assert set(images) == {older, newer}
    assert make_image.select_newest_deploy_image(images) == newer


def test_select_newest_deploy_image_errors_on_empty_list() -> None:
    with pytest.raises(make_image.MakeImageError, match="no image"):
        make_image.select_newest_deploy_image([])


def test_find_deploy_images_ignores_unrelated_files(tmp_path: Path) -> None:
    (tmp_path / "image_2026-01-01-palmimo.img.xz").write_bytes(b"x")
    (tmp_path / "some-other-file.txt").write_bytes(b"x")
    (tmp_path / "image_2026-01-01-lite.img.xz").write_bytes(b"x")
    images = make_image.find_deploy_images(tmp_path)
    assert [p.name for p in images] == ["image_2026-01-01-palmimo.img.xz"]


def test_find_deploy_images_on_missing_dir_returns_empty(tmp_path: Path) -> None:
    assert make_image.find_deploy_images(tmp_path / "does-not-exist") == []


# ---------------------------------------------------------------------------
# --dry-run: plan output, zero side effects
# ---------------------------------------------------------------------------


def test_dry_run_prints_a_plan_and_touches_nothing(tmp_path: Path) -> None:
    fake_repo = tmp_path / "fake-repo"
    fake_script = fake_repo / "tools" / "make_image.py"
    fake_script.parent.mkdir(parents=True)
    fake_script.write_text("# stub\n", encoding="utf-8")

    plan = make_image.build_plan(fake_repo, pigen_ref=make_image.PIGEN_REF, portal_tag=None)
    text = make_image.render_plan(plan)

    assert "make_image.py plan" in text
    assert str(fake_repo) in text
    assert make_image.PIGEN_REF in text
    assert "--volume" in text
    # Building/rendering the plan is pure -- only the directories/file we
    # created ourselves for the fake script should exist afterward.
    before = set(fake_repo.rglob("*"))
    make_image.build_plan(fake_repo, pigen_ref=make_image.PIGEN_REF, portal_tag=None)
    make_image.render_plan(plan)
    after = set(fake_repo.rglob("*"))
    assert after == before, f"dry-run plan construction touched: {after - before}"


def test_dry_run_cli_invocation_produces_a_plan_and_makes_no_docker_calls(tmp_path: Path) -> None:
    # Full main() through --dry-run must not shell out to docker/git at all
    # -- assert on the output shape and that the workspace is unchanged
    # (snapshot comparison: on a machine that has actually built, pi-gen/
    # and build.log are legitimately present).
    workspace = REPO_ROOT / "pigen" / ".workspace"
    before = {p.name for p in workspace.iterdir()}
    result = subprocess.run(
        [sys.executable, str(MAKE_IMAGE_SCRIPT), "--dry-run"],
        capture_output=True,
        text=True,
        check=False,
        cwd=str(tmp_path),
    )
    assert result.returncode == 0, result.stderr
    assert "make_image.py plan" in result.stdout
    assert str(REPO_ROOT) in result.stdout
    assert {p.name for p in workspace.iterdir()} == before


# ---------------------------------------------------------------------------
# --clean
# ---------------------------------------------------------------------------


def test_clean_workspace_removes_everything_but_gitignore(tmp_path: Path) -> None:
    workspace = tmp_path / ".workspace"
    workspace.mkdir()
    (workspace / ".gitignore").write_text("*\n!.gitignore\n", encoding="utf-8")
    (workspace / "build.log").write_text("log\n", encoding="utf-8")
    (workspace / "pi-gen").mkdir()
    (workspace / "pi-gen" / "some-file").write_text("x", encoding="utf-8")

    make_image.clean_workspace(workspace)

    assert {p.name for p in workspace.iterdir()} == {".gitignore"}


def test_clean_workspace_on_missing_dir_is_a_noop(tmp_path: Path) -> None:
    # Must not raise even if the workspace was never created.
    make_image.clean_workspace(tmp_path / "does-not-exist")
