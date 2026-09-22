import json
from pathlib import Path

from mediahub import jobs
from mediahub.extensions import db
from mediahub.models import Source, Task


def test_dashboard_shows_task_context_times_progress_and_collapsed_error(app, admin_client):
    error = "FFmpeg failed\n" + "unreadable video stream " * 20
    with app.app_context():
        task = Task(
            kind="transcode",
            status="error",
            progress=30,
            message=error,
            result_json=json.dumps(
                {"subject": "Episode 7.mkv", "object_title": "Series", "file_id": 37}
            ),
        )
        db.session.add(task)
        db.session.commit()

    response = admin_client.get("/admin/")
    assert response.status_code == 200
    assert "Episode 7.mkv" in response.text
    assert "Series" in response.text
    assert 'href="/play/37"' in response.text
    assert 'class="task-time" datetime="' in response.text
    assert '<progress max="100" value="30"' in response.text
    assert "<details>" in response.text
    assert "Полное сообщение" in response.text
    assert error in response.text


def test_scan_keeps_source_name_when_finished(app, tmp_path: Path, monkeypatch):
    root = tmp_path / "library"
    (root / "video").mkdir(parents=True)
    (root / "photo").mkdir()
    with app.app_context():
        source = Source(name="Family NAS", kind="local", root=str(root))
        db.session.add(source)
        db.session.commit()
        monkeypatch.setattr(jobs._executor, "submit", lambda *args: None)
        task = jobs.queue_scan(source)
        assert task.context["subject"] == "Family NAS"
        jobs._scan_worker(app, source.id, task.id)
        db.session.refresh(task)
        assert task.status == "done"
        assert task.context["subject"] == "Family NAS"
        assert task.context["objects"] == 0


def test_task_context_handles_legacy_or_invalid_data():
    assert Task(result_json=None).context == {}
    assert Task(result_json="broken").context == {}
