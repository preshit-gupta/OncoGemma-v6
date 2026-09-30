"""Role and permission names (SPEC-03 §4.1). Which role holds which permission is configs/auth.yaml."""
from typing import Literal, get_args

Role = Literal["admin", "researcher", "pathologist", "viewer"]
Permission = Literal[
    "case:read",
    "case:create",
    "case:delete",
    "slide:upload",
    "slide:set_mpp",
    "stage:review",
    "stage:confirm",
    "stage:retry",
    "batch:create",
    "batch:cancel",
    "batch:read",
    "research:read",
    "research:annotate",
    "labels:qa",
    "issue:write",
    "eval:test_split",
    "model:promote",
    "user:manage",
    "audit:read_all",
]

ROLES: tuple[str, ...] = get_args(Role)
PERMISSIONS: tuple[str, ...] = get_args(Permission)
