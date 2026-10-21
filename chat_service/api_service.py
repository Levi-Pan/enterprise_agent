#!/usr/bin/env python
# -*- coding:utf-8 -*-
# author:PZW
# datetime:2026/10/07 11:34
# software:Vscode
# brief : 创建接口应用并提供健康检查

import asyncio
import logging
import os
import secrets
from contextlib import asynccontextmanager
from io import BytesIO
from urllib.parse import urlsplit
from zipfile import BadZipFile, ZipFile

from fastapi import Depends, FastAPI, Form, Header, HTTPException, Request, UploadFile
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles
from lxml.etree import XMLSyntaxError
from openai import APIError, APITimeoutError
from pydantic import BaseModel, ConfigDict, Field

from chat_service.model_client import EmptyModelResponseError
from chat_service.account_service import current_user, require_reader, router as account_router
from chat_service.account_store import AccountStore
from chat_service.knowledge_agent import run_knowledge_agent
from document_service.document_processor import split_document, split_document_blocks
from document_service.document_reader import read_docx_blocks
from document_service.document_store import DocumentConflictError, DocumentStore
from private_module.config import FRONTEND_PATH, Settings, get_settings

logger = logging.getLogger(__name__)


class ChatRequest(BaseModel):
    """员工的制度问题。"""

    model_config = ConfigDict(str_strip_whitespace=True)
    question: str = Field(min_length=1, max_length=2000)
    conversation_id: str | None = Field(default=None, min_length=1, max_length=64)


class DocumentRequest(BaseModel):
    """正文预览请求。"""

    model_config = ConfigDict(str_strip_whitespace=True)
    document_name: str = Field(min_length=1, max_length=200)
    content: str = Field(min_length=1, max_length=500000)


class DocumentChunk(BaseModel):
    """带章节和来源信息的知识块。"""

    document_name: str
    chunk_number: int
    content: str
    section_path: list[str] = Field(default_factory=list)
    source_start: int | None = None
    source_end: int | None = None
    source_type: str = "paragraph"
    table_row: int | None = None


def require_admin(request: Request, x_admin_token: str = Header(default="")) -> None:
    """
    校验管理员密钥，未配置时关闭文档管理写入。

    :param request: 当前请求
    :param x_admin_token: 请求头中的管理员密钥
    :return: 无，失败时抛出 HTTP 异常
    """
    expected = request.app.state.settings.admin_api_token.get_secret_value()
    if not expected:
        raise HTTPException(status_code=503, detail="尚未配置管理员密钥，请先配置 ADMIN_API_TOKEN")
    if not x_admin_token or not secrets.compare_digest(x_admin_token.encode("utf-8"), expected.encode("utf-8")):
        raise HTTPException(status_code=401, detail="管理员密钥不正确或未填写")


def check_admin() -> dict:
    """
    返回管理员验证成功状态。

    :param: 无，管理员校验由依赖完成
    :return: 管理权限状态
    """
    return {"authorized": True}


def check_health() -> dict[str, str]:
    """
    返回服务健康状态。

    :param: 无
    :return: 健康状态
    """
    return {"status": "healthy"}


def workspace_status(request: Request) -> dict:
    """
    返回知识库规模和文档管理是否已配置。

    :param request: 当前请求
    :return: 不包含密钥的工作空间状态
    """
    documents = request.app.state.document_store.list_documents()
    return {
        "document_count": len(documents),
        "chunk_count": sum(document["chunk_count"] for document in documents),
        "admin_configured": bool(request.app.state.settings.admin_api_token.get_secret_value()),
    }


async def chat(payload: ChatRequest, request: Request, user: dict = Depends(current_user)) -> dict:
    """
    检索已保存的制度段落，依据资料回答并返回检索来源。

    :param payload: 员工问题
    :param request: 当前应用请求
    :param user: 服务端认证用户
    :return: 回答、来源及是否匹配资料
    """
    account_store = request.app.state.account_store
    try:
        conversation_id, turn_id = await asyncio.to_thread(
            account_store.begin_turn, user["user_id"], payload.question, payload.conversation_id,
        )
    except LookupError as error:
        raise HTTPException(status_code=404, detail=str(error)) from error
    try:
        history = await asyncio.to_thread(account_store.recent_history,user["user_id"],conversation_id,turn_id,)
        result = await answer_question(payload.question, request,history=history)
    except HTTPException as error:
        await asyncio.to_thread(account_store.finish_turn, user["user_id"], turn_id,
                                {"answer": str(error.detail)}, "failed")
        raise
    except Exception:
        await asyncio.to_thread(account_store.finish_turn, user["user_id"], turn_id,
                                {"answer": "回答未完成，请稍后重试"}, "failed")
        raise
    await asyncio.to_thread(account_store.finish_turn, user["user_id"], turn_id, result)
    return dict(result, conversation_id=conversation_id, turn_id=turn_id)


async def answer_question(question: str, request: Request, history:list[dict] | None = None) -> dict:
    """
    运行知识库 Agent，返回回答及本轮工具读取的来源。

    :param question: 当前用户问题
    :param request: 当前应用请求
    :param history: 当前会话近期已完成的问答
    :return: 回答、来源和工具执行摘要
    """
    settings = request.app.state.settings
    try:
        async with asyncio.timeout(settings.agent_timeout_seconds):
            async with request.app.state.model_slots:
                return await run_knowledge_agent(
                    question=question,
                    store=request.app.state.document_store,
                    settings=settings,
                    history=history,
                )
    except (TimeoutError, APITimeoutError) as error:
        logger.error("知识库 Agent 执行超时", exc_info=True)
        raise HTTPException(
            status_code=504,
            detail="问答执行超时，请稍后重试",
        ) from error
    except (APIError, EmptyModelResponseError) as error:
        logger.error("知识库 Agent 模型调用失败", exc_info=True)
        raise HTTPException(
            status_code=502,
            detail="模型服务暂时无法完成问答，请稍后重试",
        ) from error


def preview_document(payload: DocumentRequest, request: Request) -> list[DocumentChunk]:
    """
    切分文本并返回预览，不保存数据。

    :param payload: 文档正文
    :param request: 当前请求
    :return: 分段预览
    """
    settings = request.app.state.settings
    chunks = split_document(payload.content, settings.document_chunk_size, settings.document_chunk_overlap)
    return [
        DocumentChunk(document_name=payload.document_name, chunk_number=number, content=chunk)
        for number, chunk in enumerate(chunks, start=1)
    ]


def extract_upload(file: UploadFile, settings: Settings) -> tuple[str, bytes, list[dict]]:
    """
    校验文件大小及解压大小，提取上传的 Word 正文。

    :param file: 上传文件
    :param settings: 应用配置
    :return: 安全文件名、原始字节和结构化内容块
    """
    name = os.path.basename((file.filename or "").replace("\\", "/"))
    if not name.lower().endswith(".docx") or len(name) > 200:
        raise HTTPException(status_code=400, detail="请上传名称不超过 200 字符的 .docx 文件")
    original = file.file.read(settings.document_upload_max_bytes + 1)
    if len(original) > settings.document_upload_max_bytes:
        raise HTTPException(status_code=413, detail="文件过大，请压缩或拆分文档后重试")
    try:
        with ZipFile(BytesIO(original)) as archive:
            if sum(item.file_size for item in archive.infolist()) > settings.document_xml_max_bytes:
                raise HTTPException(status_code=413, detail="文档解压后过大，请拆分文档")
            blocks = read_docx_blocks(BytesIO(original))
    except (BadZipFile, ValueError, KeyError, XMLSyntaxError) as error:
        logger.warning("上传文档无法解析", exc_info=True)
        raise HTTPException(status_code=400, detail="文档损坏、格式不正确或未包含可读取的正文") from error
    return name, original, blocks


def preview_uploaded_document(file: UploadFile,request: Request,) -> list[DocumentChunk]:
    """
    读取 Word 结构并预览知识块，不保存数据。

    :param file: 上传的 Word 文件
    :param request: 当前请求
    :return: 包含章节和来源信息的分段预览
    """
    settings = request.app.state.settings
    name, _, blocks = extract_upload(file, settings)
    chunks = split_document_blocks(
        blocks,
        settings.document_chunk_size,
        settings.document_chunk_overlap,
    )
    return [
        DocumentChunk(
            document_name=name,
            chunk_number=number,
            **chunk,
        )
        for number, chunk in enumerate(chunks, start=1)
    ]

def import_document(file: UploadFile, request: Request, replace_existing: bool = Form(default=False)) -> dict:
    """
    持久化导入文档及检索索引，同名替换需要显式授权。

    :param file: 上传的 Word 文件
    :param request: 当前请求
    :param replace_existing: 是否替换同名文档
    :return: 文档导入结果
    """
    settings = request.app.state.settings
    name, original, blocks = extract_upload(file, settings)
    chunks = split_document_blocks(blocks,settings.document_chunk_size,settings.document_chunk_overlap,)
    try:
        result = request.app.state.document_store.save_document(name, original, chunks, replace_existing)
    except DocumentConflictError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    try:
        request.app.state.document_store.rebuild_document_vector(
            result["document_id"]
        )
    except Exception as error:
        logger.error(
            "文档已保存，但向量索引重建失败：document_id=%s",
            result["document_id"],
            exc_info=True,
        )
        raise HTTPException(
            status_code=503,
            detail="文档已保存，但向量索引建立失败，请稍后重新导入同一文档。",
        ) from error
    logger.info("文档导入完成 document_id=%s chunks=%s duplicate=%s", result["document_id"], result["chunk_count"], result["already_exists"])
    return result


def list_documents(request: Request) -> list[dict]:
    """
    返回已保存的公司文档目录。

    :param request: 当前请求
    :return: 文档列表
    """
    return request.app.state.document_store.list_documents()


def read_document(document_id: str, request: Request) -> list[dict]:
    """
    返回已保存文档的段落。

    :param document_id: 文档标识
    :param request: 当前请求
    :return: 正文段落
    """
    chunks = request.app.state.document_store.document_chunks(document_id)
    if not chunks:
        raise HTTPException(status_code=404, detail="文档不存在或已被删除")
    return chunks


def delete_document(document_id: str, request: Request) -> dict:
    """
    删除指定文档和索引，管理员权限由接口依赖校验。

    :param document_id: 文档标识
    :param request: 当前请求
    :return: 删除结果
    """
    try:
        deleted = request.app.state.document_store.delete_document(document_id)
    except Exception as error:
        logger.error("文档删除失败：document_id=%s",document_id,exc_info=True,)
        raise HTTPException(status_code=503,detail="文档删除未完成，请稍后重试。",) from error

    if not deleted:
        raise HTTPException(status_code=404,detail="文档不存在或已被删除",)
    logger.info("文档删除完成 document_id=%s", document_id)
    return {"deleted": True}


def home_page() -> FileResponse:
    """
    返回前端首页。

    :param: 无
    :return: 首页文件响应
    """
    return FileResponse(os.path.join(FRONTEND_PATH, "index.html"))


def create_app(settings_override: Settings | None = None) -> FastAPI:
    """
    创建本地知识库应用，通过生命周期初始化数据库。

    :param settings_override: 测试或部署时覆盖配置
    :return: FastAPI 应用
    """
    @asynccontextmanager
    async def lifespan(application):
        """
        初始化服务资源，数据库错误使启动失败。

        :param application: 当前应用
        :return: 生命周期上下文
        """
        settings = settings_override or get_settings()
        application.state.settings = settings

        vector_store = None
        if settings.retrieval_mode == "vector":
            from document_service.vector_store import VectorStore
            vector_store = VectorStore(settings)
            await asyncio.to_thread(vector_store.initialize)
        application.state.document_store = DocumentStore(settings.knowledge_db_path,vector_store=vector_store,)

        application.state.account_store = AccountStore(settings)
        application.state.model_slots = asyncio.Semaphore(settings.model_concurrency)
        await asyncio.to_thread(application.state.account_store.initialize)
        yield
    application = FastAPI(title="企业制度知识库", lifespan=lifespan)
    application.include_router(account_router)

    @application.middleware("http")
    async def protect_browser_requests(request, call_next):
        """
        拒绝跨站写入并禁止缓存账号、文档及个人历史响应。

        :param request: 当前请求
        :param call_next: 后续请求处理器
        :return: 受保护的响应
        """
        if request.method not in ("GET", "HEAD", "OPTIONS"):
            origin = request.headers.get("origin")
            if origin:
                parsed = urlsplit(origin)
                if parsed.scheme != request.url.scheme or parsed.netloc != request.url.netloc:
                    return JSONResponse(status_code=403, content={"detail": "不允许跨站提交"})
            if request.headers.get("sec-fetch-site") == "cross-site":
                return JSONResponse(status_code=403, content={"detail": "不允许跨站提交"})
        response = await call_next(request)
        if not request.url.path.startswith("/static/"):
            response.headers["Cache-Control"] = "no-store"
        return response
    application.add_api_route("/", home_page, methods=["GET"], include_in_schema=False)
    application.mount("/static", StaticFiles(directory=FRONTEND_PATH), name="static")
    application.add_api_route("/health", check_health, methods=["GET"])
    application.add_api_route("/workspace", workspace_status, methods=["GET"], dependencies=[Depends(require_reader)])
    application.add_api_route("/chat", chat, methods=["POST"])
    application.add_api_route("/documents/preview", preview_document, methods=["POST"], response_model=list[DocumentChunk], dependencies=[Depends(require_reader)])
    application.add_api_route("/documents/upload/preview", preview_uploaded_document, methods=["POST"], response_model=list[DocumentChunk], dependencies=[Depends(require_reader)])
    application.add_api_route("/admin/check", check_admin, methods=["GET"], dependencies=[Depends(require_admin)])
    application.add_api_route("/documents/upload", import_document, methods=["POST"], dependencies=[Depends(require_admin)])
    application.add_api_route("/documents", list_documents, methods=["GET"], dependencies=[Depends(require_reader)])
    application.add_api_route("/documents/{document_id}/chunks", read_document, methods=["GET"], dependencies=[Depends(require_reader)])
    application.add_api_route("/documents/{document_id}", delete_document, methods=["DELETE"], dependencies=[Depends(require_admin)])
    return application
