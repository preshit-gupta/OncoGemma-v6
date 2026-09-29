"""Signed Google ID tokens without Google: a local RSA key stands in for Google's signing keys."""
import time
import uuid
from datetime import datetime, timezone

import jwt
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import rsa
from sqlalchemy.orm import Session

from app.core.config import settings
from app.models.user import User

GOOGLE_ISSUER = "https://accounts.google.com"


class FakeGoogle:
    """Issues RS256 ID tokens and serves the public key where google-auth fetches Google's certs."""

    def __init__(self):
        self.kid = "test-key"
        self._key = rsa.generate_private_key(public_exponent=65537, key_size=2048)
        self.public_pem = self._key.public_key().public_bytes(
            serialization.Encoding.PEM, serialization.PublicFormat.SubjectPublicKeyInfo
        ).decode()

    def certs(self, request, certs_url):
        return {self.kid: self.public_pem}

    def token(self, *, signing_key=None, **overrides) -> str:
        now = int(time.time())
        claims = {
            "iss": GOOGLE_ISSUER,
            "aud": settings.GOOGLE_OAUTH_CLIENT_ID,
            "sub": "sub-" + uuid.uuid4().hex,
            "email": "path@example.org",
            "email_verified": True,
            "hd": "example.org",
            "name": "Dr Path",
            "iat": now,
            "exp": now + 3600,
        }
        claims.update(overrides)
        claims = {k: v for k, v in claims.items() if v is not None}
        return jwt.encode(claims, signing_key or self._key, algorithm="RS256", headers={"kid": self.kid})

    @staticmethod
    def other_key():
        return rsa.generate_private_key(public_exponent=65537, key_size=2048)


def add_user(db: Session, email: str, role: str = "pathologist", status: str = "invited", **fields) -> User:
    user = User(email=email, role=role, status=status, created_at=datetime.now(timezone.utc), **fields)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user
