# 问渠 wenqu

> 问渠那得清如许？为有源头活水来。

轻量跨来源知识检索工具：**本地文件 + 飞书文档快照 → 统一 SQLite 索引 → MCP 检索 + launchd 自动同步 + AI 客户端 Skill**。

把资料放进目录、或接入飞书空间，之后在 Trae / Codex / Claude Code 等任意 AI 客户端里直接对话检索——带完整出处、可追溯版本、支持对话式改配置，**你永远不需要手改任何配置文件或数据库**。

```
本地文件（md/txt/docx/xlsx/pptx/pdf，仅显式 watch 的目录）
飞书线上（config 圈定的知识空间/文件夹）
  │ wenqu-sync（launchd 常驻，唯一索引写入者）
  │ 本地：FSEvents 事件 + 30min 全扫 + SHA-256 变化判定
  │ 飞书：周期快照落地（docx/wiki → markdown，frontmatter 携带原文链接）
  ▼
单一 SQLite（WAL）：file_manifest + chunks + FTS5(jieba) [+ 向量(sqlite-vec)]
  ▲
各 AI 客户端 ── wenqu-mcp（只读 STDIO MCP，五工具，每查询直读库）
       └──── wenqu skill（检索规范 + 配置变更管理面）
```

## 特性

- **跨源统一**：本地文件与飞书快照同引擎、同索引、同召回路径；内容相同的本地原件与飞书快照自动合并为双出处
- **落盘即可搜**：文件写入后立即可检索，无需重启客户端（每查询直读 SQLite，WAL 读写不阻塞）
- **身份锚定**：文件改名/移动不重解析、不重嵌入、检索引用不断裂（file_id / remote_token 稳定身份）
- **中文优先**：jieba 应用层分词 + BM25（零编译依赖，领域词典热补）；可选向量混合检索（bge-small-zh / e5-large）
- **自动同步**：launchd 常驻守护，事件防抖 + 冷却补检 + 删除快路径 + 网盘断连冻结保护 + 云盘路径黑名单
- **对话式管理**：加目录、接飞书、换检索模式、加领域词、确认归档……全部通过 AI 客户端对话完成，agent 经 skill 执行 `wenqu` 命令
- **诚实检索**：needs_ocr / 解析失败 / 模型降级 / 孤儿快照（权限收回 / 线上删除）如实标注，不假装可搜；提示词注入防御
- **飞书快照保鲜**：周期增量落地 + 引用回链线上原文 + 重要结论实时核对 updated_at

## 环境要求

- macOS（launchd / FSEvents / TCC 检测；Apple Silicon 与 Intel 均可）
- [uv](https://docs.astral.sh/uv/)（Python ≥ 3.11 由 uv 自动管理）
- 磁盘余量 ≥ 5GB（方向三向量模型约 1–2GB）

## 安装

本仓库位于 [PM-Lab](https://github.com/cosin9527/PM-Lab) monorepo 的 [`projects/wenqu/`](https://github.com/cosin9527/PM-Lab/tree/main/projects/wenqu) 子目录。

### 方式一：clone 后安装（可改代码）

```bash
git clone https://github.com/cosin9527/PM-Lab.git
cd PM-Lab/projects/wenqu
brew install uv          # 若未安装
uv tool install --editable .
```

### 方式二：一行安装（免 clone，monorepo 子目录）

```bash
uv tool install --from "git+https://github.com/cosin9527/PM-Lab#subdirectory=projects/wenqu" wenqu
```

验证：

```bash
wenqu --help
which wenqu-mcp           # 通常为 ~/.local/bin/wenqu-mcp
```

## 快速开始（Day-0）

```bash
# 1. 初始化（建目录树 + 强制 watch 配置 + 自检 + 首次全量索引）
#    --mode keyword = 纯关键词（零模型依赖）；hybrid = 混合检索（首次下载 bge 模型约 0.1GB）
wenqu init --watch ~/Knowledge/Documents --mode hybrid

# 2. 查看状态
wenqu status

# 3.（可选）安装 launchd 守护：登录自启 + 崩溃自动拉起
wenqu sync start
```

### 注册 MCP（给你的 AI 客户端）

JSON（Trae 及通用）——`<你>` 替换为实际用户名：

```json
{ "mcpServers": { "wenqu": { "command": "/Users/<你>/.local/bin/wenqu-mcp", "env": { "WENQU_HOME": "/Users/<你>/Knowledge" } } } }
```

TOML（Codex，追加到 `~/.codex/config.toml`）：

```toml
[mcp_servers.wenqu]
command = "/Users/<你>/.local/bin/wenqu-mcp"

[mcp_servers.wenqu.env]
WENQU_HOME = "/Users/<你>/Knowledge"
```

## 安装 wenqu skill

仓库内 [skill/wenqu/SKILL.md](skill/wenqu/SKILL.md) 即安装物（检索规范 + 配置变更管理面）。
把它复制到你的 AI 客户端 skill 目录并重启客户端：

| 客户端 | skill 安装位置 |
|---|---|
| Trae | `~/.trae-cn/skills/wenqu/SKILL.md` |
| Codex CLI | `~/.codex/skills/wenqu/SKILL.md` |
| Claude Code | `~/.claude/skills/wenqu/SKILL.md` |
| 其他 | 探测 `~/.<client>/skills/`（workbuddy、千问工作、Cursor 等遵循各自文档） |

```bash
# 示例：Trae
mkdir -p ~/.trae-cn/skills/wenqu
cp skill/wenqu/SKILL.md ~/.trae-cn/skills/wenqu/SKILL.md
```

装好后，在对话里说「检索知识库：达人管理的历史方案」即可触发规范检索；说「把 XX 目录加进检索」即可对话式改配置。

## 日常使用

**检索**（agent 自动走 skill 流程）：

> 帮我查一下之前关于达人管理的产品方案，有没有讨论过好友关系监控？

返回带完整出处的回答：来源平台 / 文件名 / 章节 / 更新时间 / 逐字摘录 / 链接；多文档冲突并列披露不裁决。

**配置变更**（agent 执行 `wenqu` 命令并复述验证摘要）：

> 把 ~/Documents/Meeting 加进检索范围
> 接入飞书知识空间 https://feishu.cn/wiki/xxx
> 检索改回纯关键词模式

## CLI 一览

| 命令 | 行为 |
|---|---|
| `wenqu init` | Day-0 向导：目录树 / config / 强制 watch / 自检 / 模型下载 / 首扫 |
| `wenqu status` / `wenqu verify` | 状态摘要 / 安装自检 |
| `wenqu watch add <dir>` | 云盘黑名单 + TCC 自检 → 写入 → 立即首扫 |
| `wenqu watch remove <dir> --keep\|--purge` | 冻结保留 / 清除（必须显式） |
| `wenqu feishu add <URL>` | token 解析 → 规模预览确认 → 限速首次落地 |
| `wenqu config get/set <key> <value>` | 标量配置读写（校验 + 生效时点提示） |
| `wenqu userdict add\|remove\|list` | jieba 领域词典 |
| `wenqu archive confirm --all\|--path` | 归档候选确认（归档非删除） |
| `wenqu prune-feishu --status <s>` | 清理孤儿快照（破坏性，先列清单） |
| `wenqu reembed` / `wenqu rebuild` | 向量重建 / SQLite 全量重建 |
| `wenqu sync start\|stop\|restart\|status\|daemon` | launchd 守护管理 |

## 检索方向

| | 方向一 | 方向二（默认） | 方向三 |
|---|---|---|---|
| 路径 | jieba+BM25 | + bge-small-zh-v1.5 | + e5-large |
| 模型下载 | 0 | ~0.1GB | ~1–2GB |
| 查询延迟 | 5–30ms | 50–150ms | 100–300ms |

切换 = `wenqu config set retrieval.mode ...`（+ `wenqu reembed`），FTS 索引全程无损；模型失败自动降级纯 BM25（`wenqu_status` 报 degraded）。

## 飞书接入（可选）

前置：本机已装 lark-cli（随 Trae lark 插件分发）并完成授权（需要 `search:docs:read` 等 scope）。

```bash
wenqu feishu add https://feishu.cn/wiki/<空间或节点URL>
```

先展示范围规模，确认后限速落地；此后 wenqu-sync 周期增量同步（默认日级），引用一律回链飞书线上原文。

## 开发与测试

```bash
uv venv --python 3.12 .venv
uv pip install -p .venv/bin/python -e ".[dev]"
.venv/bin/python -m pytest        # 59 项测试（单测 + tmpdir 集成）
```

项目结构：

```
wenqu/
├── skill/wenqu/SKILL.md     # wenqu skill（安装物）
├── src/wenqu/
│   ├── config.py            # 配置：加载/校验/热重载/原子写
│   ├── db.py                # SQLite(WAL)：DDL/身份锚/事务
│   ├── parsers/             # md txt docx pptx xlsx pdf
│   ├── chunker.py           # ~600 字符 + ~80 重叠
│   ├── fts.py               # jieba 两端预分词
│   ├── embed.py             # fastembed：cache_dir/降级/闲置卸载
│   ├── search.py            # BM25 / RRF+新近度 / 预算 / 双出处
│   ├── mcp_server.py        # 五只读工具（STDIO）
│   ├── sync/daemon.py       # 唯一写入者：全部同步机制
│   ├── sync/feishu.py       # RemoteSource 飞书实现
│   ├── launchd.py           # plist 与 launchctl 封装
│   └── cli.py               # 命令面
└── tests/                   # 59 用例
```

## 卸载

```bash
wenqu sync stop                       # 停 launchd 守护（plist 一并移除）
rm -rf ~/Knowledge/.wenqu             # 删运行数据（Documents 原始文件保留）
uv tool uninstall wenqu               # 卸载命令
# 各客户端移除 skill 文件与 MCP 配置条目
```

## License

[MIT](LICENSE)
