from __future__ import annotations

from .extensions import db
from .models import Task


def initialize_database() -> None:
    db.create_all()
    db.session.execute(
        db.text(
            "CREATE VIRTUAL TABLE IF NOT EXISTS media_search USING fts5("
            "object_id UNINDEXED, title, category, tags, tokenize='unicode61')"
        )
    )
    db.session.commit()

    interrupted = Task.query.filter(Task.status.in_(["queued", "running"])).all()
    for task in interrupted:
        task.status = "error"
        task.message = "Задача была прервана перезапуском сервера. Запустите её повторно."
    if interrupted:
        db.session.commit()
