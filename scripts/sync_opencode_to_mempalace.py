#!/usr/bin/env python3
"""编排：增量同步 opencode session → MemPalace（供 plugin 或手动调用）。

架构（单写者模型，所有写操作经 HTTP MCP 路由到常驻的 mempalace-serve 容器）：
  opencode plugin (session.idle 防抖15min)
    → 本脚本
        1. 扫 opencode.db：最近 --recent 分钟内更新过的 session
        2. 项目冷启动回填：某项目首次出现新 session 时，自动回填该项目全部历史
        3. 新 session → 项目 wing 分流导出 jsonl（proj-<slug> / 全局）
           更新过的 session → 先清旧 drawer 再重导（标题变化也触发重导）
        4. 按变更 wing 分组，经 MCP mine（HTTP JSON-RPC + Bearer）
        5. 更新 state，日志追加写 .opencode-sync.log

前提：mempalace serve 容器常驻（端口 8765）。
     环境变量 MEMPALACE_SERVE_URL 可覆盖。

用法：
  uv run python scripts/sync_opencode_to_mempalace.py --dry-run
  uv run python scripts/sync_opencode_to_mempalace.py --recent 30
"""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import time
import urllib.request
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import export_opencode_sessions as ex  # noqa: E402

LOG_FILE = ex.OUT_BASE / ".opencode-sync.log"
SERVE_URL = "http://127.0.0.1:8765/mcp"
TOKEN_FILE = Path.home() / ".mempalace/server/bearer-token"


def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


class McpHttp:
    """最小 MCP streamable-http 客户端（initialize + tools/call，Bearer 鉴权）。"""

    def __init__(self, url: str = SERVE_URL, timeout: int = 600):
        import os
        self.url = os.environ.get("MEMPALACE_SERVE_URL", url)
        self.timeout = timeout
        self.session: str | None = None
        self._id = 0
        self.token: str | None = None
        if TOKEN_FILE.exists():
            self.token = TOKEN_FILE.read_text(encoding="utf-8").strip() or None

    def _headers(self) -> dict:
        h = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _post(self, payload: dict) -> dict:
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=self._headers())
        if self.session:
            req.add_header("mcp-session-id", self.session)
        with urllib.request.urlopen(req, timeout=self.timeout) as resp:
            sid = resp.headers.get("mcp-session-id")
            if sid:
                self.session = sid
            body = resp.read().decode()
        for chunk in body.split("\n"):
            if chunk.startswith("data:"):
                body = chunk[5:].strip()
                break
        return json.loads(body) if body.strip() else {}

    def _rpc(self, method: str, params: dict | None = None) -> dict:
        self._id += 1
        payload = {"jsonrpc": "2.0", "id": self._id, "method": method}
        if params is not None:
            payload["params"] = params
        out = self._post(payload)
        if "error" in out:
            raise RuntimeError(f"MCP {method}: {out['error']}")
        return out.get("result", {})

    def connect(self) -> None:
        self._rpc("initialize", {
            "protocolVersion": "2025-03-26",
            "capabilities": {},
            "clientInfo": {"name": "opencode-mempalace-sync", "version": "1.1"},
        })
        self._notify_initialized()

    def _notify_initialized(self) -> None:
        payload = {"jsonrpc": "2.0", "method": "notifications/initialized"}
        req = urllib.request.Request(self.url, data=json.dumps(payload).encode(), headers=self._headers())
        if self.session:
            req.add_header("mcp-session-id", self.session)
        try:
            urllib.request.urlopen(req, timeout=30).read()
        except Exception:
            pass  # 通知无需响应

    def call(self, tool: str, args: dict) -> dict:
        r = self._rpc("tools/call", {"name": tool, "arguments": args})
        if r.get("isError"):
            texts = [c.get("text", "") for c in r.get("content", []) if isinstance(c, dict)]
            raise RuntimeError(f"{tool} 失败: {'; '.join(texts)[:200]}")
        for c in r.get("content", []):
            if isinstance(c, dict) and "json" in c:
                return c["json"]
            if isinstance(c, dict) and c.get("text", "").startswith("{"):
                try:
                    return json.loads(c["text"])
                except json.JSONDecodeError:
                    pass
        return r


def delete_old_drawers(mcp: McpHttp, source_path: str) -> int:
    """按 source_file（全路径）删除旧 drawer。"""
    probe = mcp.call("mempalace_delete_by_source", {"source_file": source_path, "dry_run": True})
    n = int(probe.get("match_count", 0) or 0)
    if n > 0:
        mcp.call("mempalace_delete_by_source", {"source_file": source_path, "dry_run": False})
        log(f"  · 清除旧 drawer {n} 条 ({Path(source_path).name})")
    return n


def submit_mine(mcp: McpHttp, wing: str) -> None:
    d = ex.OUT_BASE / wing
    if not d.exists() or not any(d.glob("*.jsonl")):
        return
    r = mcp.call("mempalace_mine", {
        "source": str(d), "mode": "convos", "extract": "exchange",
        "wing": wing,
    })
    if not isinstance(r, dict) or not r.get("success", True):
        out = str(r.get("output", "") if isinstance(r, dict) else r)
        raise RuntimeError(f"mine 返回失败: success={r.get('success') if isinstance(r, dict) else '?'} {out[:200]}")
    out = str(r.get("output", ""))
    filed = [l.strip() for l in out.splitlines() if "Drawers filed" in l]
    log(f"  · mine[{wing}] {filed[0] if filed else '完成'}")


def needs_reexport(s: dict, rec: dict) -> bool:
    """session 有更新（时间或标题变化）→ 需重导。"""
    try:
        exported_ts = time.mktime(time.strptime(rec["exported_at"], "%Y-%m-%dT%H:%M:%S"))
    except (KeyError, ValueError):
        exported_ts = 0
    if s["time_updated"] / 1000 > exported_ts:
        return True
    # 标题异步生成/变化 → 重导（换新文件名，清旧）
    if (rec.get("title") or "") != (s.get("title") or ""):
        return True
    return False


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--recent", type=int, default=30, help="扫描最近 N 分钟内更新过的 session")
    ap.add_argument("--max", type=int, default=50, help="常规增量单次最多处理（冷启动回填不受限）")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    con = sqlite3.connect(f"file:{ex.DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    state = ex.load_state()
    counts = ex.dir_session_counts(con)
    cutoff = (time.time() - args.recent * 60) * 1000

    rows = con.execute(
        """SELECT id, title, directory, time_created, time_updated FROM session
           WHERE time_updated > ? ORDER BY time_updated DESC""",
        (cutoff,),
    ).fetchall()
    candidates = [dict(r) for r in rows]

    fresh, updated = [], []
    for s in candidates:
        rec = state.get(s["id"])
        if rec is None:
            fresh.append(s)
        elif needs_reexport(s, rec):
            updated.append(s)

    # 项目冷启动回填：新 session 所属项目（有专属 wing 资格）在 state 中无任何记录
    # → 一次性回填该项目全部历史 session
    wings_in_state = {rec.get("wing") for rec in state.values() if isinstance(rec, dict)}
    backfill: dict[str, list] = {}
    for s in fresh:
        n = counts.get(s.get("directory") or "", 0)
        if n < ex.PROJECT_WING_MIN_SESSIONS:
            continue
        wing = f"proj-{ex.slug_for(s.get('directory'))}"
        if wing not in wings_in_state:
            all_rows = con.execute(
                """SELECT id, title, directory, time_created, time_updated FROM session
                   WHERE directory = ? ORDER BY time_created DESC""",
                (s.get("directory") or "",),
            ).fetchall()
            todo = [dict(r) for r in all_rows if dict(r)["id"] not in state]
            if todo:
                backfill[wing] = todo

    todo = (fresh + updated)[: args.max]
    n_backfill = sum(len(v) for v in backfill.values())
    log(f"扫描最近 {args.recent}min：候选 {len(candidates)}（新 {len(fresh)} / 更新 {len(updated)}），"
        f"常规 {len(todo)}" + (f"，冷启动回填 {n_backfill}（{', '.join(backfill)}）" if backfill else ""))
    if not todo and not backfill:
        return 0

    mcp = None
    if not args.dry_run:
        mcp = McpHttp()
        try:
            mcp.connect()
        except Exception as e:
            log(f"! mempalace serve 不可达（{e}）：本批只导出 jsonl，mine 留待下次")
            mcp = None

    changed: dict[str, list[str]] = {}

    def process(s: dict, label: str) -> None:
        tier, reason, turns = ex.classify(con, s)
        wing = ex.wing_for(s.get("directory"), counts, tier)
        lines = ex.export_turns(turns, tier)
        sid = s["id"]
        old = state.get(sid, {})
        old_file = old.get("file", "")
        title_label = s.get("title", "")[:50]
        if args.dry_run:
            log(f"  [dry] {label} {wing:28} {tier:4} lines={len(lines):>4} [{reason}] {title_label}")
            return
        if not lines:
            return
        if old_file:
            if mcp:
                try:
                    delete_old_drawers(mcp, old_file)
                except Exception as e:
                    log(f"  ! 清理旧 drawer 失败（继续重导）: {e}")
            Path(old_file).unlink(missing_ok=True)
        path = ex.out_path_for(wing, s, state)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        state[sid] = {
            "wing": wing,
            "tier": tier,
            "reason": reason,
            "file": str(path),
            "title": s.get("title", ""),
            "directory": s.get("directory", ""),
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        changed.setdefault(wing, []).append(path.name)
        log(f"  + {label} {wing:28} {tier:4} lines={len(lines):>4} [{reason}] {title_label} → {path.name}")

    for s in todo:
        process(s, "更新" if s["id"] in state else "新增")
    for wing, sessions in backfill.items():
        log(f"  ⚡ 冷启动回填 {wing}: {len(sessions)} 个历史 session")
        for s in sessions:
            process(s, "回填")

    if args.dry_run:
        return 0

    ex.save_state(state)
    if mcp:
        for wing in sorted(changed):
            try:
                submit_mine(mcp, wing)
            except Exception as e:
                log(f"  ! mine[{wing}] 失败（jsonl 已就位，可手动补跑）: {e}")
    else:
        log("  （serve 未连接；恢复后运行 mempalace mine 补开采）")
    summary = ", ".join(f"{k}={len(v)}" for k, v in sorted(changed.items()))
    log(f"完成：{summary or '无变更'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
