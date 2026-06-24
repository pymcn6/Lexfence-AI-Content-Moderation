# -*- coding: utf-8 -*-
"""首次安装向导。

未安装时所有页面重定向到 /install。完成后写锁文件 instance/install.lock，
/install 不再可访问。收集：数据库(沿用启动配置)、管理员账号、站点信息。
"""

import os

from flask import (
    Blueprint, render_template, redirect, url_for, request, flash, current_app,
)

import config
from models import db, User
from i18n import _

install_bp = Blueprint("install", __name__, url_prefix="/install")


@install_bp.route("/", methods=["GET", "POST"])
def index():
    if config.is_installed():
        return redirect(url_for("auth.login"))

    if request.method == "POST":
        site_name = (request.form.get("site_name") or config.APP_NAME).strip()
        site_base_url = (request.form.get("site_base_url") or "").strip().rstrip("/")
        admin_user = (request.form.get("admin_username") or "").strip()
        admin_pwd = request.form.get("admin_password") or ""
        admin_pwd2 = request.form.get("admin_password2") or ""
        db_type = (request.form.get("db_type") or "sqlite").strip().lower()

        errors = []
        if not admin_user or len(admin_user) > 80:
            errors.append(_("Admin username invalid"))
        if len(admin_pwd) < 6:
            errors.append(_("Admin password must be at least 6 characters"))
        if admin_pwd != admin_pwd2:
            errors.append(_("Passwords do not match"))

        # 解析所选数据库连接串
        env_locked = bool(os.environ.get("DATABASE_URL") or os.environ.get("MYSQL_HOST"))
        chosen_uri = current_app.config["SQLALCHEMY_DATABASE_URI"]
        if not env_locked:
            if db_type == "mysql":
                host = (request.form.get("mysql_host") or "").strip()
                port = request.form.get("mysql_port") or "3306"
                user = (request.form.get("mysql_user") or "").strip()
                password = request.form.get("mysql_password") or ""
                dbname = (request.form.get("mysql_db") or "").strip()
                if not host or not user or not dbname:
                    errors.append(_("MySQL host, user and database are required"))
                else:
                    chosen_uri = config.build_mysql_uri(host, port, user, password, dbname)
            else:
                chosen_uri = config.default_sqlite_uri()

        if errors:
            for e in errors:
                flash(e, "danger")
            return render_template("install.html",
                                   db_uri=current_app.config["SQLALCHEMY_DATABASE_URI"],
                                   env_locked=env_locked, form=request.form)

        # 应用所选数据库（非环境变量锁定时）：重绑引擎
        if not env_locked and chosen_uri != current_app.config["SQLALCHEMY_DATABASE_URI"]:
            try:
                _rebind_database(chosen_uri)
            except Exception as exc:
                flash(_("Database connection failed: {err}").format(err=exc), "danger")
                return render_template("install.html",
                                       db_uri=current_app.config["SQLALCHEMY_DATABASE_URI"],
                                       env_locked=env_locked, form=request.form)

        try:
            db.create_all()
            from app import _migrate_schema
            _migrate_schema()
            # 防御：若同名用户已存在（老库残留），复用并提升为管理员、重置密码
            admin = User.query.filter_by(username=admin_user).first()
            if admin:
                admin.is_admin = True
                admin.active = True
                admin.set_password(admin_pwd)
            else:
                admin = User(username=admin_user, is_admin=True,
                             quota=999999, monthly_quota=999999,
                             max_text_length=5000, prompt_quota=50, active=True)
                admin.set_password(admin_pwd)
                db.session.add(admin)

            import settings_store
            settings_store.save_settings({"site_name": site_name, "site_title": site_name,
                                          "site_base_url": site_base_url})
            db.session.commit()
        except Exception as exc:
            db.session.rollback()
            flash(_("Installation failed: {err}").format(err=exc), "danger")
            return render_template("install.html",
                                   db_uri=current_app.config["SQLALCHEMY_DATABASE_URI"],
                                   env_locked=env_locked, form=request.form)

        # 持久化数据库选择（非环境变量锁定时）
        if not env_locked:
            try:
                config.save_database_url(chosen_uri)
            except OSError:
                pass

        # 写锁文件
        try:
            with open(config.INSTALL_LOCK_FILE, "w", encoding="utf-8") as f:
                f.write("installed\n")
        except OSError as exc:
            flash(_("Cannot write install lock file: {err}").format(err=exc), "danger")
            return render_template("install.html",
                                   db_uri=current_app.config["SQLALCHEMY_DATABASE_URI"],
                                   env_locked=env_locked, form=request.form)

        flash(_("Installation complete, please sign in with the admin account"), "success")
        return redirect(url_for("auth.login"))

    env_locked = bool(os.environ.get("DATABASE_URL") or os.environ.get("MYSQL_HOST"))
    return render_template("install.html",
                           db_uri=current_app.config["SQLALCHEMY_DATABASE_URI"],
                           env_locked=env_locked, form={})


def _rebind_database(uri: str):
    """切换 SQLAlchemy 引擎到新的连接串（install 时使用），并测试连通性。"""
    from sqlalchemy import text
    current_app.config["SQLALCHEMY_DATABASE_URI"] = uri
    config.Config.SQLALCHEMY_DATABASE_URI = uri
    try:
        db.session.remove()
        db.engine.dispose()
    except Exception:
        pass
    # 触发新引擎创建并测试连通
    with db.engine.connect() as conn:
        conn.execute(text("SELECT 1"))
