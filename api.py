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
    if not plain_key or not plain_key.startswith("snf_"):
        return None
    # 先用 key 前缀缩小候选范围，再逐条做慢哈希校验
    prefix = plain_key[:8] if len(plain_key) >= 8 else plain_key
    candidates = ApiKey.query.filter_by(active=True, key_prefix=prefix).all()
    if not candidates:
        # 仅回退扫描未记录前缀的历史 key，避免全表慢哈希扫描（DoS/计时风险）
        candidates = ApiKey.query.filter_by(active=True, key_prefix="").all()
    for k in candidates:
        if k.check_key(plain_key):
            return k.user
    return None


def _log_detection(user: User, text: str, result: dict):
    import settings_store
    settings_store.record_detection(user.id if user else None, text, result)


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
    user = _authenticate_key(api_key)
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

    if not text or not text.strip():
        return _make_response(True, fmt, 400, error="missing_text")

    max_len = user.max_text_length
    if len(text) > max_len:
        return _make_response(True, fmt, 400, error="text_too_long")

    # 解析使用的标签集与追加引导语
    labels, extra_prompt, custom = _resolve_labels(user, scene, prompt_id, categories)
    if labels == "invalid_prompt":
        return _make_response(True, fmt, 400, error="invalid_prompt_id")

    if not user.consume_quota(1):
        return _make_response(True, fmt, 429, error="quota_exceeded")

    result = fighter.detect(text, labels=labels, extra_prompt=extra_prompt)
    _log_detection(user, text, result)

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
