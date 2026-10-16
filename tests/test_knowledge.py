#!/usr/bin/env python
# -*- coding:utf-8 -*-
# author:PZW
# datetime:2026/10/9 18:27
# software:Vscode
# brief : 验证知识库导入权限、持久化、检索及来源问答行为

import os
import tempfile
import unittest
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from io import BytesIO
from unittest.mock import AsyncMock, patch

from docx import Document
from fastapi.testclient import TestClient

from chat_service.api_service import create_app
from chat_service.agent_evaluator import save_report
from document_service.document_store import DocumentStore
from private_module.config import Settings


def word_bytes(text: str) -> bytes:
    """
    在内存中生成测试 Word。

    :param text: 测试正文
    :return: 文件字节
    """
    document = Document()
    document.add_paragraph(text)
    stream = BytesIO()
    document.save(stream)
    return stream.getvalue()


class KnowledgeTests(unittest.TestCase):
    """覆盖真实数据库和 HTTP 接口，模型调用使用明确的替身。"""

    def setUp(self):
        """
        创建每个测试独立使用的数据库和配置。

        :param self: 测试实例
        :return: 无
        """
        self.directory = tempfile.TemporaryDirectory()
        self.settings = Settings(
            _env_file=None,
            openai_api_key="test-only-key",
            openai_model="test-only-model",
            admin_api_token="test-only-admin-token",
            knowledge_db_path=os.path.join(self.directory.name, "knowledge.db"),
        )
        self.headers = {"X-Admin-Token": "test-only-admin-token"}
        self.original = word_bytes("员工请假必须提前申请，经主管审批后生效。年假为五天。")

    def tearDown(self):
        """
        删除隔离测试数据。

        :param self: 测试实例
        :return: 无
        """
        self.directory.cleanup()

    @contextmanager
    def authenticated_client(self, settings=None):
        """
        为原有接口测试提供登录账号，不使用匿名身份访问公司资料。

        :param settings: 可选测试配置
        :return: 已登录客户端上下文
        """
        with TestClient(create_app(settings or self.settings)) as client:
            credentials = {"username": "knowledge-test", "password": "test-password-123"}
            response = client.post("/auth/register", json=credentials)
            if response.status_code == 409:
                response = client.post("/auth/login", json=credentials)
            self.assertIn(response.status_code, (200, 201))
            yield client

    def upload(self, client, original=None, headers=None, replace=False):
        """
        提交管理员导入请求。

        :param client: 测试客户端
        :param original: 可选文件字节
        :param headers: 可选鉴权头
        :param replace: 是否替换同名文档
        :return: HTTP 响应
        """
        return client.post(
            "/documents/upload",
            files={"file": ("请假制度.docx", original or self.original)},
            headers=self.headers if headers is None else headers,
            data={"replace_existing": str(replace).lower()},
        )

    def test_authentication_and_persistence(self):
        """
        未授权不能写入，授权导入后新应用实例仍能读取。

        :param self: 测试实例
        :return: 无
        """
        with self.authenticated_client() as client:
            self.assertEqual(self.upload(client, headers={}).status_code, 401)
            self.assertEqual(self.upload(client, headers={"X-Admin-Token": "wrong"}).status_code, 401)
            self.assertEqual(client.get("/documents").json(), [])
            response = self.upload(client)
            self.assertEqual(response.status_code, 200)
            document_id = response.json()["document_id"]
            self.assertEqual(client.get("/workspace").json()["document_count"], 1)
            self.assertEqual(client.delete(f"/documents/{document_id}").status_code, 401)
        with self.authenticated_client() as client:
            chunks = client.get(f"/documents/{document_id}/chunks").json()
            self.assertIn("主管审批", chunks[0]["content"])
            self.assertEqual(len(client.get("/documents").json()), 1)

    def test_duplicate_replacement_and_deletion(self):
        """
        重复导入不增长，同名更新需明确授权，删除同步清理索引。

        :param self: 测试实例
        :return: 无
        """
        with self.authenticated_client() as client:
            original_id = self.upload(client).json()["document_id"]
            duplicate = self.upload(client)
            self.assertTrue(duplicate.json()["already_exists"])
            updated = word_bytes("员工出差报销必须提交发票，财务人员审批。")
            self.assertEqual(self.upload(client, updated).status_code, 409)
            self.assertIn("主管审批", client.get(f"/documents/{original_id}/chunks").json()[0]["content"])
            replacement = self.upload(client, updated, replace=True)
            self.assertEqual(replacement.status_code, 200)
            self.assertEqual(replacement.json()["document_id"], original_id)
            self.assertEqual(len(client.get("/documents").json()), 1)
            store = DocumentStore(self.settings.knowledge_db_path)
            self.assertEqual(store.search("年假", 4), [])
            self.assertTrue(store.search("报销发票", 4))
            self.assertEqual(client.delete(f"/documents/{original_id}", headers=self.headers).status_code, 200)
            self.assertEqual(store.search("报销发票", 4), [])

    def test_grounded_chat_and_missing_material(self):
        """
        问答实际传入检索原文，没有匹配时不调用模型。

        :param self: 测试实例
        :return: 无
        """
        model = AsyncMock(return_value="员工请假需要主管审批。[1]")
        with self.authenticated_client() as client, patch("chat_service.api_service.ask_model", model):
            self.upload(client)
            result = client.post("/chat", json={"question": "请假需要谁审批？"}).json()
            self.assertTrue(result["grounded"])
            self.assertEqual(result["sources"][0]["reference_number"], 1)
            self.assertIn("主管审批", model.call_args.kwargs["sources"][0]["content"])
            model.reset_mock()
            missing = client.post("/chat", json={"question": "火星机器人"}).json()
            self.assertFalse(missing["grounded"])
            self.assertEqual(missing["sources"], [])
            model.assert_not_called()

    def test_upload_limits_and_invalid_documents(self):
        """
        损坏、空文档及超限文件不能写入数据库。

        :param self: 测试实例
        :return: 无
        """
        settings = self.settings.model_copy(update={"document_upload_max_bytes": 100000})
        with self.authenticated_client(settings) as client:
            self.assertEqual(self.upload(client, b"invalid").status_code, 400)
            self.assertEqual(self.upload(client, word_bytes("")).status_code, 400)
            self.assertEqual(self.upload(client, b"x" * 100001).status_code, 413)
            self.assertEqual(client.get("/documents").json(), [])

    def test_parallel_duplicate_import(self):
        """
        并发导入相同内容只创建一个文档和索引。

        :param self: 测试实例
        :return: 无
        """
        store = DocumentStore(self.settings.knowledge_db_path)
        store.initialize()
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda number: store.save_document("请假制度.docx", self.original, ["主管审批请假"]), range(4)))
        self.assertEqual(len({result["document_id"] for result in results}), 1)
        self.assertEqual(len(store.list_documents()), 1)
        self.assertEqual(len(store.search("请假审批", 4)), 1)

    def test_specific_section_retrieval(self):
        """
        指定编码风格时只返回对应章节，全文问题保留其他章节。

        :param self: 测试实例
        :return: 无
        """
        store = DocumentStore(self.settings.knowledge_db_path)
        store.initialize()
        store.save_document("代码规范.docx", b"synthetic", [
            {"content": "命名使用统一规则。", "section_path": ["一、命名规范"]},
            {"content": "缩进方式保持一致，规范大括号和注释。", "section_path": ["二、编码风格"]},
            {"content": "源码和测试分别存放。", "section_path": ["三、代码结构"]},
        ])
        for question in ("我们公司的编码风格是怎么样的", "编码的风格是什么"):
            sources = store.search(question, 4)
            self.assertTrue(sources)
            self.assertTrue(all(source["section_path"] == ["二、编码风格"] for source in sources))
        sources = store.search("总结整份代码规范，包括编码风格", 4)
        self.assertTrue(any(source["section_path"] != ["二、编码风格"] for source in sources))

    def test_evaluation_report_preserves_and_escapes_answers(self):
        """
        报告保留原始回答，HTML 中的模型和文档文本不会变成脚本。

        :param self: 测试实例
        :return: 无
        """
        report = {"status": "complete", "model": "test", "data_mode": "synthetic", "tests": [
            {"test_number": 1, "question": "<script>alert(1)</script>", "category": "unrelated",
             "seconds": 1, "answer": "<img src=x onerror=alert(1)>", "sources": [],
             "review": {"verdict": "pass", "reason": "synthetic", "suggestion": ""}},
        ]}
        report_base = os.path.join(self.directory.name, "report")
        save_report(report, report_base)
        with open(report_base + ".html", encoding="utf-8") as stream:
            content = stream.read()
        self.assertNotIn("<script>", content)
        self.assertNotIn("<img", content)
        self.assertIn("&lt;img", content)


if __name__ == "__main__":
    unittest.main()
