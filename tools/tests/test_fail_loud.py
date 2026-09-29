"""Backend code raises instead of swallowing errors (SPEC-01 §5: ruff BLE001, S110, PLW0603)."""
import subprocess
import sys
import tomllib
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
CONFIG = REPO_ROOT / "tools" / "ruff_fail_loud.toml"
PACKAGES = ["backend/app", "backend/pipeline", "backend/worker", "backend/eval"]


def ruff(*args: str) -> subprocess.CompletedProcess:
    return subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "--config", str(CONFIG), *args],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,  # the tests read the exit code
    )


def test_backend_code_does_not_swallow_errors():
    result = ruff(*PACKAGES)
    assert result.returncode == 0, result.stdout


def test_every_allowlisted_file_still_needs_its_entry():
    """The list only shrinks: a file and rule whose last finding is gone must leave it."""
    ignored = tomllib.loads(CONFIG.read_text(encoding="utf-8"))["lint"]["per-file-ignores"]
    files = [pattern.removeprefix("**/") for pattern in ignored]
    result = subprocess.run(
        [sys.executable, "-m", "ruff", "check", "--no-cache", "--isolated", "--output-format", "concise",
         "--select", "BLE001,S110,PLW0603", *files],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        check=False,  # findings are expected: they are what the entries cover
    )
    found = set()
    for line in result.stdout.splitlines():
        path, sep, rest = line.partition(":")
        if sep and rest.count(":") >= 2:
            found.add((Path(path).as_posix(), rest.split(":", 2)[2].split()[0]))
    stale = [
        (pattern, rule)
        for pattern, rules in ignored.items()
        for rule in rules
        if (pattern.removeprefix("**/"), rule) not in found
    ]
    assert not stale, f"remove these entries from {CONFIG.name}: {stale}"


def test_the_gateway_era_modules_are_not_allowlisted():
    ignored = tomllib.loads(CONFIG.read_text(encoding="utf-8"))["lint"]["per-file-ignores"]
    for module in (
        "backend/app/inference/gateway.py",
        "backend/app/core/fallbacks.py",
        "backend/worker/execution.py",
        "backend/worker/runtime.py",
    ):
        assert not any(Path(module).match(pattern.removeprefix("**/")) for pattern in ignored), module


def test_the_rules_catch_a_swallowed_error(tmp_path):
    module = tmp_path / "backend" / "worker" / "new_stage.py"
    module.parent.mkdir(parents=True)
    module.write_text(
        "def run():\n"
        "    try:\n"
        "        return 1 / 0\n"
        "    except Exception:\n"
        "        pass\n",
        encoding="utf-8",
    )
    result = ruff(str(module))
    assert result.returncode == 1
    assert "BLE001" in result.stdout and "S110" in result.stdout
