# -*- coding: utf-8 -*-
"""应用配置（开源版）。

设计原则：
- 不在代码中硬编码任何密钥、域名、账号等敏感信息。
- 仅保留启动必需项（SECRET_KEY、数据库连接）在 .env / 环境变量。
- AI 渠道、站点信息、管理员账号等运行时配置存数据库，由 install 页与后台管理。
- 默认 SQLite，零依赖即可运行；可通过 DATABASE_URL 切换 MySQL/PostgreSQL。
"""

import os
import secrets
from urllib.parse import quote_plus

BASE_DIR = os.path.abspath(os.path.dirname(__file__))
INSTANCE_DIR = os.path.join(BASE_DIR, "instance")

# 安装完成标记锁文件
INSTALL_LOCK_FILE = os.path.join(INSTANCE_DIR, "install.lock")
# install 向导写入的数据库连接串（优先级低于环境变量，高于默认 SQLite）
DB_URL_FILE = os.path.join(INSTANCE_DIR, "database.url")


def _load_dotenv(path: str):
    """从 .env 加载环境变量（无需第三方库）。"""
    if not os.path.exists(path):
        return
    with open(path, "r", encoding="utf-8") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            if line.lower().startswith("export "):
                line = line[7:].lstrip()
            key, _, value = line.partition("=")
            key = key.strip()
            value = value.strip().strip('"').strip("'")
            if key:
                os.environ.setdefault(key, value)


_load_dotenv(os.path.join(BASE_DIR, ".env"))
os.makedirs(INSTANCE_DIR, exist_ok=True)


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name, "").lower()
    return val in ("1", "true", "yes") if val else default


def is_installed() -> bool:
    """是否已完成首次安装。"""
    return os.path.exists(INSTALL_LOCK_FILE)


def _build_database_uri() -> str:
    """解析数据库连接。

    优先级：DATABASE_URL 环境变量 > 分项 MySQL_* 环境变量 >
    install 向导写入的 instance/database.url > 默认 SQLite。
    """
    explicit = os.environ.get("DATABASE_URL")
    if explicit:
        return explicit

    if os.environ.get("MYSQL_HOST"):
        host = os.environ.get("MYSQL_HOST", "127.0.0.1")
        port = int(os.environ.get("MYSQL_PORT", "3306"))
        user = os.environ.get("MYSQL_USER", "lexfence")
        password = os.environ.get("MYSQL_PASSWORD", "")
        db = os.environ.get("MYSQL_DB", "lexfence")
        return (
            f"mysql+pymysql://{user}:{quote_plus(password)}"
            f"@{host}:{port}/{db}?charset=utf8mb4"
        )

    # install 向导持久化的连接串
    if os.path.exists(DB_URL_FILE):
        try:
            with open(DB_URL_FILE, "r", encoding="utf-8") as f:
                saved = f.read().strip()
            if saved:
                return saved
        except OSError:
            pass

    # 默认 SQLite（零配置）
    sqlite_path = os.path.join(INSTANCE_DIR, "lexfence.db").replace("\\", "/")
    return "sqlite:///" + sqlite_path


def build_mysql_uri(host, port, user, password, db) -> str:
    """根据分项参数拼 MySQL 连接串（install 向导用）。"""
    return (
        f"mysql+pymysql://{user}:{quote_plus(password or '')}"
        f"@{host}:{int(port or 3306)}/{db}?charset=utf8mb4"
    )


def save_database_url(uri: str):
    """持久化 install 选择的数据库连接串。"""
    with open(DB_URL_FILE, "w", encoding="utf-8") as f:
        f.write(uri.strip())
    try:
        os.chmod(DB_URL_FILE, 0o600)
    except OSError:
        pass


def default_sqlite_uri() -> str:
    return "sqlite:///" + os.path.join(INSTANCE_DIR, "lexfence.db").replace("\\", "/")


def _resolve_secret_key() -> str:
    """会话密钥：优先环境变量，否则落盘 instance/secret_key 保证多 worker 一致。"""
    key = os.environ.get("SECRET_KEY")
    if key:
        return key
    key_file = os.path.join(INSTANCE_DIR, "secret_key")
    if os.path.exists(key_file):
        with open(key_file, "r", encoding="utf-8") as f:
            saved = f.read().strip()
        if saved:
            return saved
    generated = secrets.token_urlsafe(48)
    try:
        with open(key_file, "w", encoding="utf-8") as f:
            f.write(generated)
        os.chmod(key_file, 0o600)
    except OSError:
        pass
    return generated


# 品牌（中性默认，可经 install / 站点设置覆盖）
APP_NAME = os.environ.get("APP_NAME", "Lexfence")
GITHUB_URL = "https://github.com/pymcn6/Lexfence-AI-Content-Moderation"
# GitHub 仓库（owner/repo），用于检测更新
GITHUB_REPO = "pymcn6/Lexfence-AI-Content-Moderation"
# 当前版本号（发布新版本时同步更新，并打同名 git tag，如 v1.2.0）
APP_VERSION = os.environ.get("APP_VERSION", "1.0.0")
# Docker 镜像名（用于"拉取最新镜像更新"提示）
DOCKER_IMAGE = os.environ.get("DOCKER_IMAGE", "ghcr.io/pymcn6/lexfence-ai-content-moderation:latest")

# AI 检测全局默认（每渠道/模型可单独覆盖；留空用这些默认）
LLM_DEFAULT_MAX_TOKENS = int(os.environ.get("LLM_DEFAULT_MAX_TOKENS", "2048"))
LLM_FAIL_OPEN = _env_bool("LLM_FAIL_OPEN", False)


class Config:
    SECRET_KEY = _resolve_secret_key()
    SQLALCHEMY_DATABASE_URI = _build_database_uri()
    SQLALCHEMY_TRACK_MODIFICATIONS = False
    SQLALCHEMY_ENGINE_OPTIONS = {"pool_pre_ping": True, "pool_recycle": 3600}

    WTF_CSRF_ENABLED = True
    WTF_CSRF_TIME_LIMIT = 3600

    SESSION_COOKIE_SECURE = _env_bool("SESSION_COOKIE_SECURE")
    SESSION_COOKIE_HTTPONLY = True
    SESSION_COOKIE_SAMESITE = "Lax"
    PERMANENT_SESSION_LIFETIME = 3600 * 24

    MAX_CONTENT_LENGTH = 5 * 1024 * 1024
    MAX_TEXT_LENGTH = int(os.environ.get("MAX_TEXT_LENGTH", "5000"))

    RATELIMIT_STORAGE_URI = os.environ.get("RATELIMIT_STORAGE_URI", "memory://")
    RATELIMIT_STRATEGY = "fixed-window"

    BABEL_DEFAULT_LOCALE = os.environ.get("DEFAULT_LOCALE", "en")
    BABEL_SUPPORTED_LOCALES = ["en", "zh"]
