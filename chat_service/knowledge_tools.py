#!/usr/bin/env python
# -*- coding:utf-8 -*-
# author:PZW
# datetime:2026/10/15 11:18
# software:Vscode
# brief : 定义企业知识库只读工具并检验调用参数

from pydantic import BaseModel, ConfigDict, Field


class SearchArgument(BaseModel):
    """知识检索参数"""
    model_config = ConfigDict(extra = "forbid",strict = True)
    query: str = Field(min_length=1,max_length=2000)


class ReadArgument(BaseModel):
    """按命中知识块定位章节的参数"""
    model_config = ConfigDict(extra = "forbid",strict = True)
    document_id: str = Field(min_length=1,max_length=128)
    chunk_number: int = Field(ge=0)


class ListArgument(BaseModel):
    """查看文档目录，无需参数"""
    model_config = ConfigDict(extra = "forbid",strict = True)



TOOL_ARGUMENT_MODELS = {
    "search_knowledge": SearchArgument,
    "read_document_section": ReadArgument,
    "list_documents": ListArgument,
}

TOOL_DESCRIPTIONS ={
    "search_knowledge": "检索知识库，返回相关原文和来源信息。",
    "read_document_section": (
        "根据搜索结果中的document_id和chunk_number,"
        "读取同章节内容；内容可能因长度限制而截断。"
    ),
    "list_documents":"查看知识库文档目录，不返回正文。",
}


def knowledge_tool_definitions() -> list[dict]:
    """
    构造 Responses API 的函数工具定义。

    :param: 无
    :return: 包含名称、说明和参数约束的工具列表
    """
    return[{
        "type": "function",
        "name": name,
        "description": TOOL_DESCRIPTIONS[name],
        "parameters": argument_model.model_json_schema(),
        "strict": True,
    }
        for name,argument_model in TOOL_ARGUMENT_MODELS.items()
    ]


def execute_knowledge_tool(name:str,raw_arguments:str,store,settings,) -> dict:
    """
    校验模型参数，执行白名单中的只读工具。

    :param name: 模型请求的工具名称
    :param raw_arguments: 模型生成的 JSON 参数
    :param store: 当前应用的文档存储实例
    :param settings: 当前应用配置
    :return: 文档目录或知识块，不包含文件路径及密钥
    """
    if name not in TOOL_ARGUMENT_MODELS:
        raise ValueError("工具名称不在允许范围内")
    arguments = TOOL_ARGUMENT_MODELS[name].model_validate_json(raw_arguments)

    if name == "list_documents":
        documents = store.list_documents()
        return{"documents":documents[:settings.agent_document_limit],"truncated":len(documents) > settings.agent_document_limit,}
    
    if name == "search_knowledge":
        return {"sources": store.retrieve(question=arguments.query,limit=settings.retrieval_top_k,mode=settings.retrieval_mode,),}
    
    return store.read_section(
        document_id=arguments.document_id,
        chunk_number=arguments.chunk_number,
        limit=settings.agent_section_chunks,
    )