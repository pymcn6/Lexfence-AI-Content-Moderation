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
def root():
    """根路径：公开首页（访客与已登录用户都先看到首页）。"""
    import settings_store
    url = settings_store.get_setting("homepage_iframe_url", "") or ""
    return render_template("homepage.html", iframe_url=url)


@web_bp.route("/detect-app")
@login_required
def index():
    from models import UserPrompt
    import settings_store
    my_prompts = UserPrompt.query.filter_by(
        user_id=current_user.id, audit_status="approved"
    ).order_by(UserPrompt.id.desc()).all()
    return render_template("index.html", user_prompts=my_prompts,
                           token_reserve_text=settings_store.token_reserve("text"),
                           token_reserve_image=settings_store.token_reserve("image"),
                           token_reserve_video=settings_store.token_reserve("video"))



@web_bp.route("/detect", methods=["POST"])
@login_required
@limiter.limit("30 per minute")
def web_detect():
    from flask import jsonify
    import settings_store
    import providers
    wants_json = request.headers.get("X-Requested-With") == "XMLHttpRequest"
    media_type = (request.form.get("media_type") or "text").strip().lower()
    if media_type not in ("text", "image", "video"):
        media_type = "text"

    def _err(msg, flash_cat="warning"):
        if wants_json:
            return jsonify({"ok": False, "error": msg}), 200
        flash(msg, flash_cat)
        return redirect(url_for("web.index"))

    # ---------- 解析输入 ----------
    if media_type == "text":
        text = request.form.get("text", "").strip()
        if not text:
            return _err(_("Please enter text"))
        max_len = current_user.max_text_length or current_app.config.get("MAX_TEXT_LENGTH", 5000)
        if len(text) > max_len:
            return _err(_("Text exceeds the length limit (max {n} chars)").format(n=max_len))
        image_urls, video_url, log_preview = None, "", text
    else:
        url = (request.form.get("media_url") or "").strip()
        if not url:
            return _err(_("Please enter a media URL"))
        if not providers.is_safe_public_url(url):
            return _err(_("URL must be a public http(s) address (private/loopback addresses are not allowed)"), "danger")
        if media_type == "image":
            image_urls, video_url = [url], ""
        else:
            image_urls, video_url = None, url
        log_preview = url

    # 解析检测模板（媒体类型用媒体场景标签集）
    labels, extra_prompt, custom, mode = _resolve_web_labels(media_type)

    # Token 预扣（管理员无限不扣）：先按模态预扣估值，AI 返回后按实际用量退差
    reserve = settings_store.token_reserve(media_type)
    if not current_user.consume_quota(reserve):
        return _err(_("Token quota exhausted, cannot detect"), "danger")

    result = fighter.detect(log_preview if media_type == "text" else "",
                            labels=labels, extra_prompt=extra_prompt,
                            media_type=media_type, image_urls=image_urls, video_url=video_url)

    # 按实际 token 结算（退还预扣多出的部分或补扣）
    actual = int(result.get("usage_tokens", 0) or 0)
    current_user.settle_tokens(reserve, actual)

    # 记录检测日志（媒体存 URL）
    settings_store.record_detection(current_user.id, log_preview, result, media_type=media_type, tokens=actual)

    if wants_json:
        return jsonify({"ok": True, "result": result, "used_tokens": actual,
                        "quota": current_user.quota_display()})

    from models import UserPrompt
    my_prompts = UserPrompt.query.filter_by(
        user_id=current_user.id, audit_status="approved"
    ).order_by(UserPrompt.id.desc()).all()
    return render_template("index.html", text=log_preview, result=result,
                           user_prompts=my_prompts, selected_mode=mode, custom_labels=custom)


def _resolve_web_labels(media_type="text"):
    """根据表单的 mode 解析检测标签集。

    text 模式 mode 取值：full（全检，默认）/ scene:nickname / prompt:<id>
    image/video 模式：使用媒体场景标签集，或自定义媒体提示词模板 prompt:<id>
    返回 (labels, extra_prompt, is_custom, mode)
    """
    import settings_store
    from models import UserPrompt

    mode = (request.form.get("mode") or "").strip()

    # 自定义提示词模板（文本/媒体通用）
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

    # 媒体类型：默认用媒体场景标签集
    if media_type in ("image", "video"):
        return settings_store.MEDIA_SCENES[media_type], "", False, (mode or media_type)

    # 文本：内置场景
    if mode.startswith("scene:"):
        scene = mode.split(":", 1)[1]
        if scene in settings_store.SCENES:
            return settings_store.SCENES[scene], "", False, mode

    return None, "", False, "full"


@web_bp.route("/users")
@login_required
@admin_required
def users():
    users_list = User.query.all()
    form = UserForm()
    return render_template("users.html", users=users_list, form=form)


@web_bp.route("/users/<int:user_id>/detail")
@login_required
@admin_required
def user_detail(user_id: int):
    """用户详情（JSON）：注册时间、全部/已用 Tokens、总请求数、今日请求数。"""
    from flask import jsonify
    import settings_store
    u = User.query.get_or_404(user_id)
    return jsonify({"ok": True, **settings_store.get_user_detail(u)})


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
        # 最多可创建的 API Key 数（留空=使用后台默认值）
        mak = (request.form.get("max_api_keys") or "").strip()
        if mak == "":
            user.max_api_keys = None
        else:
            try:
                v = int(float(mak))
                user.max_api_keys = v if 0 <= v <= 1000 else user.max_api_keys
            except (TypeError, ValueError):
                pass
        # 注册信息：邮箱（留空=清除；需唯一）
        if "email" in request.form:
            email = (request.form.get("email") or "").strip()[:255]
            if not email:
                user.email = None
            elif User.query.filter(User.email == email, User.id != user.id).first():
                flash(_("Email already in use"), "danger")
                return redirect(url_for("web.users"))
            else:
                user.email = email
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
            "paid": True, "modalities": m.modality_list(),
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
        # 同时给出每个模型的推断能力（前端可预勾选）
        caps = {n: providers.infer_modalities(n) for n in names}
        return jsonify({"ok": True, "models": names, "existing": list(existing), "caps": caps})
    except Exception as exc:
        return jsonify({"ok": False, "error": str(exc)[:200]}), 200


@web_bp.route("/channels/<int:channel_id>/add-models", methods=["POST"])
@login_required
@admin_required
def add_models(channel_id):
    from models import AIChannel, AIModel
    import providers
    ch = AIChannel.query.get_or_404(channel_id)
    raw = request.form.get("models") or ""
    names = [n.strip() for n in raw.replace("\n", ",").split(",") if n.strip()]
    existing = {m.model_name for m in ch.models}
    added = 0
    for n in names:
        if n in existing:
            continue
        mdl = AIModel(channel_id=ch.id, model_name=n[:120])
        # 默认按模型名推断能力（用户之后可在弹窗里改）
        mdl.set_modalities(providers.infer_modalities(n))
        db.session.add(mdl)
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
    # 支持的输入模态（复选框 name=modality，可多选；至少 text）
    m.set_modalities(request.form.getlist("modality"))
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
    media_builtin = settings_store.MEDIA_CATEGORIES
    return render_template("prompts.html", prompts=my_prompts,
                           builtin=builtin, media_builtin=media_builtin)


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
    import settings_store
    my_keys = ApiKey.query.filter_by(user_id=current_user.id).all()
    return render_template("keys.html", keys=my_keys,
                           max_keys=settings_store.user_max_api_keys(current_user),
                           contact_info=settings_store.get_setting("contact_info", ""))


@web_bp.route("/keys/create", methods=["POST"])
@login_required
def create_key():
    import settings_store
    max_keys = settings_store.user_max_api_keys(current_user)
    if len(current_user.api_keys) >= max_keys:
        contact = (settings_store.get_setting("contact_info", "") or "").strip()
        msg = _("You can create up to {n} API keys").format(n=max_keys)
        if contact:
            msg += " — " + _("to create more, please contact {contact}").format(contact=contact)
        flash(msg, "warning")
        return redirect(url_for("web.keys"))

    name = (request.form.get("name") or "").strip()[:60]
    key = ApiKey.generate(current_user.id, name=name)
    _apply_key_limits(key, request.form)
    db.session.add(key)
    db.session.commit()
    flash(_("API key generated: {key} (shown once, please save)").format(key=key._plain_key), "success")
    return redirect(url_for("web.keys"))


def _apply_key_limits(key, form):
    """从表单解析并应用 Key 的用量/速率/有效期限制（带边界校验）。"""
    from datetime import datetime
    periods = ("minute", "hour", "day", "month", "year")

    def _posint(name, lo=1, hi=10**15):
        raw = (form.get(name) or "").strip()
        if not raw:
            return None
        try:
            v = int(float(raw))
        except (TypeError, ValueError):
            return None
        if v < lo or v > hi:
            return None
        return v

    tl = _posint("token_limit")
    tlp = (form.get("token_limit_period") or "").strip().lower()
    key.token_limit = tl if (tl and tlp in periods) else None
    key.token_limit_period = tlp if key.token_limit else None

    rl = _posint("rate_limit", hi=10**9)
    rlp = (form.get("rate_limit_period") or "").strip().lower()
    key.rate_limit = rl if (rl and rlp in periods) else None
    key.rate_limit_period = rlp if key.rate_limit else None

    exp = (form.get("expires_at") or "").strip()
    key.expires_at = None
    if exp:
        for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d %H:%M", "%Y-%m-%d"):
            try:
                key.expires_at = datetime.strptime(exp, fmt)
                break
            except ValueError:
                continue


@web_bp.route("/keys/<int:key_id>/limits", methods=["POST"])
@login_required
def update_key_limits(key_id: int):
    """修改 Key 的用量/速率/有效期限制。"""
    key = ApiKey.query.get_or_404(key_id)
    if key.user_id != current_user.id and not current_user.is_admin:
        flash(_("Not authorized"), "danger")
        return redirect(url_for("web.keys"))
    _apply_key_limits(key, request.form)
    db.session.commit()
    flash(_("API key limits updated"), "success")
    return redirect(url_for("web.keys"))


@web_bp.route("/keys/<int:key_id>/reveal", methods=["POST"])
@login_required
def reveal_key(key_id: int):
    """验证当前账户密码后返回完整明文 Key（JSON）。"""
    from flask import jsonify
    key = ApiKey.query.get_or_404(key_id)
    if key.user_id != current_user.id and not current_user.is_admin:
        return jsonify({"ok": False, "error": _("Not authorized")}), 403
    password = request.form.get("password") or ""
    if not current_user.check_password(password):
        return jsonify({"ok": False, "error": _("Incorrect password")}), 200
    plain = key.reveal()
    if not plain:
        return jsonify({"ok": False, "error": _("This key was created before view support and cannot be shown. Please regenerate.")}), 200
    return jsonify({"ok": True, "key": plain, "name": key.name or ""})


@web_bp.route("/keys/<int:key_id>/rename", methods=["POST"])
@login_required
def rename_key(key_id: int):
    """验证密码后修改 Key 名称。"""
    from flask import jsonify
    key = ApiKey.query.get_or_404(key_id)
    if key.user_id != current_user.id and not current_user.is_admin:
        return jsonify({"ok": False, "error": _("Not authorized")}), 403
    password = request.form.get("password") or ""
    if not current_user.check_password(password):
        return jsonify({"ok": False, "error": _("Incorrect password")}), 200
    key.name = (request.form.get("name") or "").strip()[:60] or None
    db.session.commit()
    return jsonify({"ok": True, "name": key.name or ""})


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


@web_bp.route("/redeem", methods=["GET", "POST"])
@login_required
def redeem():
    """用户兑换码兑换 token。"""
    from models import RedeemCode
    from datetime import datetime
    from sqlalchemy import update
    if request.method == "POST":
        code = (request.form.get("code") or "").strip()
        rc = RedeemCode.query.filter_by(code=code).first() if code else None
        if not rc:
            flash(_("Invalid redeem code"), "danger")
        elif rc.used:
            flash(_("This code has already been used"), "danger")
        else:
            # 原子标记已用，避免并发重复兑换
            from models import db as _db
            updated = _db.session.execute(
                update(RedeemCode).where(RedeemCode.id == rc.id, RedeemCode.used.is_(False))
                .values(used=True, used_by=current_user.id, used_at=datetime.utcnow())
            )
            _db.session.commit()
            if updated.rowcount:
                current_user.redeem_tokens(rc.tokens)
                flash(_("Redeemed {n} tokens successfully").format(n=rc.tokens), "success")
            else:
                flash(_("This code has already been used"), "danger")
        return redirect(url_for("web.redeem"))
    return render_template("redeem.html")


@web_bp.route("/admin/redeem")
@login_required
@admin_required
def redeem_admin():
    """兑换码管理（分页在前端处理或服务端分页）。"""
    from models import RedeemCode
    codes = RedeemCode.query.order_by(RedeemCode.id.desc()).all()
    total = len(codes)
    used = sum(1 for c in codes if c.used)
    return render_template("redeem_admin.html", codes=codes, total=total, used=used)


@web_bp.route("/admin/redeem/generate", methods=["POST"])
@login_required
@admin_required
def redeem_generate():
    """批量生成兑换码：指定面值 token 与数量。"""
    from models import RedeemCode, db as _db
    from datetime import datetime
    try:
        tokens = int(request.form.get("tokens") or 0)
        count = int(request.form.get("count") or 0)
    except ValueError:
        flash(_("Invalid input"), "danger")
        return redirect(url_for("web.redeem_admin"))
    if tokens <= 0 or count <= 0 or count > 10000:
        flash(_("Token value must be > 0 and count between 1 and 10000"), "danger")
        return redirect(url_for("web.redeem_admin"))
    batch = datetime.utcnow().strftime("%Y%m%d%H%M%S")
    existing = {c.code for c in RedeemCode.query.with_entities(RedeemCode.code).all()}
    added = 0
    for _i in range(count):
        for _retry in range(5):
            code = RedeemCode.gen_code()
            if code not in existing:
                existing.add(code)
                _db.session.add(RedeemCode(code=code, tokens=tokens, batch=batch))
                added += 1
                break
    _db.session.commit()
    flash(_("Generated {n} redeem codes").format(n=added), "success")
    return redirect(url_for("web.redeem_admin"))


@web_bp.route("/admin/redeem/import", methods=["POST"])
@login_required
@admin_required
def redeem_import():
    """批量导入兑换码：txt（每行 "码 面值"）或 json（[{"code","tokens"}]）。查重。"""
    import json as _json
    from models import RedeemCode, db as _db
    raw = (request.form.get("data") or "").strip()
    if not raw:
        flash(_("No data to import"), "warning")
        return redirect(url_for("web.redeem_admin"))
    pairs = []  # (code, tokens)
    try:
        if raw.lstrip().startswith("[") or raw.lstrip().startswith("{"):
            data = _json.loads(raw)
            if isinstance(data, dict):
                data = [data]
            for it in data:
                c = str(it.get("code", "")).strip()
                t = int(it.get("tokens", 0))
                if c and t > 0:
                    pairs.append((c, t))
        else:
            for line in raw.splitlines():
                line = line.strip()
                if not line:
                    continue
                parts = line.split()
                if len(parts) >= 2 and parts[1].isdigit():
                    pairs.append((parts[0], int(parts[1])))
    except Exception:
        flash(_("Failed to parse import data"), "danger")
        return redirect(url_for("web.redeem_admin"))

    existing = {c.code for c in RedeemCode.query.with_entities(RedeemCode.code).all()}
    seen, added, skipped = set(), 0, 0
    for code, tokens in pairs:
        if code in existing or code in seen:
            skipped += 1
            continue
        seen.add(code)
        _db.session.add(RedeemCode(code=code, tokens=tokens, batch="import"))
        added += 1
    _db.session.commit()
    flash(_("Imported {n} codes, skipped {s} duplicates").format(n=added, s=skipped), "success")
    return redirect(url_for("web.redeem_admin"))


@web_bp.route("/admin/redeem/export", methods=["POST"])
@login_required
@admin_required
def redeem_export():
    """导出勾选的兑换码为 txt（每行 "码 面值"）。"""
    from flask import Response
    from models import RedeemCode
    ids = [int(i) for i in request.form.getlist("code_ids") if i.isdigit()]
    if not ids:
        flash(_("No codes selected"), "warning")
        return redirect(url_for("web.redeem_admin"))
    codes = RedeemCode.query.filter(RedeemCode.id.in_(ids)).all()
    body = "\n".join(f"{c.code} {c.tokens}" for c in codes)
    return Response(body, mimetype="text/plain",
                    headers={"Content-Disposition": "attachment; filename=redeem_codes.txt"})


@web_bp.route("/admin/redeem/delete", methods=["POST"])
@login_required
@admin_required
def redeem_delete():
    """删除勾选的兑换码。"""
    from models import RedeemCode, db as _db
    ids = [int(i) for i in request.form.getlist("code_ids") if i.isdigit()]
    if ids:
        RedeemCode.query.filter(RedeemCode.id.in_(ids)).delete(synchronize_session=False)
        _db.session.commit()
        flash(_("Deleted {n} codes").format(n=len(ids)), "success")
    return redirect(url_for("web.redeem_admin"))


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
def dashboard():
    """个人数据看板：Token 用量统计 + 账单（检测记录，最近 15 天）。所有登录用户可见。"""
    import settings_store
    stats = settings_store.get_user_token_stats(current_user)
    bills = (DetectionLog.query
             .filter_by(user_id=current_user.id)
             .order_by(DetectionLog.created_at.desc())
             .limit(500).all())
    return render_template("dashboard.html", stats=stats, bills=bills,
                           bill_keep_days=settings_store.get_int("bill_keep_days", 15))


@web_bp.route("/bill/<int:log_id>")
@login_required
def bill_detail(log_id: int):
    """账单单条详情（JSON）：检测时间、返回类型、内容摘要、消耗 Tokens。"""
    from flask import jsonify
    log = DetectionLog.query.get_or_404(log_id)
    if log.user_id != current_user.id and not current_user.is_admin:
        return jsonify({"ok": False, "error": _("Not authorized")}), 403
    return jsonify({
        "ok": True,
        "created_at": log.created_at.strftime("%Y-%m-%d %H:%M:%S"),
        "media_type": log.media_type or "text",
        "result": log.result,
        "category": log.category,
        "tokens": log.tokens or 0,
        "detail": log.detail or "",
        "model": log.model or "",
    })


@web_bp.route("/site-data")
@login_required
@admin_required
def site_data():
    """站点数据（原管理员看板）：全站统计。仅管理员可见。"""
    import settings_store
    data = settings_store.get_dashboard()
    return render_template("site_data.html", data=data)


@web_bp.route("/home")
def homepage():
    """公开首页：内嵌后台配置的 iframe；未配置则引导登录。"""
    import settings_store
    url = settings_store.get_setting("homepage_iframe_url", "") or ""
    return render_template("homepage.html", iframe_url=url)


@web_bp.route("/pricing")
def pricing():
    """公开定价页：按后台配置展示每百万 Tokens 单价，支持多货币换算。"""
    import settings_store
    return render_template("pricing.html", pricing=settings_store.get_pricing())



@web_bp.route("/recharge")
def recharge():
    """公开在线充值页：内嵌后台配置的 iframe。"""
    import settings_store
    url = settings_store.get_setting("recharge_iframe_url", "") or ""
    return render_template("recharge.html", iframe_url=url)


@web_bp.route("/api-docs")
@login_required
def api_docs():
    """API 文档页（独立于 API Key 页）：普通 / 轮询 / webhook 三种调用方式。"""
    import settings_store
    base = settings_store.site_base_url(request.url_root)
    return render_template("api_docs.html", base=base)


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
            "token_reserve_text": str(form.token_reserve_text.data or 1000),
            "token_reserve_image": str(form.token_reserve_image.data or 2000),
            "token_reserve_video": str(form.token_reserve_video.data or 8000),
            "bill_keep_days": str(form.bill_keep_days.data or 15),
            "homepage_iframe_url": (form.homepage_iframe_url.data or "").strip(),
            "recharge_iframe_url": (form.recharge_iframe_url.data or "").strip(),
            "pricing_enabled": "1" if form.pricing_enabled.data else "0",
            "pricing_text_per_m": str(form.pricing_text_per_m.data if form.pricing_text_per_m.data is not None else 0),
            "pricing_image_per_m": str(form.pricing_image_per_m.data if form.pricing_image_per_m.data is not None else 0),
            "pricing_video_per_m": str(form.pricing_video_per_m.data if form.pricing_video_per_m.data is not None else 0),
            "pricing_currencies": (form.pricing_currencies.data or "").strip(),
            "pricing_note": (form.pricing_note.data or "").strip(),
            "default_max_api_keys": str(form.default_max_api_keys.data if form.default_max_api_keys.data is not None else 5),
            "contact_info": (form.contact_info.data or "").strip(),
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
        form.token_reserve_text.data = int(float(cur.get("token_reserve_text", 1000)))
        form.token_reserve_image.data = int(float(cur.get("token_reserve_image", 2000)))
        form.token_reserve_video.data = int(float(cur.get("token_reserve_video", 8000)))
        form.bill_keep_days.data = int(float(cur.get("bill_keep_days", 15)))
        form.homepage_iframe_url.data = cur.get("homepage_iframe_url", "")
        form.recharge_iframe_url.data = cur.get("recharge_iframe_url", "")
        form.pricing_enabled.data = str(cur.get("pricing_enabled", "1")).lower() in ("1", "true", "yes")
        form.pricing_text_per_m.data = float(cur.get("pricing_text_per_m", 0) or 0)
        form.pricing_image_per_m.data = float(cur.get("pricing_image_per_m", 0) or 0)
        form.pricing_video_per_m.data = float(cur.get("pricing_video_per_m", 0) or 0)
        form.pricing_currencies.data = cur.get("pricing_currencies", "")
        form.pricing_note.data = cur.get("pricing_note", "")
        form.default_max_api_keys.data = int(float(cur.get("default_max_api_keys", 5) or 5))
        form.contact_info.data = cur.get("contact_info", "")
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
        "site_base_url": (f.get("site_base_url") or "").strip()[:255].rstrip("/"),
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
                           site_base=settings_store.site_base_url(request.url_root),
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
