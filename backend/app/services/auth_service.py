"""自托管账号与 Bearer 会话令牌。

只使用标准库:

- 密码使用 PBKDF2-HMAC-SHA256 + 随机盐哈希;
- 客户端拿到随机 opaque token,数据库只保存 token 的 SHA-256;
- token 有明确过期时间,退出登录时直接撤销。
"""

from __future__ import annotations

import hashlib
import hmac
import math
import secrets
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import List, Optional

from ..config import get_settings


PBKDF2_ITERATIONS = 310_000
TOKEN_BYTES = 32
_DUMMY_PASSWORD = "\0" * 16
_DUMMY_SALT = b"\0" * 16
_DUMMY_PASSWORD_HASH = hashlib.pbkdf2_hmac(
    "sha256",
    _DUMMY_PASSWORD.encode("utf-8"),
    _DUMMY_SALT,
    PBKDF2_ITERATIONS,
).hex()


class AuthError(RuntimeError):
    """鉴权相关错误的基类。"""


class AuthValidationError(AuthError):
    """用户名或密码不符合规则。"""


class AuthRegistrationError(AuthError):
    """当前不允许注册,或邀请码错误。"""


class AuthCredentialsError(AuthError):
    """用户名或密码错误。"""


class AuthRateLimitError(AuthError):
    """登录失败次数过多。"""

    def __init__(self, message: str, *, retry_after: int):
        super().__init__(message)
        self.retry_after = max(1, int(retry_after))


class AuthTokenError(AuthError):
    """Bearer token 缺失、过期或无效。"""


class AuthUserNotFoundError(AuthError):
    """目标账号不存在。"""


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


@dataclass(frozen=True)
class PasswordResetIssued:
    reset_token: str
    expires_at: str
    user: AuthUser


@dataclass(frozen=True)
class AuthAccount:
    user_id: str
    username: str
    disabled: bool
    created_at: str


@dataclass(frozen=True)
class AuthAuditEvent:
    id: int
    event_type: str
    success: bool
    user_id: Optional[str]
    username: Optional[str]
    client_hash: Optional[str]
    detail: str
    created_at: str


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

                    CREATE TABLE IF NOT EXISTS auth_login_failures (
                        attempt_key TEXT NOT NULL,
                        attempted_at TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_auth_login_failures_key_time
                    ON auth_login_failures(attempt_key, attempted_at);

                    CREATE TABLE IF NOT EXISTS password_reset_tokens (
                        token_hash TEXT PRIMARY KEY,
                        user_id    TEXT NOT NULL,
                        created_at TEXT NOT NULL,
                        expires_at TEXT NOT NULL,
                        FOREIGN KEY(user_id) REFERENCES users(user_id) ON DELETE CASCADE
                    );

                    CREATE INDEX IF NOT EXISTS idx_password_reset_user
                    ON password_reset_tokens(user_id);

                    CREATE INDEX IF NOT EXISTS idx_password_reset_expires
                    ON password_reset_tokens(expires_at);

                    CREATE TABLE IF NOT EXISTS auth_audit_events (
                        id          INTEGER PRIMARY KEY AUTOINCREMENT,
                        event_type  TEXT NOT NULL,
                        success     INTEGER NOT NULL,
                        user_id     TEXT,
                        username    TEXT,
                        client_hash TEXT,
                        detail      TEXT NOT NULL DEFAULT '',
                        created_at  TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_auth_audit_created
                    ON auth_audit_events(created_at DESC, id DESC);

                    CREATE INDEX IF NOT EXISTS idx_auth_audit_type
                    ON auth_audit_events(event_type, created_at DESC);
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

    @staticmethod
    def _login_attempt_key(username: str, client_id: str) -> str:
        """把用户名与来源地址做不可逆键,避免在限流表里重复存明文。"""
        raw = f"{username}\0{(client_id or 'unknown').strip()}".encode("utf-8")
        return hashlib.sha256(raw).hexdigest()

    def _reserve_login_attempt(self, username: str, client_id: str) -> str:
        """原子预占一次失败名额,成功后由调用方清除。

        在昂贵的 PBKDF2 计算之前写入,确保并发请求也不能同时越过上限。
        """
        settings = get_settings()
        max_failures = max(1, int(settings.auth_login_max_failures))
        window_seconds = max(1, int(settings.auth_login_failure_window_seconds))
        now = self._now()
        cutoff = now - timedelta(seconds=window_seconds)
        attempt_key = self._login_attempt_key(username, client_id)

        self._ensure_schema()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            connection.execute(
                "DELETE FROM auth_login_failures WHERE attempted_at <= ?",
                (cutoff.isoformat(),),
            )
            row = connection.execute(
                """
                SELECT attempted_at
                FROM auth_login_failures
                WHERE attempt_key = ?
                ORDER BY attempted_at ASC
                """,
                (attempt_key,),
            ).fetchall()
            if len(row) >= max_failures:
                oldest = datetime.fromisoformat(row[0]["attempted_at"])
                retry_after = math.ceil(
                    (oldest + timedelta(seconds=window_seconds) - now).total_seconds()
                )
                raise AuthRateLimitError(
                    "登录失败次数过多,请稍后重试",
                    retry_after=retry_after,
                )
            connection.execute(
                """
                INSERT INTO auth_login_failures (attempt_key, attempted_at)
                VALUES (?, ?)
                """,
                (attempt_key, now.isoformat()),
            )
        return attempt_key

    def _clear_login_attempts(
        self, connection: sqlite3.Connection, attempt_key: str
    ) -> None:
        connection.execute(
            "DELETE FROM auth_login_failures WHERE attempt_key = ?",
            (attempt_key,),
        )

    @staticmethod
    def _client_hash(client_id: str) -> str:
        return hashlib.sha256(
            (client_id or "unknown").strip().encode("utf-8")
        ).hexdigest()

    def _audit(
        self,
        event_type: str,
        *,
        success: bool,
        user_id: Optional[str] = None,
        username: Optional[str] = None,
        client_id: str = "unknown",
        detail: str = "",
    ) -> None:
        """写入审计事件;审计失败不能反向破坏账号操作。"""
        try:
            self._ensure_schema()
            with self._connect() as connection:
                connection.execute(
                    """
                    INSERT INTO auth_audit_events (
                        event_type, success, user_id, username,
                        client_hash, detail, created_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        event_type,
                        int(success),
                        user_id,
                        username,
                        self._client_hash(client_id),
                        detail[:500],
                        self._now().isoformat(),
                    ),
                )
        except Exception as exc:  # noqa: BLE001
            print(f"WARNING: auth audit write failed: {type(exc).__name__}: {exc}")

    @staticmethod
    def _row_to_account(row: sqlite3.Row) -> AuthAccount:
        return AuthAccount(
            user_id=row["user_id"],
            username=row["username"],
            disabled=bool(int(row["disabled"])),
            created_at=row["created_at"],
        )

    @staticmethod
    def _row_to_audit(row: sqlite3.Row) -> AuthAuditEvent:
        return AuthAuditEvent(
            id=int(row["id"]),
            event_type=row["event_type"],
            success=bool(int(row["success"])),
            user_id=row["user_id"],
            username=row["username"],
            client_hash=row["client_hash"],
            detail=row["detail"],
            created_at=row["created_at"],
        )

    @staticmethod
    def _new_password_hash(password: str) -> tuple[bytes, str]:
        salt = secrets.token_bytes(16)
        password_hash = AuthService._hash_password(
            password,
            salt,
            PBKDF2_ITERATIONS,
        )
        return salt, password_hash

    def register(
        self,
        username: str,
        password: str,
        *,
        registration_key: Optional[str] = None,
        client_id: str = "unknown",
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

        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
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
                issued = self._issue_token(connection, user)
        except sqlite3.IntegrityError as exc:
            self._audit(
                "register",
                success=False,
                username=normalized,
                client_id=client_id,
                detail="username_exists",
            )
            raise AuthValidationError("用户名已存在") from exc

        self._audit(
            "register",
            success=True,
            user_id=user.user_id,
            username=user.username,
            client_id=client_id,
        )
        return issued

    def login(
        self, username: str, password: str, *, client_id: str = "unknown"
    ) -> IssuedToken:
        """校验账号密码并签发新令牌。

        同一用户名 + 来源地址连续失败达到上限后,在滑动窗口结束前抛
        :class:`AuthRateLimitError`。成功登录会清除该组合的失败记录。
        """
        normalized = self._normalize_username(username)
        password = self._validate_password(password)
        self._ensure_schema()
        attempt_key = self._reserve_login_attempt(normalized, client_id)

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
            if row is None:
                # 对不存在的用户名也执行同成本的哈希,避免通过响应耗时枚举账号。
                salt = _DUMMY_SALT
                stored_hash = _DUMMY_PASSWORD_HASH
                iterations = PBKDF2_ITERATIONS
                disabled = True
            else:
                salt = bytes.fromhex(row["password_salt"])
                stored_hash = row["password_hash"]
                iterations = int(row["iterations"])
                disabled = bool(int(row["disabled"]))

            candidate = self._hash_password(password, salt, iterations)
            if disabled or not hmac.compare_digest(candidate, stored_hash):
                raise AuthCredentialsError("用户名或密码错误")

            self._clear_login_attempts(connection, attempt_key)
            return self._issue_token(connection, self._row_to_user(row))

    def change_password(
        self,
        user_id: str,
        current_password: str,
        new_password: str,
        *,
        client_id: str = "unknown",
    ) -> IssuedToken:
        """校验旧密码后改密,撤销所有旧会话并签发新令牌。"""
        user_id = (user_id or "").strip()
        if not user_id:
            raise AuthUserNotFoundError("账号不存在")
        current_password = self._validate_password(current_password)
        new_password = self._validate_password(new_password)
        if hmac.compare_digest(current_password, new_password):
            raise AuthValidationError("新密码不能与当前密码相同")

        self._ensure_schema()
        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT user_id, username, password_hash, password_salt,
                           iterations, disabled, created_at
                    FROM users
                    WHERE user_id = ?
                    """,
                    (user_id,),
                ).fetchone()
                if row is None:
                    raise AuthUserNotFoundError("账号不存在")
                if int(row["disabled"]):
                    raise AuthCredentialsError("账号已禁用")

                candidate = self._hash_password(
                    current_password,
                    bytes.fromhex(row["password_salt"]),
                    int(row["iterations"]),
                )
                if not hmac.compare_digest(candidate, row["password_hash"]):
                    raise AuthCredentialsError("当前密码不正确")

                salt, password_hash = self._new_password_hash(new_password)
                connection.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, password_salt = ?, iterations = ?
                    WHERE user_id = ?
                    """,
                    (
                        password_hash,
                        salt.hex(),
                        PBKDF2_ITERATIONS,
                        user_id,
                    ),
                )
                connection.execute(
                    "DELETE FROM auth_sessions WHERE user_id = ?", (user_id,)
                )
                connection.execute(
                    "DELETE FROM password_reset_tokens WHERE user_id = ?",
                    (user_id,),
                )
                user = self._row_to_user(row)
                issued = self._issue_token(connection, user)
        except (AuthUserNotFoundError, AuthCredentialsError) as exc:
            self._audit(
                "password_change",
                success=False,
                user_id=user_id,
                client_id=client_id,
                detail=str(exc),
            )
            raise

        self._audit(
            "password_change",
            success=True,
            user_id=user.user_id,
            username=user.username,
            client_id=client_id,
        )
        return issued

    def issue_password_reset(
        self,
        user_id: str,
        *,
        client_id: str = "unknown",
    ) -> PasswordResetIssued:
        """由管理员签发一次性密码重置令牌。"""
        user_id = (user_id or "").strip()
        if not user_id:
            raise AuthUserNotFoundError("账号不存在")
        self._ensure_schema()
        now = self._now()
        expires_at = now + timedelta(
            minutes=max(1, int(get_settings().auth_password_reset_ttl_minutes))
        )
        reset_token = secrets.token_urlsafe(TOKEN_BYTES)
        token_hash = self._token_hash(reset_token)

        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT user_id, username, disabled, created_at
                    FROM users
                    WHERE user_id = ?
                    """,
                    (user_id,),
                ).fetchone()
                if row is None:
                    raise AuthUserNotFoundError("账号不存在")
                connection.execute(
                    "DELETE FROM password_reset_tokens WHERE user_id = ?",
                    (user_id,),
                )
                connection.execute(
                    """
                    INSERT INTO password_reset_tokens (
                        token_hash, user_id, created_at, expires_at
                    )
                    VALUES (?, ?, ?, ?)
                    """,
                    (
                        token_hash,
                        user_id,
                        now.isoformat(),
                        expires_at.isoformat(),
                    ),
                )
                user = AuthUser(
                    user_id=row["user_id"],
                    username=row["username"],
                    created_at=row["created_at"],
                )
        except AuthUserNotFoundError as exc:
            self._audit(
                "password_reset_issued",
                success=False,
                user_id=user_id,
                client_id=client_id,
                detail=str(exc),
            )
            raise

        self._audit(
            "password_reset_issued",
            success=True,
            user_id=user.user_id,
            username=user.username,
            client_id=client_id,
            detail=f"expires_at={expires_at.isoformat()}",
        )
        return PasswordResetIssued(
            reset_token=reset_token,
            expires_at=expires_at.isoformat(),
            user=user,
        )

    def reset_password(
        self,
        reset_token: str,
        new_password: str,
        *,
        client_id: str = "unknown",
    ) -> AuthUser:
        """消费一次性令牌设置新密码,并撤销该账号全部会话。"""
        reset_token = (reset_token or "").strip()
        if not reset_token:
            raise AuthTokenError("密码重置令牌无效或已过期")
        new_password = self._validate_password(new_password)
        self._ensure_schema()
        now = self._now().isoformat()
        token_hash = self._token_hash(reset_token)

        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT u.user_id, u.username, u.created_at
                    FROM password_reset_tokens r
                    JOIN users u ON u.user_id = r.user_id
                    WHERE r.token_hash = ? AND r.expires_at > ?
                    """,
                    (token_hash, now),
                ).fetchone()
                if row is None:
                    raise AuthTokenError("密码重置令牌无效或已过期")

                salt, password_hash = self._new_password_hash(new_password)
                connection.execute(
                    """
                    UPDATE users
                    SET password_hash = ?, password_salt = ?, iterations = ?
                    WHERE user_id = ?
                    """,
                    (
                        password_hash,
                        salt.hex(),
                        PBKDF2_ITERATIONS,
                        row["user_id"],
                    ),
                )
                connection.execute(
                    "DELETE FROM auth_sessions WHERE user_id = ?",
                    (row["user_id"],),
                )
                connection.execute(
                    "DELETE FROM password_reset_tokens WHERE user_id = ?",
                    (row["user_id"],),
                )
                user = self._row_to_user(row)
        except AuthTokenError as exc:
            self._audit(
                "password_reset_completed",
                success=False,
                client_id=client_id,
                detail=str(exc),
            )
            raise

        self._audit(
            "password_reset_completed",
            success=True,
            user_id=user.user_id,
            username=user.username,
            client_id=client_id,
        )
        return user

    def set_user_disabled(
        self,
        user_id: str,
        disabled: bool,
        *,
        client_id: str = "unknown",
    ) -> AuthAccount:
        """禁用或启用账号;禁用会立即撤销全部会话和重置令牌。"""
        user_id = (user_id or "").strip()
        if not user_id:
            raise AuthUserNotFoundError("账号不存在")
        self._ensure_schema()
        event_type = "account_disabled" if disabled else "account_enabled"

        try:
            with self._connect() as connection:
                connection.execute("BEGIN IMMEDIATE")
                row = connection.execute(
                    """
                    SELECT user_id, username, disabled, created_at
                    FROM users
                    WHERE user_id = ?
                    """,
                    (user_id,),
                ).fetchone()
                if row is None:
                    raise AuthUserNotFoundError("账号不存在")
                connection.execute(
                    "UPDATE users SET disabled = ? WHERE user_id = ?",
                    (int(disabled), user_id),
                )
                if disabled:
                    connection.execute(
                        "DELETE FROM auth_sessions WHERE user_id = ?",
                        (user_id,),
                    )
                    connection.execute(
                        "DELETE FROM password_reset_tokens WHERE user_id = ?",
                        (user_id,),
                    )
                account = self._row_to_account(row)
                account = AuthAccount(
                    user_id=account.user_id,
                    username=account.username,
                    disabled=disabled,
                    created_at=account.created_at,
                )
        except AuthUserNotFoundError as exc:
            self._audit(
                event_type,
                success=False,
                user_id=user_id,
                client_id=client_id,
                detail=str(exc),
            )
            raise

        self._audit(
            event_type,
            success=True,
            user_id=account.user_id,
            username=account.username,
            client_id=client_id,
        )
        return account

    def list_audit_events(
        self,
        *,
        event_type: Optional[str] = None,
        limit: int = 50,
    ) -> List[AuthAuditEvent]:
        """按时间倒序读取账号生命周期审计事件。"""
        self._ensure_schema()
        bounded_limit = max(1, min(int(limit), 200))
        clauses = []
        params = []
        if event_type and event_type.strip():
            clauses.append("event_type = ?")
            params.append(event_type.strip())
        where = f"WHERE {' AND '.join(clauses)}" if clauses else ""
        params.append(bounded_limit)
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, event_type, success, user_id, username,
                       client_hash, detail, created_at
                FROM auth_audit_events
                {where}
                ORDER BY created_at DESC, id DESC
                LIMIT ?
                """,
                params,
            ).fetchall()
        return [self._row_to_audit(row) for row in rows]

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
            now = self._now().isoformat()
            connection.execute(
                "DELETE FROM password_reset_tokens WHERE expires_at <= ?",
                (now,),
            )
            cursor = connection.execute(
                "DELETE FROM auth_sessions WHERE expires_at <= ?", (now,)
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
