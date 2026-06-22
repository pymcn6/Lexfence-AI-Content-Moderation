# -*- coding: utf-8 -*-
"""Lexfence Web 应用主入口。"""

from flask import Flask, redirect, url_for, request, session
from flask_login import LoginManager
from flask_wtf.csrf import CSRFProtect
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

import config
from config import Config, LLM_DEFAULT_MAX_TOKENS, LLM_FAIL_OPEN
from models import db, User
from spamnotefighter import SpamNoteFighter


def _llm_settings_provider():
    """从数据库读取可热改的运行时配置。"""
    import settings_store as _ss
    return {
        "default_max_tokens": _ss.get_int("default_max_tokens", LLM_DEFAULT_MAX_TOKENS),
        "fail_open": _ss.get_bool("fail_open"),
        "fallback_allow": _ss.get_bool("fallback_allow"),
    }


# 全局检测器：DB 渠道驱动
from spamnotefighter.gpt_classifier import GPTClassifier

_gpt = GPTClassifier(
    settings_provider=_llm_settings_provider,
    default_max_tokens=LLM_DEFAULT_MAX_TOKENS,
    fail_open=LLM_FAIL_OPEN,
)
fighter = SpamNoteFighter(gpt_classifier=_gpt)

login_manager = LoginManager()
csrf = CSRFProtect()
limiter = Limiter(key_func=get_remote_address, storage_uri=Config.RATELIMIT_STORAGE_URI)


def create_app() -> Flask:
    app = Flask(__name__)
    app.config.from_object(Config)

    db.init_app(app)
    login_manager.init_app(app)
    csrf.init_app(app)
    limiter.init_app(app)

    _init_babel(app)

    login_manager.login_view = "auth.login"
    login_manager.login_message = ""

    from auth import auth_bp
    from api import api_bp
    from web import web_bp
    from demo import demo_bp
    from install import install_bp

    app.register_blueprint(auth_bp)
    app.register_blueprint(api_bp, url_prefix="/api/v1")
    app.register_blueprint(web_bp)
    app.register_blueprint(demo_bp)
    app.register_blueprint(install_bp)

    _register_install_guard(app)
    _register_template_helpers(app)

    @app.after_request
    def _set_security_headers(response):
        response.headers["X-Frame-Options"] = "DENY"
        response.headers["X-Content-Type-Options"] = "nosniff"
        response.headers["Referrer-Policy"] = "strict-origin-when-cross-origin"
        if app.config.get("SESSION_COOKIE_SECURE"):
            response.headers["Strict-Transport-Security"] = "max-age=31536000; includeSubDomains"
        return response

    with app.app_context():
        _adopt_existing_db(app)
        if config.is_installed():
            db.create_all()
            _migrate_schema()
        try:
            import demo_db
            if config.is_installed():
                demo_db.init_demo_db()
        except Exception:
            pass

    return app


def _adopt_existing_db(app):
    """老库升级兼容：数据库已存在且已有管理员，则视为已安装。

    自动补建新增的表（AI 渠道/模型等），并写入安装锁文件，
    避免老用户升级后被错误地要求重新安装。
    """
    if config.is_installed():
        return
    try:
        from sqlalchemy import inspect
        inspector = inspect(db.engine)
        if "users" not in inspector.get_table_names():
            return
        # 补建新增的表（不影响已存在的表）
        db.create_all()
        _migrate_schema()
        admin = User.query.filter_by(is_admin=True).first()
        if admin:
            with open(config.INSTALL_LOCK_FILE, "w", encoding="utf-8") as f:
                f.write("adopted-existing-db\n")
    except Exception:
        # 数据库不可用 / 无表 → 走全新安装流程
        pass


def _init_babel(app):
    """国际化（自建轻量字典，零编译依赖）。

    注入 `_` / `gettext` / `get_locale` 到 Jinja，按 cookie/session/浏览器选语言，
    切换即时生效，不依赖 gettext .mo 文件。
    """
    import i18n

    def get_locale():
        lang = session.get("lang") or request.cookies.get("lang")
        if lang in i18n.SUPPORTED:
            return lang
        best = request.accept_languages.best_match(i18n.SUPPORTED) if request else None
        return best or app.config.get("BABEL_DEFAULT_LOCALE", i18n.DEFAULT)

    def _gettext(text):
        return i18n.translate(text, get_locale())

    app.jinja_env.globals["_"] = _gettext
    app.jinja_env.globals["gettext"] = _gettext
    app.jinja_env.globals["get_locale"] = get_locale


def _register_install_guard(app):
    """未安装时，除 install / 静态资源外，一律重定向到安装向导。"""
    @app.before_request
    def _guard_install():
        if config.is_installed():
            return None
        ep = request.endpoint or ""
        if ep.startswith("install.") or ep == "static" or ep == "setlang":
            return None
        return redirect(url_for("install.index"))


def _register_template_helpers(app):
    from flask import url_for as _url_for

    @app.before_request
    def _demo_isolation_guard():
        if not session.get("is_demo"):
            return None
        bp = request.blueprint
        if bp in ("demo", "auth", "install", None) or request.endpoint == "static":
            return None
        if bp == "api":
            return ("demo session cannot access real API", 403)
        return redirect(_url_for("demo.index"))

    @app.context_processor
    def _inject():
        is_demo = bool(session.get("is_demo")) and (request.blueprint == "demo")

        def nsurl(name, **kwargs):
            ns = "demo" if is_demo else "web"
            return _url_for(f"{ns}.{name}", **kwargs)

        def exiturl():
            return _url_for("demo.exit_demo") if is_demo else _url_for("auth.logout")

        # 站点品牌：优先后台设置，回退到 config
        site = {"name": config.APP_NAME, "title": config.APP_NAME,
                "description": "", "favicon": "", "logo": ""}
        if config.is_installed():
            try:
                import settings_store
                site["name"] = settings_store.get_setting("site_name") or config.APP_NAME
                site["title"] = settings_store.get_setting("site_title") or site["name"]
                site["description"] = settings_store.get_setting("site_description") or ""
                site["favicon"] = settings_store.get_setting("site_favicon") or ""
                site["logo"] = settings_store.get_setting("site_logo") or ""
            except Exception:
                pass

        return {"ns": "demo" if is_demo else "web", "is_demo": is_demo,
                "nsurl": nsurl, "exiturl": exiturl,
                "APP_NAME": site["name"], "SITE": site,
                "GITHUB_URL": config.GITHUB_URL}

    @app.route("/setlang/<lang>")
    def setlang(lang):
        from flask import make_response
        if lang not in app.config.get("BABEL_SUPPORTED_LOCALES", ["en", "zh"]):
            lang = app.config.get("BABEL_DEFAULT_LOCALE", "en")
        session["lang"] = lang
        resp = make_response(redirect(request.referrer or "/"))
        resp.set_cookie("lang", lang, max_age=31536000)
        return resp


def _migrate_schema():
    """老库增量迁移，逻辑集中在 migrate.py（全新安装无需）。"""
    try:
        import migrate
        migrate.run(db)
    except Exception:
        pass


@login_manager.user_loader
def load_user(user_id: str):
    if session.get("is_demo"):
        try:
            import demo_db
            from models import User as _U
            return demo_db.DemoSession.get(_U, int(user_id))
        except Exception:
            return None
    try:
        return db.session.get(User, int(user_id))
    except Exception:
        return None


app = create_app()

if __name__ == "__main__":
    app.run(host="0.0.0.0", port=5000, debug=False)
