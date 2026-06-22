# -*- coding: utf-8 -*-
"""敏感字段加密工具。

用 SECRET_KEY 派生 Fernet 密钥，对 AI 渠道 API 密钥等敏感字段加密存库。
即使数据库泄露，没有 SECRET_KEY 也无法解出明文密钥。
"""

import base64
import hashlib

from cryptography.fernet import Fernet, InvalidToken

from config import Config


def _fernet() -> Fernet:
    # 由 SECRET_KEY 派生 32 字节密钥
    digest = hashlib.sha256(Config.SECRET_KEY.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))


def encrypt_secret(plaintext: str) -> str:
    if not plaintext:
        return ""
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_secret(token: str) -> str:
    if not token:
        return ""
    try:
        return _fernet().decrypt(token.encode("ascii")).decode("utf-8")
    except (InvalidToken, ValueError):
        return ""


def mask_secret(plaintext: str) -> str:
    """掩码显示：sk-abc...x9k2。"""
    if not plaintext:
        return ""
    if len(plaintext) <= 8:
        return plaintext[:2] + "***"
    return f"{plaintext[:5]}***{plaintext[-4:]}"
