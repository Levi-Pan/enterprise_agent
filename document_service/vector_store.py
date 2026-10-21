#!/usr/bin/env python
# -*- coding:utf-8 -*-
# author:PZW
# datetime:2026/10/16 14:08
# software:Vscode
# brief : 管理本地语义向量索引及知识块检索

import logging
import os

import chromadb
from sentence_transformers import SentenceTransformer

logger = logging.getLogger(__name__)
class VectorStore:
    """
    管理本地Chroma向量索引，不负责保存账户和原文件
    ：param settings:项目配置
    ：return:无
    """
    def __init__(self,settings) :
        """
        保存向量库配置
        ：param settings:项目配置对象
        ：return:无
        """
        self.settings = settings
        self.model = None
        self.client = None
        self.collection = None


    def initialize(self) -> None:
        """
        加载向量模型，打开持久化数据库和知识集合。
        ：param self:当前向量存储实例
        ：return: 无
        """
        if self.collection is not None:
            return

        if not os.path.isabs(self.settings.vector_db_path):
            raise ValueError("向量数据库必须使用绝对路径")
        if not os.path.isabs(self.settings.embedding_cache_path):
            raise ValueError("模型缓存必须使用绝对路径")

        os.makedirs(self.settings.vector_db_path,exist_ok=True)
        os.makedirs(self.settings.embedding_cache_path,exist_ok=True)

        logger.info("开始加载向量模型：%s",self.settings.embedding_model)
        self.model = SentenceTransformer(self.settings.embedding_model,cache_folder = self.settings.embedding_cache_path,device = "cpu",)
        self.client = chromadb.PersistentClient(path=self.settings.vector_db_path,)
        self.collection = self.client.get_or_create_collection(name=self.settings.vector_collection_name,embedding_function=None,)
        logger.info("向量数据库初始化完成")


    def upsert_document(self,document_id:str,chunks:list[dict]) -> None:
        """
        分批生成文档块向量，并写入知识集合。
        ：param document_id: SQLite 中的文档标识
        ：param chunks：document_chunks 返回的知识块列表
        ：return：无
        """
        if not chunks:
            raise ValueError("文档没有可建立索引的知识块")
        if any(chunk["document_id"] != document_id for chunk in chunks):
            raise ValueError("知识块的文档标识不一致")

        self.initialize()
        batch_size = min(self.settings.embedding_batch_size,self.client.get_max_batch_size(),)
        for start in range(0,len(chunks),batch_size):
            batch = chunks[start:start + batch_size]
            contents = [chunk["content"] for chunk in batch]
            embeddings = self.model.encode(contents,batch_size=batch_size,normalize_embeddings=True,show_progress_bar=False,convert_to_numpy=True,)

            self.collection.upsert(ids=[f"{document_id}:{chunk['chunk_number']}"
            for chunk in batch],
            embeddings = embeddings.tolist(),
            documents = contents,
            metadatas = [{
                "document_id":document_id,
                "chunk_number":chunk["chunk_number"],
                "document_name":chunk["document_name"],
            }
                for chunk in batch
            ],)
        logger.info("文档向量写入完成,document_id=%s,知识块数=%s",document_id,len(chunks),)


    def delete_document(self,document_id:str) -> None:
        """
        删除指定文档的全部向量索引。

        :param document_id: 要删除的文档标识
        :return: 无
        """
        if not document_id.strip():
            raise ValueError("文档标识不能为空")
        self.initialize()
        self.collection.delete(where={"document_id":document_id},)
        logger.info("文档向量索引清理完成：document_id=%s",document_id,)


    def search(self,question:str,limit:int) -> list[dict]:
        """
        根据问题检索相近的知识块。

        :param question: 用户问题
        :param limit: 最大返回数量
        :return: 按向量距离排列的知识块标识列表
        """
        question = question.strip()
        if not question or limit <= 0:
            return []
        self.initialize()
        chunk_count = self.collection.count()
        if chunk_count == 0:
            return []
        embeddings = self.model.encode([question],normalize_embeddings=True,show_progress_bar=False,convert_to_numpy=True,)
        result = self.collection.query(query_embeddings=embeddings.tolist(),n_results=min(limit,chunk_count),include=["metadatas","distances"],)
        return[{
            "document_id":metadata["document_id"],
            "chunk_number":metadata["chunk_number"],
            "distance":distance,
        }
        for metadata,distance in zip(result["metadatas"][0],result["distances"][0],)]


    def clear(self) -> None:
        """
        分批清空当前知识集合，保留集合本身。

        :param self: 当前向量存储实例
        :return: 无
        """
        self.initialize()
        batch_size = min(
            self.settings.embedding_batch_size,
            self.client.get_max_batch_size(),
        )

        while True:
            records = self.collection.get(
                limit=batch_size,
                include=[],
            )
            if not records["ids"]:
                break

            self.collection.delete(ids=records["ids"])

        logger.info("当前知识集合的向量索引已清空")
