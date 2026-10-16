#!/usr/bin/env python
# -*- coding:utf-8 -*-
# author:PZW
# datetime:2026/10/09 10:48
# software:Vscode
# brief : 异步调用模型并返回文本回答

import json

from openai import AsyncOpenAI
from private_module.config import get_settings

class EmptyModelResponseError(Exception):
    """模型未返回有效文本回答。"""
async def ask_model(question: str, sources: list[dict] | None = None) -> str:
    """
    根据用户问题调用模型，返回文本回答。
    ：param question: 用户输入的问题字符串
    :param sources: 检索到的参考段落，提供时仅依据资料回答
    :return: 模型返回的文本回答
    """
    if not question.strip():
        raise ValueError("问题不能为空")
    
    settings = get_settings()
    async with AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=settings.openai_timeout,
        max_retries=settings.openai_max_retries,
    ) as client:
        response = await client.responses.create(
            model = settings.openai_model,
            instructions=settings.knowledge_instructions if sources is not None else None,
            input=json.dumps({"question": question, "reference_materials": sources}, ensure_ascii=False) if sources is not None else question,
            max_output_tokens=settings.model_max_output_tokens,
            store=False,
        )

    if not response.output_text.strip():
        raise EmptyModelResponseError("模型未返回有效回答")
    return response.output_text
