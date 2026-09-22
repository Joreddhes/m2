import json
from pathlib import Path

from mediahub.extensions import db
from mediahub.media import (
    _audio_label,
    _finalize_hls_playlists,
    _hls_is_ready,
    _media_duration,
    _playlist_duration,
    _refresh_cached_result,
    _write_master_playlist,
)
from mediahub.models import Task


def test_media_duration_prefers_container_and_ignores_invalid_values():
    assert (
        _media_duration(
            {
                "format": {"duration": "3701.25"},
                "streams": [{"codec_type": "video", "duration": "3700"}],
            }
        )
        == 3701.25
    )
    assert (
        _media_duration(
            {
                "format": {"duration": "N/A"},
                "streams": [{"codec_type": "video", "duration": "52.5"}],
            }
        )
        == 52.5
    )
    assert _media_duration({"format": {"duration": "nan"}, "streams": []}) is None


def test_undefined_audio_language_uses_default_label():
    assert _audio_label({"language": "und"}, 0) == "default"
    assert _audio_label({"title": "Studio", "language": "und"}, 0) == "Studio"


def test_existing_cache_relabels_undefined_audio_without_reencoding(tmp_path: Path):
    result_path = tmp_path / "result.json"
    master_path = tmp_path / "index.m3u8"
    master_path.write_text('NAME="und"\nNAME="und (2)"\nNAME="Original"\n', encoding="utf-8")
    (tmp_path / "video.m3u8").write_text(
        "#EXTM3U\n#EXT-X-PLAYLIST-TYPE:VOD\n#EXTINF:6.0,\nsegment-0.ts\n#EXTINF:4.25,\nsegment-1.ts\n#EXT-X-ENDLIST\n",
        encoding="utf-8",
    )
    result = {"audio_tracks": [{"label": "und"}, {"label": "und (2)"}, {"label": "Original"}]}

    updated = _refresh_cached_result(result_path, result)

    assert [track["label"] for track in updated["audio_tracks"]] == [
        "default",
        "default (2)",
        "Original",
    ]
    assert (
        master_path.read_text(encoding="utf-8")
        == 'NAME="default"\nNAME="default (2)"\nNAME="Original"\n'
    )
    assert '"default"' in result_path.read_text(encoding="utf-8")
    assert updated["duration"] == 10.25


def test_multi_audio_master_playlist_and_progressive_readiness(tmp_path: Path):
    tracks = [
        {"label": 'Russian "Studio"', "language": "rus"},
        {"label": "Original", "language": "eng"},
    ]
    _write_master_playlist(tmp_path, {"width": 1280, "height": 720}, tracks)

    master = (tmp_path / "index.m3u8").read_text(encoding="utf-8")
    assert master.count("#EXT-X-MEDIA:TYPE=AUDIO") == 2
    assert master.count('CHANNELS="2"') == 2
    assert 'RESOLUTION=1280x720,AUDIO="audio"' in master
    assert "NAME=\"Russian 'Studio'\"" in master
    assert not _hls_is_ready(tmp_path, 2)

    playlist = "#EXTM3U\n#EXT-X-PLAYLIST-TYPE:EVENT\n#EXTINF:6.0,\nsegment.ts\n"
    (tmp_path / "video.m3u8").write_text(playlist, encoding="utf-8")
    assert _playlist_duration(tmp_path / "video.m3u8") == 6.0
    (tmp_path / "audio-0.m3u8").write_text(playlist, encoding="utf-8")
    assert not _hls_is_ready(tmp_path, 2)
    (tmp_path / "audio-1.m3u8").write_text(playlist, encoding="utf-8")
    assert _hls_is_ready(tmp_path, 2)

    _finalize_hls_playlists(tmp_path)
    assert "PLAYLIST-TYPE:VOD" in (tmp_path / "video.m3u8").read_text(encoding="utf-8")
    assert "PLAYLIST-TYPE:EVENT" not in (tmp_path / "audio-1.m3u8").read_text(encoding="utf-8")


def test_task_status_reports_created_segments_not_total_length(app, client):
    key = "a" * 24
    playlist = Path(app.config["CACHE_DIR"]) / "hls" / key / "video.m3u8"
    playlist.parent.mkdir(parents=True)
    playlist.write_text(
        "#EXTM3U\n#EXT-X-PLAYLIST-TYPE:EVENT\n"
        "#EXTINF:6.0,\nvideo-00000.ts\n#EXTINF:4.25,\nvideo-00001.ts\n",
        encoding="utf-8",
    )
    with app.app_context():
        task = Task(
            kind="transcode", status="ready", result_json=json.dumps({"key": key, "duration": 100})
        )
        db.session.add(task)
        db.session.commit()
        task_id = task.id

    response = client.get(f"/api/tasks/{task_id}")
    assert response.status_code == 200
    assert response.json["duration"] == 100
    assert response.json["prepared_until"] == 10.25

    with app.app_context():
        task = db.session.get(Task, task_id)
        task.status = "done"
        db.session.commit()
    assert client.get(f"/api/tasks/{task_id}").json["prepared_until"] == 100
