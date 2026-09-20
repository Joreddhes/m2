import json
from pathlib import Path

from mediahub.extensions import db
from mediahub.models import MediaObject, Source
from mediahub.scanner import natural_key, scan_source


def test_natural_sort_order():
    names = ["Серия 10", "Серия 2", "Серия 1"]
    assert sorted(names, key=natural_key) == ["Серия 1", "Серия 2", "Серия 10"]


def test_scan_creates_sidecar_and_indexes_files(app, tmp_path: Path):
    library = tmp_path / "library"
    episode = library / "video" / "Пространство" / "Сезон 1" / "Серия 01.mkv"
    episode.parent.mkdir(parents=True)
    episode.write_bytes(b"not-real-video")

    with app.app_context():
        source = Source(name="Локальный", kind="local", root=str(library))
        db.session.add(source)
        db.session.commit()
        assert scan_source(source) == 1

        item = MediaObject.query.one()
        assert item.title == "Пространство"
        assert item.files[0].relative_path.endswith("Серия 01.mkv")
        payload = json.loads((library / "video" / "Пространство" / ".mediahub.json").read_text("utf-8"))
        assert payload["id"] == item.id
        assert payload["type"] == "video"


def test_scan_groups_loose_files(app, tmp_path: Path):
    library = tmp_path / "library"
    (library / "video").mkdir(parents=True)
    (library / "video" / "movie.mp4").write_bytes(b"movie")

    with app.app_context():
        source = Source(name="Локальный", kind="local", root=str(library))
        db.session.add(source)
        db.session.commit()
        scan_source(source)
        item = MediaObject.query.one()
        assert item.title == "Без категории"
        assert item.is_loose is True
        assert (library / "video" / ".mediahub.loose.json").exists()


def test_scan_imports_media_directly_from_source_root(app, tmp_path: Path):
    library = tmp_path / "7 Season"
    library.mkdir()
    (library / "01. Episode.mkv").write_bytes(b"episode")

    with app.app_context():
        source = Source(name="7 Season", kind="local", root=str(library))
        db.session.add(source)
        db.session.commit()
        assert scan_source(source) == 1
        item = MediaObject.query.one()
        assert item.title == "7 Season"
        assert item.relative_path == ""
        assert [media.name for media in item.files] == ["01. Episode.mkv"]
        assert (library / ".mediahub.video.json").exists()


def test_scan_detects_existing_top_level_media_folder(app, tmp_path: Path):
    library = tmp_path / "library"
    season = library / "7 Season"
    season.mkdir(parents=True)
    (season / "01.mkv").write_bytes(b"episode")
    (library / "Documents").mkdir()
    (library / "Documents" / "readme.pdf").write_bytes(b"document")

    with app.app_context():
        source = Source(name="Library", kind="local", root=str(library))
        db.session.add(source)
        db.session.commit()
        assert scan_source(source) == 1
        item = MediaObject.query.one()
        assert item.title == "7 Season"
        assert item.relative_path == "7 Season"
