#!/usr/bin/env python
# -*- coding:utf-8 -*-
# author:PZW
# datetime:2026/10/9 18:23
# software:Vscode
# brief : 持久化制度文档并使用中文全文索引检索相关段落

import hashlib
import json
import os
import re
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from uuid import uuid4


class DocumentConflictError(Exception):
    """同名文档已存在，替换操作需要显式指定。"""


def tokenize_text(text: str, query: bool = False) -> list[str]:
    """
    将中文字符对和英文词转换为检索词，查询时去除常见问句词。

    :param text: 正文或问题
    :param query: 是否为查询文本
    :return: 去重后的检索词列表
    """
    if query:
        for phrase in ("请问", "什么", "怎么", "如何", "是否", "我们", "公司", "制度", "一下", "可以", "需要", "应该"):
            text = text.replace(phrase, " ")
    terms = []
    for part in re.findall(r"[a-zA-Z][a-zA-Z0-9_]*|[\u4e00-\u9fff]+|\d+", text.lower()):
        if re.fullmatch(r"[\u4e00-\u9fff]+", part):
            terms.extend(part[index:index + 2] for index in range(len(part) - 1))
        elif len(part) > 1:
            terms.append(part)
    return list(dict.fromkeys(terms))


def normalize_section_text(text: str) -> str:
    """
    去除章节编号、空白及连接词，用于匹配明确的章节名称。

    :param text: 章节标题或用户问题
    :return: 归一化文本
    """
    text = re.sub(
        r"^(?:第\s*[一二三四五六七八九十百千万零〇两\d]+\s*[章节条]|"
        r"[一二三四五六七八九十百\d]+[、.．]|"
        r"[（(][一二三四五六七八九十百\d]+[）)])\s*",
        "",
        text.strip(),
    )
    return re.sub(r"[\W_]+", "", text.lower()).replace("的", "")


def focus_section_sources(question: str, sources: list[dict]) -> list[dict]:
    """
    问题明确提及最末级章节时，只保留该章节及其分块。

    :param question: 用户问题
    :param sources: 按检索相关度排列的候选知识块
    :return: 聚焦章节的候选块，未匹配标题时保留原候选
    """
    if any(phrase in question for phrase in ("全文", "整份", "所有章节", "全部章节")):
        return sources
    normalized_question = normalize_section_text(question)
    matches = []
    for source in sources:
        section_path = source.get("section_path") or []
        if not section_path:
            continue
        title = normalize_section_text(section_path[-1])
        if len(title) >= 2 and title in normalized_question:
            matches.append(source)
    return matches or sources


class DocumentStore:
    """使用短连接和事务保存文档、原文件及段落索引。"""

    def __init__(self, database_path: str,vector_store=None):
        """
        保存数据库路径，不在构造阶段执行文件写入。

        :param database_path: 数据库绝对路径
        :return: 无
        """
        self.database_path = database_path
        self.vector_store = vector_store

    @contextmanager
    def connection(self):
        """
        创建独立连接，在退出时提交或回滚并关闭。

        :param self: 当前存储实例
        :return: SQLite 连接上下文
        """
        connection = sqlite3.connect(self.database_path, timeout=15)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    def initialize(self) -> None:
        """
        创建数据库目录、文档表和全文索引。

        :param self: 当前存储实例
        :return: 无
        """
        os.makedirs(os.path.dirname(self.database_path), exist_ok=True)
        with self.connection() as connection:
            connection.execute("PRAGMA journal_mode = WAL")
            connection.executescript("""
                CREATE TABLE IF NOT EXISTS documents (
                    document_id TEXT PRIMARY KEY,
                    document_name TEXT NOT NULL UNIQUE,
                    content_hash TEXT NOT NULL UNIQUE,
                    original_file BLOB NOT NULL,
                    chunk_count INTEGER NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS chunks (
                    chunk_id INTEGER PRIMARY KEY AUTOINCREMENT,
                    document_id TEXT NOT NULL REFERENCES documents(document_id) ON DELETE CASCADE,
                    chunk_number INTEGER NOT NULL,
                    content TEXT NOT NULL,
                    UNIQUE(document_id, chunk_number)
                );
                CREATE VIRTUAL TABLE IF NOT EXISTS chunk_search USING fts5(terms);
            """)
            # 兼容已有数据库，只添加尚不存在的字段
            connection.execute("BEGIN IMMEDIATE")
            existing_columns ={row["name"] for row in connection.execute("PRAGMA table_info(chunks)")}
            new_columns = {
                "section_path": "TEXT NOT NULL DEFAULT '[]'",
                "source_start": "INTEGER",
                "source_end": "INTEGER",
                "source_type": "TEXT NOT NULL DEFAULT 'paragraph'",
                "table_row": "INTEGER",
            }

            for column_name, definition in new_columns.items():
                if column_name not in existing_columns:
                    connection.execute(
                        f"ALTER TABLE chunks "
                        f"ADD COLUMN {column_name} {definition}"
                    )

    def list_documents(self) -> list[dict]:
        """
        返回文档目录，不返回原文件内容。

        :param self: 当前存储实例
        :return: 文档元数据列表
        """
        with self.connection() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT document_id, document_name, chunk_count, created_at, updated_at "
                "FROM documents ORDER BY updated_at DESC, document_id"
            )]

    def save_document(self, name: str, original: bytes, chunks: list[str | dict], replace_existing: bool = False) -> dict:
        """
        在一个事务内保存原文件和段落，避免重复导入或静默覆盖。

        :param name: 文档名称
        :param original: 原始 Word 文件字节
        :param chunks: 已切分的正文
        :param replace_existing: 是否允许替换同名文档
        :return: 文档元数据及是否已存在
        """
        if not chunks:
            raise ValueError("文档没有可保存的知识块")

        normalized_chunks = []
        for chunk in chunks:
            block = {"content": chunk} if isinstance(chunk, str) else chunk
            normalized_chunks.append({
                "content": block["content"],
                "section_path": block.get("section_path", []),
                "source_start": block.get("source_start"),
                "source_end": block.get("source_end"),
                "source_type": block.get("source_type", "paragraph"),
                "table_row": block.get("table_row"),
            })

        # 旧字符串块保持原有指纹；结构化块的指纹包含来源信息。
        if all(isinstance(chunk, str) for chunk in chunks):
            hash_content = "\n".join(chunks)
        else:
            hash_content = json.dumps(
                normalized_chunks,
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            )

        digest = hashlib.sha256(hash_content.encode("utf-8")).hexdigest()

        timestamp = datetime.now(timezone.utc).isoformat()
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            duplicate = connection.execute("SELECT * FROM documents WHERE content_hash = ?", (digest,)).fetchone()
            if duplicate:
                return self.document_metadata(duplicate, True)
            existing = connection.execute("SELECT * FROM documents WHERE document_name = ?", (name,)).fetchone()
            if existing and not replace_existing:
                raise DocumentConflictError("同名文档已存在，请勾选替换同名文档后重新导入")
            document_id = existing["document_id"] if existing else str(uuid4())
            created_at = existing["created_at"] if existing else timestamp
            if existing:
                self.remove_chunks(connection, document_id)
                connection.execute("DELETE FROM documents WHERE document_id = ?", (document_id,))
            connection.execute(
                "INSERT INTO documents VALUES (?, ?, ?, ?, ?, ?, ?)",
                (document_id, name, digest, original, len(chunks), created_at, timestamp),
            )
            for number, block in enumerate(normalized_chunks, start=1):
                cursor = connection.execute(
                    """
                    INSERT INTO chunks(
                        document_id, chunk_number, content,
                        section_path, source_start, source_end,
                        source_type, table_row
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                    """,
                    (
                        document_id,
                        number,
                        block["content"],
                        json.dumps(
                            block["section_path"],
                            ensure_ascii=False,
                        ),
                        block["source_start"],
                        block["source_end"],
                        block["source_type"],
                        block["table_row"],
                    ),
                )

                # 把章节标题也加入索引，支持按章节内容检索。
                search_text = "\n".join([
                    name,
                    *block["section_path"],
                    block["content"],
                ])
                terms = " ".join(tokenize_text(search_text))
                connection.execute(
                    "INSERT INTO chunk_search(rowid, terms) VALUES (?, ?)",
                    (cursor.lastrowid, terms),
                )
            row = connection.execute("SELECT * FROM documents WHERE document_id = ?", (document_id,)).fetchone()
            return self.document_metadata(row, False)

    @staticmethod
    def document_metadata(row, already_exists: bool) -> dict:
        """
        组装可公开返回的文档信息。

        :param row: 数据库记录
        :param already_exists: 是否为重复导入
        :return: 不包含文件和指纹的元数据
        """
        result = {key: row[key] for key in ("document_id", "document_name", "chunk_count", "created_at", "updated_at")}
        result["already_exists"] = already_exists
        return result

    @staticmethod
    def remove_chunks(connection, document_id: str) -> None:
        """
        在当前事务中删除段落和对应全文索引。

        :param connection: 当前数据库连接
        :param document_id: 文档标识
        :return: 无
        """
        connection.execute("DELETE FROM chunk_search WHERE rowid IN (SELECT chunk_id FROM chunks WHERE document_id = ?)", (document_id,))
        connection.execute("DELETE FROM chunks WHERE document_id = ?", (document_id,))

    def delete_document(self, document_id: str) -> bool:
        """
        删除文档、关键词索引，并清理已启用的向量索引。

        :param document_id: 文档标识
        :return: 是否实际删除了 SQLite 中的文档
        """
        with self.connection() as connection:
            connection.execute("BEGIN IMMEDIATE")
            self.remove_chunks(connection, document_id)
            cursor = connection.execute(
                "DELETE FROM documents WHERE document_id = ?",
                (document_id,),
            )

            if self.vector_store is not None:
                self.vector_store.delete_document(document_id)

            return cursor.rowcount > 0

    @staticmethod
    def chunk_metadata(row: sqlite3.Row) -> dict:
        """
        将查询记录转换为字典，并解析章节路径。

        :param row: 包含正文和来源字段的数据库记录
        :return: 可供接口和模型使用的知识块
        """
        result = dict(row)
        result["section_path"] = json.loads(result["section_path"])
        return result


    def document_chunks(self, document_id: str) -> list[dict]:
        """
        按编号返回文档知识块，包含章节及来源位置。

        :param document_id: 文档标识
        :return: 知识块列表
        """
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    c.document_id,
                    d.document_name,
                    c.chunk_number,
                    c.content,
                    c.section_path,
                    c.source_start,
                    c.source_end,
                    c.source_type,
                    c.table_row
                FROM chunks c
                JOIN documents d USING(document_id)
                WHERE c.document_id = ?
                ORDER BY c.chunk_number
                """,
                (document_id,),
            )
            return [self.chunk_metadata(row) for row in rows]


    def search(self, question: str, limit: int) -> list[dict]:
        """
        使用全文索引检索正文，同时返回章节及来源位置。

        :param question: 用户问题
        :param limit: 最大返回知识块数量
        :return: 按相关程度排列的知识块列表
        """
        terms = tokenize_text(question, query=True)[:64]
        if not terms:
            return []

        expression = " OR ".join(
            '"' + term.replace('"', '""') + '"'
            for term in terms
        )
        with self.connection() as connection:
            rows = connection.execute(
                """
                SELECT
                    c.document_id,
                    d.document_name,
                    c.chunk_number,
                    c.content,
                    c.section_path,
                    c.source_start,
                    c.source_end,
                    c.source_type,
                    c.table_row
                FROM chunk_search
                JOIN chunks c ON c.chunk_id = chunk_search.rowid
                JOIN documents d ON d.document_id = c.document_id
                WHERE chunk_search MATCH ?
                ORDER BY bm25(chunk_search), c.chunk_id
                LIMIT ?
                """,
                (expression, max(limit * 5, 20)),
            )
            sources = [self.chunk_metadata(row) for row in rows]
            return focus_section_sources(question, sources)[:limit]


    def search_vector(self,question: str,limit: int,vector_store,) -> list[dict]:
        """
        批量读取向量命中的知识块，保留检索排序。

        :param question: 用户问题
        :param limit: 最大返回数量
        :param vector_store: 向量存储实例
        :return: 包含正文和来源的知识块列表
        """
        hits = vector_store.search(question, limit)
        if not hits:
            return []

        placeholders = ", ".join("(?, ?)" for hit in hits)
        parameters = [value
            for hit in hits
            for value in (hit["document_id"], hit["chunk_number"])
        ]

        with self.connection() as connection:
            rows = connection.execute(
                f"""
                WITH targets(document_id, chunk_number) AS (VALUES {placeholders})
                SELECT
                    c.document_id, d.document_name, c.chunk_number,
                    c.content, c.section_path, c.source_start,
                    c.source_end, c.source_type, c.table_row
                FROM chunks c
                JOIN documents d USING(document_id)
                JOIN targets t
                    ON t.document_id = c.document_id
                    AND t.chunk_number = c.chunk_number
                """,
                parameters,
            )
            matched_chunks = {
                (row["document_id"], row["chunk_number"]):
                    self.chunk_metadata(row)
                for row in rows
            }

        sources = []
        for hit in hits:
            chunk = matched_chunks.get(
                (hit["document_id"], hit["chunk_number"])
            )
            if chunk is not None:
                sources.append({
                    **chunk,
                    "vector_distance": hit["distance"],
                })

        return focus_section_sources(question, sources)[:limit]

    def retrieve(self,question:str,limit:int,mode:str,) -> list[dict]:
        """
        根据配置选择检索方式，统一返回完整知识块。

        :param question: 用户问题
        :param limit: 最大返回数量
        :param mode: keyword 或 vector
        :return: 包含正文和来源信息的知识块列表
        """
        if mode == "keyword":
            return self.search(question,limit)

        if mode == "vector":
            if self.vector_store is None:
                raise RuntimeError("向量检索尚未初始化")
            return self.search_vector(question,limit,self.vector_store,)
        raise ValueError(f"不支持的检索方式：{mode}")


    def rebuild_document_vector(self, document_id: str) -> None:
        """
        使用 SQLite 中的最新正文重建指定文档的向量索引。

        :param document_id: 文档标识
        :return: 无
        """
        if self.vector_store is None:
            return

        chunks = self.document_chunks(document_id)
        if not chunks:
            raise ValueError("文档不存在或没有知识块")

        self.vector_store.delete_document(document_id)
        self.vector_store.upsert_document(document_id, chunks)


    def read_section(self,document_id: str,chunk_number: int,limit: int,) -> dict:
        """
        定位知识块，读取同章节中距离最近的若干块。

        :param document_id: 文档标识
        :param chunk_number: 命中的块编号
        :param limit: 最大返回数量
        :return: 来源列表及是否截断
        """
        if limit <= 0:
            raise ValueError("章节读取数量必须大于零")

        source_query = """
            SELECT
                c.document_id, d.document_name, c.chunk_number,
                c.content, c.section_path, c.source_start,
                c.source_end, c.source_type, c.table_row
            FROM chunks c
            JOIN documents d USING(document_id)
        """

        with self.connection() as connection:
            anchor = connection.execute(
                source_query + """
                    WHERE c.document_id = ? AND c.chunk_number = ?
                """,
                (document_id, chunk_number),
            ).fetchone()
            if anchor is None:
                raise ValueError("指定的文档或知识块不存在")

            if not json.loads(anchor["section_path"]):
                return {
                    "sources": [self.chunk_metadata(anchor)],
                    "truncated": False,
                }

            rows = connection.execute(
                source_query + """
                    WHERE c.document_id = ? AND c.section_path = ?
                    ORDER BY ABS(c.chunk_number - ?), c.chunk_number
                    LIMIT ?
                """,
                (
                    document_id,
                    anchor["section_path"],
                    chunk_number,
                    limit + 1,
                ),
            ).fetchall()

        sources = [
            self.chunk_metadata(row)
            for row in rows[:limit]
        ]
        return {
            "sources": sorted(
                sources,
                key=lambda chunk: chunk["chunk_number"],
            ),
            "truncated": len(rows) > limit,
        }
