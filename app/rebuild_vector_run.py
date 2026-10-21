#!/usr/bin/env python
# -*- coding:utf-8 -*-
# author:PZW
# datetime:2026/10/20 12:28
# software:Vscode
# brief : 根据 SQLite 中的现有文档全量重建向量索引

import logging
import os
import sys

PROJECT_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_PATH not in sys.path:
    sys.path.insert(0, PROJECT_PATH)

from document_service.document_store import DocumentStore
from document_service.vector_store import VectorStore
from private_module.config import get_settings


logger = logging.getLogger(__name__)


def main() -> None:
    """
    读取现有文档，清理并重新建立全部向量索引。

    :param: 无
    :return: 无
    """
    settings = get_settings()
    if not os.path.isfile(settings.knowledge_db_path):
        raise FileNotFoundError("未找到现有知识库，请检查数据库路径")

    vector_store = VectorStore(settings)
    store = DocumentStore(
        settings.knowledge_db_path,
        vector_store=vector_store,
    )
    documents = store.list_documents()
    vector_store.initialize()
    vector_store.clear()

    for number, document in enumerate(documents, start=1):
        store.rebuild_document_vector(document["document_id"])
        logger.info(
            "重建进度：%s/%s，文档：%s",
            number,
            len(documents),
            document["document_name"],
        )

    logger.info("全部向量索引重建完成，文档数：%s", len(documents))


if __name__ == "__main__":
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s %(message)s",
    )
    try:
        main()
    except Exception:
        logger.error("向量索引重建失败，请修复后重新运行", exc_info=True)
        raise