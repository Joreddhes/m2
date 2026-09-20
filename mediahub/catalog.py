from __future__ import annotations

import re
from collections import defaultdict
from pathlib import PurePosixPath

from flask import Blueprint, abort, render_template, request

from .extensions import db
from .models import MediaObject, Source
from .scanner import natural_key

catalog_bp = Blueprint("catalog", __name__)


def _search_expression(query: str) -> str:
    words = re.findall(r"[\w-]+", query, flags=re.UNICODE)
    return " AND ".join(f'"{word}"*' for word in words)


@catalog_bp.get("/")
def home():
    counts = dict(db.session.query(MediaObject.media_type, db.func.count()).group_by(MediaObject.media_type).all())
    recent = MediaObject.query.order_by(MediaObject.updated_at.desc()).limit(8).all()
    return render_template("home.html", counts=counts, recent=recent)


@catalog_bp.get("/library/<media_type>")
def library(media_type: str):
    if media_type not in {"video", "photo"}:
        abort(404)
    query_text = request.args.get("q", "").strip()
    category = request.args.get("category", "").strip()
    source_id = request.args.get("source", type=int)
    query = MediaObject.query.filter_by(media_type=media_type)
    if query_text:
        expression = _search_expression(query_text)
        if expression:
            ids = db.session.execute(
                db.text("SELECT object_id FROM media_search WHERE media_search MATCH :query"),
                {"query": expression},
            ).scalars()
            query = query.filter(MediaObject.id.in_(list(ids)))
    if category:
        query = query.filter_by(category=category)
    if source_id:
        query = query.filter_by(source_id=source_id)
    objects = query.order_by(MediaObject.title.collate("NOCASE")).all()
    categories = [row[0] for row in db.session.query(MediaObject.category).filter_by(media_type=media_type).filter(MediaObject.category != "").distinct().order_by(MediaObject.category)]
    sources = Source.query.filter_by(enabled=True).order_by(Source.name).all()
    return render_template(
        "library.html", media_type=media_type, objects=objects, categories=categories,
        sources=sources, query_text=query_text, selected_category=category, selected_source=source_id,
    )


@catalog_bp.get("/content/<object_id>")
def content(object_id: str):
    item = db.get_or_404(MediaObject, object_id)
    grouped: dict[str, list] = defaultdict(list)
    root = PurePosixPath(item.relative_path)
    for media in sorted(item.files, key=lambda entry: natural_key(entry.relative_path)):
        if media.kind == "subtitle":
            continue
        relative = PurePosixPath(media.relative_path).relative_to(root)
        folder = str(relative.parent) if str(relative.parent) != "." else "Основное"
        grouped[folder].append(media)
    return render_template("content.html", item=item, grouped=grouped)

