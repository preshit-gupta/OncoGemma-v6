"""Authentication and authorization failures, as the API returns them (docs/contracts/auth_v1.md)."""
from fastapi import HTTPException


class AuthError(HTTPException):
    """``detail`` is the contract's error code; ``reason`` says why, for the audit log only."""

    def __init__(self, status_code: int, code: str, reason: str | None = None):
        super().__init__(status_code=status_code, detail=code)
        self.code = code
        self.reason = reason or code


class AuthUnavailable(AuthError):
    """Google's signing keys could not be fetched, so no token can be verified now."""

    def __init__(self, reason: str):
        super().__init__(503, "auth_unavailable", reason=reason)


def session_expired(reason: str) -> AuthError:
    return AuthError(401, "session_expired", reason=reason)


class AuthConfigError(RuntimeError):
    """A sign-in setting is missing or malformed, so no identity can be verified."""
