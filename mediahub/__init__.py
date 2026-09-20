from __future__ import annotations

import os
import secrets
from pathlib import Path

from flask import Flask, jsonify, redirect, request, url_for
from flask_wtf.csrf import CSRFProtect

from .extensions import db, login_manager

csrf = CSRFProtect()


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    Path(app.instance_path).mkdir(parents=True, exist_ok=True)
    secret_file = Path(app.instance_path) / "secret_key"
    configured_secret = os.environ.get("MEDIAHUB_SECRET_KEY")
    if not configured_secret:
        if not secret_file.exists():
            secret_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        configured_secret = secret_file.read_text(encoding="utf-8").strip()
    app.config.from_mapping(
        SECRET_KEY=configured_secret,
        SQLALCHEMY_DATABASE_URI=f"sqlite:///{Path(app.instance_path) / 'mediahub.db'}",
        SQLALCHEMY_TRACK_MODIFICATIONS=False,
        SESSION_COOKIE_HTTPONLY=True,
        SESSION_COOKIE_SAMESITE="Lax",
        REMEMBER_COOKIE_HTTPONLY=True,
        REMEMBER_COOKIE_SAMESITE="Lax",
        MAX_CONTENT_LENGTH=8 * 1024 * 1024 * 1024,
        MAX_FILE_SIZE=int(float(os.environ.get("MEDIAHUB_MAX_FILE_GB", "500")) * 1024 ** 3),
        CACHE_DIR=str(Path(app.instance_path) / "cache"),
        UPLOAD_DIR=str(Path(app.instance_path) / "uploads"),
        CACHE_MAX_GB=float(os.environ.get("MEDIAHUB_CACHE_MAX_GB", "50")),
        FFMPEG_PATH=os.environ.get("MEDIAHUB_FFMPEG", "ffmpeg"),
        FFPROBE_PATH=os.environ.get("MEDIAHUB_FFPROBE", "ffprobe"),
    )
    if test_config:
        app.config.update(test_config)

    Path(app.config["CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    from .auth import auth_bp
    from .catalog import catalog_bp
    from .admin import admin_bp
    from .media import media_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(catalog_bp)
    app.register_blueprint(admin_bp, url_prefix="/admin")
    app.register_blueprint(media_bp)

    @app.get("/healthz")
    def healthz():
        db.session.execute(db.text("SELECT 1"))
        return jsonify(status="ok")

    @app.before_request
    def require_first_run_setup():
        from .models import Admin

        if Admin.query.first() is None and request.endpoint not in {"auth.setup", "static", "healthz"}:
            return redirect(url_for("auth.setup"))

    with app.app_context():
        from . import models  # noqa: F401

        db.create_all()
        _create_search_index()
        _recover_interrupted_tasks()

    return app


def _create_search_index() -> None:
    db.session.execute(db.text(
        "CREATE VIRTUAL TABLE IF NOT EXISTS media_search USING fts5("
        "object_id UNINDEXED, title, category, tags, tokenize='unicode61')"
    ))
    db.session.commit()


def _recover_interrupted_tasks() -> None:
    from .models import Task

    interrupted = Task.query.filter(Task.status.in_(["queued", "running"])).all()
    for task in interrupted:
        task.status = "error"
        task.message = "Задача была прервана перезапуском сервера. Запустите её повторно."
    if interrupted:
        db.session.commit()
