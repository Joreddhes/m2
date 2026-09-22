from __future__ import annotations

import os
import secrets
from pathlib import Path


def default_config(instance_path: str | Path) -> dict:
    instance = Path(instance_path)
    instance.mkdir(parents=True, exist_ok=True)
    secret_file = instance / "secret_key"
    secret = os.environ.get("MEDIAHUB_SECRET_KEY")
    if not secret:
        if not secret_file.exists():
            secret_file.write_text(secrets.token_urlsafe(48), encoding="utf-8")
        secret = secret_file.read_text(encoding="utf-8").strip()
    return {
        "SECRET_KEY": secret,
        "SQLALCHEMY_DATABASE_URI": f"sqlite:///{instance / 'mediahub.db'}",
        "SQLALCHEMY_TRACK_MODIFICATIONS": False,
        "SESSION_COOKIE_HTTPONLY": True,
        "SESSION_COOKIE_SAMESITE": "Lax",
        "REMEMBER_COOKIE_HTTPONLY": True,
        "REMEMBER_COOKIE_SAMESITE": "Lax",
        "MAX_CONTENT_LENGTH": 8 * 1024 * 1024 * 1024,
        "MAX_FILE_SIZE": int(float(os.environ.get("MEDIAHUB_MAX_FILE_GB", "500")) * 1024**3),
        "CACHE_DIR": str(instance / "cache"),
        "UPLOAD_DIR": str(instance / "uploads"),
        "CACHE_MAX_GB": float(os.environ.get("MEDIAHUB_CACHE_MAX_GB", "50")),
        "FFMPEG_PATH": os.environ.get("MEDIAHUB_FFMPEG", "ffmpeg"),
        "FFPROBE_PATH": os.environ.get("MEDIAHUB_FFPROBE", "ffprobe"),
    }
