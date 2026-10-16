#!/usr/bin/env python
# -*- coding:utf-8 -*-
# author:PZW
# datetime:2026/10/09 17:00
# software:Vscode
# brief : 从上传的 Word 文件中提取正文和表格文本


import re
from typing import BinaryIO
from docx import Document
from docx.table import Table
from docx.text.paragraph import Paragraph

def read_docx(file_stream:BinaryIO) -> str:
    """
    按文档顺序提取正文段落和表格文本。
    ：param file_stream: Word 文件的二进制流
    ：return: 提取的文本字符串
    """
    document = Document(file_stream)
    text_parts = []
    for block in document.iter_inner_content():
        if isinstance(block, Paragraph) and block.text.strip():
            text_parts.append(block.text.strip())
        elif isinstance(block, Table):
            for row in block.rows:
                row_text =  " | ".join(cell.text.strip() for cell in row.cells )
                if row_text.replace("|","").strip():
                    text_parts.append(row_text)
    text = "\n".join(text_parts)
    if not text.strip():
        raise ValueError("文档未包含可读取的正文")
    return text


def detect_heading_level(content:str,style_name:str,) -> int | None:
    """
    优先识别 Word 标题样式，再识别短文本中的编号标题。

    :param content: 当前段落内容
    :param style_name: Word 段落样式名称
    :return: 标题层级，普通正文返回 None
    """
    style_match = re.fullmatch(r"(?:Heading|标题)\s*([1-9])",style_name, flags=re.IGNORECASE,)
    if style_match:
        return int(style_match.group(1))
    
    # 仅对简短，没有句末标点的独立段落推断标题。
    if len(content) > 40 or re.search(r"[。！？；.!?;]", content):
        return None
    
    number = r"[一二三四五六七八九十百千万零〇两\d]+"
    patterns = ((rf"第\s*{number}\s*章.*", 1),(rf"第\s*{number}\s*节.*", 2),(rf"第\s*{number}\s*条.*", 3),(rf"[一二三四五六七八九十百]+、\s*\S.*", 1),(rf"[（(]{number}[）)]\s*\S.*", 2),)

    for pattern, level in patterns:
        if re.fullmatch(pattern, content):
            return level

    return None   

def read_docx_blocks(file_stream: BinaryIO) -> list[dict]:
    """
    按文档顺序提取内容，逐行识别段落内部的标题。

    :param file_stream: Word 文件的二进制流
    :return: 包含章节路径及来源位置的内容块列表
    """
    document = Document(file_stream)
    blocks = []
    headings = {}

    for position, block in enumerate(
        document.iter_inner_content(),
        start=1,
    ):
        if isinstance(block, Paragraph):
            lines = [
                line.strip()
                for line in block.text.splitlines()
                if line.strip()
            ]
            style_name = block.style.name if block.style else ""

            for line_number, content in enumerate(lines):
                # Word 标题样式只用于段落第一行。
                # 后续行仍可通过“一、”“（一）”等编号识别标题。
                heading_level = detect_heading_level(
                    content,
                    style_name if line_number == 0 else "",
                )

                if heading_level is not None:
                    # 新标题替换同级标题，并清除其下级标题。
                    headings = {
                        level: title
                        for level, title in headings.items()
                        if level < heading_level
                    }
                    headings[heading_level] = content

                blocks.append({
                    "type": (
                        "heading"
                        if heading_level is not None
                        else "paragraph"
                    ),
                    "content": content,
                    "heading_level": heading_level,
                    "section_path": [
                        headings[level]
                        for level in sorted(headings)
                    ],
                    # 同一 Word 段落拆出的文本行共享来源位置。
                    "source_position": position,
                })

        elif isinstance(block, Table):
            rows = [
                [cell.text.strip() for cell in row.cells]
                for row in block.rows
            ]
            if not any(cell for row in rows for cell in row):
                continue

            blocks.append({
                "type": "table",
                "rows": rows,
                "section_path": [
                    headings[level]
                    for level in sorted(headings)
                ],
                "source_position": position,
            })

    if not blocks:
        raise ValueError("文档未包含可读取的正文或表格")

    return blocks