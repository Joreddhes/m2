from __future__ import annotations

import time

from flask import Blueprint, flash, redirect, render_template, request, session, url_for
from flask_login import current_user, login_user, logout_user

from .extensions import db
from .models import Admin

auth_bp = Blueprint("auth", __name__)


@auth_bp.route("/setup", methods=["GET", "POST"])
def setup():
    if Admin.query.first():
        return redirect(url_for("catalog.home"))
    if request.method == "POST":
        password = request.form.get("password", "")
        confirmation = request.form.get("confirmation", "")
        if len(password) < 10:
            flash("Пароль должен содержать не менее 10 символов.", "danger")
        elif password != confirmation:
            flash("Пароли не совпадают.", "danger")
        else:
            admin = Admin()
            admin.set_password(password)
            db.session.add(admin)
            db.session.commit()
            login_user(admin)
            flash("Администратор создан.", "success")
            return redirect(url_for("admin.sources"))
    return render_template("setup.html")


@auth_bp.route("/login", methods=["GET", "POST"])
def login():
    if current_user.is_authenticated:
        return redirect(url_for("catalog.home"))
    admin = Admin.query.first()
    if admin is None:
        return redirect(url_for("auth.setup"))
    locked_until = float(session.get("login_locked_until", 0))
    if request.method == "POST":
        if locked_until > time.time():
            flash("Слишком много попыток. Подождите минуту.", "danger")
        elif admin.check_password(request.form.get("password", "")):
            session.pop("login_attempts", None)
            session.pop("login_locked_until", None)
            login_user(admin, remember=True)
            next_url = request.args.get("next", "")
            return redirect(next_url if next_url.startswith("/") and not next_url.startswith("//") else url_for("catalog.home"))
        else:
            attempts = int(session.get("login_attempts", 0)) + 1
            session["login_attempts"] = attempts
            if attempts >= 5:
                session["login_locked_until"] = time.time() + 60
                session["login_attempts"] = 0
            flash("Неверный пароль.", "danger")
    return render_template("login.html")


@auth_bp.post("/logout")
def logout():
    logout_user()
    return redirect(url_for("catalog.home"))
