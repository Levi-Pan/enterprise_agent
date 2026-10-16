#!/usr/bin/env python
# -*- coding:utf-8 -*-
# author:PZW
# datetime:2026/10/09 16:18
# software:Vscode
# brief : 将制度正文切分未带重叠的文本段落 

import re


def split_document(
    text: str,
    chunk_size: int,
    chunk_overlap: int,
) -> list[str]:
    """
    优先保留完整段落，超长段落按句子拆分，超长句子按字符截取。

    :param text: 提取后的文档正文
    :param chunk_size: 每块的最大字符数
    :param chunk_overlap: 超长句子截取时的重叠字符数
    :return: 切分后的文本块列表
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    if not 0 <= chunk_overlap < chunk_size:
        raise ValueError("重叠长度必须非负且小于段落长度")

    chunks = []
    current = ""
    paragraphs = [
        paragraph.strip()
        for paragraph in text.splitlines()
        if paragraph.strip()
    ]

    for paragraph in paragraphs:
        if len(paragraph) <= chunk_size:
            parts = [paragraph]
        else:
            parts = re.findall(
                r"[^。！？；.!?;]+[。！？；.!?;]*|[。！？；.!?;]+",
                paragraph,
            )

        for part in parts:
            if len(part) > chunk_size:
                if current:
                    chunks.append(current)
                    current = ""

                start = 0
                while start < len(part):
                    end = min(start + chunk_size, len(part))
                    chunks.append(part[start:end])
                    if end == len(part):
                        break
                    start = end - chunk_overlap
                continue

            separator = "\n" if current else ""
            if len(current) + len(separator) + len(part) > chunk_size:
                chunks.append(current)
                current = part
            else:
                current += separator + part

    if current:
        chunks.append(current)

    return chunks
                    
def split_document_blocks(
    blocks: list[dict],
    chunk_size: int,
    chunk_overlap: int
) -> list[dict]:
    """
    按章节切分结构化文档，保留来源位置，表格按行单独处理。

    :param blocks: read_docx_blocks 返回的文档内容块
    :param chunk_size: 每块的最大字符数
    :param chunk_overlap: 超长句子截取时的重叠字符数
    :return: 包含正文、章节和来源位置的知识块列表
    """
    if chunk_size <= 0:
        raise ValueError("chunk_size 必须大于 0")
    if not 0 <= chunk_overlap < chunk_size:
        raise ValueError("重叠长度必须非负且小于段落长度")
    
    chunks = []
    current = None
    for block in blocks:
        section_path = list(block["section_path"])
        position = block["source_position"]

        if block["type"] == "table":
            if current is not None:
                chunks.append(current)
                current = None

            for row_number,row in enumerate(block["rows"],start=1):
                if not any(cell.strip() for cell in row):
                    continue
                row_text = " | ".join(row)
                for content in split_document(
                    row_text, chunk_size, chunk_overlap
                ):
                    chunks.append({
                        "content": content,
                        "section_path": section_path.copy(),
                        "source_start": position,
                        "source_end": position,
                        "source_type": "table",
                        "table_row": row_number,
                    })
            continue

        # 遇到标题时结束上一组正文，避免跨章节合并。
        if block["type"] == "heading" and current is not None:
            chunks.append(current)
            current = None

        parts = split_document(
            block["content"], chunk_size, chunk_overlap
        )

        for content in parts:
            can_merge =(current is not None and current["section_path"] == section_path and len(current["content"]) + 1 + len(content) <= chunk_size)
            if can_merge:
                current["content"] += "\n" + content
                current["source_end"] = position
            else:
                if current is not None:
                    chunks.append(current)
                current = {
                    "content": content,
                    "section_path": section_path.copy(),
                    "source_start": position,
                    "source_end": position,
                    "source_type": "paragraph",
                    "table_row": None,
                }
    if current is not None:
        chunks.append(current)

    return chunks