#!/usr/bin/env python
# -*- coding:utf-8 -*-
# author:PZW
# datetime:2026/10/11 12:14
# software:Vscode
# brief : 启动真实模型工具调用评测并生成问答报告

import argparse
import logging
import os
import sys

PROJECT_PATH = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if PROJECT_PATH not in sys.path:
    sys.path.insert(0, PROJECT_PATH)

from chat_service.agent_evaluator import run_evaluation


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    parser = argparse.ArgumentParser(description="真实 Agent 问答评测，默认仅使用合成文档")
    parser.add_argument("--company-data", action="store_true", help="明确允许本轮公司文档片段外发给配置模型")
    parser.add_argument("--questions", type=int, default=6, choices=range(1, 11))
    arguments = parser.parse_args()
    run_evaluation(arguments.company_data, arguments.questions)
