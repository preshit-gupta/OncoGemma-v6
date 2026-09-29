"""Each container's entrypoint imports with only the packages its image installs (WP-1.3 follow-up)."""
import re

import pytest

from tools.check_image_imports import REPO_ROOT, blocked_modules, check, lockfile_of

API = REPO_ROOT / "ops" / "docker" / "Dockerfile.api"
WORKER = REPO_ROOT / "ops" / "docker" / "Dockerfile.worker"


@pytest.mark.parametrize(
    "dockerfile, module",
    [
        # The API serves every router, runs stages in-process (RUN_IN_PROCESS_WORKER) and
        # executes Cloud Tasks webhooks, so it needs the pipeline's libraries.
        (API, "app.main"),
        (WORKER, "worker.cloud_job_entry"),
        (WORKER, "worker.main"),
    ],
)
def test_entrypoint_imports_with_its_image_dependencies(dockerfile, module):
    result = check(dockerfile, module)
    assert result.returncode == 0, result.stderr.strip().splitlines()[-1]


def test_the_check_detects_a_missing_package(tmp_path):
    """A lockfile without scipy must fail app.main (routers/mitosis -> pipeline/hpf -> scipy)."""
    slim = tmp_path / "slim.lock.txt"
    slim.write_text((REPO_ROOT / "requirements" / "api.lock.txt").read_text(encoding="utf-8"), encoding="utf-8")
    assert "scipy" in blocked_modules(slim)
    dockerfile = tmp_path / "Dockerfile"
    dockerfile.write_text("COPY requirements/api.lock.txt /app/requirements/api.lock.txt\n", encoding="utf-8")
    assert lockfile_of(dockerfile) == REPO_ROOT / "requirements" / "api.lock.txt"
    assert check(dockerfile, "app.main").returncode != 0


def test_images_that_run_slides_install_openslide_and_models():
    for dockerfile in (API, WORKER):
        text = dockerfile.read_text(encoding="utf-8")
        assert re.search(r"\blibopenslide0\b", text), dockerfile.name
        assert re.search(r"^COPY models /app/models$", text, re.M), dockerfile.name
