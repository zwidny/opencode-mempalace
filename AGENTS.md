# AGENTS.md — mempalace_mth

本项目维护 MemPalace 记忆宫殿的 opencode 自动记忆管道。AI 会话请遵守以下**记忆协议**：

1. **会话收尾**（完成重要里程碑或用户说再见时）：`mempalace_diary_write`（agent_name=`opencode`，AAAK 格式）记录做了什么、学到什么、待办什么
2. **单值事实变更**（默认模型、端口、版本、路径等）：用 `mempalace_kg_supersede` 原子替换，**不要**手工 invalidate + add（避免边界重叠）
3. **新事实/决策落定**：`mempalace_kg_add`，带 `valid_from` 与 `source_drawer_id`（或 `source_file`）溯源
4. **回答"之前做过什么/谁/何时/为什么"类问题前**：先 `mempalace_search` / `mempalace_kg_query` 查宫殿，勿凭参数记忆猜测
5. **改动管道组件**（scripts/、~/.opencode/plugins/、docker/）后：跑 `uv run python scripts/sync_opencode_to_mempalace.py --dry-run` 确认无回归

## 组件速查

| 组件 | 位置 | 职责 |
|---|---|---|
| export_opencode_sessions.py | scripts/ | session → JSONL 三级分流导出（high/mid/low + proj-\<slug\>） |
| sync_opencode_to_mempalace.py | scripts/ | 增量同步编排（HTTP MCP → serve 容器，冷启动自动回填） |
| mempalace-sync.ts | plugins/opencode/ | `session.idle` 触发自动开采（15min 防抖） |
| mempalace-recall.ts | plugins/opencode/ | `chat.message` 首条消息注入项目记忆 |
| （插件注册） | ~/.config/opencode/opencode.json | 独立目录入口 `file://…/plugins/opencode`，与 ECC 等其他插件隔离 |
| mempalace-serve | docker/mempalace-serve/ | 宫殿服务容器（127.0.0.1:8765，Bearer token） |

关键事实（端口/版本/策略等）已录入宫殿 KG，`mempalace_kg_query(entity="mempalace_mth")` 可查。
