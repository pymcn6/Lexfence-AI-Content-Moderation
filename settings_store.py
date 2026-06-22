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
    "log_keep_per_user": "150",    # 每用户日志保留条数
    "demo_enabled": "0",           # 体验模式开关：1=开启 /demomode，0=关闭
    # 站点品牌与展示
    "site_name": "Lexfence",       # 站点名称
    "site_title": "Lexfence",      # 浏览器标题 / 顶部标题
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


def get_int(key: str, fallback: int) -> int:
    try:
        return int(float(get_setting(key, fallback)))
    except (TypeError, ValueError):
        return fallback


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


def record_detection(user_id, text: str, result: dict):
    """记录一次检测：写日志 + 累加月度统计 + 裁剪该用户日志。"""
    period = current_period()
    category = (result.get("category", "normal") or "normal")[:64]
    model = ((result.get("details") or {}).get("model") or result.get("source") or "unknown")[:64]

    try:
        # 1) 写检测日志
        log = DetectionLog(
            user_id=user_id,
            text_preview=text[:200],
            result="spam" if result.get("is_spam") else "normal",
            category=category,
            confidence=result.get("confidence", 0.0),
            model=model,
        )
        db.session.add(log)

        # 2) 累加月度统计：类别维度 + 模型维度 + 合规/违规维度
        _incr_counter(period, "category", category)
        _incr_counter(period, "model", model)
        verdict = "compliant" if result.get("allowed") else "violation"
        _incr_counter(period, "verdict", verdict)

        db.session.commit()
    except Exception:
        db.session.rollback()
        return

    # 3) 裁剪该用户日志，仅保留最近 N 条
    try:
        if user_id is not None:
            keep = get_int("log_keep_per_user", 150)
            _trim_user_logs(user_id, keep)
    except Exception:
        db.session.rollback()


def _trim_user_logs(user_id: int, keep: int):
    """删除该用户超出 keep 条的旧日志。"""
    total = DetectionLog.query.filter_by(user_id=user_id).count()
    if total <= keep:
        return
    # 找出第 keep 条的时间界限，删除更早的
    threshold = (
        DetectionLog.query.filter_by(user_id=user_id)
        .order_by(DetectionLog.id.desc())
        .offset(keep - 1)
        .limit(1)
        .first()
    )
    if threshold:
        DetectionLog.query.filter(
            DetectionLog.user_id == user_id,
            DetectionLog.id < threshold.id,
        ).delete(synchronize_session=False)
        db.session.commit()


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
