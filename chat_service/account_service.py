#!/usr/bin/env python
# -*- coding:utf-8 -*-
# author:PZW
# datetime:2026/10/11 12:36
# software:Vscode
# brief : 提供账号注册登录和个人历史会话接口

import secrets

from fastapi import APIRouter, Depends, Header, HTTPException, Request, Response
from pydantic import BaseModel, Field, SecretStr, field_validator

from private_module.config import SESSION_COOKIE_NAME

router = APIRouter()


class Credentials(BaseModel):
    """账号可用字母、数字、邮箱符号，密码保留原始空白。"""

    username: str = Field(min_length=3, max_length=64, pattern=r"^[a-zA-Z0-9_.@-]+$")
    password: SecretStr = Field(min_length=8, max_length=128)

    @field_validator("username", mode="before")
    @classmethod
    def normalize_username(cls, value):
        """
        将账号统一为小写，避免同名账号大小写混淆。

        :param value: 用户输入的账号
        :return: 规范化账号
        """
        return value.strip().lower() if isinstance(value, str) else value


def current_user(request: Request) -> dict:
    """
    从有效 Cookie 会话取得用户，不从请求参数接受用户身份。

    :param request: 当前请求
    :return: 已认证账号，未登录时拒绝访问
    """
    user = request.app.state.account_store.session_user(request.cookies.get(SESSION_COOKIE_NAME, ""))
    if not user:
        raise HTTPException(status_code=401, detail="请先登录账号")
    return user


def require_reader(request: Request, x_admin_token: str = Header(default="")) -> None:
    """
    公司文档允许已登录用户读取，兼容管理员密钥的接口访问。

    :param request: 当前请求
    :param x_admin_token: 可选管理员密钥
    :return: 无
    """
    expected = request.app.state.settings.admin_api_token.get_secret_value()
    if x_admin_token and expected and secrets.compare_digest(x_admin_token.encode(), expected.encode()):
        return
    current_user(request)


def set_session(response: Response, request: Request, user: dict) -> dict:
    """
    创建服务端会话，仅通过 HttpOnly Cookie 返回令牌。

    :param response: 响应对象
    :param request: 当前请求
    :param user: 验证后的账号
    :return: 不含令牌的账号信息
    """
    token = request.app.state.account_store.create_session(user["user_id"])
    settings = request.app.state.settings
    response.set_cookie(SESSION_COOKIE_NAME, token, max_age=settings.session_lifetime_seconds,
                        httponly=True, secure=settings.session_cookie_secure, samesite="lax", path="/")
    return user


@router.get("/auth/status")
def auth_status(request: Request) -> dict:
    """
    返回当前登录状态及是否开放注册。

    :param request: 当前请求
    :return: 登录状态，不返回密码和令牌
    """
    user = request.app.state.account_store.session_user(request.cookies.get(SESSION_COOKIE_NAME, ""))
    return {"user": user, "registration_enabled": request.app.state.settings.allow_self_registration}


@router.post("/auth/register", status_code=201)
def register(payload: Credentials, request: Request, response: Response) -> dict:
    """
    创建员工账号并登录，注册开关由服务端配置控制。

    :param payload: 账号和密码
    :param request: 当前请求
    :param response: 响应对象
    :return: 新账号信息
    """
    if not request.app.state.settings.allow_self_registration:
        raise HTTPException(status_code=403, detail="注册已关闭，请联系管理员")
    try:
        user = request.app.state.account_store.register(payload.username, payload.password.get_secret_value())
    except ValueError as error:
        raise HTTPException(status_code=409, detail=str(error)) from error
    return set_session(response, request, user)


@router.post("/auth/login")
def login(payload: Credentials, request: Request, response: Response) -> dict:
    """
    校验密码并登录，不向客户端区分账号不存在和密码错误。

    :param payload: 账号和密码
    :param request: 当前请求
    :param response: 响应对象
    :return: 账号信息
    """
    try:
        user = request.app.state.account_store.login(payload.username, payload.password.get_secret_value(),
                                                     request.client.host if request.client else "unknown")
    except RuntimeError as error:
        raise HTTPException(status_code=429, detail=str(error)) from error
    if not user:
        raise HTTPException(status_code=401, detail="账号或密码不正确")
    return set_session(response, request, user)


@router.post("/auth/logout")
def logout(request: Request, response: Response) -> dict:
    """
    注销当前服务端会话，并清除浏览器 Cookie。

    :param request: 当前请求
    :param response: 响应对象
    :return: 注销结果
    """
    request.app.state.account_store.logout(request.cookies.get(SESSION_COOKIE_NAME, ""))
    response.delete_cookie(SESSION_COOKIE_NAME, path="/", httponly=True,
                           secure=request.app.state.settings.session_cookie_secure, samesite="lax")
    return {"logged_out": True}


@router.get("/conversations")
def list_conversations(request: Request, user: dict = Depends(current_user)) -> list[dict]:
    """
    列出登录用户自己的会话。

    :param request: 当前请求
    :param user: 服务端认证用户
    :return: 个人历史列表
    """
    return request.app.state.account_store.conversations(user["user_id"])


@router.get("/conversations/{conversation_id}")
def read_conversation(conversation_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    """
    读取自己的历史，其他账号的记录统一返回不存在。

    :param conversation_id: 会话标识
    :param request: 当前请求
    :param user: 服务端认证用户
    :return: 历史问答和引用原文
    """
    conversation = request.app.state.account_store.conversation(user["user_id"], conversation_id)
    if not conversation:
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")
    return conversation


@router.delete("/conversations/{conversation_id}")
def delete_conversation(conversation_id: str, request: Request, user: dict = Depends(current_user)) -> dict:
    """
    删除自己的历史会话，不影响公司文档或其他账号。

    :param conversation_id: 会话标识
    :param request: 当前请求
    :param user: 服务端认证用户
    :return: 删除结果
    """
    if not request.app.state.account_store.delete_conversation(user["user_id"], conversation_id):
        raise HTTPException(status_code=404, detail="会话不存在或无权访问")
    return {"deleted": True}
