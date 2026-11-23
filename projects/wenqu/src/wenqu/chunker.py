"""分块：长度上限 ~600 字符 + 相邻重叠 ~80 字符。

重叠的目的：防截断语义反转（"不推荐"被切成"推荐"，风险 16）——
相邻 chunk 携带上一块尾部，保证否定词等上下文在边界处仍可被检索与阅读。

chunk_id = '{file_id}:{seq}'：身份锚定，rename 后不变。
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass

from .parsers import Section

MAX_LEN = 600
OVERLAP = 80
# 段落合并预算：为重叠前缀预留空间
_MAX_BODY = MAX_LEN - OVERLAP - 1

_SENT_SPLIT = re.compile(r"(?<=[。！？；!?;])\s*")


@dataclass
class ChunkData:
    chunk_id: str
    seq: int
    content: str
    heading_path: str | None
    doc_meta: str | None  # JSON 字符串


def _split_long(para: str) -> list[str]:
    """超长段落：句子边界优先，硬切兜底。"""
    if len(para) <= _MAX_BODY:
        return [para]
    out: list[str] = []
    buf = ""
    for s in _SENT_SPLIT.split(para):
        if not s:
            continue
        if len(buf) + len(s) <= _MAX_BODY:
            buf += s
        else:
            if buf:
                out.append(buf)
            while len(s) > _MAX_BODY:
                out.append(s[:_MAX_BODY])
                s = s[_MAX_BODY:]
            buf = s
    if buf:
        out.append(buf)
    return out


def _pieces(text: str) -> list[str]:
    """段落切分 + 相邻小段合并，每段 ≤ _MAX_BODY。"""
    paras = [p.strip() for p in re.split(r"\n\s*\n", text) if p.strip()]
    pieces: list[str] = []
    for p in paras:
        pieces.extend(_split_long(p))
    merged: list[str] = []
    for p in pieces:
        if merged and len(merged[-1]) + len(p) + 1 <= _MAX_BODY:
            merged[-1] += "\n" + p
        else:
            merged.append(p)
    return merged


def chunk_document(file_id: int, sections: list[Section]) -> list[ChunkData]:
    chunks: list[ChunkData] = []
    seq = 0
    tail = ""
    for sec in sections:
        for piece in _pieces(sec.text):
            if tail and not piece.startswith(tail):
                content = f"{tail}\n{piece}"
            else:
                content = piece
            if len(content) > MAX_LEN:
                content = content[:MAX_LEN]
            if not content.strip():
                continue
            chunks.append(
                ChunkData(
                    chunk_id=f"{file_id}:{seq}",
                    seq=seq,
                    content=content,
                    heading_path=sec.heading_path,
                    doc_meta=(
                        json.dumps(sec.meta, ensure_ascii=False) if sec.meta else None
                    ),
                )
            )
            seq += 1
            tail = content[-OVERLAP:] if len(content) > OVERLAP else content
    return chunks
