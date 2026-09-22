from __future__ import annotations

import json
from datetime import datetime, timezone
from uuid import uuid4

from flask_login import UserMixin
from werkzeug.security import check_password_hash, generate_password_hash

from .extensions import db, login_manager


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


class Admin(UserMixin, db.Model):
    id: db.Mapped[int] = db.mapped_column(primary_key=True, default=1)
    password_hash: db.Mapped[str] = db.mapped_column(db.String(255))

    def set_password(self, value: str) -> None:
        self.password_hash = generate_password_hash(value, method="scrypt")

    def check_password(self, value: str) -> bool:
        return check_password_hash(self.password_hash, value)


@login_manager.user_loader
def load_user(user_id: str) -> Admin | None:
    return db.session.get(Admin, int(user_id))


class Source(db.Model):
    id: db.Mapped[int] = db.mapped_column(primary_key=True)
    name: db.Mapped[str] = db.mapped_column(db.String(120), unique=True)
    kind: db.Mapped[str] = db.mapped_column(db.String(16))
    root: db.Mapped[str] = db.mapped_column(db.String(1024))
    host: db.Mapped[str | None] = db.mapped_column(db.String(255))
    port: db.Mapped[int | None]
    username: db.Mapped[str | None] = db.mapped_column(db.String(255))
    encrypted_password: db.Mapped[str | None] = db.mapped_column(db.Text)
    use_tls: db.Mapped[bool] = db.mapped_column(default=False)
    enabled: db.Mapped[bool] = db.mapped_column(default=True)
    status: db.Mapped[str] = db.mapped_column(db.String(32), default="new")
    last_error: db.Mapped[str | None] = db.mapped_column(db.Text)
    last_scanned_at: db.Mapped[datetime | None]

    objects: db.Mapped[list["MediaObject"]] = db.relationship(
        back_populates="source", cascade="all, delete-orphan"
    )


class MediaObject(db.Model):
    id: db.Mapped[str] = db.mapped_column(
        db.String(36), primary_key=True, default=lambda: str(uuid4())
    )
    source_id: db.Mapped[int] = db.mapped_column(db.ForeignKey("source.id"), index=True)
    media_type: db.Mapped[str] = db.mapped_column(db.String(16), index=True)
    relative_path: db.Mapped[str] = db.mapped_column(db.String(1024))
    title: db.Mapped[str] = db.mapped_column(db.String(255), index=True)
    category: db.Mapped[str] = db.mapped_column(db.String(120), default="", index=True)
    tags_json: db.Mapped[str] = db.mapped_column(db.Text, default="[]")
    is_loose: db.Mapped[bool] = db.mapped_column(default=False)
    updated_at: db.Mapped[datetime] = db.mapped_column(default=utcnow, onupdate=utcnow)

    source: db.Mapped[Source] = db.relationship(back_populates="objects")
    files: db.Mapped[list["MediaFile"]] = db.relationship(
        back_populates="media_object", cascade="all, delete-orphan"
    )

    __table_args__ = (db.UniqueConstraint("source_id", "relative_path", "media_type"),)

    @property
    def tags(self) -> list[str]:
        try:
            return json.loads(self.tags_json)
        except (TypeError, json.JSONDecodeError):
            return []

    @tags.setter
    def tags(self, value: list[str]) -> None:
        self.tags_json = json.dumps(value, ensure_ascii=False)

    @property
    def cover(self) -> "MediaFile | None":
        return next((item for item in self.files if item.kind == "image"), None)


class MediaFile(db.Model):
    id: db.Mapped[int] = db.mapped_column(primary_key=True)
    object_id: db.Mapped[str] = db.mapped_column(db.ForeignKey("media_object.id"), index=True)
    relative_path: db.Mapped[str] = db.mapped_column(db.String(1200))
    name: db.Mapped[str] = db.mapped_column(db.String(255))
    kind: db.Mapped[str] = db.mapped_column(db.String(16), index=True)
    size: db.Mapped[int] = db.mapped_column(default=0)
    modified: db.Mapped[float] = db.mapped_column(default=0)

    media_object: db.Mapped[MediaObject] = db.relationship(back_populates="files")

    __table_args__ = (db.UniqueConstraint("object_id", "relative_path"),)


class Task(db.Model):
    id: db.Mapped[str] = db.mapped_column(
        db.String(36), primary_key=True, default=lambda: str(uuid4())
    )
    kind: db.Mapped[str] = db.mapped_column(db.String(32))
    status: db.Mapped[str] = db.mapped_column(db.String(24), default="queued", index=True)
    progress: db.Mapped[int] = db.mapped_column(default=0)
    message: db.Mapped[str] = db.mapped_column(db.Text, default="")
    result_json: db.Mapped[str | None] = db.mapped_column(db.Text)
    created_at: db.Mapped[datetime] = db.mapped_column(default=utcnow)
    updated_at: db.Mapped[datetime] = db.mapped_column(default=utcnow, onupdate=utcnow)

    @property
    def context(self) -> dict:
        try:
            value = json.loads(self.result_json or "{}")
        except (TypeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}


class UploadSession(db.Model):
    id: db.Mapped[str] = db.mapped_column(
        db.String(36), primary_key=True, default=lambda: str(uuid4())
    )
    object_id: db.Mapped[str] = db.mapped_column(db.ForeignKey("media_object.id"), index=True)
    relative_path: db.Mapped[str] = db.mapped_column(db.String(1400))
    total_size: db.Mapped[int]
    offset: db.Mapped[int] = db.mapped_column(default=0)
    temp_name: db.Mapped[str] = db.mapped_column(db.String(64), unique=True)
    created_at: db.Mapped[datetime] = db.mapped_column(default=utcnow)
