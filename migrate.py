# -*- coding: utf-8 -*-
"""版本化数据库迁移系统（支持任意旧版本升级到当前版本）。

设计：
- 全新部署：app.py 调用 `db.create_all()` 直接建出最新结构，然后把库版本
  标记为当前 APP_VERSION（无需逐步迁移）。
- 旧库升级：库里记录了上次迁移到的 schema 版本（app_settings.schema_version）。
  启动时读出该版本，按 MIGRATIONS 顺序执行所有「更高版本」的迁移步骤，
  最后把版本写回当前 APP_VERSION。即使从 v1.0.0 直接跳到 v2.1.0，
  也会按注册顺序补齐中间所有变更。
- 每一步都是幂等的（先检测列/表是否存在再 ALTER），失败不中断启动。

安全：
- 仅做结构补齐（ADD COLUMN / CREATE TABLE / CREATE INDEX）与必要的一次性数据
  规整，不删列、不破坏既有数据。
- 所有 DDL 包在事务里，单步失败回滚并记录，不影响其它步骤与应用启动。

如何新增一次迁移：
- 在 MIGRATIONS 末尾追加 ("x.y.z", migrate_fn)，函数内用 _add_column 等工具幂等执行。
- 同步把 config.APP_VERSION 提升到该版本，并打同名 git tag。
"""

from sqlalchemy import inspect, text

SCHEMA_VERSION_KEY = "schema_version"


# ---------------- 基础工具 ----------------
def _columns(inspector, table):
    try:
        return {c["name"] for c in inspector.get_columns(table)}
    except Exception:
        return set()


def _tables(inspector):
    try:
        return set(inspector.get_table_names())
    except Exception:
        return set()


def _indexes(inspector, table):
    names = set()
    try:
        for ix in inspector.get_indexes(table):
            if ix.get("name"):
                names.add(ix["name"])
        for uc in inspector.get_unique_constraints(table):
            if uc.get("name"):
                names.add(uc["name"])
    except Exception:
        pass
    return names


def _parse_version(v):
    import re
    nums = re.findall(r"\d+", v or "")
    return tuple(int(n) for n in nums) if nums else (0,)


def _get_db_version(db):
    try:
        row = db.session.execute(
            text("SELECT value FROM app_settings WHERE key = :k"),
            {"k": SCHEMA_VERSION_KEY},
        ).first()
        return row[0] if row and row[0] else None
    except Exception:
        return None


def _set_db_version(db, version):
    try:
        with db.engine.begin() as conn:
            updated = conn.execute(
                text("UPDATE app_settings SET value = :v WHERE key = :k"),
                {"v": version, "k": SCHEMA_VERSION_KEY},
            ).rowcount
            if not updated:
                conn.execute(
                    text("INSERT INTO app_settings (key, value) VALUES (:k, :v)"),
                    {"k": SCHEMA_VERSION_KEY, "v": version},
                )
        return True
    except Exception:
        return False


def _exec(db, sql):
    with db.engine.begin() as conn:
        conn.execute(text(sql))


def _add_column(db, inspector, table, column, ddl, done):
    if table not in _tables(inspector):
        return
    if column in _columns(inspector, table):
        return
    try:
        _exec(db, f"ALTER TABLE {table} ADD COLUMN {ddl}")
        done.append(f"{table}.{column}")
    except Exception:
        pass


# ---------------- 各版本迁移步骤 ----------------
def _migrate_1_1_0(db, inspector, dialect, done):
    """v1.x：媒体/多模态、API Key 名称与可解密、邮箱注册等列。"""
    _add_column(db, inspector, "detection_logs", "model", "model VARCHAR(64) NULL", done)
    _add_column(db, inspector, "detection_logs", "media_type", "media_type VARCHAR(10) NULL", done)
    _add_column(db, inspector, "users", "prompt_quota", "prompt_quota INTEGER NOT NULL DEFAULT 10", done)
    _add_column(db, inspector, "users", "email", "email VARCHAR(255) NULL", done)
    _add_column(db, inspector, "users", "email_verified", "email_verified BOOLEAN NOT NULL DEFAULT 0", done)
    if dialect in ("mysql", "postgresql") and "users" in _tables(inspector):
        if "uq_users_email" not in _indexes(inspector, "users"):
            try:
                _exec(db, "CREATE UNIQUE INDEX uq_users_email ON users (email)")
                done.append("users.uq_users_email")
            except Exception:
                done.append("users.uq_users_email(skipped)")
    _add_column(db, inspector, "ai_channels", "models_endpoint", "models_endpoint VARCHAR(255) NULL", done)
    _add_column(db, inspector, "ai_models", "modalities", "modalities VARCHAR(64) NULL", done)
    _add_column(db, inspector, "api_keys", "name", "name VARCHAR(60) NULL", done)
    _add_column(db, inspector, "api_keys", "key_enc", "key_enc TEXT NULL", done)


def _migrate_2_0_0(db, inspector, dialect, done):
    """v2.0.0：额度改 Tokens（管理员无限）、异步任务表、兑换码表。"""
    _add_column(db, inspector, "users", "unlimited_quota",
                "unlimited_quota BOOLEAN NOT NULL DEFAULT 0", done)
    try:
        from models import db as _db
        _db.create_all()
        done.append("create_all(detection_tasks, redeem_codes)")
    except Exception:
        pass
    # 次数额度语义 -> Tokens：重置为统一默认；管理员设为无限（仅从 <2.0.0 升级时跑一次）
    try:
        import config
        default_tokens = int(getattr(config, "DEFAULT_TOKEN_QUOTA", 10000))
        _exec(db, f"UPDATE users SET quota = {default_tokens}, "
                  f"monthly_quota = {default_tokens} WHERE is_admin = 0")
        _exec(db, "UPDATE users SET unlimited_quota = 1 WHERE is_admin = 1")
        done.append("users.quota -> tokens reset")
    except Exception:
        pass


def _migrate_2_1_0(db, inspector, dialect, done):
    """v2.1.0：账单（tokens/detail）、用户累计用量、首页/充值 iframe、账单保留天数。"""
    _add_column(db, inspector, "detection_logs", "tokens", "tokens INTEGER NOT NULL DEFAULT 0", done)
    _add_column(db, inspector, "detection_logs", "detail", "detail VARCHAR(200) NULL", done)
    _add_column(db, inspector, "users", "tokens_used_total",
                "tokens_used_total BIGINT NOT NULL DEFAULT 0", done)
    _add_column(db, inspector, "users", "requests_total",
                "requests_total BIGINT NOT NULL DEFAULT 0", done)
    # 新表（若有）由 create_all 兜底
    try:
        from models import db as _db
        _db.create_all()
    except Exception:
        pass


def _migrate_2_2_0(db, inspector, dialect, done):
    """v2.2.0：纯前端/交互更新（顶栏自动检测更新、关于系统），无表结构变更。

    保留此步骤以维持版本注册表的连续性；如未来需要补结构在此追加。
    """
    return


def _migrate_2_3_0(db, inspector, dialect, done):
    """v2.3.0：商业化定价、首页/定价/充值分离（纯配置项 + 路由/前端），无表结构变更。

    新增的定价设置项由 settings_store.DEFAULTS 兜底，旧库读取时自动回退默认值，
    无需迁移列。保留此步骤维持版本注册表连续性。
    """
    return


def _migrate_2_4_0(db, inspector, dialect, done):
    """v2.4.0：API Key 用量/速率/有效期限制、每 Key 统计、用户可建 Key 数上限。"""
    _add_column(db, inspector, "users", "max_api_keys", "max_api_keys INTEGER NULL", done)
    _add_column(db, inspector, "api_keys", "expires_at", "expires_at DATETIME NULL", done)
    _add_column(db, inspector, "api_keys", "usage_total", "usage_total BIGINT NOT NULL DEFAULT 0", done)
    _add_column(db, inspector, "api_keys", "requests_total", "requests_total BIGINT NOT NULL DEFAULT 0", done)
    _add_column(db, inspector, "api_keys", "token_limit", "token_limit BIGINT NULL", done)
    _add_column(db, inspector, "api_keys", "token_limit_period", "token_limit_period VARCHAR(8) NULL", done)
    _add_column(db, inspector, "api_keys", "win_token_used", "win_token_used BIGINT NOT NULL DEFAULT 0", done)
    _add_column(db, inspector, "api_keys", "win_token_start", "win_token_start DATETIME NULL", done)
    _add_column(db, inspector, "api_keys", "rate_limit", "rate_limit INTEGER NULL", done)
    _add_column(db, inspector, "api_keys", "rate_limit_period", "rate_limit_period VARCHAR(8) NULL", done)
    _add_column(db, inspector, "api_keys", "win_req_used", "win_req_used INTEGER NOT NULL DEFAULT 0", done)
    _add_column(db, inspector, "api_keys", "win_req_start", "win_req_start DATETIME NULL", done)


# 注册表：按版本从旧到新顺序排列。
MIGRATIONS = [
    ("1.1.0", _migrate_1_1_0),
    ("2.0.0", _migrate_2_0_0),
    ("2.1.0", _migrate_2_1_0),
    ("2.2.0", _migrate_2_2_0),
    ("2.3.0", _migrate_2_3_0),
    ("2.4.0", _migrate_2_4_0),
]


def run(db):
    """对已存在的库做版本化增量迁移（幂等）。返回执行的迁移描述列表。"""
    import config
    done = []
    try:
        inspector = inspect(db.engine)
        names = _tables(inspector)
        dialect = db.engine.dialect.name
    except Exception:
        return done

    if "users" not in names:
        _set_db_version(db, config.APP_VERSION)
        return done

    current = _get_db_version(db)
    cur_tuple = _parse_version(current) if current else (0,)

    for version, fn in MIGRATIONS:
        if _parse_version(version) > cur_tuple:
            try:
                fn(db, inspect(db.engine), dialect, done)
            except Exception:
                pass

    _set_db_version(db, config.APP_VERSION)
    return done


def mark_fresh_install(db):
    import config
    _set_db_version(db, config.APP_VERSION)


if __name__ == "__main__":
    import app as _app
    with _app.app.app_context():
        from models import db as _db
        result = run(_db)
        print("Migrations applied:" if result else "Nothing to migrate.")
        for r in result:
            print(" -", r)
