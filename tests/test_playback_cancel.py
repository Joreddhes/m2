import time
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Event, Thread

import pytest

from mediahub import media as playback
from mediahub.extensions import db
from mediahub.models import MediaFile, MediaObject, Source, Task


@pytest.fixture()
def file_id(app, tmp_path: Path):
    (tmp_path / "episode.mkv").write_bytes(b"video")
    with app.app_context():
        source = Source(name="Local", kind="local", root=str(tmp_path))
        item = MediaObject(
            source=source, media_type="video", relative_path="episode.mkv", title="Test"
        )
        media = MediaFile(
            media_object=item, relative_path="episode.mkv", name="episode.mkv", kind="video"
        )
        db.session.add_all([source, item, media])
        db.session.commit()
        return media.id


@pytest.fixture()
def clean_playback_state():
    with playback._active_lock:
        playback._active.clear()
        playback._viewers.clear()
        playback._cancelled_viewers.clear()
    yield
    with playback._active_lock:
        playback._active.clear()
        playback._viewers.clear()
        playback._cancelled_viewers.clear()


def test_cancel_before_prepare_blocks_late_request(
    app, client, file_id, clean_playback_state, monkeypatch
):
    monkeypatch.setattr(playback.shutil, "which", lambda _: "ffmpeg")
    viewer_id = "page:1"
    response = client.post("/api/playback/cancel", json={"viewer_id": viewer_id})
    assert response.status_code == 200
    assert response.json["stopped"] is True

    response = client.post(f"/api/media/{file_id}/prepare", json={"viewer_id": viewer_id})
    assert response.status_code == 409
    with app.app_context():
        assert Task.query.count() == 0


def test_cancel_preserves_transcode_for_another_viewer(
    app, client, file_id, clean_playback_state, monkeypatch
):
    class DeferredExecutor:
        def submit(self, *args):
            pass

    monkeypatch.setattr(playback.shutil, "which", lambda _: "ffmpeg")
    monkeypatch.setattr(playback, "_executor", DeferredExecutor())
    first = client.post(f"/api/media/{file_id}/prepare", json={"viewer_id": "page-a:1"})
    second = client.post(f"/api/media/{file_id}/prepare", json={"viewer_id": "page-b:1"})
    assert first.status_code == second.status_code == 202
    assert first.json["task_id"] == second.json["task_id"]
    job = next(iter(playback._active.values()))

    client.post("/api/playback/cancel", json={"viewer_id": "page-a:1"})
    assert not job.cancel.is_set()
    assert client.post(
        "/api/playback/heartbeat", json={"viewer_id": "page-b:1", "task_id": job.task_id}
    ).json["active"]

    job.finished.set()  # No worker is running in this test.
    client.post("/api/playback/cancel", json={"viewer_id": "page-b:1"})
    assert job.cancel.is_set()


@pytest.mark.parametrize("with_subtitle", [False, True])
def test_cancel_stops_running_ffmpeg_and_discards_partial_cache(
    app,
    client,
    file_id,
    clean_playback_state,
    monkeypatch,
    tmp_path: Path,
    with_subtitle,
):
    started = Event()
    processes = []

    class FakeProcess:
        returncode = None

        def __init__(self, *args, **kwargs):
            self.terminated = False
            processes.append(self)
            started.set()

        def poll(self):
            return self.returncode

        def terminate(self):
            self.terminated = True
            self.returncode = -15

        def wait(self, timeout=None):
            return self.returncode

        def kill(self):
            self.returncode = -9

    monkeypatch.setattr(playback.shutil, "which", lambda _: "ffmpeg")
    streams = [
        {"codec_type": "video", "codec_name": "h264"},
        {"codec_type": "audio", "index": 1, "tags": {"language": "und"}},
    ]
    if with_subtitle:
        streams.append({"codec_type": "subtitle", "codec_name": "subrip", "index": 2})
    monkeypatch.setattr(playback, "_probe", lambda path, ffprobe: {"streams": streams})
    monkeypatch.setattr(playback.subprocess, "Popen", FakeProcess)
    executor = ThreadPoolExecutor(max_workers=1)
    monkeypatch.setattr(playback, "_executor", executor)
    try:
        response = client.post(f"/api/media/{file_id}/prepare", json={"viewer_id": "page:1"})
        assert response.status_code == 202
        assert started.wait(timeout=3)

        cancelled = client.post("/api/playback/cancel", json={"viewer_id": "page:1"})
        assert cancelled.status_code == 200
        assert cancelled.json["stopped"] is True
        assert processes[0].terminated
        with app.app_context():
            task = db.session.get(Task, response.json["task_id"])
            assert task.status == "cancelled"
            assert task.context["subject"] == "episode.mkv"
        assert not list((tmp_path / "cache" / "hls").glob("*/result.json"))
    finally:
        executor.shutdown(wait=True)


def test_inactive_viewer_times_out(clean_playback_state):
    job = playback.ActiveTranscode(task_id="task", viewers={"page:1": time.monotonic() - 100})
    playback._viewers["page:1"] = job
    with pytest.raises(playback.TranscodeCancelled):
        playback._check_transcode_viewers(job)
    assert job.cancel.is_set()
    assert "page:1" not in playback._viewers


def test_replacement_waits_for_previous_job_before_touching_same_cache(
    app, file_id, clean_playback_state
):
    with app.app_context():
        task = Task(kind="transcode", message="episode")
        db.session.add(task)
        db.session.commit()
        task_id = task.id
    previous = playback.ActiveTranscode(task_id="previous")
    replacement = playback.ActiveTranscode(task_id=task_id)
    replacement.cancel.set()
    worker = Thread(
        target=playback._prepare_worker, args=(app, file_id, replacement, 0, None, previous)
    )
    worker.start()
    try:
        assert not replacement.started.wait(timeout=0.1)
        previous.finished.set()
        assert replacement.finished.wait(timeout=2)
        with app.app_context():
            assert db.session.get(Task, task_id).status == "cancelled"
    finally:
        previous.finished.set()
        worker.join(timeout=2)
