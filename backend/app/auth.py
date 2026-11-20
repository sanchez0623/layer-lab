"""应用级登录的密码哈希与会话令牌工具（纯标准库，不引入第三方依赖）。

替代原先的 nginx HTTP Basic 认证：
- 密码用 ``hashlib.pbkdf2_hmac('sha256', ...)`` + 随机盐，落库格式
  ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>``，校验用
  ``hmac.compare_digest`` 做常量时间比较。
- 会话令牌用 ``secrets.token_urlsafe(32)``。

有效期口径：普通登录 :data:`DEFAULT_SESSION_HOURS`（12 小时），
勾选「记住我」:data:`REMEMBER_DAYS`（7 天）。时间统一走
:func:`app.domain.utc_now` 的 ISO 字符串口径，便于按字符串比较判断过期。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
from datetime import datetime, timedelta

from .domain import utc_now

PBKDF2_ITERATIONS = 200_000
_HASH_NAME = "pbkdf2_sha256"

# 「记住我」有效期（天）与普通会话有效期（小时）。
REMEMBER_DAYS = 7
DEFAULT_SESSION_HOURS = 12


def _derive(password: str, salt: bytes, iterations: int) -> bytes:
    return hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, iterations)


def hash_password(password: str, *, iterations: int = PBKDF2_ITERATIONS) -> str:
    """生成 ``pbkdf2_sha256$<iterations>$<salt_hex>$<hash_hex>`` 格式的口令哈希。"""
    salt = secrets.token_bytes(16)
    digest = _derive(password, salt, iterations)
    return f"{_HASH_NAME}${iterations}${salt.hex()}${digest.hex()}"


def verify_password(password: str, stored: str) -> bool:
    """常量时间校验口令；格式不符（旧数据/脏数据）一律返回 False。"""
    try:
        name, iterations_text, salt_hex, hash_hex = (stored or "").split("$", 3)
        iterations = int(iterations_text)
    except (ValueError, AttributeError):
        return False
    if name != _HASH_NAME or iterations <= 0:
        return False
    try:
        salt = bytes.fromhex(salt_hex)
        expected = bytes.fromhex(hash_hex)
    except ValueError:
        return False
    actual = _derive(password, salt, iterations)
    return hmac.compare_digest(actual, expected)


def new_token() -> str:
    """生成一次性的会话令牌（URL 安全、32 字节熵）。"""
    return secrets.token_urlsafe(32)


def _iso(moment: datetime) -> str:
    return moment.isoformat(timespec="seconds")


def remember_expiry(*, now: datetime | None = None) -> str:
    """「记住我」的过期时间（ISO）：now + REMEMBER_DAYS 天。"""
    return _iso((now or datetime.now()) + timedelta(days=REMEMBER_DAYS))


def default_expiry(*, now: datetime | None = None) -> str:
    """普通登录的过期时间（ISO）：now + DEFAULT_SESSION_HOURS 小时。"""
    return _iso((now or datetime.now()) + timedelta(hours=DEFAULT_SESSION_HOURS))
