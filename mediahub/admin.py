from __future__ import annotations

from pathlib import Path, PurePosixPath

from flask import Blueprint, current_app, flash, jsonify, redirect, render_template, request, url_for
from flask_login import login_required
from uuid import uuid4

from .extensions import db
from .models import MediaObject, Source, Task, UploadSession
from .jobs import queue_scan
from .scanner import scan_object, scan_source, update_sidecar
from .storage import backend_for, encrypt_password, safe_component, safe_relative

admin_bp = Blueprint("admin", __name__)


@admin_bp.get("/")
@login_required
def dashboard():
    return render_template(
        "admin/dashboard.html",
        source_count=Source.query.count(),
        object_count=MediaObject.query.count(),
        tasks=Task.query.order_by(Task.created_at.desc()).limit(10).all(),
    )


@admin_bp.route("/sources", methods=["GET", "POST"])
@login_required
def sources():
    if request.method == "POST":
        kind = request.form.get("kind", "local")
        name = request.form.get("name", "").strip()
        root = request.form.get("root", "").strip()
        if not name or kind not in {"local", "ftp"} or not root:
            flash("Заполните название, тип и корневой путь.", "danger")
        else:
            source = Source(
                name=name, kind=kind, root=root,
                host=request.form.get("host", "").strip() or None,
                port=request.form.get("port", type=int) or 21,
                username=request.form.get("username", "").strip() or None,
                use_tls=bool(request.form.get("use_tls")),
            )
            password = request.form.get("password", "")
            if password:
                source.encrypted_password = encrypt_password(password)
            try:
                backend = backend_for(source)
                backend.mkdir("video")
                backend.mkdir("photo")
                backend.list("video")
                db.session.add(source)
                db.session.commit()
                queue_scan(source)
                flash("Источник подключён. Сканирование запущено в фоне.", "success")
                return redirect(url_for("admin.sources"))
            except Exception as exc:
                db.session.rollback()
                flash(f"Не удалось подключить источник: {exc}", "danger")
    return render_template("admin/sources.html", sources=Source.query.order_by(Source.name).all())


@admin_bp.post("/sources/<int:source_id>/scan")
@login_required
def scan(source_id: int):
    source = db.get_or_404(Source, source_id)
    try:
        task = queue_scan(source)
        flash("Сканирование поставлено в очередь." if task else "Этот источник уже сканируется.", "success")
    except Exception as exc:
        flash(f"Ошибка сканирования: {exc}", "danger")
    return redirect(url_for("admin.sources"))


@admin_bp.route("/content/new", methods=["GET", "POST"])
@login_required
def new_content():
    sources = Source.query.filter_by(enabled=True).order_by(Source.name).all()
    if request.method == "POST":
        source = db.get_or_404(Source, request.form.get("source_id", type=int))
        media_type = request.form.get("media_type", "")
        folder = request.form.get("folder", "").strip()
        if media_type not in {"video", "photo"} or not folder:
            flash("Выберите тип и задайте имя папки.", "danger")
        else:
            try:
                folder = safe_component(folder)
                relative = safe_relative(f"{media_type}/{folder}")
                backend = backend_for(source)
                if backend.exists(relative):
                    raise ValueError("Такая папка уже существует")
                backend.mkdir(relative)
                scan_source(source)
                item = MediaObject.query.filter_by(source_id=source.id, relative_path=relative).one()
                item.title = request.form.get("title", "").strip() or folder
                item.category = request.form.get("category", "").strip()
                item.tags = [tag.strip() for tag in request.form.get("tags", "").split(",") if tag.strip()]
                update_sidecar(item)
                flash("Объект создан. Теперь можно загрузить файлы.", "success")
                return redirect(url_for("admin.edit_content", object_id=item.id))
            except Exception as exc:
                flash(f"Не удалось создать объект: {exc}", "danger")
    return render_template("admin/new_content.html", sources=sources)


@admin_bp.route("/content/<object_id>", methods=["GET", "POST"])
@login_required
def edit_content(object_id: str):
    item = db.get_or_404(MediaObject, object_id)
    if request.method == "POST":
        item.title = request.form.get("title", "").strip() or item.title
        item.category = request.form.get("category", "").strip()
        item.tags = [tag.strip() for tag in request.form.get("tags", "").split(",") if tag.strip()]
        try:
            update_sidecar(item)
            flash("Метаданные сохранены.", "success")
        except Exception as exc:
            db.session.rollback()
            flash(f"Не удалось сохранить метаданные: {exc}", "danger")
        return redirect(url_for("admin.edit_content", object_id=item.id))
    return render_template("admin/edit_content.html", item=item)


@admin_bp.post("/content/<object_id>/folder")
@login_required
def create_folder(object_id: str):
    item = db.get_or_404(MediaObject, object_id)
    try:
        child = safe_relative(request.form.get("folder", ""))
        backend_for(item.source).mkdir(f"{item.relative_path}/{child}")
        flash("Папка создана.", "success")
    except Exception as exc:
        flash(f"Не удалось создать папку: {exc}", "danger")
    return redirect(url_for("admin.edit_content", object_id=item.id))


@admin_bp.post("/content/<object_id>/upload")
@login_required
def upload(object_id: str):
    item = db.get_or_404(MediaObject, object_id)
    destination = request.form.get("destination", "").strip()
    try:
        destination = safe_relative(destination) if destination else ""
        backend = backend_for(item.source)
        uploaded = 0
        for incoming in request.files.getlist("files"):
            original = (incoming.filename or "").replace("\\", "/")
            parts = [safe_component(part) for part in PurePosixPath(original).parts if part not in {"", ".", ".."}]
            if not parts or any(not part for part in parts):
                continue
            relative_name = "/".join(parts)
            target = "/".join(part for part in (item.relative_path, destination, relative_name) if part)
            target = safe_relative(target)
            if backend.exists(target):
                raise ValueError(f"Файл уже существует: {relative_name}")
            temporary = Path(current_app.config["UPLOAD_DIR"]) / f"{item.id}-{uploaded}.part"
            incoming.save(temporary)
            backend.upload_file(target, temporary)
            temporary.unlink(missing_ok=True)
            uploaded += 1
        scan_object(item)
        flash(f"Загружено файлов: {uploaded}.", "success")
    except Exception as exc:
        flash(f"Ошибка загрузки: {exc}", "danger")
    return redirect(url_for("admin.edit_content", object_id=item.id))


@admin_bp.post("/api/uploads")
@login_required
def create_upload():
    payload = request.get_json(silent=True) or {}
    item = db.get_or_404(MediaObject, payload.get("object_id", ""))
    try:
        destination = payload.get("destination", "").strip()
        destination = safe_relative(destination) if destination else ""
        raw_name = str(payload.get("name", "")).replace("\\", "/")
        parts = [safe_component(part) for part in PurePosixPath(raw_name).parts if part not in {"", ".", ".."}]
        if not parts:
            raise ValueError("Имя файла отсутствует")
        target = safe_relative("/".join(part for part in (item.relative_path, destination, "/".join(parts)) if part))
        total = int(payload.get("size", 0))
        if total <= 0 or total > current_app.config["MAX_FILE_SIZE"]:
            raise ValueError("Недопустимый размер файла")
        if backend_for(item.source).exists(target):
            return jsonify(error="Файл уже существует"), 409
        existing = UploadSession.query.filter_by(object_id=item.id, relative_path=target, total_size=total).first()
        if existing:
            return jsonify(id=existing.id, offset=existing.offset)
        upload = UploadSession(
            object_id=item.id, relative_path=target, total_size=total,
            temp_name=f"{uuid4()}.part",
        )
        db.session.add(upload)
        db.session.commit()
        return jsonify(id=upload.id, offset=0), 201
    except (TypeError, ValueError) as exc:
        return jsonify(error=str(exc)), 400


@admin_bp.patch("/api/uploads/<upload_id>")
@login_required
def append_upload(upload_id: str):
    upload = db.get_or_404(UploadSession, upload_id)
    try:
        expected = int(request.headers.get("Upload-Offset", "-1"))
        if expected != upload.offset:
            return jsonify(error="Смещение не совпадает", offset=upload.offset), 409
        chunk = request.get_data(cache=False)
        if not chunk or upload.offset + len(chunk) > upload.total_size:
            raise ValueError("Недопустимый фрагмент")
        temporary = Path(current_app.config["UPLOAD_DIR"]) / upload.temp_name
        temporary.parent.mkdir(parents=True, exist_ok=True)
        with temporary.open("ab") as handle:
            handle.write(chunk)
        upload.offset += len(chunk)
        complete = upload.offset == upload.total_size
        if complete:
            item = db.session.get(MediaObject, upload.object_id)
            backend_for(item.source).upload_file(upload.relative_path, temporary)
            temporary.unlink(missing_ok=True)
            db.session.delete(upload)
            db.session.commit()
            scan_object(db.session.get(MediaObject, item.id))
            return jsonify(done=True, offset=upload.total_size)
        db.session.commit()
        return jsonify(done=False, offset=upload.offset)
    except (OSError, TypeError, ValueError) as exc:
        db.session.rollback()
        return jsonify(error=str(exc), offset=upload.offset), 400
