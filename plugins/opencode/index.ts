/**
 * mempalace opencode plugin 入口。
 *
 * 目录入口无 package.json → opencode 自动认本 index.ts（INDEX_FILES 优先级
 * index.ts > index.tsx > index.js > ...；注意：目录入口不扫描散文件）。
 * npm 包入口（@zwidny/opencode-mempalace）→ package.json main 指向本文件。
 * bun 原生执行 TS，无需构建步骤。
 *
 * 注册方式（~/.config/opencode/opencode.json）：
 *   npm 发布形态：  "plugin": ["@zwidny/opencode-mempalace"]（bun 自动安装）
 *   本地开发形态：  "plugin": ["file:///home/zhao/repos/mth/mempalace_mth/plugins/opencode"]
 *
 * 插件内部路径（scripts/、pyproject、uv.lock）均从 import.meta.dir 推导包根，
 * 两种安装形态无需改代码。
 */

// MemPalace session 同步（session.idle 防抖 → python 编排脚本）
export * from "./mempalace-sync.js"

// MemPalace 项目记忆自动注入（chat.message 首条 → 检索 proj wing → unshift synthetic part）
export * from "./mempalace-recall.js"
