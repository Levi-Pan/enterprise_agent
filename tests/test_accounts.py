#!/usr/bin/env python
# -*- coding:utf-8 -*-
# author:PZW
# datetime:2026/10/11 12:40
# software:Vscode
# brief : 验证账号登录、会话持久化与不同用户的历史隔离

import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

from fastapi.testclient import TestClient

from chat_service.api_service import create_app
from chat_service.model_client import EmptyModelResponseError
from private_module.config import SESSION_COOKIE_NAME, Settings


class AccountTests(unittest.TestCase):
    """使用真实隔离数据库和模拟模型验证所有账号边界。"""

    def setUp(self):
        """
        创建独立数据库与测试凭据。

        :param self: 测试实例
        :return: 无
        """
        self.directory = tempfile.TemporaryDirectory()
        self.settings = Settings(_env_file=None, openai_api_key="test-only", openai_model="test-only",
                                 admin_api_token="test-admin", password_hash_iterations=100000,
                                 knowledge_db_path=os.path.join(self.directory.name, "test.db"))
        self.alice = {"username": "alice", "password": "test-password-123"}
        self.bob = {"username": "bob", "password": "test-password-123"}

    def tearDown(self):
        """
        清理隔离数据库。

        :param self: 测试实例
        :return: 无
        """
        self.directory.cleanup()

    def test_login_cookie_hashing_and_logout(self):
        """
        密码和令牌只存摘要，账号大小写统一，注销令牌不能复用。

        :param self: 测试实例
        :return: 无
        """
        with TestClient(create_app(self.settings)) as client:
            response = client.post("/auth/register", json=dict(self.alice, username="Alice"))
            self.assertEqual(response.status_code, 201)
            self.assertEqual(response.json()["username"], "alice")
            self.assertNotIn("password", response.text)
            self.assertIn("HttpOnly", response.headers["set-cookie"])
            self.assertIn("SameSite=lax", response.headers["set-cookie"])
            token = client.cookies.get(SESSION_COOKIE_NAME)
            self.assertEqual(client.post("/auth/register", json=self.alice).status_code, 409)
            client.post("/auth/register", json=self.bob)
            with client.app.state.account_store.connection() as connection:
                hashes = [row["password_hash"] for row in connection.execute("SELECT password_hash FROM users")]
                self.assertNotEqual(hashes[0], hashes[1])
                self.assertTrue(all(self.alice["password"] not in value for value in hashes))
                self.assertFalse(connection.execute("SELECT 1 FROM sessions WHERE token_hash = ?", (token,)).fetchone())
            logout_token = client.cookies.get(SESSION_COOKIE_NAME)
            self.assertEqual(client.post("/auth/logout").status_code, 200)
            client.cookies.set(SESSION_COOKIE_NAME, logout_token)
            self.assertEqual(client.get("/conversations").status_code, 401)
            self.assertEqual(client.post("/auth/login", json=dict(self.alice, password="wrong-password")).status_code, 401)
            self.assertEqual(client.post("/auth/login", json=self.alice).status_code, 200)

    def test_unauthenticated_and_cross_origin_requests(self):
        """
        未登录不能看公司资料或历史，跨站不能注册和写入。

        :param self: 测试实例
        :return: 无
        """
        with TestClient(create_app(self.settings)) as client:
            for path in ("/documents", "/workspace", "/conversations"):
                self.assertEqual(client.get(path).status_code, 401)
            self.assertEqual(client.post("/chat", json={"question": "hello"}).status_code, 401)
            self.assertEqual(client.post("/auth/register", json=self.alice, headers={"Origin": "https://other.example"}).status_code, 403)
            self.assertEqual(client.post("/auth/register", json=self.alice).status_code, 201)
            self.assertEqual(client.post("/auth/logout", headers={"Origin": "https://other.example"}).status_code, 403)
            self.assertIsNotNone(client.get("/auth/status").json()["user"])

    def test_history_isolation_and_restart(self):
        """
        两个账号共享文档，彼此不能查看、追加或删除对方会话，重启后历史恢复。

        :param self: 测试实例
        :return: 无
        """
        model = AsyncMock(return_value="Approval requires a manager.[1]")
        with TestClient(create_app(self.settings)) as alice, TestClient(create_app(self.settings)) as bob, patch("chat_service.api_service.ask_model", model):
            alice.post("/auth/register", json=self.alice)
            bob.post("/auth/register", json=self.bob)
            alice.app.state.document_store.save_document("shared.docx", b"synthetic", ["Approval requires a manager."])
            self.assertEqual(len(bob.get("/documents").json()), 1)
            response = alice.post("/chat", json={"question": "Approval?"})
            self.assertEqual(response.status_code, 200)
            conversation_id = response.json()["conversation_id"]
            alice.post("/chat", json={"question": "manager?", "conversation_id": conversation_id})
            self.assertEqual(len(alice.get("/conversations").json()), 1)
            detail = alice.get("/conversations/" + conversation_id).json()
            self.assertEqual(len(detail["turns"]), 2)
            self.assertEqual(detail["turns"][0]["sources"][0]["content"], "Approval requires a manager.")
            self.assertEqual(bob.get("/conversations").json(), [])
            self.assertEqual(bob.get("/conversations/" + conversation_id).status_code, 404)
            self.assertEqual(bob.delete("/conversations/" + conversation_id).status_code, 404)
            model.reset_mock()
            self.assertEqual(bob.post("/chat", json={"question": "manager?", "conversation_id": conversation_id}).status_code, 404)
            model.assert_not_called()
            bob.post("/chat", json={"question": "Unmatched topic"})
            bob_id = bob.get("/conversations").json()[0]["conversation_id"]
        with TestClient(create_app(self.settings)) as client:
            client.post("/auth/login", json=self.alice)
            self.assertEqual(client.get("/conversations/" + conversation_id).json(), detail)
            self.assertEqual(client.get("/conversations/" + bob_id).status_code, 404)
            self.assertEqual(client.delete("/conversations/" + conversation_id).status_code, 200)
            self.assertEqual(client.get("/conversations").json(), [])
            self.assertEqual(len(client.get("/documents").json()), 1)
            with client.app.state.account_store.connection() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM chat_turns WHERE conversation_id = ?", (conversation_id,)).fetchone()[0], 0)

    def test_missing_material_and_model_failure_are_saved(self):
        """
        未找到资料和模型失败都留下可恢复的记录。

        :param self: 测试实例
        :return: 无
        """
        with TestClient(create_app(self.settings)) as client:
            client.post("/auth/register", json=self.alice)
            missing = client.post("/chat", json={"question": "Unmatched"}).json()
            detail = client.get("/conversations/" + missing["conversation_id"]).json()
            self.assertFalse(detail["turns"][0]["grounded"])
            self.assertEqual(detail["turns"][0]["status"], "complete")
            client.app.state.document_store.save_document("test.docx", b"test", ["Approval policy"])
            with patch("chat_service.api_service.ask_model", AsyncMock(side_effect=EmptyModelResponseError("synthetic failure"))):
                self.assertEqual(client.post("/chat", json={"question": "Approval"}).status_code, 502)
            conversation_id = client.get("/conversations").json()[0]["conversation_id"]
            failed = client.get("/conversations/" + conversation_id).json()["turns"][0]
            self.assertEqual(failed["status"], "failed")
            self.assertTrue(failed["answer"])

    def test_lockout_registration_switch_and_expired_sessions(self):
        """
        验证登录失败限制、关闭注册以及失效会话。

        :param self: 测试实例
        :return: 无
        """
        settings = self.settings.model_copy(update={"allow_self_registration": False, "login_failure_limit": 2})
        with TestClient(create_app(settings)) as client:
            self.assertEqual(client.post("/auth/register", json=self.alice).status_code, 403)
            client.app.state.account_store.register(self.alice["username"], self.alice["password"])
            for attempt in range(2):
                self.assertEqual(client.post("/auth/login", json=dict(self.alice, password="wrong-password")).status_code, 401)
            self.assertEqual(client.post("/auth/login", json=self.alice).status_code, 429)
            with client.app.state.account_store.connection() as connection:
                connection.execute("DELETE FROM login_failures")
            self.assertEqual(client.post("/auth/login", json=self.alice).status_code, 200)
            with client.app.state.account_store.connection() as connection:
                connection.execute("UPDATE sessions SET expires_at = 0")
            self.assertEqual(client.get("/conversations").status_code, 401)


if __name__ == "__main__":
    unittest.main()
