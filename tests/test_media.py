from pathlib import Path

from mediahub.extensions import db
from mediahub.models import MediaFile, MediaObject, Source


def test_prepare_reports_missing_ffmpeg(app, client, tmp_path: Path):
    with app.app_context():
        source = Source(name="Local", kind="local", root=str(tmp_path))
        item = MediaObject(
            source=source, media_type="video", relative_path="video/Test", title="Test"
        )
        media = MediaFile(
            media_object=item,
            relative_path="video/Test/episode.mkv",
            name="episode.mkv",
            kind="video",
        )
        db.session.add_all([source, item, media])
        db.session.commit()
        file_id = media.id
        app.config["FFMPEG_PATH"] = "missing-ffmpeg-for-test"
        app.config["FFPROBE_PATH"] = "missing-ffprobe-for-test"

    response = client.post(f"/api/media/{file_id}/prepare")
    assert response.status_code == 503
    assert "FFmpeg не найден" in response.json["error"]
