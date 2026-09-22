from __future__ import annotations

import json
from concurrent.futures import ThreadPoolExecutor
from threading import Lock

from flask import current_app

from .extensions import db
from .models import Source, Task
from .scanner import scan_source

_executor = ThreadPoolExecutor(max_workers=2, thread_name_prefix="mediahub-scan")
_active_sources: set[int] = set()
_lock = Lock()


def _scan_worker(app, source_id: int, task_id: str) -> None:
    with app.app_context():
        task = db.session.get(Task, task_id)
        source = db.session.get(Source, source_id)
        try:
            task.status = "running"
            task.progress = 10
            task.message = f"Сканирование «{source.name}»"
            source.status = "scanning"
            db.session.commit()
            count = scan_source(source)
            task = db.session.get(Task, task_id)
            task.status = "done"
            task.progress = 100
            task.message = f"Найдено объектов: {count}"
            task.result_json = json.dumps(
                {**task.context, "source_id": source_id, "objects": count}, ensure_ascii=False
            )
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            task = db.session.get(Task, task_id)
            task.status = "error"
            task.message = str(exc)
            db.session.commit()
        finally:
            with _lock:
                _active_sources.discard(source_id)


def queue_scan(source: Source) -> Task | None:
    with _lock:
        if source.id in _active_sources:
            return None
        _active_sources.add(source.id)
    task = Task(
        kind="scan",
        message="Ожидает сканирования",
        result_json=json.dumps(
            {"subject": source.name, "source_id": source.id}, ensure_ascii=False
        ),
    )
    db.session.add(task)
    source.status = "queued"
    db.session.commit()
    _executor.submit(_scan_worker, current_app._get_current_object(), source.id, task.id)
    return task
