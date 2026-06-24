# -*- coding: utf-8 -*-
"""API 接口路由。"""

from flask import Blueprint, request, jsonify, make_response
from flask_limiter import Limiter
from flask_limiter.util import get_remote_address

from models import db, ApiKey, User, DetectionLog
from app import fighter, limiter

api_bp = Blueprint("api", __name__)


def _get_api_key() -> str:
    """从 Header 或 Query 参数读取 API Key。"""
    key = request.headers.get("X-API-Key", "").strip()
    if not key:
        key = request.args.get("api_key", "").strip()
    return key


def _authenticate_key(plain_key: str):
    """验证 API Key，返回对应用户；失败返回 None。"""
    k = _authenticate_full(plain_key)
    return k.user if k else None


def _authenticate_full(plain_key: str):
    """验证 API Key，返回 ApiKey 对象（含 user）；失败返回 None。"""
    if not plain_key or not plain_key.startswith("snf_"):
        return None
    prefix = plain_key[:8] if len(plain_key) >= 8 else plain_key
    candidates = ApiKey.query.filter_by(active=True, key_prefix=prefix).all()
    if not candidates:
        candidates = ApiKey.query.filter_by(active=True, key_prefix="").all()
    for k in candidates:
        if k.check_key(plain_key):
            return k
    return None


def _log_detection(user: User, text: str, result: dict):
    import settings_store
    settings_store.record_detection(user.id if user else None, text, result,
                                    tokens=int(result.get("usage_tokens", 0) or 0))


def _log_detection_media(user: User, text: str, result: dict, media_type: str = "text"):
    import settings_store
    settings_store.record_detection(user.id if user else None, text, result, media_type=media_type,
                                    tokens=int(result.get("usage_tokens", 0) or 0))


@api_bp.route("/detect", methods=["GET", "POST"])
@limiter.limit("30 per minute")
def detect():
    """
    检测文本是否为垃圾/违禁内容。

    参数：
    - text: 待检测文本（必填）
    - format: 返回格式，json 或 txt（默认 json）
    - scene: 内置场景，message（消息审核，全检）/ nickname（昵称审核，不查无意义）
    - prompt_id: 使用某个【已审核通过】的自定义提示词模板（自定义标签集）
    - categories: 在内置 6 类中仅检测勾选的子集（逗号分隔）

    认证：Header X-API-Key 或 Query api_key

    返回：
    - 默认返回 {"result": true/false}（true=违规）
    - 指定 prompt_id（自定义标签）时返回 {"result": bool, "label": "命中标签名"}
    """
    api_key = _get_api_key()
    key_obj = _authenticate_full(api_key)
    user = key_obj.user if key_obj else None
    if not user:
        return _make_response(True, "json", 401, error="invalid_api_key")

    if not user.active:
        return _make_response(True, "json", 403, error="user_disabled")

    if request.method == "GET":
        params = request.args
    else:
        params = request.get_json(silent=True) or {}

    text = params.get("text", "")
    fmt = str(params.get("format", "json")).lower()
    scene = str(params.get("scene", "") or "").strip().lower()
    prompt_id = params.get("prompt_id")
    categories = params.get("categories")

    # 多模态：media_type=image/video，配合 image_url / video_url
    media_type = str(params.get("media_type", "text") or "text").strip().lower()
    if media_type not in ("text", "image", "video"):
        media_type = "text"
    image_url = str(params.get("image_url", "") or "").strip()
    video_url = str(params.get("video_url", "") or "").strip()

    import settings_store
    import providers

    if media_type == "text":
        if not text or not text.strip():
            return _make_response(True, fmt, 400, error="missing_text")
        max_len = user.max_text_length
        if len(text) > max_len:
            return _make_response(True, fmt, 400, error="text_too_long")
        image_urls, vurl, log_preview = None, "", text
    else:
        media_url = image_url if media_type == "image" else video_url
        if not media_url:
            return _make_response(True, fmt, 400, error="missing_media_url")
        if not providers.is_safe_public_url(media_url):
            return _make_response(True, fmt, 400, error="invalid_media_url")
        image_urls = [media_url] if media_type == "image" else None
        vurl = media_url if media_type == "video" else ""
        log_preview = media_url

    # 解析使用的标签集与追加引导语（媒体类型用媒体场景标签集）
    if media_type in ("image", "video") and not prompt_id:
        labels, extra_prompt, custom = settings_store.MEDIA_SCENES[media_type], "", False
    else:
        labels, extra_prompt, custom = _resolve_labels(user, scene, prompt_id, categories)
        if labels == "invalid_prompt":
            return _make_response(True, fmt, 400, error="invalid_prompt_id")

    reserve = settings_store.token_reserve(media_type)
    ok, reason = key_obj.precheck(reserve)
    if not ok:
        status = 403 if reason == "key_expired" else 429
        return _make_response(True, fmt, status, error=reason)
    if not user.consume_quota(reserve):
        return _make_response(True, fmt, 429, error="quota_exceeded")

    result = fighter.detect(log_preview if media_type == "text" else "",
                            labels=labels, extra_prompt=extra_prompt,
                            media_type=media_type, image_urls=image_urls, video_url=vurl)
    # 按实际 token 结算（退差/补扣；管理员无限不扣）
    actual = int(result.get("usage_tokens", 0) or 0)
    user.settle_tokens(reserve, actual)
    key_obj.record_usage(reserve, actual)
    _log_detection_media(user, log_preview, result, media_type)

    # 自定义标签集：返回命中的标签名
    if custom:
        if fmt == "txt":
            resp = make_response(result["category"], 200)
            resp.mimetype = "text/plain"
            return resp
        return jsonify({"result": result["is_spam"], "label": result["category"]}), 200

    return _make_response(result["is_spam"], fmt, 200)


def _resolve_labels(user, scene, prompt_id, categories):
    """返回 (labels, extra_prompt, is_custom)。labels=None 表示内置全集。

    优先级：prompt_id（自定义模板） > scene（内置场景） > categories（内置子集）。
    """
    import settings_store
    from models import UserPrompt

    # 1) 自定义提示词模板（必须属于该用户且已审核通过）
    if prompt_id:
        try:
            pid = int(prompt_id)
        except (TypeError, ValueError):
            return "invalid_prompt", "", False
        p = UserPrompt.query.filter_by(id=pid, user_id=user.id, audit_status="approved").first()
        if not p:
            return "invalid_prompt", "", False
        return p.labels(), (p.extra_prompt or ""), True

    # 2) 内置场景
    if scene and scene in settings_store.SCENES:
        return settings_store.SCENES[scene], "", False

    # 3) 内置子集勾选
    if categories:
        if isinstance(categories, str):
            cats = [c.strip() for c in categories.split(",") if c.strip()]
        elif isinstance(categories, (list, tuple)):
            cats = [str(c).strip() for c in categories]
        else:
            cats = []
        cats = [c for c in cats if c in settings_store.CATEGORIES]
        if cats:
            if "normal" not in cats:
                cats = ["normal"] + cats  # 必须保留 normal 作为兜底类别
            return settings_store._builtin_labels(cats), "", False

    # 4) 默认内置全集
    return None, "", False


def _make_response(value: bool, fmt: str, status: int, error: str = None):
    fmt = fmt if fmt in ("txt", "json") else "json"
    if fmt == "txt":
        body = "true" if value else "false"
        resp = make_response(body, status)
        resp.mimetype = "text/plain"
        return resp

    payload = {"result": value}
    if error:
        payload["error"] = error
    return jsonify(payload), status


# ============ 异步检测 API（轮询：submit + result） ============
# 解决同步 /detect 在思考模型下长时间阻塞、易触发网关超时的问题。
# 流程：POST /detect/submit 立即返回 task_id → GET /detect/result?task_id= 轮询。
def _parse_detect_request(user, params):
    """从请求参数解析检测输入；返回 (ok, payload_or_error)。

    payload: dict(media_type, labels, extra_prompt, custom, image_urls, video_url, log_preview)
    """
    import settings_store
    text = params.get("text", "")
    scene = str(params.get("scene", "") or "").strip().lower()
    prompt_id = params.get("prompt_id")
    categories = params.get("categories")
    media_type = str(params.get("media_type", "text") or "text").strip().lower()
    if media_type not in ("text", "image", "video"):
        media_type = "text"
    image_url = str(params.get("image_url", "") or "").strip()
    video_url = str(params.get("video_url", "") or "").strip()

    if media_type == "text":
        if not text or not text.strip():
            return False, "missing_text"
        if len(text) > user.max_text_length:
            return False, "text_too_long"
        image_urls, vurl, log_preview = None, "", text
    else:
        import providers
        media_url = image_url if media_type == "image" else video_url
        if not media_url:
            return False, "missing_media_url"
        if not providers.is_safe_public_url(media_url):
            return False, "invalid_media_url"
        image_urls = [media_url] if media_type == "image" else None
        vurl = media_url if media_type == "video" else ""
        log_preview = media_url

    if media_type in ("image", "video") and not prompt_id:
        labels, extra_prompt, custom = settings_store.MEDIA_SCENES[media_type], "", False
    else:
        labels, extra_prompt, custom = _resolve_labels(user, scene, prompt_id, categories)
        if labels == "invalid_prompt":
            return False, "invalid_prompt_id"

    return True, {
        "media_type": media_type, "labels": labels, "extra_prompt": extra_prompt,
        "custom": custom, "image_urls": image_urls, "video_url": vurl,
        "log_preview": log_preview,
    }


def _post_webhook(url, payload):
    """向调用方回调结果（best-effort，失败静默）。"""
    try:
        import requests
        requests.post(url, json=payload, timeout=8)
    except Exception:
        pass


def _run_task(app, task_id, user_id, p, reserve, callback_url="", key_id=None):
    """后台线程：执行检测、结算 token、写任务结果；可选 webhook 回调。"""
    import json
    from models import db, DetectionTask, User, ApiKey
    with app.app_context():
        try:
            result = fighter.detect(
                p["log_preview"] if p["media_type"] == "text" else "",
                labels=p["labels"], extra_prompt=p["extra_prompt"],
                media_type=p["media_type"], image_urls=p["image_urls"],
                video_url=p["video_url"])
            user = db.session.get(User, user_id)
            actual = int(result.get("usage_tokens", 0) or 0)
            if user:
                user.settle_tokens(reserve, actual)
            if key_id:
                k = db.session.get(ApiKey, key_id)
                if k:
                    k.record_usage(reserve, actual)
            import settings_store
            settings_store.record_detection(user_id, p["log_preview"], result, media_type=p["media_type"],
                                            tokens=actual)
            payload = {"is_spam": result["is_spam"], "label": result["category"],
                       "category": result["category"], "allowed": result["allowed"],
                       "source": result["source"], "used_tokens": int(result.get("usage_tokens", 0) or 0)}
            for _attempt in range(5):
                try:
                    t = db.session.get(DetectionTask, task_id)
                    if t:
                        t.status = "done"; t.result_json = json.dumps(payload, ensure_ascii=False)
                        from datetime import datetime
                        t.finished_at = datetime.utcnow()
                        db.session.commit()
                    break
                except Exception:
                    db.session.rollback()
                    import time as _t; _t.sleep(0.3)
            # webhook：检测完成后向调用方推送结果（best-effort）
            if callback_url:
                _post_webhook(callback_url, {"task_id": task_id, "status": "done", "result": payload})
        except Exception as exc:
            try:
                t = db.session.get(DetectionTask, task_id)
                if t:
                    t.status = "error"; t.error = str(exc)[:200]
                    db.session.commit()
            except Exception:
                db.session.rollback()


@api_bp.route("/detect/submit", methods=["POST"])
@limiter.limit("60 per minute")
def detect_submit():
    """异步提交检测任务，立即返回 task_id（不阻塞等待 AI）。"""
    import uuid, threading
    from flask import current_app
    from models import db, DetectionTask
    import settings_store

    user = _authenticate_key(_get_api_key())
    if not user:
        return jsonify({"ok": False, "error": "invalid_api_key"}), 401
    if not user.active:
        return jsonify({"ok": False, "error": "user_disabled"}), 403

    key_obj = _authenticate_full(_get_api_key())

    params = request.get_json(silent=True) or request.form or {}
    ok, payload = _parse_detect_request(user, params)
    if not ok:
        return jsonify({"ok": False, "error": payload}), 400

    # 可选 webhook 回调地址（仅允许公网 http(s)，防 SSRF）
    callback_url = str(params.get("callback_url", "") or "").strip()
    if callback_url:
        import providers
        if not providers.is_safe_public_url(callback_url):
            return jsonify({"ok": False, "error": "invalid_callback_url"}), 400

    reserve = settings_store.token_reserve(payload["media_type"])
    pok, reason = key_obj.precheck(reserve)
    if not pok:
        return jsonify({"ok": False, "error": reason}), (403 if reason == "key_expired" else 429)
    if not user.consume_quota(reserve):
        return jsonify({"ok": False, "error": "quota_exceeded"}), 429

    task_id = uuid.uuid4().hex
    task = DetectionTask(id=task_id, user_id=user.id, status="pending",
                         media_type=payload["media_type"], reserved_tokens=reserve)
    db.session.add(task)
    db.session.commit()

    threading.Thread(target=_run_task,
                     args=(current_app._get_current_object(), task_id, user.id, payload, reserve, callback_url, key_obj.id),
                     daemon=True).start()
    return jsonify({"ok": True, "task_id": task_id, "status": "pending"}), 200


@api_bp.route("/detect/result", methods=["GET"])
@limiter.limit("240 per minute")
def detect_result():
    """轮询异步检测任务结果。"""
    import json
    from models import DetectionTask
    user = _authenticate_key(_get_api_key())
    if not user:
        return jsonify({"ok": False, "error": "invalid_api_key"}), 401
    task_id = (request.args.get("task_id") or "").strip()
    task = DetectionTask.query.get(task_id) if task_id else None
    if not task or task.user_id != user.id:
        return jsonify({"ok": False, "error": "task_not_found"}), 404
    if task.status == "pending":
        return jsonify({"ok": True, "status": "pending"}), 200
    if task.status == "error":
        return jsonify({"ok": False, "status": "error", "error": task.error or "error"}), 200
    data = json.loads(task.result_json or "{}")
    return jsonify({"ok": True, "status": "done", "result": data}), 200
