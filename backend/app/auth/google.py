"""Google ID-token verification (SPEC-03 §3.2, §3.4).

``google.oauth2.id_token`` checks the RS256 signature against Google's published keys (cached by
the transport), and ``exp``, ``iat`` and ``aud``. This module adds the issuer, the verified-email
and the hosted-domain rules. Each refusal raises ``AuthError`` with the error code the API returns.
"""
from dataclasses import dataclass

import google.auth.exceptions
from google.auth.transport import requests as g_requests
from google.oauth2 import id_token

from app.auth.errors import AuthConfigError, AuthError, AuthUnavailable
from app.core.config import settings

GOOGLE_ISSUERS = ("accounts.google.com", "https://accounts.google.com")


@dataclass(frozen=True)
class GoogleIdentity:
    sub: str
    email: str
    hd: str | None
    name: str | None


def allowed_domains() -> frozenset[str]:
    return frozenset(d.strip().lower() for d in settings.AUTH_ALLOWED_DOMAINS.split(",") if d.strip())


def _verify_signed_token(token: str, audience: str) -> dict:
    """Signature, ``exp``, ``iat`` and ``aud``; any failure is ``invalid_token``."""
    try:
        return id_token.verify_token(token, g_requests.Request(), audience=audience)
    except google.auth.exceptions.TransportError as exc:
        raise AuthUnavailable("could not fetch Google's signing keys") from exc
    except ValueError as exc:  # google-auth's MalformedError/InvalidValue and PyJWT-style errors subclass it
        raise AuthError(401, "invalid_token", reason=str(exc)) from exc


def verify_google_credential(credential: str) -> GoogleIdentity:
    """Verify a Google Identity Services credential issued to this app's OAuth client.

    The domain rule (``hd`` in AUTH_ALLOWED_DOMAINS, or the email allow-listed) is applied by the
    caller, which can look the email up (``app.auth.service.sign_in``).
    """
    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        raise AuthConfigError("GOOGLE_OAUTH_CLIENT_ID is not set")
    claims = _verify_signed_token(credential, settings.GOOGLE_OAUTH_CLIENT_ID)
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise AuthError(401, "invalid_token", reason="bad_issuer")
    if claims.get("email_verified") is not True:
        raise AuthError(401, "invalid_token", reason="email_unverified")
    email = claims.get("email")
    sub = claims.get("sub")
    if not email or not sub:
        raise AuthError(401, "invalid_token", reason="missing_email_or_sub")
    hd = claims.get("hd")
    return GoogleIdentity(sub=sub, email=email.lower(), hd=hd.lower() if hd else None, name=claims.get("name"))


def verify_cloud_tasks_token(authorization: str | None) -> str:
    """The Cloud Tasks OIDC bearer token on the worker webhook (SPEC-03 §3.4). Returns the caller's email.

    401 when the token is absent or invalid; 403 when it is valid but not the queue's service account.
    """
    if not authorization or not authorization.startswith("Bearer "):
        raise AuthError(401, "invalid_token", reason="missing_bearer_token")
    claims = _verify_signed_token(authorization.removeprefix("Bearer ").strip(), settings.WORKER_SERVICE_URL)
    if claims.get("iss") not in GOOGLE_ISSUERS:
        raise AuthError(401, "invalid_token", reason="bad_issuer")
    email = claims.get("email")
    if not settings.CLOUD_TASKS_SERVICE_ACCOUNT or email != settings.CLOUD_TASKS_SERVICE_ACCOUNT:
        raise AuthError(403, "forbidden", reason="wrong_service_account")
    if claims.get("email_verified") is not True:
        raise AuthError(403, "forbidden", reason="email_unverified")
    return email
