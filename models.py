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

    # 配额（以 Tokens 计）：quota=当月剩余 token，monthly_quota=每月初始 token。
    # 管理员 unlimited_quota=True 时不限量、不扣减。
    quota = db.Column(db.Integer, default=10000, nullable=False)
    monthly_quota = db.Column(db.Integer, default=10000, nullable=False)
    quota_reset_at = db.Column(db.DateTime, nullable=True)
    unlimited_quota = db.Column(db.Boolean, default=False, nullable=False)
    # 累计已用 Tokens（看板「总用量」用；不受账单 15 天清理影响）
    tokens_used_total = db.Column(db.BigInteger, default=0, nullable=False)
    # 累计请求数（看板「总请求数」用；不受账单 15 天清理影响）
    requests_total = db.Column(db.BigInteger, default=0, nullable=False)

    # 单次 API 请求允许的最大文本长度
    max_text_length = db.Column(db.Integer, default=5000, nullable=False)

    # 自定义提示词提交配额（每月，防刷；提交一次消耗 1）
    prompt_quota = db.Column(db.Integer, default=10, nullable=False)

    # 最多可创建的 API Key 数（为空则用后台默认值 default_max_api_keys）
    max_api_keys = db.Column(db.Integer, nullable=True)

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
        if self.unlimited_quota:
            return True
        self.reset_quota_if_needed()
        return self.quota >= amount

    def consume_quota(self, amount: int = 1) -> bool:
        """原子性扣减配额（Tokens），避免并发超卖；管理员无限不扣。"""
        if self.unlimited_quota:
            return True
        if amount <= 0:
            return True
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

    def settle_tokens(self, reserved: int, actual: int):
        """检测结束按实际 token 结算：退还预扣多出的部分，或补扣不足的部分。

        预扣 reserved，已在 consume_quota 中扣除；actual 为 AI 实际消耗。
        - actual < reserved：退还差额（quota 增加）。
        - actual > reserved：补扣差额（quota 减少，允许扣成负数，下次即被拦截）。
        无论是否无限，都累加 tokens_used_total 以供看板统计。
        """
        actual = max(0, int(actual or 0))
        # 累计总用量（所有用户，含管理员）
        try:
            db.session.execute(
                update(User).where(User.id == self.id)
                .values(tokens_used_total=User.tokens_used_total + actual)
            )
            db.session.commit()
        except Exception:
            db.session.rollback()
        if self.unlimited_quota:
            db.session.refresh(self)
            return
        diff = reserved - actual  # >0 退还，<0 补扣
        if diff != 0:
            db.session.execute(
                update(User).where(User.id == self.id)
                .values(quota=User.quota + diff)
            )
            db.session.commit()
        db.session.refresh(self)

    def redeem_tokens(self, amount: int):
        """兑换码充值：增加当月剩余 token（不改 monthly_quota）。"""
        db.session.execute(
            update(User).where(User.id == self.id)
            .values(quota=User.quota + amount)
        )
        db.session.commit()
        db.session.refresh(self)

    def quota_display(self):
        """页面展示用：无限返回 None（模板显示“无限”），否则返回数值。"""
        return None if self.unlimited_quota else self.quota

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
    # 可读名称（便于区分多把 Key 用途）
    name = db.Column(db.String(60), nullable=True)
    key_hash = db.Column(db.String(255), unique=True, nullable=False)
    key_prefix = db.Column(db.String(20), nullable=False, index=True, server_default="")
    key_preview = db.Column(db.String(20), nullable=False)
    # 明文密文（Fernet 加密存储）——用于后台验证密码后“再次查看”完整 Key。
    # 注意：这是相对哈希存储的安全降级，按产品需求开启。
    key_enc = db.Column(db.Text, nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False)
    active = db.Column(db.Boolean, default=True, nullable=False)

    # 有效期（精确到分钟）；为空表示永不过期
    expires_at = db.Column(db.DateTime, nullable=True)
    # 该 Key 累计已用 Tokens / 请求数（不受账单清理影响）
    usage_total = db.Column(db.BigInteger, default=0, nullable=False)
    requests_total = db.Column(db.BigInteger, default=0, nullable=False)
    # 用量限制：每 token_limit_period（year/month/day/hour/minute）最多 token_limit 个 Tokens
    token_limit = db.Column(db.BigInteger, nullable=True)
    token_limit_period = db.Column(db.String(8), nullable=True)
    win_token_used = db.Column(db.BigInteger, default=0, nullable=False)
    win_token_start = db.Column(db.DateTime, nullable=True)
    # 速率限制：每 rate_limit_period 最多 rate_limit 次请求
    rate_limit = db.Column(db.Integer, nullable=True)
    rate_limit_period = db.Column(db.String(8), nullable=True)
    win_req_used = db.Column(db.Integer, default=0, nullable=False)
    win_req_start = db.Column(db.DateTime, nullable=True)

    _plain_key = None

    _PERIODS = ("minute", "hour", "day", "month", "year")

    @staticmethod
    def generate(user_id: int, name: str = None):
        """生成新的 API Key，返回一次明文，同时加密留存以便后续查看。"""
        from crypto_utils import encrypt_secret
        plain = "snf_" + secrets.token_urlsafe(32)
        key = ApiKey(
            user_id=user_id,
            name=(name or "").strip()[:60] or None,
            key_hash=generate_password_hash(plain),
            key_prefix=plain[:8],
            key_preview=plain[:8] + "..." + plain[-4:],
            key_enc=encrypt_secret(plain),
        )
        key._plain_key = plain
        return key

    def check_key(self, plain: str) -> bool:
        return check_password_hash(self.key_hash, plain)

    def reveal(self) -> str:
        """解密返回完整明文 Key；无密文（历史 Key）返回空串。"""
        if not self.key_enc:
            return ""
        try:
            from crypto_utils import decrypt_secret
            return decrypt_secret(self.key_enc) or ""
        except Exception:
            return ""

    def is_expired(self) -> bool:
        return bool(self.expires_at and datetime.utcnow() >= self.expires_at)

    @staticmethod
    def _period_start(period: str, now: datetime) -> datetime:
        """返回 period 当前窗口的起始时间（按自然边界对齐）。"""
        if period == "minute":
            return now.replace(second=0, microsecond=0)
        if period == "hour":
            return now.replace(minute=0, second=0, microsecond=0)
        if period == "day":
            return now.replace(hour=0, minute=0, second=0, microsecond=0)
        if period == "month":
            return now.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
        if period == "year":
            return now.replace(month=1, day=1, hour=0, minute=0, second=0, microsecond=0)
        return now.replace(second=0, microsecond=0)

    def precheck(self, reserve: int):
        """请求前校验：有效期 + 速率窗口 + 用量窗口。

        返回 (ok: bool, reason: str)。会按需滚动窗口并把本次请求计入速率窗口、
        把预扣 token 计入用量窗口（不足时拒绝，不计数）。
        """
        now = datetime.utcnow()
        if self.is_expired():
            return False, "key_expired"

        # 速率限制
        if self.rate_limit and self.rate_limit > 0 and self.rate_limit_period in self._PERIODS:
            start = self._period_start(self.rate_limit_period, now)
            if self.win_req_start is None or self.win_req_start < start:
                self.win_req_start = start
                self.win_req_used = 0
            if self.win_req_used + 1 > self.rate_limit:
                return False, "rate_limit_exceeded"

        # 用量限制（按预扣值判断；实际用量在 record_usage 修正）
        if self.token_limit and self.token_limit > 0 and self.token_limit_period in self._PERIODS:
            tstart = self._period_start(self.token_limit_period, now)
            if self.win_token_start is None or self.win_token_start < tstart:
                self.win_token_start = tstart
                self.win_token_used = 0
            if self.win_token_used + max(0, int(reserve or 0)) > self.token_limit:
                return False, "key_token_limit_exceeded"

        # 通过校验：计速率次数 + 预扣用量
        if self.rate_limit and self.rate_limit > 0 and self.rate_limit_period in self._PERIODS:
            self.win_req_used = (self.win_req_used or 0) + 1
        if self.token_limit and self.token_limit > 0 and self.token_limit_period in self._PERIODS:
            self.win_token_used = (self.win_token_used or 0) + max(0, int(reserve or 0))
        self.requests_total = (self.requests_total or 0) + 1
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
        return True, ""

    def record_usage(self, reserve: int, actual: int):
        """检测结束按实际 token 修正该 Key 的窗口用量与累计用量。"""
        actual = max(0, int(actual or 0))
        diff = actual - max(0, int(reserve or 0))
        self.usage_total = (self.usage_total or 0) + actual
        if self.token_limit and self.token_limit_period in self._PERIODS and diff != 0:
            self.win_token_used = max(0, (self.win_token_used or 0) + diff)
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()


class DetectionLog(db.Model):
    __tablename__ = "detection_logs"

    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    text_preview = db.Column(db.String(200), nullable=False)
    # 媒体类型：text / image / video。旧数据为空视为 text。
    media_type = db.Column(db.String(10), nullable=True)
    result = db.Column(db.String(20), nullable=False)
    category = db.Column(db.String(20), nullable=False)
    confidence = db.Column(db.Float, nullable=False)
    model = db.Column(db.String(64), nullable=True)
    # 本次检测消耗的 Tokens（账单用）
    tokens = db.Column(db.Integer, default=0, nullable=False)
    # 账单详情：检测内容摘要（文本近 100 字 / URL 或 base64 前 50 位）
    detail = db.Column(db.String(200), nullable=True)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)


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


class DetectionTask(db.Model):
    """异步检测任务：客户端提交后立即拿到 task_id，轮询获取结果。

    解决同步 /detect 在思考模型下长时间阻塞、易触发网关超时的问题。
    """

    __tablename__ = "detection_tasks"

    id = db.Column(db.String(36), primary_key=True)  # uuid
    user_id = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True, index=True)
    status = db.Column(db.String(16), nullable=False, default="pending")  # pending/done/error
    media_type = db.Column(db.String(10), nullable=True)  # text/image/video
    result_json = db.Column(db.Text, nullable=True)       # 完成后存检测结果
    error = db.Column(db.String(200), nullable=True)
    reserved_tokens = db.Column(db.Integer, default=0, nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)
    finished_at = db.Column(db.DateTime, nullable=True)


class RedeemCode(db.Model):
    """兑换码：可兑换 token 额度。码为 67 位字符串。"""

    __tablename__ = "redeem_codes"

    id = db.Column(db.Integer, primary_key=True)
    code = db.Column(db.String(80), unique=True, nullable=False, index=True)
    tokens = db.Column(db.Integer, nullable=False)            # 面值（可兑换的 token 数）
    used = db.Column(db.Boolean, default=False, nullable=False, index=True)
    used_by = db.Column(db.Integer, db.ForeignKey("users.id"), nullable=True)
    used_at = db.Column(db.DateTime, nullable=True)
    batch = db.Column(db.String(40), nullable=True)          # 批次标记（便于管理）
    created_at = db.Column(db.DateTime, default=datetime.utcnow, nullable=False, index=True)

    @staticmethod
    def gen_code() -> str:
        """生成 67 位兑换码（去掉易混字符的大小写字母+数字）。"""
        import secrets
        alphabet = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz23456789"
        return "".join(secrets.choice(alphabet) for _ in range(67))



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

    # 支持的输入模态（逗号分隔：text,image,video）。空/None 视为仅 text。
    # 获取模型时若接口返回能力则自动填入，否则默认仅 text，由用户勾选保存。
    modalities = db.Column(db.String(64), nullable=True)

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

    _VALID_MODALITIES = ("text", "image", "video")

    def modality_list(self):
        """返回支持的模态列表；空值回退为仅 ['text']。"""
        raw = (self.modalities or "").strip()
        if not raw:
            return ["text"]
        out = [m.strip() for m in raw.split(",") if m.strip() in self._VALID_MODALITIES]
        return out or ["text"]

    def supports(self, modality: str) -> bool:
        return modality in self.modality_list()

    def set_modalities(self, mods):
        """从列表/可迭代设置模态，去重保序，至少保留 text。"""
        seen = []
        for m in (mods or []):
            m = str(m).strip()
            if m in self._VALID_MODALITIES and m not in seen:
                seen.append(m)
        if "text" not in seen:
            seen.insert(0, "text")
        self.modalities = ",".join(seen)
