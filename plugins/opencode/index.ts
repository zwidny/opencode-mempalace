/**
 * mempalace opencode plugin 入口（hindsight 模式：自包含目录）。
 *
 * 目录入口无 package.json → opencode 自动认本 index.ts（INDEX_FILES 优先级
 * index.ts > index.tsx > index.js > ...；注意：目录入口不扫描散文件）。
 * bun 原生执行 TS，无需构建步骤。
 *
 * 注册方式（~/.config/opencode/opencode.json）：
 *   "plugin": ["file:///home/zhao/repos/mth/mempalace_mth/plugins/opencode"]
 *
 * 本目录受本仓库 git 管理；与 ~/.opencode/plugins（ECC 体系）完全隔离——
 * loader 对每个 plugin entry 独立 attempt，单入口故障不波及其他插件。
 */

// MemPalace session 同步（session.idle 防抖 → python 编排脚本）
export * from "./mempalace-sync.js"

// MemPalace 项目记忆自动注入（chat.message 首条 → 检索 proj wing → unshift synthetic part）
export * from "./mempalace-recall.js"
