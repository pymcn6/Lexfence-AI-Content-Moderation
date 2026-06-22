# -*- coding: utf-8 -*-
"""网页管理后台路由。"""

from functools import wraps

from flask import Blueprint, render_template, redirect, url_for, flash, request, current_app
from flask_login import login_required, current_user

from models import db, User, ApiKey, DetectionLog
from forms import UserForm, QuotaForm
from app import fighter, limiter
from i18n import _

web_bp = Blueprint("web", __name__)


def admin_required(f):
    @wraps(f)
    def decorated(*args, **kwargs):
        if not current_user.is_authenticated or not current_user.is_admin:
            flash(_("Administrator privileges required"), "danger")
            return redirect(url_for("web.index"))
        return f(*args, **kwargs)
    return decorated


@web_bp.route("/")
@login_required
def index():
    from models import UserPrompt
    my_prompts = UserPrompt.query.filter_by(
        user_id=current_user.id, audit_status="approved"
    ).order_by(UserPrompt.id.desc()).all()
    return render_template("index.html", user_prompts=my_prompts)


@web_bp.route("/detect", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
def web_detect():
    from flask import jsonify
    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    text = request.form.get("text", "").strip()
    if not text:
        if wants_json:
            return jsonify({"ok": False, "error": _("Please enter text")}), 200
        flash(_("Please enter text"), "warning")
        return redirect(url_for("web.index"))

    # 字数限制与用户配置一致
    max_len = current_user.max_text_length or current_app.config.get("MAX_TEXT_LENGTH", 5000)
    if len(text) > max_len:
        msg = _("Text exceeds the length limit (max {n} chars)").format(n=max_len)
        if wants_json:
            return jsonify({"ok": False, "error": msg}), 200
        flash(msg, "warning")
        return redirect(url_for("web.index"))

    # 解析检测模板：scene（内置场景）或 prompt_id（自定义模板）
    labels, extra_prompt, custom, mode = _resolve_web_labels()

    # 网页端检测同样扣减配额（原子扣减，避免并发超卖）
    if not current_user.consume_quota(1):
        msg = _("Quota exhausted, cannot detect")
        if wants_json:
            return jsonify({"ok": False, "error": msg}), 200
        flash(msg, "danger")
        return redirect(url_for("web.index"))

    result = fighter.detect(text, labels=labels, extra_prompt=extra_prompt)

    # 记录检测日志 + 累加月度统计 + 裁剪日志
    import settings_store
    settings_store.record_detection(current_user.id, text, result)

    if wants_json:
        return jsonify({"ok": True, "result": result})

    from models import UserPrompt
    my_prompts = UserPrompt.query.filter_by(
        user_id=current_user.id, audit_status="approved"
    ).order_by(UserPrompt.id.desc()).all()
    return render_template("index.html", text=text, result=result,
                           user_prompts=my_prompts, selected_mode=mode, custom_labels=custom)


def _resolve_web_labels():
    """根据表单的 mode 解析检测标签集。

    mode 取值：full（全检，默认）/ scene:nickname / prompt:<id>
    返回 (labels, extra_prompt, is_custom, mode)
    """
    import settings_store
    from models import UserPrompt

    mode = (request.form.get("mode") or "full").strip()

    if mode.startswith("scene:"):
        scene = mode.split(":", 1)[1]
        if scene in settings_store.SCENES:
            return settings_store.SCENES[scene], "", False, mode

    if mode.startswith("prompt:"):
        try:
            pid = int(mode.split(":", 1)[1])
            p = UserPrompt.query.filter_by(
                id=pid, user_id=current_user.id, audit_status="approved"
            ).first()
            if p:
                return p.labels(), (p.extra_prompt or ""), True, mode
        except (TypeError, ValueError):
            pass

    return None, "", False, "full"


@web_bp.route("/users")
@login_required
@admin_required
def users():
    users_list = User.query.all()
    form = UserForm()
    return render_template("users.html", users=users_list, form=form)


@web_bp.route("/users/create", methods=["POST"])
@login_required
@admin_required
def create_user():
    form = UserForm()
    if form.validate_on_submit():
        username = form.username.data.strip()
        if User.query.filter_by(username=username).first():
            flash(_("Username already exists"), "danger")
            return redirect(url_for("web.users"))

        user = User(
            username=username,
            monthly_quota=form.monthly_quota.data,
            quota=form.monthly_quota.data,
            max_text_length=form.max_text_length.data,
            prompt_quota=form.prompt_quota.data,
            active=True,
        )
        user.set_password(form.password.data)
        db.session.add(user)
        db.session.commit()
        flash(_("User {name} created").format(name=username), "success")
    else:
        flash(_("Form validation failed"), "danger")
    return redirect(url_for("web.users"))


@web_bp.route("/users/<int:user_id>/toggle", methods=["POST"])
@login_required
@admin_required
def toggle_user(user_id: int):
    user = User.query.get_or_404(user_id)
    if user.is_admin:
        flash(_("Cannot disable an administrator"), "danger")
        return redirect(url_for("web.users"))
    user.active = not user.active
    db.session.commit()
    msg = _("User {name} enabled") if user.active else _("User {name} disabled")
    flash(msg.format(name=user.username), "success")
    return redirect(url_for("web.users"))


@web_bp.route("/users/<int:user_id>/quota", methods=["POST"])
@login_required
@admin_required
def update_quota(user_id: int):
    user = User.query.get_or_404(user_id)
    form = QuotaForm()
    if form.validate_on_submit():
        current = form.current_quota.data
        user.update_quota_settings(
            monthly_quota=form.monthly_quota.data,
            current_quota=current if current is not None else form.monthly_quota.data,
        )
        user.max_text_length = form.max_text_length.data
        user.prompt_quota = form.prompt_quota.data
        db.session.commit()
        flash(_("User {name} quota/limits updated").format(name=user.username), "success")
    else:
        flash(_("Invalid quota input"), "danger")
    return redirect(url_for("web.users"))


@web_bp.route("/users/<int:user_id>/rename", methods=["POST"])
@login_required
@admin_required
def rename_user(user_id: int):
    user = User.query.get_or_404(user_id)
    new_name = (request.form.get("username") or "").strip()
    if not new_name or len(new_name) > 80:
        flash(_("Invalid username"), "danger")
        return redirect(url_for("web.users"))
    exists = User.query.filter(User.username == new_name, User.id != user_id).first()
    if exists:
        flash(_("Username already exists"), "danger")
        return redirect(url_for("web.users"))
    user.username = new_name
    db.session.commit()
    flash(_("Username updated"), "success")
    return redirect(url_for("web.users"))


@web_bp.route("/users/<int:user_id>/password", methods=["POST"])
@login_required
@admin_required
def reset_user_password(user_id: int):
    user = User.query.get_or_404(user_id)
    new_pwd = request.form.get("password") or ""
    if len(new_pwd) < 6 or len(new_pwd) > 128:
        flash(_("Password must be 6-128 characters"), "danger")
        return redirect(url_for("web.users"))
    user.set_password(new_pwd)
    db.session.commit()
    flash(_("Password for user {name} has been reset").format(name=user.username), "success")
    return redirect(url_for("web.users"))


@web_bp.route("/users/<int:user_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_user(user_id: int):
    user = User.query.get_or_404(user_id)
    if user.is_admin:
        flash(_("Cannot delete an administrator"), "danger")
        return redirect(url_for("web.users"))
    if user.id == current_user.id:
        flash(_("Cannot delete yourself"), "danger")
        return redirect(url_for("web.users"))
    # 清理该用户的检测日志（API Key 已设置级联删除）
    DetectionLog.query.filter_by(user_id=user.id).delete(synchronize_session=False)
    db.session.delete(user)
    db.session.commit()
    flash(_("User {name} deleted").format(name=user.username), "success")
    return redirect(url_for("web.users"))


@web_bp.route("/ai-models")
@login_required
@admin_required
def ai_models():
    """可用 AI 列表：按优先级排序，展示今日可用与全部模型（被动状态）。"""
    from models import AIChannel, AIModel
    from datetime import datetime

    today = datetime.utcnow().strftime("%Y-%m-%d")
    now = datetime.utcnow()
    rows = (db.session.query(AIModel, AIChannel)
            .join(AIChannel, AIModel.channel_id == AIChannel.id)
            .all())
    rows.sort(key=lambda t: (t[0].priority, t[1].priority, t[0].id))

    full = []
    for i, (m, ch) in enumerate(rows, 1):
        # 计算可用性（与检测引擎一致的判断）
        if not m.enabled or not ch.enabled:
            avail, reason = False, "Disabled"
        elif m.cooldown_until and m.cooldown_until > now:
            avail, reason = False, "Rate limited (cooling down)"
        elif (m.used_date == today and m.daily_token_limit
              and m.used_tokens_today >= m.daily_token_limit):
            avail, reason = False, "Daily quota reached"
        elif m.available is False and m.used_date == today:
            avail, reason = False, "Unavailable today"
        else:
            avail, reason = True, "Available"
        full.append({
            "order": i, "name": m.model_name, "provider": ch.provider.upper(),
            "channel": ch.name, "available": avail, "reason": reason,
            "paid": True,
        })
    available_list = [x for x in full if x["available"]]

    return render_template(
        "ai_models.html",
        full_list=full,
        available_list=available_list,
        day=today,
    )



@web_bp.route("/channels")
@login_required
@admin_required
def channels():
    from models import AIChannel
    chs = AIChannel.query.order_by(AIChannel.priority, AIChannel.id).all()
    return render_template("channels.html", channels=chs)


@web_bp.route("/channels/create", methods=["POST"])
@login_required
@admin_required
def create_channel():
    from models import AIChannel
    name = (request.form.get("name") or "").strip()
    provider = (request.form.get("provider") or "openai").strip().lower()
    base_url = (request.form.get("base_url") or "").strip()
    api_key = (request.form.get("api_key") or "").strip()
    models_endpoint = (request.form.get("models_endpoint") or "").strip()
    if not name or provider not in ("openai", "openai_compatible", "claude", "gemini"):
        flash(_("Invalid channel info"), "danger")
        return redirect(url_for("web.channels"))
    import providers
    if not providers.is_safe_public_url(base_url) or not providers.is_safe_public_url(models_endpoint):
        flash(_("URL must be a public http(s) address (private/loopback addresses are not allowed)"), "danger")
        return redirect(url_for("web.channels"))
    ch = AIChannel(name=name[:80], provider=provider, base_url=base_url or None,
                   models_endpoint=models_endpoint or None,
                   priority=int(request.form.get("priority") or 100))
    ch.set_api_key(api_key)
    db.session.add(ch)
    db.session.commit()
    flash(_("Channel \"{name}\" created").format(name=name), "success")
    return redirect(url_for("web.channels"))


@web_bp.route("/channels/<int:channel_id>/update", methods=["POST"])
@login_required
@admin_required
def update_channel(channel_id):
    from models import AIChannel
    ch = AIChannel.query.get_or_404(channel_id)
    base_url = (request.form.get("base_url") or "").strip()
    models_endpoint = (request.form.get("models_endpoint") or "").strip()
    import providers
    if not providers.is_safe_public_url(base_url) or not providers.is_safe_public_url(models_endpoint):
        flash(_("URL must be a public http(s) address (private/loopback addresses are not allowed)"), "danger")
        return redirect(url_for("web.channels"))
    ch.name = (request.form.get("name") or ch.name).strip()[:80]
    ch.base_url = base_url or None
    ch.models_endpoint = models_endpoint or None
    ch.priority = int(request.form.get("priority") or ch.priority)
    ch.enabled = bool(request.form.get("enabled"))
    new_key = (request.form.get("api_key") or "").strip()
    if new_key:
        ch.set_api_key(new_key)
    db.session.commit()
    flash(_("Channel updated"), "success")
    return redirect(url_for("web.channels"))


@web_bp.route("/channels/<int:channel_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_channel(channel_id):
    from models import AIChannel
    ch = AIChannel.query.get_or_404(channel_id)
    db.session.delete(ch)
    db.session.commit()
    flash(_("Channel and its models deleted"), "success")
    return redirect(url_for("web.channels"))


@web_bp.route("/channels/<int:channel_id>/toggle", methods=["POST"])
@login_required
@admin_required
def toggle_channel(channel_id):
    """一键启用/暂停渠道：同时开关该渠道下所有模型。"""
    from models import AIChannel, AIModel
    ch = AIChannel.query.get_or_404(channel_id)
    ch.enabled = not ch.enabled
    AIModel.query.filter_by(channel_id=ch.id).update(
        {AIModel.enabled: ch.enabled}, synchronize_session=False)
    db.session.commit()
    flash(_("Channel enabled") if ch.enabled else _("Channel paused"), "success")
    return redirect(url_for("web.channels"))


@web_bp.route("/channels/<int:channel_id>/fetch-models", methods=["POST"])
@login_required
@admin_required
def fetch_models(channel_id):
    """一键拉取该渠道可用模型（返回 JSON 供前端勾选）。"""
    from flask import jsonify
    from models import AIChannel
    import providers
    ch = AIChannel.query.get_or_404(channel_id)
    try:
        names = providers.list_models(ch.provider, ch.base_url, ch.get_api_key(),
                                      models_endpoint=ch.models_endpoint or "")
        existing = {m.model_name for m in ch.models}
        return jsonify({"ok": True, "models": names, "existing": list(existing)})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)[:200]}), 200


@web_bp.route("/channels/<int:channel_id>/add-models", methods=["POST"])
@login_required
@admin_required
def add_models(channel_id):
    from models import AIChannel, AIModel
    ch = AIChannel.query.get_or_404(channel_id)
    raw = request.form.get("models") or ""
    names = [n.strip() for n in raw.replace("\n", ",").split(",") if n.strip()]
    existing = {m.model_name for m in ch.models}
    added = 0
    for n in names:
        if n in existing:
            continue
        db.session.add(AIModel(channel_id=ch.id, model_name=n[:120]))
        added += 1
    db.session.commit()
    flash(_("Added {n} models").format(n=added), "success")
    return redirect(url_for("web.channels"))


@web_bp.route("/models/<int:model_id>/update", methods=["POST"])
@login_required
@admin_required
def update_model(model_id):
    from models import AIModel
    m = AIModel.query.get_or_404(model_id)

    def _int_or_none(key):
        v = (request.form.get(key) or "").strip()
        return int(v) if v.isdigit() else None

    m.priority = int(request.form.get("priority") or m.priority)
    m.context_window = _int_or_none("context_window")
    m.max_tokens = _int_or_none("max_tokens")
    m.daily_token_limit = _int_or_none("daily_token_limit")
    m.rate_limit_per_min = _int_or_none("rate_limit_per_min")
    m.thinking_mode = bool(request.form.get("thinking_mode"))
    m.enabled = bool(request.form.get("enabled"))
    db.session.commit()
    flash(_("Model {name} updated").format(name=m.model_name), "success")
    return redirect(url_for("web.channels"))


@web_bp.route("/models/<int:model_id>/delete", methods=["POST"])
@login_required
@admin_required
def delete_model(model_id):
    from models import AIModel
    m = AIModel.query.get_or_404(model_id)
    db.session.delete(m)
    db.session.commit()
    flash(_("Model deleted"), "success")
    return redirect(url_for("web.channels"))


@web_bp.route("/models/batch", methods=["POST"])
@login_required
@admin_required
def batch_models():
    """批量操作选中的模型：删除 / 启用 / 禁用。"""
    from models import AIModel
    action = request.form.get("action") or ""
    ids = [int(i) for i in request.form.getlist("model_ids") if i.isdigit()]
    if not ids:
        flash(_("No models selected"), "warning")
        return redirect(url_for("web.channels"))
    q = AIModel.query.filter(AIModel.id.in_(ids))
    if action == "delete":
        n = q.delete(synchronize_session=False)
        msg = _("Deleted {n} models").format(n=n)
    elif action == "enable":
        n = q.update({AIModel.enabled: True}, synchronize_session=False)
        msg = _("Enabled {n} models").format(n=n)
    elif action == "disable":
        n = q.update({AIModel.enabled: False}, synchronize_session=False)
        msg = _("Disabled {n} models").format(n=n)
    else:
        flash(_("Unknown action"), "danger")
        return redirect(url_for("web.channels"))
    db.session.commit()
    flash(msg, "success")
    return redirect(url_for("web.channels"))



@web_bp.route("/prompts")
@login_required
def prompts():
    from models import UserPrompt
    my_prompts = UserPrompt.query.filter_by(user_id=current_user.id).order_by(UserPrompt.id.desc()).all()
    import settings_store
    builtin = settings_store.CATEGORIES
    return render_template("prompts.html", prompts=my_prompts, builtin=builtin)


@web_bp.route("/prompts/create", methods=["POST"])
@login_required
def create_prompt():
    import json
    import settings_store
    from models import UserPrompt

    name = (request.form.get("name") or "").strip()
    labels_raw = request.form.get("labels_json") or "[]"
    extra_prompt = (request.form.get("extra_prompt") or "").strip()

    if not name or len(name) > 80:
        flash(_("Invalid template name"), "danger")
        return redirect(url_for("web.prompts"))

    try:
        labels = json.loads(labels_raw)
        assert isinstance(labels, list) and labels
        clean = []
        for it in labels:
            lab = str(it.get("label", "")).strip()
            if not lab:
                continue
            clean.append({
                "label": lab[:32],
                "definition": str(it.get("definition", "")).strip()[:500],
                "blocked": bool(it.get("blocked")),
            })
        assert clean
    except Exception:
        flash(_("Invalid label set; at least one valid label is required"), "danger")
        return redirect(url_for("web.prompts"))

    # 消耗提示词配额（防刷）
    if current_user.prompt_quota <= 0:
        flash(_("Prompt submission quota exhausted, contact admin"), "danger")
        return redirect(url_for("web.prompts"))

    # 组合待审核文本：标签定义 + 追加引导语
    audit_text = "; ".join(f"{c['label']}={c['definition']}" for c in clean)
    if extra_prompt:
        audit_text += "\nExtra: " + extra_prompt

    # 先落库为「审核中」并扣减配额，立即返回，避免同步等待思考审核导致网关超时(524)
    current_user.prompt_quota = max(0, current_user.prompt_quota - 1)
    p = UserPrompt(
        user_id=current_user.id, name=name,
        labels_json=json.dumps(clean, ensure_ascii=False),
        extra_prompt=extra_prompt, audit_status="pending", audit_note="AI reviewing...",
    )
    db.session.add(p)
    db.session.commit()

    # 后台异步做 intern 思考模式恶意检测，完成后更新状态
    _spawn_prompt_audit(current_app._get_current_object(), p.id, audit_text)

    flash(_("Template \"{name}\" submitted for AI review; refresh later to see the result").format(name=name), "success")
    return redirect(url_for("web.prompts"))


def _spawn_prompt_audit(app, prompt_id: int, audit_text: str):
    """后台线程：对自定义提示词做恶意检测并更新审核状态。"""
    import threading
    from models import UserPrompt

    def worker():
        with app.app_context():
            verdict = "unknown"
            gpt = getattr(fighter, "gpt", None)
            if gpt is not None:
                try:
                    verdict = gpt.check_prompt_safety(audit_text).get("verdict", "unknown")
                except Exception:
                    verdict = "unknown"
            status = "approved" if verdict == "safe" else (
                "rejected" if verdict == "malicious" else "approved")
            note = {"safe": "AI review passed",
                    "malicious": "AI flagged malicious intent; rejected",
                    "unknown": "AI review unavailable; allowed by default"}.get(verdict, "")
            # 重试提交，避免 SQLite 多线程写锁导致状态卡在 pending
            import time as _t
            for _attempt in range(5):
                try:
                    p = UserPrompt.query.get(prompt_id)
                    if p:
                        p.audit_status = status
                        p.audit_note = note
                        db.session.commit()
                    break
                except Exception:
                    db.session.rollback()
                    _t.sleep(0.3)

    t = threading.Thread(target=worker, daemon=True)
    t.start()


@web_bp.route("/prompts/<int:prompt_id>/delete", methods=["POST"])
@login_required
def delete_prompt(prompt_id: int):
    from models import UserPrompt
    p = UserPrompt.query.get_or_404(prompt_id)
    if p.user_id != current_user.id and not current_user.is_admin:
        flash(_("Not authorized"), "danger")
        return redirect(url_for("web.prompts"))
    db.session.delete(p)
    db.session.commit()
    flash(_("Template deleted"), "success")
    return redirect(url_for("web.prompts"))


@web_bp.route("/keys")
@login_required
def keys():
    my_keys = ApiKey.query.filter_by(user_id=current_user.id).all()
    return render_template("keys.html", keys=my_keys)


@web_bp.route("/keys/create", methods=["POST"])
@login_required
def create_key():
    if len(current_user.api_keys) >= 5:
        flash(_("Up to 5 API keys per user"), "warning")
        return redirect(url_for("web.keys"))

    key = ApiKey.generate(current_user.id)
    db.session.add(key)
    db.session.commit()
    flash(_("API key generated: {key} (shown once, please save)").format(key=key._plain_key), "success")
    return redirect(url_for("web.keys"))


@web_bp.route("/keys/<int:key_id>/revoke", methods=["POST"])
@login_required
def revoke_key(key_id: int):
    key = ApiKey.query.get_or_404(key_id)
    if key.user_id != current_user.id and not current_user.is_admin:
        flash(_("Not authorized"), "danger")
        return redirect(url_for("web.keys"))
    db.session.delete(key)
    db.session.commit()
    flash(_("API key revoked"), "success")
    return redirect(url_for("web.keys"))


@web_bp.route("/logs")
@login_required
def logs():
    query = DetectionLog.query
    if not current_user.is_admin:
        query = query.filter_by(user_id=current_user.id)
    logs_list = query.order_by(DetectionLog.created_at.desc()).limit(150).all()
    return render_template("logs.html", logs=logs_list)


@web_bp.route("/dashboard")
@login_required
@admin_required
def dashboard():
    import settings_store
    data = settings_store.get_dashboard()
    return render_template("dashboard.html", data=data)


@web_bp.route("/settings", methods=["GET", "POST"])
@login_required
@admin_required
def settings():
    import settings_store
    from forms import SettingsForm
    form = SettingsForm()
    if form.validate_on_submit():
        settings_store.save_settings({
            "system_prompt": form.system_prompt.data or "",
            "fail_open": "1" if form.fail_open.data else "0",
            "fallback_allow": "1" if form.fallback_allow.data else "0",
            "default_max_tokens": str(form.default_max_tokens.data or 2048),
            "log_keep_per_user": str(form.log_keep_per_user.data or 150),
            "demo_enabled": "1" if form.demo_enabled.data else "0",
        })
        flash(_("Settings saved and applied immediately"), "success")
        return redirect(url_for("web.settings"))

    # GET：用当前值填充表单
    cur = settings_store.get_all()
    if request.method == "GET":
        form.system_prompt.data = cur.get("system_prompt", "")
        form.fail_open.data = str(cur.get("fail_open", "0")).lower() in ("1", "true", "yes")
        form.fallback_allow.data = str(cur.get("fallback_allow", "0")).lower() in ("1", "true", "yes")
        form.default_max_tokens.data = int(float(cur.get("default_max_tokens", 2048)))
        form.log_keep_per_user.data = int(float(cur.get("log_keep_per_user", 150)))
        form.demo_enabled.data = str(cur.get("demo_enabled", "0")).lower() in ("1", "true", "yes")
    return render_template("settings.html", form=form, cur=cur)


@web_bp.route("/settings/site", methods=["POST"])
@login_required
@admin_required
def save_site_settings():
    """保存站点品牌、注册、人机验证、SMTP 等设置。"""
    import settings_store
    f = request.form
    data = {
        # 站点品牌
        "site_name": (f.get("site_name") or "").strip()[:80] or "Lexfence",
        "site_title": (f.get("site_title") or "").strip()[:120],
        "site_description": (f.get("site_description") or "").strip()[:1000],
        "site_favicon": (f.get("site_favicon") or "").strip()[:500],
        "site_logo": (f.get("site_logo") or "").strip()[:500],
        # 注册
        "registration_enabled": "1" if f.get("registration_enabled") else "0",
        "registration_verify": f.get("registration_verify") if f.get("registration_verify") in ("none", "email", "admin") else "none",
        "register_default_quota": str(_safe_int(f.get("register_default_quota"), 1000)),
        "register_default_max_text": str(_safe_int(f.get("register_default_max_text"), 5000)),
        "register_default_prompt_quota": str(_safe_int(f.get("register_default_prompt_quota"), 10)),
        # SMTP
        "smtp_host": (f.get("smtp_host") or "").strip()[:255],
        "smtp_port": str(_safe_int(f.get("smtp_port"), 587)),
        "smtp_user": (f.get("smtp_user") or "").strip()[:255],
        "smtp_from": (f.get("smtp_from") or "").strip()[:255],
        "smtp_use_tls": "1" if f.get("smtp_use_tls") else "0",
        # 人机验证
        "captcha_enabled": "1" if f.get("captcha_enabled") else "0",
        "captcha_type": f.get("captcha_type") if f.get("captcha_type") in ("image", "turnstile", "hcaptcha", "recaptcha") else "image",
        "turnstile_site_key": (f.get("turnstile_site_key") or "").strip()[:255],
        "hcaptcha_site_key": (f.get("hcaptcha_site_key") or "").strip()[:255],
        "recaptcha_site_key": (f.get("recaptcha_site_key") or "").strip()[:255],
    }
    # 敏感字段仅在填写了新值时更新（留空保留原值）
    if (f.get("smtp_password") or "").strip():
        data["smtp_password"] = f.get("smtp_password").strip()
    if (f.get("turnstile_secret_key") or "").strip():
        data["turnstile_secret_key"] = f.get("turnstile_secret_key").strip()
    if (f.get("hcaptcha_secret_key") or "").strip():
        data["hcaptcha_secret_key"] = f.get("hcaptcha_secret_key").strip()
    if (f.get("recaptcha_secret_key") or "").strip():
        data["recaptcha_secret_key"] = f.get("recaptcha_secret_key").strip()

    settings_store.save_settings(data)
    flash(_("Settings saved and applied immediately"), "success")
    return redirect(url_for("web.settings"))


def _safe_int(v, fallback):
    try:
        return int(float(v))
    except (TypeError, ValueError):
        return fallback


@web_bp.route("/updates")
@login_required
@admin_required
def updates():
    """系统更新检测页：显示当前/最新版本、更新日志、Docker 更新指引。"""
    import update_checker
    import config as _cfg
    import settings_store
    force = request.args.get("refresh") == "1"
    info = update_checker.check(force=force)
    return render_template("updates.html", info=info,
                           docker_image=_cfg.DOCKER_IMAGE,
                           github_url=_cfg.GITHUB_URL,
                           github_proxy=settings_store.get_setting("github_proxy", ""))


@web_bp.route("/updates/proxy", methods=["POST"])
@login_required
@admin_required
def save_update_proxy():
    import settings_store
    proxy = (request.form.get("github_proxy") or "").strip()[:255]
    import providers
    if not providers.is_safe_public_url(proxy):
        flash(_("URL must be a public http(s) address (private/loopback addresses are not allowed)"), "danger")
        return redirect(url_for("web.updates"))
    settings_store.save_settings({"github_proxy": proxy})
    flash(_("Settings saved and applied immediately"), "success")
    return redirect(url_for("web.updates"))
