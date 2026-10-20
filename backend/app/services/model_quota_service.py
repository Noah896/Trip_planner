"""按登录用户持久化模型调用额度。

额度是进入昂贵模型链路前的预占量,不是账单级 token 统计:
- 表单规划 / 结构化聊天:1 单位;
- 自然语言聊天:最多 2 单位(意图识别 + 可能的规划)。

使用 SQLite ``BEGIN IMMEDIATE`` 原子预占,多进程、多线程并发时也不会超卖。
"""

from __future__ import annotations

import math
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, time, timedelta, timezone
from pathlib import Path
from typing import Callable, Optional

from ..config import get_settings


class ModelQuotaExceeded(RuntimeError):
    """当日模型调用额度不足。"""

    def __init__(
        self,
        message: str,
        *,
        user_id: str,
        limit: int,
        used: int,
        reset_at: str,
        retry_after: int,
    ):
        super().__init__(message)
        self.user_id = user_id
        self.limit = limit
        self.used = used
        self.reset_at = reset_at
        self.retry_after = max(1, int(retry_after))


@dataclass(frozen=True)
class ModelQuotaReservation:
    """一次成功预占后的额度快照。"""

    user_id: str
    usage_day: str
    units: int
    limit: int
    used: int
    remaining: int
    reset_at: str


class ModelQuotaService:
    """SQLite 用户模型额度存储。"""

    def __init__(
        self,
        path: str | Path,
        *,
        clock: Optional[Callable[[], datetime]] = None,
    ):
        self.path = Path(path)
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._schema_lock = threading.Lock()
        self._schema_ready = False

    def _connect(self) -> sqlite3.Connection:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        connection = sqlite3.connect(self.path, timeout=5)
        connection.row_factory = sqlite3.Row
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
                    CREATE TABLE IF NOT EXISTS model_usage_daily (
                        user_id    TEXT NOT NULL,
                        usage_day  TEXT NOT NULL,
                        used_units INTEGER NOT NULL DEFAULT 0,
                        updated_at TEXT NOT NULL,
                        PRIMARY KEY (user_id, usage_day)
                    );

                    CREATE INDEX IF NOT EXISTS idx_model_usage_day
                    ON model_usage_daily(usage_day);
                    """
                )
            self._schema_ready = True

    @staticmethod
    def _offset() -> timedelta:
        hours = max(
            -12,
            min(14, int(get_settings().model_quota_utc_offset_hours)),
        )
        return timedelta(hours=hours)

    def _day_bounds(self, now: datetime) -> tuple[str, str, int]:
        offset = self._offset()
        local_now = now + offset
        local_midnight = datetime.combine(
            local_now.date() + timedelta(days=1),
            time.min,
            tzinfo=timezone.utc,
        )
        reset_at = local_midnight - offset
        retry_after = max(
            1, math.ceil((reset_at - now).total_seconds())
        )
        return local_now.date().isoformat(), reset_at.isoformat(), retry_after

    def reserve(self, user_id: str, units: int = 1) -> ModelQuotaReservation:
        """原子预占额度;不足时抛 :class:`ModelQuotaExceeded`。"""
        normalized_user_id = (user_id or "").strip()
        if not normalized_user_id:
            raise ValueError("user_id 不能为空")
        requested_units = max(1, int(units))
        limit = max(1, int(get_settings().model_quota_daily_call_limit))
        now = self._clock().astimezone(timezone.utc)
        usage_day, reset_at, retry_after = self._day_bounds(now)

        self._ensure_schema()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                """
                SELECT used_units
                FROM model_usage_daily
                WHERE user_id = ? AND usage_day = ?
                """,
                (normalized_user_id, usage_day),
            ).fetchone()
            used = int(row["used_units"]) if row is not None else 0
            if used + requested_units > limit:
                raise ModelQuotaExceeded(
                    "今日模型调用额度已用尽",
                    user_id=normalized_user_id,
                    limit=limit,
                    used=used,
                    reset_at=reset_at,
                    retry_after=retry_after,
                )

            next_used = used + requested_units
            connection.execute(
                """
                INSERT INTO model_usage_daily (
                    user_id, usage_day, used_units, updated_at
                )
                VALUES (?, ?, ?, ?)
                ON CONFLICT(user_id, usage_day) DO UPDATE SET
                    used_units = excluded.used_units,
                    updated_at = excluded.updated_at
                """,
                (normalized_user_id, usage_day, next_used, now.isoformat()),
            )

        return ModelQuotaReservation(
            user_id=normalized_user_id,
            usage_day=usage_day,
            units=requested_units,
            limit=limit,
            used=next_used,
            remaining=limit - next_used,
            reset_at=reset_at,
        )


_model_quota_service: Optional[ModelQuotaService] = None


def get_model_quota_service() -> ModelQuotaService:
    """获取进程级模型额度服务。"""
    global _model_quota_service
    if _model_quota_service is None:
        _model_quota_service = ModelQuotaService(
            get_settings().model_quota_db_path
        )
    return _model_quota_service


def reset_model_quota_service() -> None:
    """重置单例,供测试或切换数据库路径时使用。"""
    global _model_quota_service
    _model_quota_service = None
