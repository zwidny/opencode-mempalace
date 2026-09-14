/**
 * mempalace-sync plugin for OpenCode.
 *
 * 在 session.idle（每轮回复结束）时防抖触发增量同步：
 *   opencode session → 三级 jsonl → MemPalace 宫殿
 *
 * 注意：opencode 的 Hooks 类型没有 "session.idle" 这类事件 key——
 * 事件钩子必须走总分发器 `event`，在 event.type 里判断（官方 plugins.md 同款写法）。
 *
 * 实际逻辑在 python 编排脚本（幂等、可手动运行）：
 *   uv run --project ~/repos/mth/mempalace_mth python \
 *     scripts/sync_opencode_to_mempalace.py --recent 20
 *
 * 日志：~/.config/doc_crawler/_docs/.opencode-sync.log
 */

import type { PluginInput } from "@opencode-ai/plugin"

// 包根 = 本文件所在 plugins/opencode/ 的上两级。
// git clone 与 npm 安装（~/.cache/opencode/node_modules/...）两种形态都成立；
// 若脚本仓库放在别处，可用 MEMPALACE_PROJECT 环境变量覆盖。
const PROJECT = process.env.MEMPALACE_PROJECT || `${import.meta.dir}/../..`
const SCRIPT = `${PROJECT}/scripts/sync_opencode_to_mempalace.py`
const DEBOUNCE_MS = 15 * 60 * 1000 // 15 分钟防抖
const RECENT_MIN = 20 // 只处理最近 20 分钟内更新过的 session

let lastRun = 0
let running = false

async function triggerSync(): Promise<void> {
  const now = Date.now()
  if (now - lastRun < DEBOUNCE_MS || running) return
  lastRun = now
  running = true
  try {
    const { spawn } = await import("node:child_process")
    // detached：不阻塞 TUI 事件循环；脚本内部处理 token/HTTP mine
    const child = spawn(
      "uv",
      ["run", "--project", PROJECT, "python", SCRIPT, "--recent", String(RECENT_MIN)],
      { detached: true, stdio: "ignore", cwd: PROJECT },
    )
    child.unref()
  } catch (e) {
    console.error("[mempalace-sync] spawn failed:", e)
  } finally {
    running = false
  }
}

export const MemPalaceSyncPlugin = async (_input: PluginInput) => {
  return {
    event: async ({ event }: { event: { type: string } }) => {
      if (event.type === "session.idle") await triggerSync()
    },
  }
}

export default MemPalaceSyncPlugin
