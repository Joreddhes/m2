from pathlib import Path

from mediahub.extensions import db
from mediahub.models import Source
from mediahub.scanner import scan_source


def _library(app, tmp_path: Path):
    root = tmp_path / "media"
    folder = root / "video" / "Пространство"
    folder.mkdir(parents=True)
    (folder / "Серия 1.mp4").write_bytes(b"video")
    with app.app_context():
        source = Source(name="NAS", kind="local", root=str(root))
        db.session.add(source)
        db.session.commit()
        scan_source(source)


def test_catalog_and_cyrillic_search(app, client, tmp_path):
    _library(app, tmp_path)
    assert client.get("/").status_code == 200
    response = client.get("/library/video?q=Прост")
    assert response.status_code == 200
    assert "Пространство" in response.text


def test_admin_requires_login(client):
    response = client.get("/admin/", follow_redirects=False)
    assert response.status_code == 302
    assert "/login" in response.headers["Location"]


def test_health_endpoint(client):
    response = client.get("/healthz")
    assert response.status_code == 200
    assert response.json == {"status": "ok"}


def test_first_run_redirects_to_setup_and_creates_admin(app, client):
    from mediahub.models import Admin

    with app.app_context():
        Admin.query.delete()
        db.session.commit()
    response = client.get("/", follow_redirects=False)
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/setup")
    response = client.post(
        "/setup",
        data={
            "password": "a strong local password",
            "confirmation": "a strong local password",
        },
    )
    assert response.status_code == 302
    with app.app_context():
        assert Admin.query.count() == 1


def test_admin_can_edit_metadata(app, admin_client, tmp_path):
    _library(app, tmp_path)
    with app.app_context():
        from mediahub.models import MediaObject

        object_id = MediaObject.query.one().id
    response = admin_client.post(
        f"/admin/content/{object_id}",
        data={"title": "Новое название", "category": "Сериалы", "tags": "космос, фантастика"},
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Метаданные сохранены" in response.text


def test_resumable_unicode_upload(app, admin_client, tmp_path):
    _library(app, tmp_path)
    with app.app_context():
        from mediahub.models import MediaObject

        item = MediaObject.query.one()
        object_id = item.id
        root = Path(item.source.root)
    content = b"a video split into chunks"
    response = admin_client.post(
        "/admin/api/uploads",
        json={
            "object_id": object_id,
            "destination": "Сезон 2",
            "name": "Серия 02.mkv",
            "size": len(content),
        },
    )
    assert response.status_code == 201
    upload_id = response.json["id"]
    first = admin_client.patch(
        f"/admin/api/uploads/{upload_id}",
        data=content[:7],
        headers={"Upload-Offset": "0", "Content-Type": "application/octet-stream"},
    )
    assert first.json == {"done": False, "offset": 7}
    second = admin_client.patch(
        f"/admin/api/uploads/{upload_id}",
        data=content[7:],
        headers={"Upload-Offset": "7", "Content-Type": "application/octet-stream"},
    )
    assert second.json["done"] is True
    assert (root / "video" / "Пространство" / "Сезон 2" / "Серия 02.mkv").read_bytes() == content
