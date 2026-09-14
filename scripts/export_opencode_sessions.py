#!/usr/bin/env python3
"""导出 opencode session 为 mempalace convos 模式可开采的 JSONL（项目级 wing 分流）。

导出粒度（tier，决定内容取舍）：
  high — 主 session：user 提问 + assistant 全部 text
  mid  — 子任务 session 且最终结论 >= 400 字：滤 <150 字过程播报
  low  — 批量模板任务或结论 < 400 字：仅任务描述 + 最终结论

存储 wing（决定去向）：
  low                              → opencode-sessions-low（全局杂讯）
  项目 directory 总 session >= 3   → proj-<slug>（项目专属 wing，含 high+mid）
  其余（小目录/杂项）              → opencode-sessions-high（全局兜底）

输出目录 = wing 名：~/.config/doc_crawler/_docs/<wing>/

用法：
  uv run python scripts/export_opencode_sessions.py --dry-run --limit 20
  uv run python scripts/export_opencode_sessions.py --limit 20
  uv run python scripts/export_opencode_sessions.py --all
  uv run python scripts/export_opencode_sessions.py --session <id> --force
  uv run python scripts/export_opencode_sessions.py --directory <dir>   # 项目级回填
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import subprocess
import sys
import time
import unicodedata
import urllib.parse
from pathlib import Path

DB_PATH = Path.home() / ".local/share/opencode/opencode.db"
OUT_BASE = Path.home() / ".config/doc_crawler/_docs"
STATE_FILE = OUT_BASE / ".opencode_sessions_state.json"

WING_GLOBAL_HIGH = "opencode-sessions-high"
WING_GLOBAL_LOW = "opencode-sessions-low"
PROJECT_WING_MIN_SESSIONS = 3  # 项目 directory 总 session 数达到该值才建专属 wing

# 批量机器任务模板黑名单：任务描述以这些前缀开头 → low
BULK_TASK_PREFIXES = [
    "You are a graphify extraction subagent",
    "You are a graphify",
]

SUBAGENT_TITLE_RE = re.compile(r"\(@\S+ subagent\)\s*$")
MID_MIN_PART_LEN = 150      # mid 级 assistant text part 保留阈值（字符）
MID_MIN_CONCLUSION = 400    # 子任务结论长度阈值（字符）


def slugify(text: str, maxlen: int = 48) -> str:
    text = unicodedata.normalize("NFKC", text or "untitled")
    text = re.sub(r"[^\w\u4e00-\u9fff]+", "-", text).strip("-")
    return text[:maxlen] or "untitled"


# 项目改名/迁移时的 wing 别名映射（旧名 → 新名，作用于目录路径，保持 wing 连续）。
# 与 plugins/opencode/mempalace-recall.ts 的 WING_ALIASES 保持同步。
WING_ALIASES: dict[str, str] = {
    # 示例："mempalace_mth": "opencode_mempalace",
}

_SLUG_CACHE: dict[str, str] = {}


def _git_remote_slug(directory: str) -> str | None:
    """git origin remote → 稳定 slug。

    与 opencode 的 project_id 同源逻辑（packages/core/src/project.ts 的
    url()/parts()）：host 小写、去 .git 后缀、file: 协议忽略、scp 格式支持。
    目录改名/换路径不改变 remote → wing key 稳定。
    """
    try:
        r = subprocess.run(
            ["git", "-C", directory, "remote", "get-url", "origin"],
            capture_output=True, text=True, timeout=10,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
    if r.returncode != 0:
        return None
    raw = (r.stdout or "").strip()
    if not raw:
        return None
    host, name = "", ""
    # 与 opencode url() 一致：带 scheme 的 URL（含 file:）→ 只认非 file 且有 host 的；
    # 解析不出 scheme（如 git@host:path 的 scp 格式）→ 正则兜底
    try:
        u = urllib.parse.urlparse(raw)
    except ValueError:
        u = None
    if u is not None and u.scheme:
        if u.scheme == "file" or not u.hostname:
            return None
        host, name = u.hostname, u.path
    else:
        scp = re.match(r"^([^@/:]+@)?([^/:]+):(.+)$", raw)
        if not scp:
            return None
        host, name = scp.group(2), scp.group(3)
    name = name.lstrip("/").removesuffix(".git").rstrip("/")
    if not name:
        return None
    return f"{host.lower()}-{name.replace('/', '-')}"


def slug_for(directory: str | None) -> str:
    """项目目录 → wing slug（与 recall plugin 的 JS 版保持一致）。

    key 策略与 opencode 的 project_id 同源：git remote 归一化 > 目录 basename
    ——目录改名不断层（opencode session 归属即用此策略）。
    """
    directory = (directory or "").rstrip("/") or "/"
    if directory == str(Path.home()):
        return "home"
    if directory in _SLUG_CACHE:
        return _SLUG_CACHE[directory]
    for old, new in WING_ALIASES.items():
        if old in directory:
            directory = directory.replace(old, new)
            break
    slug = _git_remote_slug(directory) or _basename_slug(directory)
    _SLUG_CACHE[directory] = slug
    return slug


def _basename_slug(directory: str) -> str:
    p = Path(directory)
    name = p.name
    # 隐藏目录（如 .opencode）取上级名
    while (not name or name.startswith(".")) and str(p) != p.anchor:
        p = p.parent
        name = p.name
    if not name or name.startswith(".") or str(p) == p.anchor:
        return "misc"
    return name


def wing_for(directory: str | None, counts: dict[str, int], tier: str) -> str:
    if tier == "low":
        return WING_GLOBAL_LOW
    if counts.get(directory or "", 0) >= PROJECT_WING_MIN_SESSIONS:
        return f"proj-{slug_for(directory)}"
    return WING_GLOBAL_HIGH


def dir_session_counts(con: sqlite3.Connection) -> dict[str, int]:
    rows = con.execute(
        "SELECT directory, COUNT(*) FROM session GROUP BY directory"
    ).fetchall()
    return {d or "": c for d, c in rows}


def load_state() -> dict:
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text(encoding="utf-8"))
    return {}


def save_state(state: dict) -> None:
    STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
    STATE_FILE.write_text(
        json.dumps(state, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def message_texts(con: sqlite3.Connection, message_id: str) -> list[str]:
    rows = con.execute(
        "SELECT data FROM part WHERE message_id=? ORDER BY time_created", (message_id,)
    ).fetchall()
    out = []
    for (raw,) in rows:
        d = json.loads(raw) if raw else {}
        if isinstance(d, dict) and d.get("type") == "text":
            t = (d.get("text") or "").strip()
            if t:
                out.append(t)
    return out


def session_turns(con: sqlite3.Connection, session_id: str) -> list[tuple[str, list[str]]]:
    turns = []
    rows = con.execute(
        """SELECT id, json_extract(data,'$.role') role FROM message
           WHERE session_id=? ORDER BY time_created""",
        (session_id,),
    ).fetchall()
    for mid, role in rows:
        texts = message_texts(con, mid)
        if texts:
            turns.append((role, texts))
    return turns


def classify(con: sqlite3.Connection, sess: dict) -> tuple[str, str, list]:
    """返回 (tier, 原因, turns)。"""
    turns = session_turns(con, sess["id"])
    title = sess.get("title") or ""
    is_subagent = bool(SUBAGENT_TITLE_RE.search(title))

    if not is_subagent:
        return "high", "主 session", turns

    task_desc = "".join(turns[0][1]) if turns and turns[0][0] == "user" else ""
    conclusion = "".join(turns[-1][1]) if turns and turns[-1][0] == "assistant" else ""

    for prefix in BULK_TASK_PREFIXES:
        if task_desc.startswith(prefix):
            return "low", f"批量模板任务({prefix[:30]}…)", turns
    if len(conclusion) < MID_MIN_CONCLUSION:
        return "low", f"子任务结论过短({len(conclusion)}字)", turns
    return "mid", f"调研类子任务(结论{len(conclusion)}字)", turns


def export_turns(turns: list, tier: str) -> list[dict]:
    lines: list[dict] = []

    def add(role: str, content: str) -> None:
        if content.strip():
            lines.append(
                {"type": "user" if role == "user" else "assistant",
                 "message": {"role": role, "content": content}}
            )

    if tier == "high":
        for role, texts in turns:
            add(role, "\n\n".join(texts))
    elif tier == "mid":
        for i, (role, texts) in enumerate(turns):
            if role == "user":
                add(role, "\n\n".join(texts))
            else:
                if i == len(turns) - 1:  # 最终结论完整保留
                    add(role, "\n\n".join(texts))
                else:  # 过程叙述：仅保留 >=150 字的实质段落
                    kept = [t for t in texts if len(t) >= MID_MIN_PART_LEN]
                    add(role, "\n\n".join(kept))
    else:  # low：任务描述 + 最终结论
        if turns and turns[0][0] == "user":
            add("user", "\n\n".join(turns[0][1]))
        if turns and turns[-1][0] == "assistant":
            add("assistant", "\n\n".join(turns[-1][1]))
    return lines


def out_path_for(wing: str, sess: dict, state: dict) -> Path:
    day = time.strftime("%Y-%m-%d", time.localtime(sess["time_created"] / 1000))
    base = OUT_BASE / wing
    stem = f"{day}_{slugify(sess.get('title'))}"
    p = base / f"{stem}.jsonl"
    n = 1
    while p.exists() and str(p) != state.get(sess["id"], {}).get("file"):
        p = base / f"{stem}-{n}.jsonl"
        n += 1
    return p


def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--limit", type=int, default=15, help="本次最多导出 N 个（默认 15）")
    ap.add_argument("--all", action="store_true", help="忽略状态全量导出")
    ap.add_argument("--session", help="只处理指定 session id（配合 --force 重导）")
    ap.add_argument("--directory", help="只处理指定项目目录（项目级回填，配合 --force 重导全部）")
    ap.add_argument("--force", action="store_true", help="跳过增量检查强制导出")
    ap.add_argument("--dry-run", action="store_true", help="只列出候选与分级，不写文件")
    args = ap.parse_args()

    if not DB_PATH.exists():
        print(f"error: {DB_PATH} 不存在", file=sys.stderr)
        return 1
    con = sqlite3.connect(f"file:{DB_PATH}?mode=ro", uri=True)
    con.row_factory = sqlite3.Row
    state = load_state()
    counts = dir_session_counts(con)

    rows = con.execute(
        "SELECT id, title, directory, time_created FROM session ORDER BY time_created DESC"
    ).fetchall()
    sessions = [dict(r) for r in rows]

    if args.session:
        sessions = [s for s in sessions if s["id"] == args.session]
        if not sessions:
            print(f"error: 找不到 session {args.session}", file=sys.stderr)
            return 1
        args.force = True
    elif args.directory:
        sessions = [s for s in sessions if (s.get("directory") or "") == args.directory]
        args.force = True
    elif not args.all:
        sessions = [s for s in sessions if s["id"] not in state]

    nonempty = []
    for s in sessions:
        n = con.execute(
            "SELECT COUNT(*) FROM message WHERE session_id=?", (s["id"],)
        ).fetchone()[0]
        if n > 0:
            nonempty.append(s)

    selected = nonempty if (args.all or args.force) else nonempty[: args.limit]

    counts_summary: dict[str, int] = {}
    exported = 0
    print(f"{'wing':28} {'tier':5} {'lines':>4}  原因 / 标题")
    print("-" * 110)
    for s in selected:
        tier, reason, turns = classify(con, s)
        wing = wing_for(s.get("directory"), counts, tier)
        lines = export_turns(turns, tier)
        counts_summary[wing] = counts_summary.get(wing, 0) + 1
        if args.dry_run:
            print(f"{wing:28} {tier:5} {len(lines):>4}  [{reason}] {s.get('title','')[:55]}")
            continue
        if not lines:
            continue
        path = out_path_for(wing, s, state)
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as f:
            for line in lines:
                f.write(json.dumps(line, ensure_ascii=False) + "\n")
        state[s["id"]] = {
            "wing": wing,
            "tier": tier,
            "reason": reason,
            "file": str(path),
            "title": s.get("title", ""),
            "directory": s.get("directory", ""),
            "exported_at": time.strftime("%Y-%m-%dT%H:%M:%S"),
        }
        exported += 1
        print(f"{wing:28} {tier:5} {len(lines):>4}  [{reason}] {s.get('title','')[:55]} → {path.name}")

    print("-" * 110)
    wings = ", ".join(f"{k}={v}" for k, v in sorted(counts_summary.items()))
    print(f"合计 {len(selected)} 个 session（{wings}）"
          + (f"，已导出 {exported}" if not args.dry_run else "（dry-run，未写入）"))
    if not args.dry_run and exported:
        save_state(state)
        print(f"状态已更新：{STATE_FILE}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
