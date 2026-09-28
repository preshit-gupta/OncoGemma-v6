"""
Check that a container's entrypoint module imports with only the packages its image installs.

The Dockerfile names one lockfile (``COPY requirements/<name>.lock.txt``). Every import name
in DIST_FOR_MODULE whose distribution is missing from that lockfile is blocked, and the
entrypoint module is imported in a fresh interpreter. A blocked import fails the check, so a
dependency split that the code cannot run with is caught before an image is deployed.

Usage: python tools/check_image_imports.py <Dockerfile> <module>
       python tools/check_image_imports.py ops/docker/Dockerfile.api app.main
"""
from __future__ import annotations

import argparse
import os
import re
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]

# Import name -> distribution that provides it, for packages that only some images install.
DIST_FOR_MODULE = {
    "pandas": "pandas",
    "pyarrow": "pyarrow",
    "matplotlib": "matplotlib",
    "openslide": "openslide-python",
    "pyvips": "pyvips",
    "scipy": "scipy",
    "shapely": "shapely",
    "cv2": "opencv-python-headless",
    "sklearn": "scikit-learn",
    "skimage": "scikit-image",
    "joblib": "joblib",
    "onnxruntime": "onnxruntime",
    "google.cloud.aiplatform": "google-cloud-aiplatform",
    "vertexai": "google-cloud-aiplatform",
    "google.genai": "google-genai",
}

_IMPORT_PROBE = """
import importlib.abc, os, sys
blocked = {blocked!r}
class Block(importlib.abc.MetaPathFinder):
    def find_spec(self, fullname, path=None, target=None):
        for module in blocked:
            if fullname == module or fullname.startswith(module + "."):
                raise ModuleNotFoundError(f"No module named {{fullname!r}} (not installed by this image)")
        return None
sys.meta_path.insert(0, Block())
import {module}
"""


def lockfile_of(dockerfile: Path) -> Path:
    match = re.search(r"^COPY\s+(requirements/[\w.-]+\.lock\.txt)\s", dockerfile.read_text(encoding="utf-8"), re.M)
    if match is None:
        raise ValueError(f"{dockerfile} does not COPY a requirements/*.lock.txt")
    return REPO_ROOT / match.group(1)


def blocked_modules(lockfile: Path) -> list[str]:
    locked = {m.group(1).lower() for m in re.finditer(r"^([A-Za-z0-9_.\-]+)==", lockfile.read_text(encoding="utf-8"), re.M)}
    return sorted(module for module, dist in DIST_FOR_MODULE.items() if dist.lower() not in locked)


def check(dockerfile: Path, module: str) -> subprocess.CompletedProcess:
    blocked = blocked_modules(lockfile_of(dockerfile))
    env = {**os.environ, "DATABASE_URL": "sqlite:///:memory:", "ENV": "test", "PYTHONPATH": str(REPO_ROOT / "backend")}
    return subprocess.run(
        [sys.executable, "-c", _IMPORT_PROBE.format(blocked=blocked, module=module)],
        cwd=REPO_ROOT / "backend",
        env=env,
        capture_output=True,
        text=True,
    )


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("dockerfile", type=Path)
    parser.add_argument("module")
    args = parser.parse_args(argv)
    dockerfile = args.dockerfile if args.dockerfile.is_absolute() else REPO_ROOT / args.dockerfile
    result = check(dockerfile, args.module)
    if result.returncode != 0:
        print(f"{args.module} does not import with {lockfile_of(dockerfile).name}:", file=sys.stderr)
        print(result.stderr.strip().splitlines()[-1], file=sys.stderr)
        return 1
    print(f"{args.module} imports with {lockfile_of(dockerfile).name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
