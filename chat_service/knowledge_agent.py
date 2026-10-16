#!/usr/bin/env python
# -*- coding:utf-8 -*-
# author:PZW
# datetime:2026/10/15 10:55
# software:Vscode
# brief : 使用 LangGraph 编排知识库工具调用与回答流程

import asyncio
import json
import logging
import re
from typing import Literal, TypedDict

from langgraph.graph import END, START, StateGraph
from openai import AsyncOpenAI
from pydantic import ValidationError

from chat_service.knowledge_tools import (
    execute_knowledge_tool,
    knowledge_tool_definitions,
)
from chat_service.model_client import EmptyModelResponseError

logger = logging.getLogger(__name__)


class AgentState(TypedDict):
    """保存一次用户提问中的模型输入、工具结果和执行状态。"""

    messages: list
    pending_calls: list
    sources: list[dict]
    trace: list[dict]
    signatures: list[str]
    tool_calls: int
    context_chars: int
    answer: str
    review_passed: bool
    review_feedback: str
    rewrite_count: int


async def run_knowledge_agent(question: str,store,settings,history:list[dict] | None=None,) -> dict:
    """
    创建本次请求的执行图，运行知识库问答 Agent。

    :param question: 当前用户问题，第一版不包含历史对话
    :param store: 当前应用的文档存储实例
    :param settings: 当前应用配置
    :param question: 当前用户问题
    :param history: 当前会话近期问答，仅用于理解指代
    :return: 回答、本轮读取来源及工具执行摘要
    """
    conversation_history=list(history or [])
    async with AsyncOpenAI(
        api_key=settings.openai_api_key.get_secret_value(),
        timeout=settings.openai_timeout,
        max_retries=settings.openai_max_retries,
    ) as client:

        async def model_node(state: AgentState) -> dict:
            """
            请求模型，由模型生成工具调用或最终回答。

            :param state: 当前图状态
            :return: 更新后的消息、待执行调用及回答
            """
            allow_tools = (
                state["tool_calls"] < settings.agent_max_tool_calls
                and state["context_chars"] < settings.agent_context_chars
            )
            response = await client.responses.create(
                model=settings.openai_model,
                instructions=settings.agent_instructions,
                input=state["messages"],
                tools=knowledge_tool_definitions(),
                tool_choice="auto" if allow_tools else "none",
                parallel_tool_calls=False,
                max_output_tokens=settings.model_max_output_tokens,
                store=False,
            )
            calls = [
                item for item in response.output
                if item.type == "function_call"
            ]
            if calls and not allow_tools:
                raise EmptyModelResponseError("模型未遵守工具调用限制")
            if not calls and not response.output_text.strip():
                raise EmptyModelResponseError("模型未返回有效回答")
            return {
                "messages": [
                    *state["messages"],
                    *response.output,
                ],
                "pending_calls": calls,
                "answer": response.output_text if not calls else "",
            }

        async def tools_node(state: AgentState) -> dict:
            """
            执行模型请求，合并来源并回传工具结果。

            :param state: 包含待执行工具调用的状态
            :return: 更新后的工具结果、来源和调用计数
            """
            messages = list(state["messages"])
            sources = list(state["sources"])
            trace = list(state["trace"])
            signatures = list(state["signatures"])
            tool_calls = state["tool_calls"]
            context_chars = state["context_chars"]
            for call in state["pending_calls"]:
                output = {}
                status = "ok"
                try:
                    if tool_calls >= settings.agent_max_tool_calls:
                        raise ValueError("已达到工具调用上限")
                    tool_calls += 1
                    arguments = json.loads(call.arguments)
                    signature = call.name + json.dumps(
                        arguments, sort_keys=True, ensure_ascii=False
                    )
                    if signature in signatures:
                        raise ValueError("相同工具和参数已调用，请换关键词")
                    signatures.append(signature)
                    output = await asyncio.to_thread(
                        execute_knowledge_tool,
                        call.name,
                        call.arguments,
                        store,
                        settings,
                    )
                    returned_sources = []
                    for source in output.get("sources", []):
                        existing = next(
                            (
                                item for item in sources
                                if item["document_id"] == source["document_id"]
                                and item["chunk_number"] == source["chunk_number"]
                            ),
                            None,
                        )
                        if existing is not None:
                            returned_sources.append(existing)
                            continue
                        if (
                            context_chars + len(source["content"])
                            > settings.agent_context_chars
                        ):
                            output["truncated"] = True
                            continue
                        numbered_source = dict(
                            source,
                            reference_number=len(sources) + 1,
                        )
                        sources.append(numbered_source)
                        returned_sources.append(numbered_source)
                        context_chars += len(source["content"])
                    if "sources" in output:
                        output["sources"] = returned_sources
                except (ValueError, ValidationError) as error:
                    status = "rejected"
                    output = {"error": str(error)[:500]}
                messages.append({
                    "type": "function_call_output",
                    "call_id": call.call_id,
                    "output": json.dumps(output, ensure_ascii=False),
                })
                trace.append({
                    "tool": call.name,
                    "status": status,
                    "source_count": len(output.get("sources", [])),
                })
                logger.info(
                    "知识库工具调用 tool=%s status=%s",
                    call.name,
                    status,
                )
            return {
                "messages": messages,
                "pending_calls": [],
                "sources": sources,
                "trace": trace,
                "signatures": signatures,
                "tool_calls": tool_calls,
                "context_chars": context_chars,
            }
        async def review_node(state: AgentState) -> dict:
            """
            检查答案引用编号及原文依据。

            :param state: 包含草稿和工具结果的当前状态
            :return: 检查结果、意见及执行记录
            """
            cited_numbers = {
                int(number)
                for number in re.findall(
                    r"\[([1-9]\d*)\]",
                    state["answer"],
                )
            }
            available_numbers = {
                source["reference_number"]
                for source in state["sources"]
            }
            passed = False
            feedback = "草稿引用了本轮不存在的来源编号"

            if not cited_numbers - available_numbers:
                tool_results = [
                    json.loads(message["output"])
                    for message in state["messages"]
                    if isinstance(message, dict)
                    and message.get("type") == "function_call_output"
                ]
                response = await client.responses.create(
                    model=settings.openai_model,
                    instructions=settings.agent_review_instructions,
                    input=json.dumps(
                        {
                            "question": question,
                            "conversation_history": conversation_history,
                            "draft": state["answer"],
                            "sources": state["sources"],
                            "tool_results": tool_results,
                        },
                        ensure_ascii=False,
                    ),
                    max_output_tokens=settings.model_max_output_tokens,
                    store=False,
                )
                try:
                    review = json.loads(response.output_text)
                    if (
                        not isinstance(review, dict)
                        or type(review.get("passed")) is not bool
                        or not isinstance(review.get("reason"), str)
                        or not review["reason"].strip()
                    ):
                        raise ValueError("检查结果格式不正确")
                    passed = review["passed"]
                    feedback = review["reason"][:2000]
                except ValueError:
                    # 检查结果无法解析时，不直接放行草稿。
                    feedback = "依据检查未返回有效结果，需要重新检查"

            logger.info(
                "答案依据检查 passed=%s rewrite_count=%s",
                passed,
                state["rewrite_count"],
            )
            return {
                "review_passed": passed,
                "review_feedback": feedback,
                "trace": [
                    *state["trace"],
                    {
                        "stage": "answer_review",
                        "status": "passed" if passed else "rejected",
                        "rewrite_count": state["rewrite_count"],
                    },
                ],
            }

        async def rewrite_node(state: AgentState) -> dict:
            """
            根据检查意见重写一次，不执行额外检索。

            :param state: 草稿、资料及检查意见
            :return: 修改后的答案和重写次数
            """
            tool_results = [
                json.loads(message["output"])
                for message in state["messages"]
                if isinstance(message, dict)
                and message.get("type") == "function_call_output"
            ]
            response = await client.responses.create(
                model=settings.openai_model,
                instructions=settings.agent_rewrite_instructions,
                input=json.dumps(
                    {
                        "question": question,
                        "conversation_history": conversation_history,
                        "draft": state["answer"],
                        "feedback": state["review_feedback"],
                        "sources": state["sources"],
                        "tool_results": tool_results,
                    },
                    ensure_ascii=False,
                ),
                max_output_tokens=settings.model_max_output_tokens,
                store=False,
            )
            return {
                "answer": response.output_text.strip(),
                "rewrite_count": state["rewrite_count"] + 1,
                "trace": [
                    *state["trace"],
                    {
                        "stage": "answer_rewrite",
                        "status": "completed",
                    },
                ],
            }

        def fallback_node(state: AgentState) -> dict:
            """
            重写后仍未通过检查时，返回保守提示。

            :param state: 当前检查状态
            :return: 不包含未经确认结论的提示
            """
            return {
                "answer": (
                    "本次回答未通过资料依据检查，"
                    "暂时无法可靠确认具体要求。"
                    "请核对相关原文，或补充更明确的制度资料。"
                ),
            }
        def route_after_model(
            state: AgentState,
        ) -> Literal["tools", "review"]:
            """
            模型提出工具调用则继续执行，否则进入答案检查。

            :param state: 模型更新后的状态
            :return: tools 或 review
            """
            return "tools" if state["pending_calls"] else "review"

        def route_after_review(
            state: AgentState,
        ) -> Literal["finish", "rewrite", "fallback"]:
            """
            根据检查结果决定结束、重写或返回保守提示。

            :param state: 检查后的状态
            :return: 下一步分支名称
            """
            if state["review_passed"]:
                return "finish"
            if state["rewrite_count"] == 0:
                return "rewrite"
            return "fallback"

        graph_builder = StateGraph(AgentState)
        graph_builder.add_node("model", model_node)
        graph_builder.add_node("tools", tools_node)
        graph_builder.add_node("review", review_node)
        graph_builder.add_node("rewrite", rewrite_node)
        graph_builder.add_node("fallback", fallback_node)

        graph_builder.add_edge(START, "model")
        graph_builder.add_conditional_edges(
            "model",
            route_after_model,
            {
                "tools": "tools",
                "review": "review",
            },
        )
        graph_builder.add_edge("tools", "model")

        graph_builder.add_conditional_edges(
            "review",
            route_after_review,
            {
                "finish": END,
                "rewrite": "rewrite",
                "fallback": "fallback",
            },
        )
        graph_builder.add_edge("rewrite", "review")
        graph_builder.add_edge("fallback", END)

        graph = graph_builder.compile()

        initial_state: AgentState = {
            "messages": [{"role": "user", "content":json.dumps({
                "question":question,
                "conversation_history":conversation_history,
            },
            ensure_ascii=False,
            ),}],
            "pending_calls": [],
            "sources": [],
            "trace": [],
            "signatures": [],
            "tool_calls": 0,
            "context_chars": 0,
            "answer": "",
            "review_passed": False,
            "review_feedback": "",
            "rewrite_count": 0,
        }
        result = await graph.ainvoke(
            initial_state,
            config={
                "recursion_limit": settings.agent_max_tool_calls * 2 + 10,            },
        )

    answer = result["answer"]
    retrieved_sources = result["sources"]

    # 从答案中提取 [1]、[2] 这样的引用编号。
    cited_numbers = {
        int(number)
        for number in re.findall(r"\[([1-9]\d*)\]", answer)
    }
    available_numbers = {
        source["reference_number"]
        for source in retrieved_sources
    }

    # 防止答案引用本轮没有读取过的来源。
    if cited_numbers - available_numbers:
        logger.error("知识库回答包含不存在的来源编号")
        raise EmptyModelResponseError("回答引用了不存在的来源")

    cited_sources = [
        source
        for source in retrieved_sources
        if source["reference_number"] in cited_numbers
    ]

    return {
        "answer": answer,
        "sources": cited_sources,
        "retrieved_sources": retrieved_sources,
        "grounded": bool(cited_sources),
        "agent_trace": result["trace"],
        "review_status": ("passed" if result["review_passed"] else "fallback"),
        "rewrite_count": result["rewrite_count"],
        "history_turns": len(conversation_history),
    }