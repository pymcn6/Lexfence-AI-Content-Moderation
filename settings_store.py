# -*- coding: utf-8 -*-
"""
热配置与统计服务。

- AppSetting：管理员后台可改的运行时配置（提示词、思考模式、兜底策略等），
  带短 TTL 内存缓存，多 worker 各自缓存、保存后失效。
- StatCounter：月度统计累加（按当前年月，天然实现“每月清空”）。
- 检测日志裁剪：每用户仅保留最近 N 条。
"""

import threading
import time
from datetime import datetime

from models import db, AppSetting, StatCounter, DetectionLog


# 检测分类提示词默认值（管理员可在后台覆盖）
DEFAULT_SYSTEM_PROMPT = (
    "You are a very strict and meticulous content safety moderator. Your task is to "
    "classify the user's text and output only one English category word "
    "(normal/porn/political/advertisement/spam/insult), with no explanation, "
    "punctuation, or extra content.\n\n"
    "Category definitions:\n"
    "- normal: Complete, coherent, meaningful normal content (chat, greetings, "
    "questions, product descriptions, normal mixed Chinese-English sentences, "
    "sentences containing URLs or brand names).\n"
    "- porn: Pornography, sexual innuendo, solicitation.\n"
    "- political: Politically sensitive VIOLATING content only: inciting subversion, "
    "splitting the country, attacking or vilifying the state and government, promoting "
    "reactionary/terrorist or other harmful speech. Positive or neutral content does NOT "
    "count (e.g. patriotic content, praising the country, factual political news, normal "
    "political discussion) and must be classified as normal.\n"
    "- advertisement: Commercial advertising and promotion.\n"
    "- spam: Garbage or meaningless content.\n"
    "- insult: Insults, personal attacks, hate speech.\n\n"
    "[Key detection rules — check character by character]:\n"
    "1. Take every continuous letter/number string in the text and judge whether it is a "
    "real, meaningful word (a Chinese word, English word, well-known brand/URL/common "
    "abbreviation).\n"
    "2. If the text contains any [unreadable random string] (e.g. aofsdubdvihsbg, "
    "fawiha5i8sfy, asdkjqwe, random alphanumeric mix), whether standalone or embedded in "
    "normal Chinese/English, classify the whole text as spam.\n"
    "3. Pure number flooding, symbol piling, and meaningless repeated characters are also spam.\n"
    "4. Only classify as normal when the text has NO meaningless gibberish fragment and is "
    "semantically coherent.\n\n"
    "[Examples] (input -> output):\n"
    "Hello, how do I register? -> normal\n"
    "We love our country and our people! -> normal\n"
    "my name is whatever aofsdubdvihsbg -> spam\n"
    "nice weather today asdkjqwe -> spam\n"
    "1111111111111111 -> spam\n"
    "f*** off you idiot -> insult\n"
    "add my wechat abc for free gifts -> advertisement\n\n"
    "Now classify the user's text, output only the category word."
)

# 可后台修改的配置项及默认值（类型由 getter 负责转换）
DEFAULTS = {
    "system_prompt": DEFAULT_SYSTEM_PROMPT,
    "fail_open": "0",              # 全部失败时：0=判违规，1=放行
    "fallback_allow": "0",         # AI 有响应但分类无法识别时归入 fallback：0=拦截，1=放行
    "default_max_tokens": "2048",  # 默认单次最大输出 token（模型可单独覆盖）
    # 各模态检测的 token 预扣估值（检测前预扣，AI 返回后按实际用量退差）
    "token_reserve_text": "1000",
    "token_reserve_image": "2000",
    "token_reserve_video": "8000",
    "log_keep_per_user": "150",    # 每用户日志保留条数
    "bill_keep_days": "15",        # 账单（检测日志）保留天数，超出由定时任务清理
    "homepage_iframe_url": "",     # 首页内嵌 iframe 地址（公开访问；留空则首页跳转到登录）
    "recharge_iframe_url": "",     # 在线充值页内嵌 iframe 地址（公开访问）
    # 定价（商业化）：每百万 Tokens 单价（以基准货币计），多货币按倍率换算
    "pricing_enabled": "1",        # 是否展示定价页
    "pricing_text_per_m": "2",     # 文本检测：每百万 Tokens 单价（基准货币）
    "pricing_image_per_m": "10",   # 图片检测：每百万 Tokens 单价（基准货币）
    "pricing_video_per_m": "50",   # 视频检测：每百万 Tokens 单价（基准货币）
    # 货币表：每行 `代码,符号,倍率`，第一行为基准货币（倍率应为 1）
    "pricing_currencies": "USD,$,1\nCNY,¥,7.2",
    "pricing_note": "",            # 定价页补充说明（可选）
    # API Key 与联系方式
    "default_max_api_keys": "5",   # 普通用户默认最多可创建的 API Key 数
    "contact_info": "",            # 全站联系方式（邮箱/手机号等），用于"如需更多请联系"提示
    "demo_enabled": "0",           # 体验模式开关：1=开启 /demomode，0=关闭
    # 站点品牌与展示
    "site_name": "Lexfence",       # 站点名称
    "site_title": "Lexfence",      # 浏览器标题 / 顶部标题
    "site_base_url": "",           # 站点实际访问地址（用于 API 文档示例等；留空回退到请求地址）
    "site_description": "",        # 首页/登录页介绍文案
    "site_favicon": "",            # 网站图标 URL（留空用内置）
    "site_logo": "",               # 侧边栏/登录页 Logo URL（留空用内置）
    # 注册
    "registration_enabled": "0",   # 开放注册：1=开 0=关
    "registration_verify": "none", # 注册验证方式：none / email
    "register_default_quota": "1000",       # 注册用户默认每月配额
    "register_default_max_text": "5000",    # 注册用户默认单次字数
    "register_default_prompt_quota": "10",  # 注册用户默认提示词配额
    # SMTP（注册邮箱验证用）
    "smtp_host": "",
    "smtp_port": "587",
    "smtp_user": "",
    "smtp_password": "",           # 加密存储（见 get/save 处理）
    "smtp_from": "",
    "smtp_use_tls": "1",
    # 人机验证
    "captcha_enabled": "0",        # 人机验证开关
    "captcha_type": "image",       # image / turnstile / hcaptcha / recaptcha
    "turnstile_site_key": "",
    "turnstile_secret_key": "",    # 加密存储
    "hcaptcha_site_key": "",
    "hcaptcha_secret_key": "",     # 加密存储
    "recaptcha_site_key": "",
    "recaptcha_secret_key": "",    # 加密存储
    # 更新检测
    "github_proxy": "",            # 自定义 GitHub 代理前缀（加速），如 https://ghproxy.com/
}

# 需要加密存储的敏感设置项（保存时加密，读取时解密）
SECRET_KEYS = {"smtp_password", "turnstile_secret_key",
               "hcaptcha_secret_key", "recaptcha_secret_key"}

CATEGORIES = ["normal", "porn", "political", "advertisement", "spam", "insult"]

# 内置场景预置标签集（scene 参数）：name -> labels
# message：消息审核（全检，含 spam）；nickname：昵称审核（不含 spam，避免误判无意义）
from spamnotefighter.gpt_classifier import BUILTIN_LABEL_DEFS as _BLD


def _builtin_labels(keys):
    return [{"label": k, "definition": _BLD[k][0], "blocked": _BLD[k][1]} for k in keys]


SCENES = {
    "message": _builtin_labels(["normal", "porn", "political", "advertisement", "spam", "insult"]),
    "nickname": _builtin_labels(["normal", "porn", "political", "advertisement", "insult"]),
}

# 媒体（图片/视频）内置分类集与定义。图片/视频最可能需要判定的违规维度：
# 色情、政治敏感、暴力血腥、违禁品、广告，正常作为兜底类别。
MEDIA_LABEL_DEFS = {
    "normal": ("Normal, safe image/video content with no violation.", False),
    "porn": ("Pornographic, sexually explicit or suggestive imagery.", True),
    "political": ("Politically sensitive violating imagery (reactionary symbols, "
                  "prohibited flags/leaders, incitement). Neutral/positive content is normal.", True),
    "violence": ("Violence, gore, bloody, terrorist or extremely disturbing imagery.", True),
    "prohibited": ("Prohibited items: weapons, drugs, gambling and similar illegal goods.", True),
    "advertisement": ("Advertising / promotional imagery (logos, QR codes, contact info).", False),
}

MEDIA_CATEGORIES = ["normal", "porn", "political", "violence", "prohibited", "advertisement"]


def _media_labels(keys=None):
    keys = keys or MEDIA_CATEGORIES
    return [{"label": k, "definition": MEDIA_LABEL_DEFS[k][0], "blocked": MEDIA_LABEL_DEFS[k][1]}
            for k in keys if k in MEDIA_LABEL_DEFS]


# 媒体场景默认标签集（图片与视频共用同一套违规维度）
MEDIA_SCENES = {
    "image": _media_labels(),
    "video": _media_labels(),
}

# 各模态默认 token 预扣配置键
_TOKEN_RESERVE_KEYS = {"text": "token_reserve_text", "image": "token_reserve_image", "video": "token_reserve_video"}


def token_reserve(media_type: str) -> int:
    """返回某模态检测前预扣的 token 估值（后台可配，至少 1）。

    检测前先按此值预扣，AI 返回后用实际 usage_tokens 退差/补扣。
    """
    key = _TOKEN_RESERVE_KEYS.get((media_type or "text"), "token_reserve_text")
    return max(1, get_int(key, 1000))


_cache = {}
_cache_ts = 0.0
_cache_ttl = 10.0          # 秒；后台保存后会立即失效
_lock = threading.Lock()


def _load_all() -> dict:
    """读取全部设置（DB 覆盖默认值），带 TTL 缓存。"""
    global _cache, _cache_ts
    now = time.time()
    with _lock:
        if _cache and now - _cache_ts < _cache_ttl:
            return dict(_cache)
    data = dict(DEFAULTS)
    try:
        for row in AppSetting.query.all():
            if row.value is not None:
                data[row.key] = row.value
    except Exception:
        pass
    with _lock:
        _cache = dict(data)
        _cache_ts = now
    return data


def invalidate_cache():
    global _cache_ts
    with _lock:
        _cache_ts = 0.0


def get_setting(key: str, default=None):
    return _load_all().get(key, default if default is not None else DEFAULTS.get(key))


def get_bool(key: str) -> bool:
    return str(get_setting(key, "0")).lower() in ("1", "true", "yes", "on")


def site_base_url(fallback: str = "") -> str:
    """站点实际访问地址：优先后台配置 site_base_url，否则回退到请求地址。

    返回不带结尾斜杠的形式，供 API 文档示例等拼接使用。
    """
    configured = (get_setting("site_base_url", "") or "").strip()
    base = configured or (fallback or "")
    return base.rstrip("/")


def get_int(key: str, fallback: int) -> int:
    try:
        return int(float(get_setting(key, fallback)))
    except (TypeError, ValueError):
        return fallback


def _parse_currencies(raw: str):
    """解析货币表文本：每行 `代码,符号,倍率`。

    返回列表 [{code, symbol, rate}]；非法行跳过；倍率非正数则跳过。
    第一条作为基准货币。解析失败时回退到 USD。
    """
    out = []
    for line in (raw or "").splitlines():
        line = line.strip()
        if not line:
            continue
        parts = [p.strip() for p in line.split(",")]
        if len(parts) < 3:
            continue
        code, symbol, rate_s = parts[0], parts[1], parts[2]
        if not code:
            continue
        try:
            rate = float(rate_s)
        except (TypeError, ValueError):
            continue
        if rate <= 0:
            continue
        out.append({"code": code[:10], "symbol": (symbol or code)[:8], "rate": rate})
    if not out:
        out = [{"code": "USD", "symbol": "$", "rate": 1.0}]
    return out


def get_pricing() -> dict:
    """商业化定价信息：基准货币的每百万 Tokens 单价 + 多货币换算表。

    供定价页渲染。所有金额按 `每百万 Tokens 单价 × 货币倍率` 计算。
    """
    def _f(key, default):
        try:
            v = float(get_setting(key, default))
            return v if v >= 0 else 0.0
        except (TypeError, ValueError):
            return float(default)

    currencies = _parse_currencies(get_setting("pricing_currencies", ""))
    base = currencies[0]
    plans = [
        {"key": "text", "per_m": _f("pricing_text_per_m", 0)},
        {"key": "image", "per_m": _f("pricing_image_per_m", 0)},
        {"key": "video", "per_m": _f("pricing_video_per_m", 0)},
    ]
    return {
        "enabled": get_bool("pricing_enabled"),
        "base_currency": base,
        "currencies": currencies,
        "plans": plans,
        "note": (get_setting("pricing_note", "") or "").strip(),
    }


def get_all() -> dict:
    return _load_all()


def get_secret(key: str) -> str:
    """读取并解密敏感设置（如 smtp_password / turnstile_secret_key）。"""
    raw = get_setting(key, "")
    if not raw:
        return ""
    try:
        from crypto_utils import decrypt_secret
        dec = decrypt_secret(raw)
        return dec if dec else raw  # 兼容历史明文
    except Exception:
        return raw


def save_settings(items: dict):
    """批量保存设置并失效缓存。SECRET_KEYS 中的项加密存储。"""
    from crypto_utils import encrypt_secret
    for key, value in items.items():
        if key in SECRET_KEYS and value:
            value = encrypt_secret(value)
        row = AppSetting.query.get(key)
        if row is None:
            row = AppSetting(key=key, value=value)
            db.session.add(row)
        else:
            row.value = value
    db.session.commit()
    invalidate_cache()


def current_period() -> str:
    return datetime.utcnow().strftime("%Y-%m")


def _incr_counter(period: str, kind: str, name: str, amount: int = 1):
    """原子累加一个计数器（不存在则创建）。"""
    if not name:
        return
    updated = db.session.query(StatCounter).filter_by(
        period=period, kind=kind, name=name
    ).update({StatCounter.count: StatCounter.count + amount})
    if not updated:
        db.session.add(StatCounter(period=period, kind=kind, name=name, count=amount))


def _detail_summary(text: str, media_type: str) -> str:
    """账单详情摘要：文本保留近 100 字；图片/视频 URL 或 base64 保留前 50 位。"""
    s = (text or "")
    if media_type in ("image", "video"):
        return s[:50]
    return s[:100]


def record_detection(user_id, text: str, result: dict, media_type: str = "text", tokens: int = 0):
    """记录一次检测：写日志 + 累加月度统计 + 裁剪该用户日志。

    media_type: text / image / video。图片/视频时 text 传 URL 或文件名摘要。
    tokens: 本次检测实际消耗的 Tokens（账单展示用）。
    """
    period = current_period()
    category = (result.get("category", "normal") or "normal")[:64]
    model = ((result.get("details") or {}).get("model") or result.get("source") or "unknown")[:64]
    mtype = media_type if media_type in ("text", "image", "video") else "text"
    used = max(0, int(tokens or result.get("usage_tokens", 0) or 0))

    try:
        # 1) 写检测日志
        log = DetectionLog(
            user_id=user_id,
            text_preview=text[:200],
            media_type=mtype,
            result="spam" if result.get("is_spam") else "normal",
            category=category,
            confidence=result.get("confidence", 0.0),
            model=model,
            tokens=used,
            detail=_detail_summary(text, mtype),
        )
        db.session.add(log)

        # 2) 累加月度统计：类别维度 + 模型维度 + 合规/违规维度
        _incr_counter(period, "category", category)
        _incr_counter(period, "model", model)
        verdict = "compliant" if result.get("allowed") else "violation"
        _incr_counter(period, "verdict", verdict)

        # 累加用户累计请求数（不受账单清理影响）
        if user_id is not None:
            from models import User
            from sqlalchemy import update as _update
            db.session.execute(
                _update(User).where(User.id == user_id)
                .values(requests_total=User.requests_total + 1)
            )

        db.session.commit()
    except Exception:
        db.session.rollback()
        return

    # 3) 裁剪：按账单保留天数清理（默认 15 天），并兜底限制单用户条数
    try:
        if user_id is not None:
            _trim_user_logs_by_days(user_id, get_int("bill_keep_days", 15))
    except Exception:
        db.session.rollback()


def _trim_user_logs_by_days(user_id: int, days: int):
    """删除该用户超过 N 天的检测日志（账单仅保留最近 N 天）。"""
    from datetime import datetime, timedelta
    if days <= 0:
        return
    cutoff = datetime.utcnow() - timedelta(days=days)
    DetectionLog.query.filter(
        DetectionLog.user_id == user_id,
        DetectionLog.created_at < cutoff,
    ).delete(synchronize_session=False)
    db.session.commit()


def purge_old_logs(days: int = None):
    """全库清理超过 N 天的检测日志（供定时任务调用）。返回删除条数。"""
    from datetime import datetime, timedelta
    days = days if days is not None else get_int("bill_keep_days", 15)
    if days <= 0:
        return 0
    cutoff = datetime.utcnow() - timedelta(days=days)
    try:
        n = DetectionLog.query.filter(DetectionLog.created_at < cutoff).delete(synchronize_session=False)
        db.session.commit()
        return n or 0
    except Exception:
        db.session.rollback()
        return 0


def get_dashboard(period: str = None) -> dict:
    """看板数据：本月总量、各类别、合规/违规、模型调用排行。"""
    period = period or current_period()
    cat_rows = StatCounter.query.filter_by(period=period, kind="category").all()
    model_rows = StatCounter.query.filter_by(period=period, kind="model").all()
    verdict_rows = StatCounter.query.filter_by(period=period, kind="verdict").all()

    cat_counts = {c: 0 for c in CATEGORIES}
    for r in cat_rows:
        cat_counts[r.name] = r.count

    total = sum(cat_counts.values())

    # 合规/违规以 verdict 维度为准（依据每次检测的放行结论，
    # 能正确反映 fallback/advertisement 等按配置放行的情况）；
    # 旧数据无 verdict 记录时回退到“按类别推断”的旧口径。
    verdict_map = {r.name: r.count for r in verdict_rows}
    if verdict_map:
        compliant = verdict_map.get("compliant", 0)
        violation = verdict_map.get("violation", 0)
    else:
        compliant = cat_counts.get("normal", 0) + cat_counts.get("advertisement", 0)
        violation = total - compliant

    model_rank = sorted(
        [{"name": r.name, "count": r.count} for r in model_rows],
        key=lambda x: x["count"], reverse=True,
    )

    return {
        "period": period,
        "total": total,
        "compliant": compliant,
        "violation": violation,
        "categories": cat_counts,
        "model_rank": model_rank,
    }


def get_user_token_stats(user) -> dict:
    """用户数据看板：总用量、剩余、今日已用 Tokens、总请求数、今日请求数。"""
    from datetime import datetime
    now = datetime.utcnow()
    today_start = datetime(now.year, now.month, now.day)
    today_used = (
        db.session.query(db.func.coalesce(db.func.sum(DetectionLog.tokens), 0))
        .filter(DetectionLog.user_id == user.id, DetectionLog.created_at >= today_start)
        .scalar()
    ) or 0
    today_requests = (
        db.session.query(db.func.count(DetectionLog.id))
        .filter(DetectionLog.user_id == user.id, DetectionLog.created_at >= today_start)
        .scalar()
    ) or 0
    return {
        "total_used": int(getattr(user, "tokens_used_total", 0) or 0),
        "remaining": None if user.unlimited_quota else int(user.quota),
        "unlimited": bool(user.unlimited_quota),
        "today_used": int(today_used),
        "total_requests": int(getattr(user, "requests_total", 0) or 0),
        "today_requests": int(today_requests),
    }


def get_user_detail(user) -> dict:
    """管理员查看的单个用户信息：注册时间、全部/已用 Tokens、请求统计。"""
    s = get_user_token_stats(user)
    total_tokens = None if user.unlimited_quota else (int(user.quota) + int(s["total_used"]))
    return {
        "id": user.id,
        "username": user.username,
        "is_admin": bool(user.is_admin),
        "active": bool(user.active),
        "unlimited": bool(user.unlimited_quota),
        "created_at": user.created_at.strftime("%Y-%m-%d %H:%M") if user.created_at else "",
        "remaining": s["remaining"],
        "total_used": s["total_used"],
        "total_tokens": total_tokens,
        "total_requests": s["total_requests"],
        "today_requests": s["today_requests"],
        "max_text_length": user.max_text_length,
        "api_key_count": len(user.api_keys),
        "max_api_keys": user_max_api_keys(user),
    }


def user_max_api_keys(user) -> int:
    """该用户最多可创建的 API Key 数：优先用户级 max_api_keys，否则后台默认值。"""
    v = getattr(user, "max_api_keys", None)
    if v is not None and int(v) >= 0:
        return int(v)
    return get_int("default_max_api_keys", 5)
