"""自托管账号与 Bearer 会话令牌。

只使用标准库:

- 密码使用 PBKDF2-HMAC-SHA256 + 随机盐哈希;
- 客户端拿到随机 opaque token,数据库只保存 token 的 SHA-256;
- token 有明确过期时间,退出登录时直接撤销。
"""

from __future__ import annotations

import hashlib
import hmac
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

from ..config import get_settings


PBKDF2_ITERATIONS = 310_000
TOKEN_BYTES = 32


class AuthError(RuntimeError):
    """鉴权相关错误的基类。"""


class AuthValidationError(AuthError):
    """用户名或密码不符合规则。"""


class AuthRegistrationError(AuthError):
    """当前不允许注册,或邀请码错误。"""


class AuthCredentialsError(AuthError):
    """用户名或密码错误。"""


class AuthTokenError(AuthError):
    """Bearer token 缺失、过期或无效。"""


@dataclass(frozen=True)
class AuthUser:
    user_id: str
    username: str
    created_at: str


@dataclass(frozen=True)
class IssuedToken:
    access_token: str
    expires_at: str
    user: AuthUser


class AuthService:
    """SQLite 用户与会话存储。"""

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA journal_mode = WAL")
        connection.execute("PRAGMA busy_timeout = 5000")
        return connection

    def _ensure_schema(self) -> None:
        if self._schema_ready:
            return
        with self._schema_lock:
            if self._schema_ready:
                return
            with self._connect() as connection:
                connection.executescript(
                    """
                    CREATE TABLE IF NOT EXISTS users (
                        user_id       TEXT PRIMARY KEY,
                        username      TEXT NOT NULL UNIQUE COLLATE NOCASE,
                        password_hash TEXT NOT NULL,
                        password_salt TEXT NOT NULL,
                        iterations    INTEGER NOT NULL,
                        disabled      INTEGER NOT NULL DEFAULT 0,
                        created_at    TEXT NOT NULL
                    );

                    CREATE TABLE IF NOT EXISTS auth_sessions (
                        token_hash  TEXT PRIMARY KEY,
                        user_id     TEXT NOT NULL,
                        created_at  TEXT NOT NULL,
                        expires_at  TEXT NOT NULL,
                        last_used_at TEXT NOT NULL,
                        FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_auth_sessions_user
                    ON auth_sessions(user_id);

                    CREATE INDEX IF NOT EXISTS idx_auth_sessions_expires
                    ON auth_sessions(expires_at);
                    """
                )
            self._schema_ready = True

    @staticmethod
    def _normalize_username(username: str) -> str:
        text = (username or "").strip()
        if not 3 <= len(text) <= 32:
            raise AuthValidationError("用户名长度必须在 3 到 32 个字符之间")
        if not all(char.isalnum() or char in "_-." for char in text):
            raise AuthValidationError("用户名只能包含字母、数字、下划线、点和短横线")
        return text.casefold()

    @staticmethod
    def _validate_password(password: str) -> str:
        if not isinstance(password, str) or not 8 <= len(password) <= 128:
            raise AuthValidationError("密码长度必须在 8 到 128 个字符之间")
        return password

    @staticmethod
    def _hash_password(password: str, salt: bytes, iterations: int) -> str:
        digest = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), salt, iterations
        )
        return digest.hex()

    @staticmethod
    def _token_hash(token: str) -> str:
        return hashlib.sha256(token.encode("utf-8")).hexdigest()

    @staticmethod
    def _row_to_user(row: sqlite3.Row) -> AuthUser:
        return AuthUser(
            user_id=row["user_id"],
            username=row["username"],
            created_at=row["created_at"],
        )

    def _issue_token(
        self, connection: sqlite3.Connection, user: AuthUser
    ) -> IssuedToken:
        raw_token = secrets.token_urlsafe(TOKEN_BYTES)
        token_hash = self._token_hash(raw_token)
        now = self._now()
        ttl_days = max(1, int(get_settings().auth_session_ttl_days))
        expires_at = now + timedelta(days=ttl_days)
        connection.execute(
            "DELETE FROM auth_sessions WHERE expires_at <= ?", (now.isoformat(),)
        )
        connection.execute(
            """
            INSERT INTO auth_sessions (
                token_hash, user_id, created_at, expires_at, last_used_at
            )
            VALUES (?, ?, ?, ?, ?)
            """,
            (
                token_hash,
                user.user_id,
                now.isoformat(),
                expires_at.isoformat(),
                now.isoformat(),
            ),
        )
        return IssuedToken(
            access_token=raw_token,
            expires_at=expires_at.isoformat(),
            user=user,
        )

    def _check_registration(self, registration_key: Optional[str]) -> None:
        settings = get_settings()
        if not settings.auth_allow_registration:
            raise AuthRegistrationError("当前未开放账号注册")
        expected = (settings.auth_registration_key or "").strip()
        provided = (registration_key or "").strip()
        if expected and not hmac.compare_digest(expected, provided):
            raise AuthRegistrationError("注册邀请码不正确")

    def register(
        self,
        username: str,
        password: str,
        *,
        registration_key: Optional[str] = None,
    ) -> IssuedToken:
        """创建账号并直接签发登录令牌。"""
        self._check_registration(registration_key)
        normalized = self._normalize_username(username)
        password = self._validate_password(password)
        self._ensure_schema()

        salt = secrets.token_bytes(16)
        password_hash = self._hash_password(password, salt, PBKDF2_ITERATIONS)
        user = AuthUser(
            user_id=f"usr_{secrets.token_hex(12)}",
            username=normalized,
            created_at=self._now().isoformat(),
        )

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            try:
                connection.execute(
                    """
                    INSERT INTO users (
                        user_id, username, password_hash, password_salt,
                        iterations, disabled, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, 0, ?)
                    """,
                    (
                        user.user_id,
                        user.username,
                        password_hash,
                        salt.hex(),
                        PBKDF2_ITERATIONS,
                        user.created_at,
                    ),
                )
            except sqlite3.IntegrityError as exc:
                raise AuthValidationError("用户名已存在") from exc
            return self._issue_token(connection, user)

    def login(self, username: str, password: str) -> IssuedToken:
        """校验账号密码并签发新令牌。"""
        normalized = self._normalize_username(username)
        password = self._validate_password(password)
        self._ensure_schema()

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT user_id, username, password_hash, password_salt,
                       iterations, disabled, created_at
                FROM users
                WHERE username = ?
                """,
                (normalized,),
            ).fetchone()
            if row is None or int(row["disabled"]):
                raise AuthCredentialsError("用户名或密码错误")

            candidate = self._hash_password(
                password, bytes.fromhex(row["password_salt"]), int(row["iterations"])
            )
            if not hmac.compare_digest(candidate, row["password_hash"]):
                raise AuthCredentialsError("用户名或密码错误")
            return self._issue_token(connection, self._row_to_user(row))

    def authenticate(self, token: str) -> AuthUser:
        """验证 Bearer token 并返回当前用户。"""
        token = (token or "").strip()
        if not token:
            raise AuthTokenError("缺少 Bearer token")
        self._ensure_schema()
        now = self._now().isoformat()
        token_hash = self._token_hash(token)

        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT u.user_id, u.username, u.created_at, u.disabled,
                       s.expires_at
                FROM auth_sessions s
                JOIN users u ON u.user_id = s.user_id
                WHERE s.token_hash = ? AND s.expires_at > ?
                """,
                (token_hash, now),
            ).fetchone()
            if row is None or int(row["disabled"]):
                connection.execute(
                    "DELETE FROM auth_sessions WHERE token_hash = ?", (token_hash,)
                )
                raise AuthTokenError("登录已过期,请重新登录")
            connection.execute(
                "UPDATE auth_sessions SET last_used_at = ? WHERE token_hash = ?",
                (now, token_hash),
            )
            return self._row_to_user(row)

    def revoke(self, token: str) -> None:
        """撤销当前 token。"""
        if not token:
            return
        self._ensure_schema()
        with self._connect() as connection:
            connection.execute(
                "DELETE FROM auth_sessions WHERE token_hash = ?",
                (self._token_hash(token),),
            )

    def purge_expired(self) -> int:
        self._ensure_schema()
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM auth_sessions WHERE expires_at <= ?",
                (self._now().isoformat(),),
            )
            return int(cursor.rowcount or 0)


_auth_service: Optional[AuthService] = None


def get_auth_service() -> AuthService:
    global _auth_service
    if _auth_service is None:
        _auth_service = AuthService(get_settings().auth_db_path)
    return _auth_service


def reset_auth_service() -> None:
    global _auth_service
    _auth_service = None
