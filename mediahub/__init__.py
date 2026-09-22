from __future__ import annotations

from pathlib import Path

from flask import Flask, jsonify, redirect, request, url_for
from flask_wtf.csrf import CSRFProtect

from .config import default_config
from .database import initialize_database
from .extensions import db, login_manager

csrf = CSRFProtect()


def create_app(test_config: dict | None = None) -> Flask:
    app = Flask(__name__, instance_relative_config=True)
    app.config.from_mapping(default_config(app.instance_path))
    if test_config:
        app.config.update(test_config)

    Path(app.config["CACHE_DIR"]).mkdir(parents=True, exist_ok=True)
    Path(app.config["UPLOAD_DIR"]).mkdir(parents=True, exist_ok=True)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)

    from .admin import admin_bp
    from .auth import auth_bp
    from .catalog import catalog_bp
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

        if Admin.query.first() is None and request.endpoint not in {
            "auth.setup",
            "static",
            "healthz",
        }:
            return redirect(url_for("auth.setup"))

    with app.app_context():
        initialize_database()

    return app
