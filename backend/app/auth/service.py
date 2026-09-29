"""Sign-in: from a verified Google identity to a provisioned user and a new session (SPEC-03 §3.2–3.3)."""
from datetime import datetime

from sqlalchemy import select
from sqlalchemy.orm import Session

from app.auth.errors import AuthConfigError, AuthError
from app.auth.google import allowed_domains, verify_google_credential
from app.auth.sessions import create_session, signing_key
from app.core.config import settings
from app.models.audit import AuditEvent
from app.models.user import User

ANONYMOUS_ACTOR = "anonymous"


def record_auth_event(db: Session, *, actor: str, event_type: str, payload: dict) -> None:
    """Write an audit event that belongs to no case, and commit it with whatever the session holds."""
    db.add(AuditEvent(case_id=None, actor=actor, event_type=event_type, stage=None, payload=payload))
    db.commit()


def _deny(db: Session, error: AuthError, *, actor: str, payload: dict) -> AuthError:
    record_auth_event(db, actor=actor, event_type="auth_denied", payload={"reason": error.reason, **payload})
    return error


def _bootstraps_admin(db: Session, email: str) -> bool:
    """BOOTSTRAP_ADMIN_EMAIL becomes an admin only while no active admin exists (SPEC-03 §8)."""
    if not settings.BOOTSTRAP_ADMIN_EMAIL or email != settings.BOOTSTRAP_ADMIN_EMAIL.strip().lower():
        return False
    active_admin = db.scalar(select(User.id).where(User.role == "admin", User.status == "active").limit(1))
    return active_admin is None


def sign_in(
    db: Session, credential: str, *, ip: str | None, user_agent: str | None, now: datetime
) -> tuple[User, str]:
    """Verify ``credential`` and open a session. Returns the user and the session token.

    Every refusal is written to ``audit_events`` as ``auth_denied`` before it is raised.
    """
    try:
        identity = verify_google_credential(credential)
    except AuthError as exc:
        raise _deny(db, exc, actor=ANONYMOUS_ACTOR, payload={"ip": ip}) from exc

    actor = f"google:{identity.sub}"
    who = {"email": identity.email, "hd": identity.hd, "ip": ip}
    user = db.scalar(select(User).where(User.email == identity.email))
    bootstrap = _bootstraps_admin(db, identity.email)

    in_domain = identity.hd is not None and identity.hd in allowed_domains()
    if not (in_domain or bootstrap or (user is not None and user.allowlisted_external)):
        raise _deny(db, AuthError(403, "domain_not_allowed"), actor=actor, payload=who)

    if bootstrap:
        if user is None:
            user = User(email=identity.email, role="admin", status="invited", created_at=now)
            db.add(user)
        user.role = "admin"
        if user.status == "disabled":
            user.status = "invited"  # a locked-out admin is re-seeded (SPEC-03 §8)
    if user is None:
        raise _deny(db, AuthError(403, "not_provisioned"), actor=actor, payload=who)
    if user.status == "disabled":
        raise _deny(db, AuthError(403, "user_disabled"), actor=actor, payload=who)
    if user.google_sub is not None and user.google_sub != identity.sub:
        raise _deny(db, AuthError(401, "invalid_token", reason="google_account_changed"), actor=actor, payload=who)
    bound_elsewhere = db.scalar(select(User.id).where(User.google_sub == identity.sub, User.email != identity.email))
    if bound_elsewhere is not None:
        raise _deny(db, AuthError(401, "invalid_token", reason="google_account_bound_elsewhere"), actor=actor, payload=who)

    user.google_sub = identity.sub
    user.status = "active"
    user.last_login_at = now
    if identity.name and not user.display_name:
        user.display_name = identity.name
    db.flush()
    session_row, token = create_session(db, user, ip=ip, user_agent=user_agent, now=now)
    record_auth_event(
        db, actor=str(user.id), event_type="auth_signed_in",
        payload={"session_id": str(session_row.id), "bootstrap_admin": bootstrap, "ip": ip},
    )
    return user, token


def check_auth_settings() -> None:
    """Refuse to start an API that could not verify a sign-in or a Cloud Tasks call (SPEC-03 §7)."""
    if not settings.GOOGLE_OAUTH_CLIENT_ID:
        raise AuthConfigError("GOOGLE_OAUTH_CLIENT_ID is not set; no Google credential can be verified")
    signing_key()
    if settings.USE_CLOUD_TASKS and not settings.CLOUD_TASKS_SERVICE_ACCOUNT:
        raise AuthConfigError(
            "USE_CLOUD_TASKS is on but CLOUD_TASKS_SERVICE_ACCOUNT is not set; the worker webhook would refuse every task"
        )
