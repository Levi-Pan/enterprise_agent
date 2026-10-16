#!/usr/bin/env python
# -*- coding:utf-8 -*-
# author:PZW
# datetime:2026/10/11 12:34
# software:Vscode
# brief : 保存账号、登录会话和按用户隔离的问答历史

import hashlib
import json
import secrets
import sqlite3
import time
from datetime import datetime, timezone
from uuid import uuid4

from document_service.document_store import DocumentStore


class AccountStore(DocumentStore):
    """在统一数据库中保存身份与历史，每个历史查询都检查所属用户。"""

    def __init__(self, settings):
        """
        接收统一配置，复用短连接事务。

        :param settings: 应用配置
        :return: 无
        """
        super().__init__(settings.knowledge_db_path)
        self.settings = settings

    def initialize(self) -> None:
        """
        兼容初始化文档表，并增加账号和历史表，不修改已有正文。

        :param self: 当前存储实例
        :return: 无
        """
        super().initialize()
        with self.connection() as connection:
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id TEXT PRIMARY KEY,
                    username TEXT NOT NULL UNIQUE,
                    password_hash TEXT NOT NULL,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS sessions (
                    token_hash TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    expires_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS login_failures (
                    attempt_key TEXT PRIMARY KEY,
                    failures INTEGER NOT NULL,
                    blocked_until REAL NOT NULL,
                    updated_at REAL NOT NULL
                );
                CREATE TABLE IF NOT EXISTS conversations (
                    conversation_id TEXT PRIMARY KEY,
                    user_id TEXT NOT NULL REFERENCES users(user_id) ON DELETE CASCADE,
                    title TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS conversations_owner
                    ON conversations(user_id, updated_at DESC);
                CREATE TABLE IF NOT EXISTS chat_turns (
                    turn_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    conversation_id TEXT NOT NULL REFERENCES conversations(conversation_id) ON DELETE CASCADE,
                    question TEXT NOT NULL,
                    answer TEXT NOT NULL DEFAULT '',
                    sources TEXT NOT NULL DEFAULT '[]',
                    grounded INTEGER NOT NULL DEFAULT 0,
                    status TEXT NOT NULL DEFAULT 'pending',
                    created_at TEXT NOT NULL,
                    completed_at TEXT
                );
                CREATE INDEX IF NOT EXISTS turns_conversation
                    ON chat_turns(conversation_id, turn_id);
            """)

    def hash_password(self, password: str, salt: str | None = None, iterations: int | None = None) -> str:
        """
        使用独立随机盐和 PBKDF2 保存密码摘要。

        :param password: 用户输入密码，不写入数据库或日志
        :param salt: 校验时使用的原盐
        :param iterations: 校验时使用的原迭代次数
        :return: 带算法、迭代次数和盐的摘要
        """
        salt = salt or secrets.token_hex(16)
        iterations = iterations or self.settings.password_hash_iterations
        digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), bytes.fromhex(salt), iterations)
        return f"pbkdf2_sha256${iterations}${salt}${digest.hex()}"

    def register(self, username: str, password: str) -> dict:
        """
        创建独立用户，不自动授予文档管理权限。

        :param username: 已规范化账号
        :param password: 用户密码
        :return: 不含密码摘要的账号信息
        """
        user = {"user_id": str(uuid4()), "username": username}
        password_hash = self.hash_password(password)
        try:
            with self.connection() as connection:
                connection.execute(
                    "INSERT INTO users VALUES (?, ?, ?, ?)",
                    (user["user_id"], username, password_hash, datetime.now(timezone.utc).isoformat()),
                )
        except sqlite3.IntegrityError as error:
            raise ValueError("该账号已存在，请登录") from error
        return user

    def login(self, username: str, password: str, client_address: str) -> dict | None:
        """
        校验账号密码，按账号及来源地址限制失败重试。

        :param username: 已规范化账号
        :param password: 用户密码
        :param client_address: 请求来源地址
        :return: 账号信息，密码错误返回 None，锁定时抛出异常
        """
        attempt_key = hashlib.sha256((username + "\n" + client_address).encode()).hexdigest()
        now = time.time()
        with self.connection() as connection:
            failure = connection.execute("SELECT * FROM login_failures WHERE attempt_key = ?", (attempt_key,)).fetchone()
            if failure and failure["blocked_until"] > now:
                raise RuntimeError("登录失败次数过多，请稍后再试")
            user = connection.execute("SELECT * FROM users WHERE username = ?", (username,)).fetchone()
        if user:
            algorithm, iterations, salt, digest = user["password_hash"].split("$")
            valid = algorithm == "pbkdf2_sha256" and secrets.compare_digest(
                self.hash_password(password, salt, int(iterations)), user["password_hash"],
            )
        else:
            self.hash_password(password)
            valid = False
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if valid:
                connection.execute("DELETE FROM login_failures WHERE attempt_key = ?", (attempt_key,))
            else:
                failure = connection.execute("SELECT * FROM login_failures WHERE attempt_key = ?", (attempt_key,)).fetchone()
                count = failure["failures"] + 1 if failure and now - failure["updated_at"] < self.settings.login_lock_seconds else 1
                blocked_until = now + self.settings.login_lock_seconds if count >= self.settings.login_failure_limit else 0
                connection.execute(
                    "INSERT OR REPLACE INTO login_failures VALUES (?, ?, ?, ?)",
                    (attempt_key, count, blocked_until, now),
                )
        return {"user_id": user["user_id"], "username": user["username"]} if valid else None

    def create_session(self, user_id: str) -> str:
        """
        创建随机会话，数据库只保存令牌摘要。

        :param user_id: 登录用户标识
        :return: 仅交给 HttpOnly Cookie 的原始令牌
        """
        token = secrets.token_urlsafe(32)
        with self.connection() as connection:
            connection.execute("DELETE FROM sessions WHERE expires_at <= ?", (time.time(),))
            connection.execute(
                "INSERT INTO sessions VALUES (?, ?, ?)",
                (hashlib.sha256(token.encode()).hexdigest(), user_id, time.time() + self.settings.session_lifetime_seconds),
            )
        return token

    def session_user(self, token: str) -> dict | None:
        """
        查询有效会话所属账号，不接受客户端传入用户标识。

        :param token: Cookie 中的随机令牌
        :return: 有效账号信息或 None
        """
        if not token or len(token) > 200:
            return None
        with self.connection() as connection:
            row = connection.execute(
                "SELECT u.user_id, u.username FROM sessions s JOIN users u USING(user_id) "
                "WHERE s.token_hash = ? AND s.expires_at > ?",
                (hashlib.sha256(token.encode()).hexdigest(), time.time()),
            ).fetchone()
            return dict(row) if row else None

    def logout(self, token: str) -> None:
        """
        使当前登录令牌立即失效。

        :param token: 当前 Cookie 令牌
        :return: 无
        """
        with self.connection() as connection:
            connection.execute("DELETE FROM sessions WHERE token_hash = ?", (hashlib.sha256(token.encode()).hexdigest(),))

    def begin_turn(self, user_id: str, question: str, conversation_id: str | None) -> tuple[str, int]:
        """
        验证会话归属并在模型调用前保存问题，新会话从首个问题命名。

        :param user_id: 服务端认证的用户标识
        :param question: 本次问题
        :param conversation_id: 已有会话标识或 None
        :return: 会话标识和问答记录标识
        """
        timestamp = datetime.now(timezone.utc).isoformat()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if conversation_id:
                owner = connection.execute(
                    "SELECT 1 FROM conversations WHERE conversation_id = ? AND user_id = ?",
                    (conversation_id, user_id),
                ).fetchone()
                if not owner:
                    raise LookupError("会话不存在或无权访问")
            else:
                conversation_id = str(uuid4())
                connection.execute("INSERT INTO conversations VALUES (?, ?, ?, ?, ?)",
                                   (conversation_id, user_id, question[:60], timestamp, timestamp))
            cursor = connection.execute(
                "INSERT INTO chat_turns(conversation_id, question, created_at) VALUES (?, ?, ?)",
                (conversation_id, question, timestamp),
            )
            connection.execute("UPDATE conversations SET updated_at = ? WHERE conversation_id = ? AND user_id = ?",
                               (timestamp, conversation_id, user_id))
            return conversation_id, cursor.lastrowid
        

    def recent_history(self,user_id: str,conversation_id: str,before_turn_id: int,) -> list[dict]:
        """
        读取当前用户会话中、本轮之前最近完成的问答。

        :param user_id: 服务端认证用户标识
        :param conversation_id: 当前会话标识
        :param before_turn_id: 当前问答记录编号，不包含该记录
        :return: 按时间从旧到新排列、受长度限制的问答列表
        """
        if (
            self.settings.agent_history_turns == 0
            or self.settings.agent_history_chars == 0
        ):
            return []

        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT t.question, t.answer
                FROM chat_turns AS t
                JOIN conversations AS c
                    ON c.conversation_id = t.conversation_id
                WHERE c.user_id = ?
                    AND c.conversation_id = ?
                    AND t.turn_id < ?
                    AND t.status = 'complete'
                ORDER BY t.turn_id DESC
                LIMIT ?
                """,
                (
                    user_id,
                    conversation_id,
                    before_turn_id,
                    self.settings.agent_history_turns,
                ),
            ).fetchall()

        history = []
        used_chars = 0
        for row in rows:
            turn_chars = len(row["question"]) + len(row["answer"])
            if used_chars + turn_chars > self.settings.agent_history_chars:
                break
            history.append({
                "question": row["question"],
                "answer": row["answer"],
            })
            used_chars += turn_chars

        history.reverse()
        return history

    def finish_turn(self, user_id: str, turn_id: int, result: dict, status: str = "complete") -> None:
        """
        保存回答、引用和完成状态，更新时仍检查记录归属。

        :param user_id: 服务端认证的用户标识
        :param turn_id: 本次问答记录标识
        :param result: 回答及检索来源
        :param status: 完成或失败状态
        :return: 无
        """
        with self.connection() as connection:
            connection.execute(
                "UPDATE chat_turns SET answer = ?, sources = ?, grounded = ?, status = ?, completed_at = ? "
                "WHERE turn_id = ? AND conversation_id IN (SELECT conversation_id FROM conversations WHERE user_id = ?)",
                (result["answer"], json.dumps(result.get("sources", []), ensure_ascii=False),
                 int(result.get("grounded", False)), status, datetime.now(timezone.utc).isoformat(), turn_id, user_id),
            )

    def conversations(self, user_id: str) -> list[dict]:
        """
        列出当前用户的历史会话。

        :param user_id: 服务端认证的用户标识
        :return: 仅属于此用户的会话列表
        """
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT conversation_id, title, created_at, updated_at FROM conversations "
                "WHERE user_id = ? ORDER BY updated_at DESC", (user_id,),
            )]

    def conversation(self, user_id: str, conversation_id: str) -> dict | None:
        """
        读取自己的会话和全部问答，外部用户看不到是否存在该会话。

        :param user_id: 服务端认证的用户标识
        :param conversation_id: 请求的会话标识
        :return: 会话详情或 None
        """
        with self.connection() as connection:
            row = connection.execute("SELECT conversation_id, title FROM conversations WHERE conversation_id = ? AND user_id = ?",
                                     (conversation_id, user_id)).fetchone()
            if not row:
                return None
            result = dict(row)
            result["turns"] = []
            for turn in connection.execute("SELECT * FROM chat_turns WHERE conversation_id = ? ORDER BY turn_id", (conversation_id,)):
                item = dict(turn)
                item["sources"] = json.loads(item["sources"])
                item["grounded"] = bool(item["grounded"])
                result["turns"].append(item)
            return result

    def delete_conversation(self, user_id: str, conversation_id: str) -> bool:
        """
        删除当前用户的会话及问答记录，外部用户不能删除。

        :param user_id: 服务端认证的用户标识
        :param conversation_id: 待删除会话标识
        :return: 是否实际删除
        """
        with self.connection() as connection:
            cursor = connection.execute("DELETE FROM conversations WHERE conversation_id = ? AND user_id = ?",
                                        (conversation_id, user_id))
            return cursor.rowcount > 0
