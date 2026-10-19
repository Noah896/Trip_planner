"""基于 SQLite 的服务端会话记忆。

第一阶段只保存“工作记忆”：按 ``session_id`` 精确读取上一版请求和行程，
让用户可以直接说“第二天换成博物馆”,而不必重填整张表。

长期偏好与历史行程会复用同一个 SQLite 文件,但放在独立的表里;本阶段先不启用,
更不引入向量检索。SQLite 在这里是权威存储,不是缓存。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..config import get_settings
from ..models.schemas import TripPlan, TripRequest


class MemoryAccessError(RuntimeError):
    """会话存在,但不属于当前用户。"""


@dataclass(frozen=True)
class SessionMemory:
    """一次会话当前可用的工作记忆。"""

    session_id: str
    user_id: str
    base_request: Dict[str, Any]
    request: Dict[str, Any]
    plan: Dict[str, Any]
    version: int
    updated_at: str


@dataclass(frozen=True)
class MemoryFact:
    """一条长期偏好事实。

    同一个 ``(user_id, category, key)`` 只保留一条 active 记录。值发生变化时,
    旧记录会标记为 ``superseded``,并通过 ``superseded_by`` 指向新记录,从而保留
    用户偏好变化的出处,而不是静默覆盖。
    """

    id: int
    user_id: str
    category: str
    key: str
    value: Any
    source_text: str
    source_session_id: Optional[str]
    confidence: float
    status: str
    created_at: str
    updated_at: str
    superseded_by: Optional[int]


@dataclass(frozen=True)
class MemoryDeletion:
    """删除一个用户全部记忆后的计数。"""

    user_id: str
    facts: int
    history: int
    sessions: int

    @property
    def total(self) -> int:
        return self.facts + self.history + self.sessions


class MemoryStore:
    """SQLite 记忆存储。

    每次操作使用独立连接,避免 FastAPI 线程/协程切换时共享连接状态。写入量很小,
    这个选择比维护连接池更简单,也能避免 ``check_same_thread`` 一类问题。
    """

    def __init__(self, path: str | Path):
        self.path = Path(path)
        self._schema_lock = threading.Lock()
        self._schema_ready = False

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
                    CREATE TABLE IF NOT EXISTS session_state (
                        session_id        TEXT PRIMARY KEY,
                        user_id           TEXT NOT NULL DEFAULT '',
                        base_request_json TEXT NOT NULL,
                        request_json      TEXT NOT NULL,
                        plan_json         TEXT NOT NULL,
                        version           INTEGER NOT NULL DEFAULT 1,
                        last_message      TEXT NOT NULL DEFAULT '',
                        updated_at        TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_session_state_updated
                    ON session_state(updated_at);

                    CREATE TABLE IF NOT EXISTS memory_facts (
                        id                INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id           TEXT NOT NULL,
                        category          TEXT NOT NULL,
                        memory_key        TEXT NOT NULL,
                        value_json        TEXT NOT NULL,
                        source_text       TEXT NOT NULL DEFAULT '',
                        source_session_id TEXT,
                        confidence        REAL NOT NULL DEFAULT 1.0,
                        status            TEXT NOT NULL DEFAULT 'active',
                        created_at        TEXT NOT NULL,
                        updated_at        TEXT NOT NULL,
                        superseded_by     INTEGER
                    );

                    CREATE INDEX IF NOT EXISTS idx_memory_facts_user
                    ON memory_facts(user_id, status);

                    CREATE UNIQUE INDEX IF NOT EXISTS idx_memory_facts_active_key
                    ON memory_facts(user_id, category, memory_key)
                    WHERE status = 'active';

                    CREATE TABLE IF NOT EXISTS trip_history (
                        id                  INTEGER PRIMARY KEY AUTOINCREMENT,
                        user_id             TEXT NOT NULL,
                        session_id          TEXT NOT NULL,
                        city                TEXT NOT NULL,
                        start_date          TEXT,
                        end_date            TEXT,
                        request_json        TEXT NOT NULL,
                        plan_json           TEXT NOT NULL,
                        preference_snapshot TEXT NOT NULL DEFAULT '[]',
                        degraded            INTEGER NOT NULL DEFAULT 0,
                        created_at          TEXT NOT NULL
                    );

                    CREATE INDEX IF NOT EXISTS idx_trip_history_user_city
                    ON trip_history(user_id, city, created_at DESC);
                    """
                )
            self._schema_ready = True

    @staticmethod
    def _now() -> datetime:
        return datetime.now(timezone.utc)

    @staticmethod
    def _dump(model: TripRequest | TripPlan | Dict[str, Any]) -> str:
        if isinstance(model, dict):
            payload = model
        else:
            payload = model.model_dump(mode="json")
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))

    def get_session(
        self, session_id: str, *, user_id: Optional[str] = None
    ) -> Optional[SessionMemory]:
        """读取会话;会话不存在返回 ``None``,用户不匹配抛 ``MemoryAccessError``。"""
        session_id = (session_id or "").strip()
        if not session_id:
            return None

        self._ensure_schema()
        self.purge_expired()
        with self._connect() as connection:
            row = connection.execute(
                """
                SELECT session_id, user_id, base_request_json, request_json, plan_json,
                       version, updated_at
                FROM session_state
                WHERE session_id = ?
                """,
                (session_id,),
            ).fetchone()

        if row is None:
            return None

        owner = row["user_id"] or ""
        requester = (user_id or "").strip()
        if owner and requester and owner != requester:
            raise MemoryAccessError("该会话不属于当前用户")

        return SessionMemory(
            session_id=row["session_id"],
            user_id=owner,
            base_request=json.loads(row["base_request_json"]),
            request=json.loads(row["request_json"]),
            plan=json.loads(row["plan_json"]),
            version=int(row["version"]),
            updated_at=row["updated_at"],
        )

    def save_successful_plan(
        self,
        *,
        session_id: str,
        user_id: Optional[str],
        request: TripRequest,
        plan: TripPlan,
        last_message: str = "",
        reset_base: bool = False,
    ) -> SessionMemory:
        """保存一次成功规划;降级/占位行程不应调用这里。

        ``reset_base=False`` 用于会话内 refine:保留第一次完整请求作为稳定基座,
        只更新当前请求与最新行程。``reset_base=True`` 用于一次全新的规划。
        """
        session_id = (session_id or "").strip()
        if not session_id:
            raise ValueError("session_id 不能为空")

        self._ensure_schema()
        self.purge_expired()
        owner = (user_id or "").strip()
        now = self._now().isoformat()
        request_json = self._dump(request)
        plan_json = self._dump(plan)

        with self._connect() as connection:
            # 先拿写锁,避免两个并发请求同时读到同一个 version 再各自 +1。
            connection.execute("BEGIN IMMEDIATE")
            row = connection.execute(
                "SELECT user_id, base_request_json, version FROM session_state WHERE session_id = ?",
                (session_id,),
            ).fetchone()

            if row is not None and row["user_id"] and owner and row["user_id"] != owner:
                raise MemoryAccessError("该会话不属于当前用户")

            if row is None:
                base_request_json = request_json
                version = 1
            else:
                base_request_json = request_json if reset_base else row["base_request_json"]
                version = int(row["version"]) + 1

            connection.execute(
                """
                INSERT INTO session_state (
                    session_id, user_id, base_request_json, request_json,
                    plan_json, version, last_message, updated_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                ON CONFLICT(session_id) DO UPDATE SET
                    user_id = excluded.user_id,
                    base_request_json = excluded.base_request_json,
                    request_json = excluded.request_json,
                    plan_json = excluded.plan_json,
                    version = excluded.version,
                    last_message = excluded.last_message,
                    updated_at = excluded.updated_at
                """,
                (
                    session_id,
                    owner or (row["user_id"] if row is not None else ""),
                    base_request_json,
                    request_json,
                    plan_json,
                    version,
                    last_message,
                    now,
                ),
            )
            connection.execute(
                """
                INSERT INTO trip_history (
                    user_id, session_id, city, start_date, end_date,
                    request_json, plan_json, preference_snapshot, degraded, created_at
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0, ?)
                """,
                (
                    owner,
                    session_id,
                    request.city,
                    request.start_date.isoformat(),
                    request.end_date.isoformat(),
                    request_json,
                    plan_json,
                    json.dumps(request.preferences, ensure_ascii=False),
                    now,
                ),
            )

        saved = self.get_session(session_id, user_id=owner or None)
        if saved is None:  # pragma: no cover - 写入后立刻读取,理论上不可能
            raise RuntimeError("会话保存后无法读取")
        return saved

    @staticmethod
    def _row_to_fact(row: sqlite3.Row) -> MemoryFact:
        return MemoryFact(
            id=int(row["id"]),
            user_id=row["user_id"],
            category=row["category"],
            key=row["memory_key"],
            value=json.loads(row["value_json"]),
            source_text=row["source_text"],
            source_session_id=row["source_session_id"],
            confidence=float(row["confidence"]),
            status=row["status"],
            created_at=row["created_at"],
            updated_at=row["updated_at"],
            superseded_by=(
                int(row["superseded_by"]) if row["superseded_by"] is not None else None
            ),
        )

    def get_facts(
        self,
        user_id: str,
        *,
        status: Optional[str] = "active",
        category: Optional[str] = None,
        key: Optional[str] = None,
    ) -> List[MemoryFact]:
        """按用户读取偏好事实,默认只返回当前生效的记录。"""
        user_id = (user_id or "").strip()
        if not user_id:
            return []

        clauses = ["user_id = ?"]
        params: List[Any] = [user_id]
        if status:
            clauses.append("status = ?")
            params.append(status.strip())
        if category and category.strip():
            clauses.append("category = ?")
            params.append(category.strip())
        if key and key.strip():
            clauses.append("memory_key = ?")
            params.append(key.strip())

        self._ensure_schema()
        with self._connect() as connection:
            rows = connection.execute(
                f"""
                SELECT id, user_id, category, memory_key, value_json, source_text,
                       source_session_id, confidence, status, created_at, updated_at,
                       superseded_by
                FROM memory_facts
                WHERE {' AND '.join(clauses)}
                ORDER BY updated_at DESC, id DESC
                """,
                params,
            ).fetchall()
        return [self._row_to_fact(row) for row in rows]

    def upsert_fact(
        self,
        *,
        user_id: str,
        category: str,
        key: str,
        value: Any,
        source_text: str = "",
        source_session_id: Optional[str] = None,
        confidence: float = 1.0,
    ) -> MemoryFact:
        """写入一条显式偏好事实。

        同值写入只刷新出处和时间;值变化时先把旧记录标记为 superseded,再插入
        active 新记录。调用方负责保证 ``value`` 是用户明确表达的信息。
        """
        user_id = (user_id or "").strip()
        category = (category or "").strip()
        key = (key or "").strip()
        if not user_id:
            raise ValueError("user_id 不能为空")
        if not category:
            raise ValueError("category 不能为空")
        if not key:
            raise ValueError("key 不能为空")
        if not 0.0 <= confidence <= 1.0:
            raise ValueError("confidence 必须在 0 到 1 之间")

        self._ensure_schema()
        now = self._now().isoformat()
        value_json = json.dumps(value, ensure_ascii=False, separators=(",", ":"))

        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            current = connection.execute(
                """
                SELECT id, value_json
                FROM memory_facts
                WHERE user_id = ? AND category = ? AND memory_key = ?
                  AND status = 'active'
                """,
                (user_id, category, key),
            ).fetchone()

            if current is not None and json.loads(current["value_json"]) == value:
                connection.execute(
                    """
                    UPDATE memory_facts
                    SET source_text = ?, source_session_id = ?, confidence = ?,
                        updated_at = ?
                    WHERE id = ?
                    """,
                    (
                        source_text,
                        (source_session_id or "").strip() or None,
                        confidence,
                        now,
                        current["id"],
                    ),
                )
                fact_id = int(current["id"])
            else:
                if current is not None:
                    connection.execute(
                        """
                        UPDATE memory_facts
                        SET status = 'superseded', updated_at = ?
                        WHERE id = ?
                        """,
                        (now, current["id"]),
                    )

                cursor = connection.execute(
                    """
                    INSERT INTO memory_facts (
                        user_id, category, memory_key, value_json, source_text,
                        source_session_id, confidence, status, created_at, updated_at
                    )
                    VALUES (?, ?, ?, ?, ?, ?, ?, 'active', ?, ?)
                    """,
                    (
                        user_id,
                        category,
                        key,
                        value_json,
                        source_text,
                        (source_session_id or "").strip() or None,
                        confidence,
                        now,
                        now,
                    ),
                )
                fact_id = int(cursor.lastrowid)

                if current is not None:
                    connection.execute(
                        """
                        UPDATE memory_facts
                        SET superseded_by = ?
                        WHERE id = ?
                        """,
                        (fact_id, current["id"]),
                    )

            row = connection.execute(
                """
                SELECT id, user_id, category, memory_key, value_json, source_text,
                       source_session_id, confidence, status, created_at, updated_at,
                       superseded_by
                FROM memory_facts
                WHERE id = ?
                """,
                (fact_id,),
            ).fetchone()

        if row is None:  # pragma: no cover - 写入后立刻读取,理论上不可能
            raise RuntimeError("偏好事实保存后无法读取")
        return self._row_to_fact(row)

    def delete_user_memory(self, user_id: str) -> MemoryDeletion:
        """物理删除一个用户的偏好、历史行程和服务端会话。"""
        user_id = (user_id or "").strip()
        if not user_id:
            raise ValueError("user_id 不能为空")

        self._ensure_schema()
        with self._connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            facts = connection.execute(
                "DELETE FROM memory_facts WHERE user_id = ?", (user_id,)
            ).rowcount
            history = connection.execute(
                "DELETE FROM trip_history WHERE user_id = ?", (user_id,)
            ).rowcount
            sessions = connection.execute(
                "DELETE FROM session_state WHERE user_id = ?", (user_id,)
            ).rowcount

        return MemoryDeletion(
            user_id=user_id,
            facts=int(facts or 0),
            history=int(history or 0),
            sessions=int(sessions or 0),
        )

    def purge_expired(self) -> int:
        """删除超过 TTL 的会话,返回删除条数。"""
        self._ensure_schema()
        ttl_days = max(1, int(get_settings().memory_session_ttl_days))
        cutoff = (self._now() - timedelta(days=ttl_days)).isoformat()
        with self._connect() as connection:
            cursor = connection.execute(
                "DELETE FROM session_state WHERE updated_at < ?", (cutoff,)
            )
            return int(cursor.rowcount or 0)


_memory_store: Optional[MemoryStore] = None


def get_memory_store() -> MemoryStore:
    """获取进程级 MemoryStore 单例。"""
    global _memory_store
    if _memory_store is None:
        _memory_store = MemoryStore(get_settings().memory_db_path)
    return _memory_store


def reset_memory_store() -> None:
    """重置单例(测试或切换数据库路径时使用)。"""
    global _memory_store
    _memory_store = None
