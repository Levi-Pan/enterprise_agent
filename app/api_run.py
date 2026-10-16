#!/usr/bin/env python
# -*- coding:utf-8 -*-
# author:PZW
# datetime:2026/10/07 12:26
# software:Vscode
# brief : 提供企业制度助手接口启动入口.

import os
import sys

import uvicorn

PROJECT_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_PATH not in sys.path:
    sys.path.insert(0, PROJECT_PATH)

from chat_service.api_service import create_app
from private_module.config import API_HOST, API_PORT


app = create_app()


if __name__ == "__main__":
    uvicorn.run(app, host=API_HOST, port=API_PORT)

