"""问渠 wenqu —— 轻量跨来源知识检索工具。

本地文件 + 飞书文档快照 → 统一 SQLite 索引（jieba+BM25，可选向量混合）
wenqu-mcp：只读 STDIO MCP Server（检索）
wenqu-sync：launchd 常驻守护，唯一索引写入者（同步）
wenqu CLI：管理面（watch / feishu / config / userdict / archive / prune / reembed / rebuild / sync）
"""

__version__ = "0.1.0"
