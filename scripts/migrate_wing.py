#!/usr/bin/env python3
"""一次性 wing 迁移工具：把一个 wing 的全部 drawer + jsonl 目录 + state 记录
迁移到新 wing 名（项目改名 / 增加 git remote 后保持记忆连续）。

用法：
  uv run python scripts/migrate_wing.py --from proj-mempalace_mth --to proj-github.com-x-y --dry-run
  uv run python scripts/migrate_wing.py --from proj-mempalace_mth --to proj-github.com-x-y

前提：mempalace serve 容器在跑（写操作经 HTTP MCP，保持单写者模型）。
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

SCRIPT_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(SCRIPT_DIR))

import export_opencode_sessions as ex  # noqa: E402
from sync_opencode_to_mempalace import McpHttp, log  # noqa: E402


def list_drawer_ids(mcp: McpHttp, wing: str) -> list[str]:
    ids: list[str] = []
    offset = 0
    while True:
        r = mcp.call("mempalace_list_drawers", {"wing": wing, "limit": 100, "offset": offset})
        drawers = r.get("drawers", r if isinstance(r, list) else [])
        if not drawers:
            break
        ids.extend(d["drawer_id"] for d in drawers if isinstance(d, dict) and "drawer_id" in d)
        if len(drawers) < 100:
            break
        offset += 100
    return ids


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--from", dest="src", required=True, help="源 wing 名")
    ap.add_argument("--to", dest="dst", required=True, help="目标 wing 名")
    ap.add_argument("--dry-run", action="store_true")
    args = ap.parse_args()

    mcp = McpHttp()
    mcp.connect()

    # 1) 宫殿 drawer 批量改 wing
    ids = list_drawer_ids(mcp, args.src)
    log(f"源 wing {args.src}：{len(ids)} drawers")
    if args.dry_run:
        log(f"[dry] 将批量迁移到 {args.dst}，jsonl 目录与 state 同步改名")
        return 0
    ok = fail = 0
    for i, did in enumerate(ids, 1):
        try:
            mcp.call("mempalace_update_drawer", {"drawer_id": did, "wing": args.dst})
            ok += 1
        except Exception as e:
            fail += 1
            log(f"  ! {did}: {e}")
        if i % 50 == 0:
            log(f"  … {i}/{len(ids)}")
    log(f"drawer 迁移完成：成功 {ok} / 失败 {fail}")

    # 2) jsonl 导出目录改名
    src_dir = ex.OUT_BASE / args.src
    dst_dir = ex.OUT_BASE / args.dst
    if src_dir.exists():
        if dst_dir.exists():
            for f in src_dir.iterdir():
                f.rename(dst_dir / f.name)
            src_dir.rmdir()
            log(f"jsonl 合并进已有目录 {dst_dir.name}")
        else:
            src_dir.rename(dst_dir)
            log(f"jsonl 目录改名 → {dst_dir.name}")

    # 3) state 记录同步（wing 字段 + file 路径）
    state = ex.load_state()
    n = 0
    for rec in state.values():
        if isinstance(rec, dict) and rec.get("wing") == args.src:
            rec["wing"] = args.dst
            if "file" in rec:
                rec["file"] = rec["file"].replace(f"/{args.src}/", f"/{args.dst}/")
            n += 1
    ex.save_state(state)
    log(f"state 更新 {n} 条")

    # 4) 验证
    left = len(list_drawer_ids(mcp, args.src))
    moved = len(list_drawer_ids(mcp, args.dst))
    log(f"验证：{args.src} 剩余 {left}，{args.dst} 现有 {moved}")
    return 0 if left == 0 and fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
