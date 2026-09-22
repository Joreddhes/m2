from pathlib import Path

import pytest

from mediahub import create_app
from mediahub.extensions import db
from mediahub.models import Admin


@pytest.fixture()
def app(tmp_path: Path):
    application = create_app(
        {
            "TESTING": True,
            "WTF_CSRF_ENABLED": False,
            "SECRET_KEY": "test-secret",
            "SQLALCHEMY_DATABASE_URI": f"sqlite:///{tmp_path / 'test.db'}",
            "CACHE_DIR": str(tmp_path / "cache"),
            "UPLOAD_DIR": str(tmp_path / "uploads"),
        }
    )
    with application.app_context():
        admin = Admin()
        admin.set_password("correct horse battery staple")
        db.session.add(admin)
        db.session.commit()
    yield application


@pytest.fixture()
def client(app):
    return app.test_client()


@pytest.fixture()
def admin_client(client):
    response = client.post("/login", data={"password": "correct horse battery staple"})
    assert response.status_code == 302
    return client
