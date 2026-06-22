# -*- coding: utf-8 -*-
"""
体验模式（demo）蓝图。

与真实业务【完全隔离】：
- 所有数据读写只走 demo_db.DemoSession（独立 SQLite），绝不碰真实 db.session。
- 真实的 web/api/auth 蓝图一行不改。
- 虚拟只读管理员：可浏览全部页面，但所有写操作被拦截。
- 独立限速：API 5 次/分、网页 10 次/分。
- demo 检测每次最多 30 字；检测日志只保留最近 10 条。
"""

from functools import wraps

from flask import (
    Blueprint, render_template, redirect, url_for, flash, request, abort,
    session, current_app,
)
from flask_login import login_user, logout_user, login_required, current_user

import settings_store
import demo_db
from app import fighter, limiter
from i18n import _

demo_bp = Blueprint("demo", __name__, url_prefix="/demomode")

DEMO_MAX_TEXT = 30  # 体验模式每次检测最多 30 字


def _demo_enabled() -> bool:
    return settings_store.get_bool("demo_enabled")


def demo_guard(f):
    """确保体验模式已开启，且当前是 demo 会话。"""
    @wraps(f)
    def decorated(*args, **kwargs):
        if not _demo_enabled():
            flash(_("Demo mode is currently disabled"), "warning")
            return redirect(url_for("auth.login"))
        return f(*args, **kwargs)
    return decorated


def block_write():
    """只读拦截：demo 下所有写操作直接挡掉。"""
    flash(_("Demo mode is read-only; this action is disabled"), "warning")
    return redirect(request.referrer or url_for("demo.index"))


@demo_bp.route("/")
@limiter.limit("10 per minute")
def enter():
    """免登录进入体验模式：自动登录 demo 虚拟管理员。"""
    if not _demo_enabled():
        return render_template("demo_closed.html"), 200

    demo_db.init_demo_db()
    admin = demo_db.get_demo_admin()
    if not admin:
        flash(_("Demo environment failed to initialize"), "danger")
        return redirect(url_for("auth.login"))

    # 标记 demo 会话并登录虚拟管理员（user_loader 会据此从 demo.db 加载）
    session["is_demo"] = True
    login_user(admin, remember=False)
    return redirect(url_for("demo.index"))


@demo_bp.route("/exit", methods=["POST", "GET"])
def exit_demo():
    logout_user()
    session.pop("is_demo", None)
    flash(_("Exited demo mode"), "info")
    return redirect(url_for("auth.login"))


@demo_bp.route("/home", endpoint="index")
@demo_guard
@login_required
def index():
    from models import UserPrompt
    prompts = demo_db.DemoSession.query(UserPrompt).filter_by(
        user_id=current_user.id, audit_status="approved").all()
    return render_template("index.html", user_prompts=prompts)


@demo_bp.route("/detect", methods=["POST"], endpoint="web_detect")
@demo_guard
@login_required
@limiter.limit("10 per minute")
def detect():
    from flask import jsonify
    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    text = (request.form.get("text") or "").strip()
    if not text:
        if wants_json:
            return jsonify({"ok": False, "error": _("Please enter text")}), 200
        flash(_("Please enter text"), "warning")
        return redirect(url_for("demo.index"))
    if len(text) > DEMO_MAX_TEXT:
        msg = _("Demo mode allows at most {n} characters per detection").format(n=DEMO_MAX_TEXT)
        if wants_json:
            return jsonify({"ok": False, "error": msg}), 200
        flash(msg, "warning")
        return redirect(url_for("demo.index"))

    labels, extra_prompt, custom, mode = _resolve_demo_labels()
    result = fighter.detect(text, labels=labels, extra_prompt=extra_prompt)
    demo_db.record_demo_detection(current_user.id, text, result, keep=10)

    if wants_json:
        return jsonify({"ok": True, "result": result})

    from models import UserPrompt
    prompts = demo_db.DemoSession.query(UserPrompt).filter_by(
        user_id=current_user.id, audit_status="approved").all()
    return render_template("index.html", text=text, result=result,
                           user_prompts=prompts, selected_mode=mode)


def _resolve_demo_labels():
    mode = (request.form.get("mode") or "full").strip()
    if mode.startswith("scene:"):
        scene = mode.split(":", 1)[1]
        if scene in settings_store.SCENES:
            return settings_store.SCENES[scene], "", False, mode
    if mode.startswith("prompt:"):
        from models import UserPrompt
        try:
            pid = int(mode.split(":", 1)[1])
            p = demo_db.DemoSession.query(UserPrompt).filter_by(
                id=pid, user_id=current_user.id, audit_status="approved").first()
            if p:
                return p.labels(), (p.extra_prompt or ""), True, mode
        except (TypeError, ValueError):
            pass
    return None, "", False, "full"


# ---------------- 只读浏览路由（从 demo.db 取假数据） ----------------

@demo_bp.route("/keys", endpoint="keys")
@demo_guard
@login_required
def keys():
    from models import ApiKey
    ks = demo_db.DemoSession.query(ApiKey).filter_by(user_id=current_user.id).all()
    return render_template("keys.html", keys=ks)


@demo_bp.route("/prompts", endpoint="prompts")
@demo_guard
@login_required
def prompts():
    from models import UserPrompt
    ps = demo_db.DemoSession.query(UserPrompt).filter_by(
        user_id=current_user.id).order_by(UserPrompt.id.desc()).all()
    return render_template("prompts.html", prompts=ps, builtin=settings_store.CATEGORIES)


@demo_bp.route("/logs", endpoint="logs")
@demo_guard
@login_required
def logs():
    from models import DetectionLog
    ls = demo_db.DemoSession.query(DetectionLog).order_by(
        DetectionLog.created_at.desc()).limit(10).all()
    return render_template("logs.html", logs=ls)


@demo_bp.route("/dashboard", endpoint="dashboard")
@demo_guard
@login_required
def dashboard():
    return render_template("dashboard.html", data=demo_db.get_demo_dashboard())


@demo_bp.route("/ai-models", endpoint="ai_models")
@demo_guard
@login_required
def ai_models():
    # demo 下展示静态示例（不暴露真实渠道/密钥）
    full = [
        {"name": "gpt-4o-mini", "provider": "OPENAI", "channel": "Demo OpenAI", "available": True, "reason": "Available", "order": 1, "paid": True},
        {"name": "claude-3-5-haiku", "provider": "CLAUDE", "channel": "Demo Claude", "available": True, "reason": "Available", "order": 2, "paid": True},
        {"name": "gemini-1.5-flash", "provider": "GEMINI", "channel": "Demo Gemini", "available": False, "reason": "Daily quota reached", "order": 3, "paid": True},
    ]
    return render_template("ai_models.html", full_list=full,
                           available_list=[x for x in full if x["available"]], day="demo")


@demo_bp.route("/channels", endpoint="channels")
@demo_guard
@login_required
def channels():
    # demo 下展示空的渠道管理页（只读，不暴露真实渠道）
    return render_template("channels.html", channels=[])


@demo_bp.route("/updates", endpoint="updates")
@demo_guard
@login_required
def updates():
    import config as _cfg
    info = {"current": _cfg.APP_VERSION, "latest": _cfg.APP_VERSION,
            "has_update": False, "changelog": "", "error": "",
            "release_url": _cfg.GITHUB_URL + "/releases", "checked_at": 0}
    return render_template("updates.html", info=info,
                           docker_image=_cfg.DOCKER_IMAGE,
                           github_url=_cfg.GITHUB_URL, github_proxy="")


@demo_bp.route("/users", endpoint="users")
@demo_guard
@login_required
def users():
    from models import User
    from forms import UserForm
    us = demo_db.DemoSession.query(User).all()
    return render_template("users.html", users=us, form=UserForm())


@demo_bp.route("/settings", methods=["GET", "POST"], endpoint="settings")
@demo_guard
@login_required
def settings():
    if request.method == "POST":
        return block_write()
    from forms import SettingsForm
    form = SettingsForm()
    cur = settings_store.get_all()
    form.system_prompt.data = cur.get("system_prompt", "")
    form.fail_open.data = str(cur.get("fail_open", "0")).lower() in ("1", "true", "yes")
    form.fallback_allow.data = str(cur.get("fallback_allow", "0")).lower() in ("1", "true", "yes")
    form.default_max_tokens.data = int(float(cur.get("default_max_tokens", 2048)))
    form.log_keep_per_user.data = int(float(cur.get("log_keep_per_user", 150)))
    form.demo_enabled.data = str(cur.get("demo_enabled", "0")).lower() in ("1", "true", "yes")
    return render_template("settings.html", form=form, cur=cur, demo_readonly=True)


# ---------------- 写操作：全部拦截（只读演示） ----------------

_WRITE_ENDPOINTS = [
    ("/keys/create", "create_key"),
    ("/keys/<int:key_id>/revoke", "revoke_key"),
    ("/prompts/create", "create_prompt"),
    ("/prompts/<int:prompt_id>/delete", "delete_prompt"),
    ("/users/create", "create_user"),
    ("/users/<int:user_id>/toggle", "toggle_user"),
    ("/users/<int:user_id>/quota", "update_quota"),
    ("/users/<int:user_id>/rename", "rename_user"),
    ("/users/<int:user_id>/password", "reset_user_password"),
    ("/users/<int:user_id>/delete", "delete_user"),
    ("/channels/create", "create_channel"),
    ("/channels/<int:channel_id>/update", "update_channel"),
    ("/channels/<int:channel_id>/delete", "delete_channel"),
    ("/channels/<int:channel_id>/toggle", "toggle_channel"),
    ("/channels/<int:channel_id>/fetch-models", "fetch_models"),
    ("/channels/<int:channel_id>/add-models", "add_models"),
    ("/models/<int:model_id>/update", "update_model"),
    ("/models/<int:model_id>/delete", "delete_model"),
    ("/models/batch", "batch_models"),
    ("/settings/site", "save_site_settings"),
    ("/updates/proxy", "save_update_proxy"),
]


def _make_blocker(**_kw):
    @demo_guard
    @login_required
    def _blocked(*args, **kwargs):
        return block_write()
    return _blocked


for _rule, _ep in _WRITE_ENDPOINTS:
    demo_bp.add_url_rule(_rule, endpoint=_ep, view_func=_make_blocker(), methods=["POST"])
