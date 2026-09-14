/**
 * mempalace-recall plugin for OpenCode.
 *
 * 自动注入项目记忆（借鉴 opencode-hindsight 的 chat.message 注入机制）：
 * 新 session 的首条用户消息发出时，按当前项目目录检索 MemPalace 宫殿：
 *   1. 优先 proj-<slug> 项目专属 wing（distance ≤ 0.55）
 *   2. 不足时全局 opencode-sessions-high 兜底（distance ≤ 0.45，更严防跨项目噪声）
 *   3. 命中片段格式化为 synthetic text part，unshift 到消息最前 → AI 首条消息自带历史
 *
 * 每个 session 只注入一次（首条）；需要更多细节 AI 可自行调 mempalace_search 工具。
 */

import type { PluginInput } from "@opencode-ai/plugin"

const SERVE_URL = process.env.MEMPALACE_SERVE_URL || "http://127.0.0.1:8765/mcp"
// token 路径跟随当前用户 home（osMod 在插件初始化时预加载）；
// 非默认布局可用 MEMPALACE_TOKEN_FILE 覆盖
const TOKEN_FILE = () =>
  process.env.MEMPALACE_TOKEN_FILE ?? `${osMod!.homedir()}/.mempalace/server/bearer-token`
const GLOBAL_WING = "opencode-sessions-high"
const PROJ_LIMIT = 4
const GLOBAL_LIMIT = 3
const PROJ_MAX_DISTANCE = 0.55
const GLOBAL_MAX_DISTANCE = 0.45
const SNIPPET_CHARS = 200 // 每条片段截断
const BUDGET_CHARS = 1500 // 注入总预算

interface SearchHit {
  text: string
  similarity: number
  distance: number
  source_file: string
}

// 预加载的 node 模块（ESM 环境无 require，动态 import 在 async 主函数中完成）
let fsMod: typeof import("fs") | null = null
let osMod: typeof import("os") | null = null
let pathMod: typeof import("path") | null = null

let tokenCache: string | null = null
let tokenRead = false

function bearerToken(): string | null {
  if (!tokenRead) {
    tokenRead = true
    try {
      tokenCache = fsMod!.readFileSync(TOKEN_FILE(), "utf-8").trim() || null
    } catch {
      tokenCache = null
    }
  }
  return tokenCache
}

// 项目改名/迁移时的 wing 别名映射（旧名 → 新名，作用于目录路径，保持 wing 连续）。
// 与 scripts/export_opencode_sessions.py 的 WING_ALIASES 保持同步。
const WING_ALIASES: Record<string, string> = {}

/** git origin remote → 稳定 slug（与 opencode project_id 同源逻辑：
 * packages/core/src/project.ts 的 url()/parts()）。目录改名/换路径不改变 remote。 */
async function gitRemoteSlug(directory: string): Promise<string | null> {
  try {
    const { execFile } = await import("node:child_process")
    return await new Promise((resolve) => {
      execFile(
        "git", ["-C", directory, "remote", "get-url", "origin"], { timeout: 10_000 },
        (err: unknown, stdout: string) => {
          const raw = (stdout || "").trim()
          if (err || !raw) return resolve(null)
          // 与 opencode url() 一致：带 scheme 的 URL（含 file:）→ 只认非 file 且有 host 的；
          // 解析不出 scheme（如 git@host:path 的 scp 格式）→ 正则兜底
          let host = "", name = ""
          try {
            const u = new URL(raw)
            if (u.protocol === "file:" || !u.hostname) return resolve(null)
            host = u.hostname
            name = u.pathname
          } catch {
            const scp = raw.match(/^([^@/:]+@)?([^/:]+):(.+)$/)
            if (!scp) return resolve(null)
            host = scp[2]; name = scp[3]
          }
          name = name.replace(/^\/+/, "").replace(/\.git\/?$/, "").replace(/\/+$/, "")
          if (!name) return resolve(null)
          resolve(`${host.toLowerCase()}-${name.replace(/\//g, "-")}`)
        },
      )
    })
  } catch {
    return null
  }
}

/** 项目目录 → 稳定 wing slug（与 python 版 slug_for 保持一致）：
 * git remote 归一化 > 目录 basename —— 目录改名不断层。 */
async function projectSlug(rawDirectory: string): Promise<string> {
  const home = osMod!.homedir()
  const path = pathMod!
  let dir = (rawDirectory || "/").replace(/\/+$/, "") || "/"
  if (dir === home) return "home"
  for (const [oldName, newName] of Object.entries(WING_ALIASES)) {
    if (dir.includes(oldName)) {
      dir = dir.replace(oldName, newName)
      break
    }
  }
  const remote = await gitRemoteSlug(dir)
  if (remote) return remote
  return basenameSlug(dir)
}

function basenameSlug(rawDirectory: string): string {
  const path = pathMod!
  let p = path.parse(rawDirectory)
  let name = p.base
  while ((!name || name.startsWith(".")) && p.root !== p.dir) {
    p = path.parse(p.dir)
    name = p.base
  }
  if (!name || name.startsWith(".")) return "misc"
  return name
}

/** 最小 MCP streamable-http 客户端（单次连接内完成调用）。 */
async function mcpCall(tool: string, args: Record<string, unknown>): Promise<unknown> {
  const token = bearerToken()
  const headers: Record<string, string> = {
    "Content-Type": "application/json",
    Accept: "application/json, text/event-stream",
  }
  if (token) headers.Authorization = `Bearer ${token}`

  const rpc = async (body: string, session?: string): Promise<{ json: any; session?: string }> => {
    const h = { ...headers }
    if (session) h["mcp-session-id"] = session
    const resp = await fetch(SERVE_URL, { method: "POST", headers: h, body })
    const sid = resp.headers.get("mcp-session-id") ?? undefined
    const raw = await resp.text()
    let payload = raw
    for (const line of raw.split("\n")) {
      if (line.startsWith("data:")) {
        payload = line.slice(5).trim()
        break
      }
    }
    return { json: payload ? JSON.parse(payload) : {}, session: sid }
  }

  const init = await rpc(
    JSON.stringify({
      jsonrpc: "2.0",
      id: 1,
      method: "initialize",
      params: {
        protocolVersion: "2025-03-26",
        capabilities: {},
        clientInfo: { name: "mempalace-recall", version: "1.0" },
      },
    }),
  )
  await rpc(JSON.stringify({ jsonrpc: "2.0", method: "notifications/initialized" }), init.session)
  const call = await rpc(
    JSON.stringify({ jsonrpc: "2.0", id: 2, method: "tools/call", params: { name: tool, arguments: args } }),
    init.session,
  )
  const result = call.json?.result
  if (result?.isError) throw new Error(`MCP ${tool} error`)
  for (const c of result?.content ?? []) {
    if (c?.json) return c.json
    if (typeof c?.text === "string" && c.text.startsWith("{")) {
      try {
        return JSON.parse(c.text)
      } catch {
        /* not json */
      }
    }
  }
  return result
}

async function searchWing(query: string, wing: string, limit: number, maxDistance: number): Promise<SearchHit[]> {
  try {
    const r = (await mcpCall("mempalace_search", { query, wing, limit })) as
      | { results?: SearchHit[] }
      | undefined
    return (r?.results ?? []).filter((h) => (h.distance ?? 1) <= maxDistance)
  } catch {
    return [] // wing 不存在 / serve 不可达 → 静默跳过
  }
}

function titleFromSource(sourceFile: string): string {
  // "2026-09-14_标题-slug.jsonl" → "09-14 · 标题"
  const base = sourceFile.split("/").pop() ?? ""
  const m = base.match(/^\d{4}-(\d{2}-\d{2})_(.+)\.jsonl$/)
  if (!m) return base.replace(/\.jsonl$/, "")
  return `${m[1]} · ${m[2].replace(/-/g, " ")}`
}

function truncate(text: string, n: number): string {
  return text.length > n ? text.slice(0, n) + "…" : text
}

function formatContext(projWing: string, hits: SearchHit[]): string {
  if (hits.length === 0) return ""
  const lines: string[] = [`[MEMPALACE 项目记忆 | ${projWing}]（按你的首条消息自动检索）`]
  let budget = BUDGET_CHARS - lines[0].length
  for (const h of hits) {
    const head = `\n\n## ${titleFromSource(h.source_file)}（相关度 ${Math.round(h.similarity * 100)}%）`
    const body = `\n${truncate(h.text.replace(/\s+/g, " ").trim(), SNIPPET_CHARS)}`
    if (head.length + body.length > budget) break
    lines.push(head + body)
    budget -= head.length + body.length
  }
  lines.push("\n\n（更多细节可用 mempalace_search 工具检索该 wing）")
  lines.push(
    "\n（维护协议：会话收尾 → mempalace_diary_write；单值事实变更 → mempalace_kg_supersede；新决策 → mempalace_kg_add 带溯源）",
  )
  return lines.join("")
}

export const MemPalaceRecallPlugin = async (ctx: PluginInput) => {
  ;[fsMod, osMod, pathMod] = await Promise.all([
    import("node:fs"),
    import("node:os"),
    import("node:path"),
  ])
  const directory: string = (ctx as { directory?: string }).directory ?? process.cwd()
  const projWing = `proj-${await projectSlug(directory)}`
  console.log(`[mempalace-recall] wing=${projWing} (dir=${directory})`)
  const injectedSessions = new Set<string>()

  return {
    "chat.message": async (input: { sessionID: string }, output: { parts: any[] }) => {
      const isFirstMessage = !injectedSessions.has(input.sessionID)
      if (!isFirstMessage) return
      injectedSessions.add(input.sessionID)

      try {
        const textParts = output.parts.filter(
          (p) => p && p.type === "text" && typeof p.text === "string",
        )
        const userMessage = textParts.map((p) => p.text).join("\n").trim()
        if (!userMessage) return

        // 1) 项目专属 wing 优先；2) 全局兜底（更严阈值）
        let hits = await searchWing(userMessage, projWing, PROJ_LIMIT, PROJ_MAX_DISTANCE)
        if (hits.length === 0) {
          hits = await searchWing(userMessage, GLOBAL_WING, GLOBAL_LIMIT, GLOBAL_MAX_DISTANCE)
        }
        const context = formatContext(projWing, hits)
        if (!context) return

        output.parts.unshift({
          id: `prt_mempalace-recall-${Date.now()}`,
          sessionID: input.sessionID,
          messageID: textParts[0]?.messageID ?? output.parts[0]?.messageID,
          type: "text",
          text: context,
          synthetic: true,
        })
        console.log(`[mempalace-recall] injected ${hits.length} hits from ${projWing} (session ${input.sessionID.slice(0, 12)}…)`)
      } catch (e) {
        console.error("[mempalace-recall] inject failed:", e)
      }
    },
  }
}

export default MemPalaceRecallPlugin
