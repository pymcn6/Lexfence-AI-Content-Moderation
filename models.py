# -*- coding: utf-8 -*-
"""数据库模型：用户、API Key、调用日志。"""

import secrets
from datetime import datetime

from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin
from sqlalchemy import update
from werkzeug.security import check_password_hash, generate_password_hash

db = SQLAlchemy()


class User(UserMixin, db.Model):
    __tablename__ = "users"

    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False, index=True)
    password_hash = db.Column(db.String(255), nullable=False)
    is_admin = db.Column(db.Boolean, default=False, nullable=False)

    # 邮箱（注册功能用，可空以兼容旧数据；唯一约束允许多个 NULL）
    email = db.Column(db.String(255), nullable=True, unique=True, index=True)
    email_verified = db.Column(db.Boolean, default=False, nullable=False)

    # 配额：quota 表示当月剩余次数，monthly_quota 表示每月初始额度
    quota = db.Column(db.Integer, default=1000, nullable=False)
    monthly_quota = db.Column(db.Integer, default=1000, nullable=False)
    quota_reset_at = db.Column(db.DateTime, nullable=True)

    # 单次 API 请求允许的最大文本长度
    max_text_length = db.Column(db.Integer, default=5000, nullable=False)

    # 自定义提示词提交配额（每月，防刷；提交一次消耗 1）
    prompt_quota = db.Column(db.Integer, default=10, nullable=False)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)

    api_keys = db.relationship("ApiKey", backref="user", lazy=True, cascade="all, delete-orphan")

    def set_password(self, password: str):
        self.password_hash = generate_password_hash(password)

    def check_password(self, password: str) -> bool:
        return check_password_hash(self.password_hash, password)

    def reset_quota_if_needed(self):
        """按月自动重置配额（基于数据库条件原子更新，避免多 worker 重复重置）。"""
        now = datetime.utcnow()
        month_start = datetime(now.year, now.month, 1)
        result = db.session.execute(
            update(User)
            .where(
                User.id == self.id,
                db.or_(
                    User.quota_reset_at.is_(None),
                    User.quota_reset_at < month_start,
                ),
            )
            .values(quota=User.monthly_quota, quota_reset_at=now)
        )
        db.session.commit()
        if result.rowcount:
            db.session.refresh(self)

    def has_quota(self, amount: int = 1) -> bool:
        self.reset_quota_if_needed()
        return self.quota >= amount

    def consume_quota(self, amount: int = 1) -> bool:
        """原子性扣减配额，避免并发超卖。"""
        self.reset_quota_if_needed()
        result = db.session.execute(
            update(User)
            .where(User.id == self.id, User.quota >= amount)
            .values(quota=User.quota - amount)
        )
        db.session.commit()
        if result.rowcount == 0:
            return False
        # 刷新本地对象，避免页面上显示旧配额
        db.session.refresh(self)
        return True

    def update_quota_settings(self, monthly_quota: int, current_quota: int = None):
        self.monthly_quota = monthly_quota
        self.quota = current_quota if current_quota is not None else monthly_quota
        self.quota_reset_at = datetime.utcnow()
        db.session.add(self)
        db.session.commit()


class ApiKey(db.Model):
    __tablename__ = "api_keys"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    key_hash = db.Column(db.String(255), unique=True, nullable=False)
    key_prefix = db.Column(db.String(20), nullable=False, index=True, server_default="")
    key_preview = db.Column(db.String(20), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)

    _plain_key = None

    @staticmethod
    def generate(user_id: int):
        """生成新的 API Key，仅返回一次明文。"""
        plain = "snf_" + secrets.token_urlsafe(32)
        key = ApiKey(
            user_id=user_id,
            key_hash=generate_password_hash(plain),
            key_prefix=plain[:8],
            key_preview=plain[:8] + "..." + plain[-4:],
        )
        key._plain_key = plain
        return key

    def check_key(self, plain: str) -> bool:
        return check_password_hash(self.key_hash, plain)


class DetectionLog(db.Model):
    __tablename__ = "detection_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    text_preview = db.Column(db.String(200), nullable=False)
    result = db.Column(db.String(20), nullable=False)
    category = db.Column(db.String(20), nullable=False)
    confidence = db.Column(db.Float, nullable=False)
    model = db.Column(db.String(64), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)


class AppSetting(db.Model):
    """热配置：键值对存储，供管理员后台修改并被检测器读取。"""

    __tablename__ = "app_settings"

    key = db.Column(db.String(64), primary_key=True)
    value = db.Column(db.Text, nullable=True)
    updated_at = db.Column(db.DateTime, default=datetime.utcnow, onupdate=datetime.utcnow)


class StatCounter(db.Model):
    """月度统计计数：按 (年月, 维度, 名称) 累加，不受日志裁剪影响。

    kind 取值：
    - 'category'：name 为 normal/porn/political/advertisement/spam/insult
    - 'model'：name 为实际命中的模型名（用于调用次数排行榜）
    """

    __tablename__ = "stat_counters"

    id = db.Column(db.Integer, primary_key=True)
    period = db.Column(db.String(7), nullable=False, index=True)  # YYYY-MM
    kind = db.Column(db.String(16), nullable=False)
    name = db.Column(db.String(64), nullable=False)
    count = db.Column(db.Integer, default=0, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("period", "kind", "name", name="uq_stat_period_kind_name"),
    )


class UserPrompt(db.Model):
    """用户自定义提示词模板（标签集 + 追加引导语）。"""

    __tablename__ = "user_prompts"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=False, index=True)
    name = db.Column(db.String(80), nullable=False)
    # 标签集 JSON：[{"label","definition","blocked"}]
    labels_json = db.Column(db.Text, nullable=False, default="[]")
    extra_prompt = db.Column(db.Text, nullable=True)
    # 审核状态：pending / approved / rejected
    audit_status = db.Column(db.String(16), nullable=False, default="pending")
    audit_note = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    user = db.relationship("User", backref=db.backref("prompts", lazy=True, cascade="all, delete-orphan"))

    def labels(self):
        import json
        try:
            return json.loads(self.labels_json or "[]")
        except Exception:
            return []


class AIChannel(db.Model):
    """AI 渠道：一个服务商接入点（OpenAI / Claude / Gemini / OpenAI 兼容）。"""

    __tablename__ = "ai_channels"

    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(80), nullable=False)
    # 渠道类型：openai / claude / gemini / openai_compatible
    provider = db.Column(db.String(32), nullable=False, default="openai")
    base_url = db.Column(db.String(255), nullable=True)
    # 自定义“获取模型列表”接口（留空则用默认 /models 等推断）
    models_endpoint = db.Column(db.String(255), nullable=True)
    # API 密钥（加密存储）
    api_key_enc = db.Column(db.Text, nullable=True)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    # 渠道级优先级（数字越小越优先）
    priority = db.Column(db.Integer, default=100, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    models = db.relationship("AIModel", backref="channel", lazy=True,
                             cascade="all, delete-orphan")

    def set_api_key(self, plaintext: str):
        from crypto_utils import encrypt_secret
        self.api_key_enc = encrypt_secret(plaintext or "")

    def get_api_key(self) -> str:
        from crypto_utils import decrypt_secret
        return decrypt_secret(self.api_key_enc or "")

    def masked_key(self) -> str:
        from crypto_utils import mask_secret
        return mask_secret(self.get_api_key())


class AIModel(db.Model):
    """渠道下的一个可用模型及其限制配置。"""

    __tablename__ = "ai_models"

    id = db.Column(db.Integer, primary_key=True)
    channel_id = db.Column(db.Integer, db.ForeignKey("ai_channels.id"), nullable=False, index=True)
    model_name = db.Column(db.String(120), nullable=False)
    enabled = db.Column(db.Boolean, default=True, nullable=False)
    # 调用优先级（数字越小越优先；同优先级按渠道优先级）
    priority = db.Column(db.Integer, default=100, nullable=False)

    # 模型上下文长度（参考信息）
    context_window = db.Column(db.Integer, nullable=True)
    # 单次最大输出 token（留空则用后台全局默认）—— 用于根治“思考模型无限输出导致超时”
    max_tokens = db.Column(db.Integer, nullable=True)
    # 每日 token 限额（0/空=不限）
    daily_token_limit = db.Column(db.Integer, nullable=True)
    # 速率限制：每分钟最大请求数（0/空=不限）
    rate_limit_per_min = db.Column(db.Integer, nullable=True)
    # 是否开启思考模式（仅对支持的模型，如 intern）
    thinking_mode = db.Column(db.Boolean, default=False, nullable=False)

    # 运行时状态（被动更新）
    available = db.Column(db.Boolean, default=True, nullable=False)
    last_status = db.Column(db.String(32), nullable=True)        # ok / quota / rate / error
    last_checked_at = db.Column(db.DateTime, nullable=True)
    # 当日已用 token 与所属日期（跨日自动重置）
    used_tokens_today = db.Column(db.Integer, default=0, nullable=False)
    used_date = db.Column(db.String(10), nullable=True)          # YYYY-MM-DD
    # 速率/额度冷却到期时间
    cooldown_until = db.Column(db.DateTime, nullable=True)

    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)

    __table_args__ = (
        db.UniqueConstraint("channel_id", "model_name", name="uq_channel_model"),
    )
