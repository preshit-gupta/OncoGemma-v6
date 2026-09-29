import pytest
from fastapi.testclient import TestClient
from google.oauth2 import id_token

from app.core.db import Base, SessionLocal, engine, get_db
from app.main import app
from tests.auth.helpers import FakeGoogle


@pytest.fixture
def db():
    Base.metadata.drop_all(bind=engine)
    Base.metadata.create_all(bind=engine)
    session = SessionLocal()
    yield session
    session.close()
    Base.metadata.drop_all(bind=engine)


@pytest.fixture
def client(db):
    def override_get_db():
        session = SessionLocal()
        try:
            yield session
        finally:
            session.close()

    app.dependency_overrides[get_db] = override_get_db
    # Session cookies are Secure, so the client must speak https to send them back.
    with TestClient(app, base_url="https://testserver") as test_client:
        yield test_client
    app.dependency_overrides.pop(get_db, None)


@pytest.fixture
def google(monkeypatch):
    fake = FakeGoogle()
    monkeypatch.setattr(id_token, "_fetch_certs", fake.certs)
    return fake
