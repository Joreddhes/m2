import subprocess
from pathlib import Path

from mediahub import media as media_module
from mediahub.extensions import db
from mediahub.models import MediaFile, MediaObject, Source


def _video(app, tmp_path: Path, source_kind: str = "local") -> tuple[str, int]:
    root = tmp_path / "media"
    video_path = root / "video" / "Series" / "Season 1" / "Episode 1.mp4"
    if source_kind == "local":
        video_path.parent.mkdir(parents=True)
        video_path.write_bytes(b"fake video")
    with app.app_context():
        source = Source(
            name="Test source", kind=source_kind, root=str(root), host="example.invalid"
        )
        item = MediaObject(
            source=source, media_type="video", relative_path="video/Series", title="Series"
        )
        media = MediaFile(
            media_object=item,
            relative_path="video/Series/Season 1/Episode 1.mp4",
            name="Episode 1.mp4",
            kind="video",
            size=10,
        )
        db.session.add_all([source, item, media])
        db.session.commit()
        return item.id, media.id


def test_video_directory_uses_tiles_and_player_keeps_sidebar(app, client, tmp_path):
    object_id, file_id = _video(app, tmp_path)
    directory = client.get(f"/content/{object_id}")
    assert directory.status_code == 200
    assert 'class="video-grid"' in directory.text
    assert f'data-video-thumbnail="/media/{file_id}/thumbnail"' in directory.text
    assert f'href="/play/{file_id}"' in directory.text
    assert 'class="episode-list"' not in directory.text
    assert 'class="playlist"' in client.get(f"/play/{file_id}").text


def test_video_thumbnail_uses_one_frame_and_reuses_cache(app, client, tmp_path, monkeypatch):
    _, file_id = _video(app, tmp_path)
    commands = []
    run_options = []

    def fake_run(command, **kwargs):
        commands.append(command)
        run_options.append(kwargs)
        if len(commands) == 1:
            raise subprocess.CalledProcessError(1, command)
        Path(command[-1]).write_bytes(b"jpeg")

    monkeypatch.setattr(media_module.shutil, "which", lambda _: "ffmpeg")
    monkeypatch.setattr(media_module.subprocess, "run", fake_run)
    first = client.get(f"/media/{file_id}/thumbnail")
    second = client.get(f"/media/{file_id}/thumbnail")
    assert first.status_code == second.status_code == 200
    assert first.data == second.data == b"jpeg"
    assert len(commands) == 2
    assert [command[command.index("-ss") + 1] for command in commands] == ["10", "0"]
    assert all(command[command.index("-frames:v") + 1] == "1" for command in commands)
    assert all(command[command.index("-threads") + 1] == "1" for command in commands)
    assert all(options["timeout"] == 8 for options in run_options)


def test_busy_thumbnail_generator_asks_browser_to_retry(app, client, tmp_path, monkeypatch):
    _, file_id = _video(app, tmp_path)
    monkeypatch.setattr(media_module.shutil, "which", lambda _: "ffmpeg")
    media_module._thumbnail_lock.acquire()
    try:
        response = client.get(f"/media/{file_id}/thumbnail")
    finally:
        media_module._thumbnail_lock.release()
    assert response.status_code == 202
    assert response.headers["Retry-After"] == "2"


def test_failed_thumbnail_is_not_retried_on_every_page_view(app, client, tmp_path, monkeypatch):
    _, file_id = _video(app, tmp_path)
    calls = []

    def fail(command, **kwargs):
        calls.append(command)
        raise subprocess.TimeoutExpired(command, kwargs["timeout"])

    monkeypatch.setattr(media_module.shutil, "which", lambda _: "ffmpeg")
    monkeypatch.setattr(media_module.subprocess, "run", fail)
    assert client.get(f"/media/{file_id}/thumbnail").status_code == 404
    assert client.get(f"/media/{file_id}/thumbnail").status_code == 404
    assert len(calls) == 2


def test_uncached_ftp_video_does_not_download_for_thumbnail(app, client, tmp_path, monkeypatch):
    _, file_id = _video(app, tmp_path, "ftp")
    monkeypatch.setattr(media_module.shutil, "which", lambda _: "ffmpeg")

    class NoDownload:
        def local_file(self, relative):
            return None

        def download(self, relative, target):
            raise AssertionError("thumbnail must not download a whole FTP video")

    monkeypatch.setattr(media_module, "backend_for", lambda source: NoDownload())
    assert client.get(f"/media/{file_id}/thumbnail").status_code == 404
