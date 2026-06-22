# -*- coding: utf-8 -*-
"""邮件发送与注册邮箱验证。

使用后台配置的 SMTP 信息发送验证邮件。验证采用带签名的 token（itsdangerous 风格，
此处用标准库 hmac 自实现，避免新增依赖），无需额外存储。
"""

import hashlib
import hmac
import smtplib
import time
from email.mime.text import MIMEText
from email.header import Header

import settings_store
from config import Config


def smtp_configured() -> bool:
    return bool(settings_store.get_setting("smtp_host")
                and settings_store.get_setting("smtp_from"))


def _sign(payload: str) -> str:
    return hmac.new(Config.SECRET_KEY.encode(), payload.encode(), hashlib.sha256).hexdigest()


def make_token(email: str, ttl: int = 86400) -> str:
    """生成带过期时间的签名 token：email|expire|sig。"""
    expire = int(time.time()) + ttl
    payload = f"{email}|{expire}"
    return f"{payload}|{_sign(payload)}"


def verify_token(token: str):
    """校验 token，返回 email 或 None。"""
    try:
        email, expire, sig = token.rsplit("|", 2)
    except (ValueError, AttributeError):
        return None
    payload = f"{email}|{expire}"
    if not hmac.compare_digest(sig, _sign(payload)):
        return None
    if int(expire) < int(time.time()):
        return None
    return email


def send_email(to_addr: str, subject: str, body: str) -> bool:
    host = settings_store.get_setting("smtp_host")
    port = settings_store.get_int("smtp_port", 587)
    user = settings_store.get_setting("smtp_user")
    password = settings_store.get_secret("smtp_password")
    from_addr = settings_store.get_setting("smtp_from") or user
    use_tls = settings_store.get_bool("smtp_use_tls")
    if not host or not from_addr:
        return False

    msg = MIMEText(body, "plain", "utf-8")
    msg["Subject"] = Header(subject, "utf-8")
    msg["From"] = from_addr
    msg["To"] = to_addr

    try:
        if port == 465:
            server = smtplib.SMTP_SSL(host, port, timeout=15)
        else:
            server = smtplib.SMTP(host, port, timeout=15)
            if use_tls:
                server.starttls()
        if user and password:
            server.login(user, password)
        server.sendmail(from_addr, [to_addr], msg.as_string())
        server.quit()
        return True
    except Exception:
        return False


def send_verification(to_addr: str, verify_url: str) -> bool:
    site = settings_store.get_setting("site_name", "Lexfence")
    subject = f"[{site}] Verify your email / 邮箱验证"
    body = (
        f"Welcome to {site}!\n\n"
        f"Please click the link below to verify your email and activate your account:\n"
        f"{verify_url}\n\n"
        f"The link expires in 24 hours. If you did not request this, please ignore.\n\n"
        f"欢迎注册 {site}！请点击以下链接验证邮箱并激活账户（24 小时内有效）：\n{verify_url}\n"
    )
    return send_email(to_addr, subject, body)


def send_verification_async(app, to_addr: str, verify_url: str):
    """后台线程异步发送验证邮件，避免注册请求卡顿。"""
    import threading

    def worker():
        with app.app_context():
            try:
                send_verification(to_addr, verify_url)
            except Exception:
                pass

    threading.Thread(target=worker, daemon=True).start()
