from __future__ import annotations

import json
import re
from datetime import datetime, timezone
from pathlib import PurePosixPath
from uuid import uuid4

from .extensions import db
from .models import MediaFile, MediaObject, Source
from .storage import StorageBackend, backend_for

SIDECAR = ".mediahub.json"
LOOSE_SIDECAR = ".mediahub.loose.json"
VIDEO_EXTENSIONS = {".mp4", ".m4v", ".mkv", ".avi", ".mov", ".webm", ".mpeg", ".mpg", ".ts", ".mts"}
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".gif", ".webp", ".bmp", ".tif", ".tiff", ".heic", ".heif"}
SUBTITLE_EXTENSIONS = {".srt", ".ass", ".ssa", ".vtt"}


def natural_key(value: str) -> list[object]:
    return [int(part) if part.isdigit() else part.casefold() for part in re.split(r"(\d+)", value)]


def classify(name: str) -> str | None:
    suffix = PurePosixPath(name).suffix.casefold()
    if suffix in VIDEO_EXTENSIONS:
        return "video"
    if suffix in IMAGE_EXTENSIONS:
        return "image"
    if suffix in SUBTITLE_EXTENSIONS:
        return "subtitle"
    return None


def _sidecar_payload(media_type: str, title: str, object_id: str | None = None) -> dict:
    return {
        "schema_version": 1,
        "id": object_id or str(uuid4()),
        "type": media_type,
        "title": title,
        "category": "",
        "tags": [],
    }


def _load_or_create_sidecar(
    backend: StorageBackend, sidecar_path: str, media_type: str, title: str
) -> dict:
    if backend.exists(sidecar_path):
        try:
            data = json.loads(backend.read_bytes(sidecar_path).decode("utf-8-sig"))
            if data.get("schema_version") == 1 and data.get("id"):
                return data
        except (UnicodeError, json.JSONDecodeError, OSError):
            pass
    data = _sidecar_payload(media_type, title)
    backend.write_bytes(sidecar_path, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
    return data


def _join_path(root: str, name: str) -> str:
    return f"{root.rstrip('/')}/{name}".strip("/")


def _sidecar_path(root: str, media_type: str, loose: bool) -> str:
    if loose and not root:
        return f".mediahub.{media_type}.json"
    return _join_path(root, LOOSE_SIDECAR if loose else SIDECAR)


def _upsert_object(
    source: Source, backend: StorageBackend, media_type: str, root: str, title: str, loose: bool,
    force_new_id: bool = False,
) -> MediaObject:
    sidecar = _sidecar_path(root, media_type, loose)
    data = _load_or_create_sidecar(backend, sidecar, media_type, title)
    object_id = str(data["id"])
    item = db.session.get(MediaObject, object_id)
    if force_new_id or (item is not None and item.source_id != source.id):
        data["id"] = object_id = str(uuid4())
        backend.write_bytes(sidecar, json.dumps(data, ensure_ascii=False, indent=2).encode("utf-8"))
        item = None
    if item is None:
        item = MediaObject(id=object_id, source_id=source.id, media_type=media_type, relative_path=root)
        db.session.add(item)
    item.title = str(data.get("title") or title)
    item.category = str(data.get("category") or "")
    item.tags = sorted({str(tag).strip() for tag in data.get("tags", []) if str(tag).strip()})
    item.relative_path = root
    item.media_type = media_type
    item.is_loose = loose
    return item


def _index_files(item: MediaObject, backend: StorageBackend, loose_only: bool = False) -> None:
    known = {media.relative_path: media for media in item.files}
    seen: set[str] = set()
    entries = backend.list(item.relative_path) if loose_only else backend.walk_files(item.relative_path)
    for entry in entries:
        if entry.is_dir or entry.name.startswith(".mediahub"):
            continue
        if loose_only and "/" in entry.path[len(item.relative_path):].strip("/"):
            continue
        kind = classify(entry.name)
        allowed = {"video", "subtitle"} if item.media_type == "video" else {"image"}
        if kind not in allowed:
            continue
        seen.add(entry.path)
        media = known.get(entry.path)
        if media is None:
            media = MediaFile(object_id=item.id, relative_path=entry.path)
            db.session.add(media)
        media.name = entry.name
        media.kind = kind
        media.size = entry.size
        media.modified = entry.modified
    for path, media in known.items():
        if path not in seen:
            db.session.delete(media)


def _refresh_search(item: MediaObject) -> None:
    db.session.execute(db.text("DELETE FROM media_search WHERE object_id = :id"), {"id": item.id})
    db.session.execute(
        db.text("INSERT INTO media_search(object_id, title, category, tags) VALUES (:id, :title, :category, :tags)"),
        {"id": item.id, "title": item.title, "category": item.category, "tags": " ".join(item.tags)},
    )


def _detect_tree_type(backend: StorageBackend, root: str) -> str | None:
    has_image = False
    for child in backend.walk_files(root):
        kind = classify(child.name)
        if kind == "video":
            return "video"
        if kind == "image":
            has_image = True
    return "photo" if has_image else None


def scan_source(source: Source) -> int:
    backend = backend_for(source)
    found: set[str] = set()
    count = 0
    try:
        for media_type in ("video", "photo"):
            backend.mkdir(media_type)
            entries = sorted(backend.list(media_type), key=lambda entry: natural_key(entry.name))
            loose_files = [entry for entry in entries if not entry.is_dir and classify(entry.name)]
            if loose_files:
                item = _upsert_object(source, backend, media_type, media_type, "Без категории", True)
                if item.id in found:
                    item = _upsert_object(source, backend, media_type, media_type, "Без категории", True, True)
                db.session.flush()
                _index_files(item, backend, loose_only=True)
                _refresh_search(item)
                found.add(item.id)
                count += 1
            for entry in entries:
                if not entry.is_dir:
                    continue
                item = _upsert_object(source, backend, media_type, entry.path, entry.name, False)
                if item.id in found:
                    item = _upsert_object(source, backend, media_type, entry.path, entry.name, False, True)
                db.session.flush()
                _index_files(item, backend)
                _refresh_search(item)
                found.add(item.id)
                count += 1

        root_entries = sorted(backend.list(""), key=lambda entry: natural_key(entry.name))
        root_files = [entry for entry in root_entries if not entry.is_dir]
        for media_type, required_kind in (("video", "video"), ("photo", "image")):
            if any(classify(entry.name) == required_kind for entry in root_files):
                title = source.name or "Без категории"
                item = _upsert_object(source, backend, media_type, "", title, True)
                if item.id in found:
                    item = _upsert_object(source, backend, media_type, "", title, True, True)
                db.session.flush()
                _index_files(item, backend, loose_only=True)
                _refresh_search(item)
                found.add(item.id)
                count += 1

        for entry in root_entries:
            if not entry.is_dir or entry.name.casefold() in {"video", "photo"}:
                continue
            detected_type = _detect_tree_type(backend, entry.path)
            if detected_type is None:
                continue
            item = _upsert_object(source, backend, detected_type, entry.path, entry.name, False)
            if item.id in found:
                item = _upsert_object(source, backend, detected_type, entry.path, entry.name, False, True)
            db.session.flush()
            _index_files(item, backend)
            _refresh_search(item)
            found.add(item.id)
            count += 1
        stale = MediaObject.query.filter_by(source_id=source.id).filter(~MediaObject.id.in_(found)).all() if found else MediaObject.query.filter_by(source_id=source.id).all()
        for item in stale:
            db.session.execute(db.text("DELETE FROM media_search WHERE object_id = :id"), {"id": item.id})
            db.session.delete(item)
        source.status = "ready"
        source.last_error = None
        source.last_scanned_at = datetime.now(timezone.utc)
        db.session.commit()
        return count
    except Exception as exc:
        db.session.rollback()
        source = db.session.get(Source, source.id)
        source.status = "error"
        source.last_error = str(exc)
        db.session.commit()
        raise


def update_sidecar(item: MediaObject) -> None:
    backend = backend_for(item.source)
    sidecar = _sidecar_path(item.relative_path, item.media_type, item.is_loose)
    payload = _sidecar_payload(item.media_type, item.title, item.id)
    payload["category"] = item.category
    payload["tags"] = item.tags
    backend.write_bytes(sidecar, json.dumps(payload, ensure_ascii=False, indent=2).encode("utf-8"))
    _refresh_search(item)
    db.session.commit()


def scan_object(item: MediaObject) -> int:
    backend = backend_for(item.source)
    _index_files(item, backend, loose_only=item.is_loose)
    _refresh_search(item)
    db.session.commit()
    return len(item.files)
