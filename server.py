#!/usr/bin/env python3
"""
待办管理 Web 服务
- 读取/展示待办（JSON 存储）
- 新增/更新/完成/删除待办
- AI 拆解、AI 工作总结
- Excel 导出
- 提醒检查
"""

import hashlib
import http.server
import json
import os
import secrets
import socketserver
import tempfile
import threading
import time
from datetime import datetime, timedelta
from http.cookies import SimpleCookie
from pathlib import Path
from typing import Any, Dict, List, Optional

PORT = int(os.environ.get("PORT", 19101))
TODO_DIR = os.path.dirname(os.path.abspath(__file__))
HTML_PATH = os.path.join(TODO_DIR, "index.html")
DATA_PATH = os.path.join(TODO_DIR, "todos.json")

# ── Multi-user data directory ──
DATA_DIR = os.path.join(TODO_DIR, "data")
USERS_PATH = os.path.join(DATA_DIR, "users.json")
SESSION_EXPIRY = 7 * 24 * 3600  # 7 days in seconds

# In-memory sessions: token → {"username": str, "expires": float}
_sessions: Dict[str, Dict[str, Any]] = {}
_sessions_lock = threading.Lock()

JOYCLAW_GATEWAY = "http://127.0.0.1:18798"
JOYCLAW_CONFIG = os.path.expanduser("~/.joyclaw/openclaw.json")
REMINDERS_DIR = os.path.expanduser("~/.joyclaw/workspace/reminders2")

UPLOADS_DIR = os.path.join(TODO_DIR, "uploads")

# AI config — 智谱 GLM (OpenAI-compatible API)
AI_BASE_URL = os.environ.get("AI_BASE_URL", "https://open.bigmodel.cn/api/paas/v4")
AI_API_KEY = os.environ.get("AI_API_KEY", "")
AI_MODEL = os.environ.get("AI_MODEL", "glm-4-flash-250414")

TAGS = [
    "运营工作", "降本运营", "数据相关", "综合统筹", "大满贯",
    "合规整改消保", "加速landing", "绩效汇报", "SOP", "AIAI",
]

TAGS_PATH = os.path.join(TODO_DIR, "tags.json")
CONTACTS_PATH = os.path.join(TODO_DIR, "contacts.json")


def load_tags(username=None):
    path = user_tags_path(username) if username else TAGS_PATH
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    save_tags(TAGS, username)
    return list(TAGS)


def save_tags(tags, username=None):
    path = user_tags_path(username) if username else TAGS_PATH
    with open(path, "w", encoding="utf-8") as f:
        json.dump(tags, f, ensure_ascii=False, indent=2)


def load_contacts(username=None):
    path = user_contacts_path(username) if username else CONTACTS_PATH
    if os.path.exists(path):
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    # Default contacts
    default = [
        {"name": "陈实", "erp": "chenshi23", "type": "person"},
        {"name": "冯汉禹", "erp": "fenghanyu.3", "type": "person"},
        {"name": "高志华", "erp": "gaozhihua9", "type": "person"},
        {"name": "黄萌", "erp": "huangmeng16", "type": "person"},
        {"name": "姜海燕", "erp": "jianghaiyan1", "type": "person"},
        {"name": "李雪", "erp": "lixue368", "type": "person"},
        {"name": "苏星宇", "erp": "suxingyu7", "type": "person"},
        {"name": "孙雅宜", "erp": "sunyayi.1", "type": "person"},
        {"name": "王峰", "erp": "wangfeng479", "type": "person"},
        {"name": "肖伶俐", "erp": "xiaolingli", "type": "person"},
        {"name": "叶帆", "erp": "yefan23", "type": "person"},
        {"name": "郑犇犇", "erp": "zhengbenben", "type": "person"},
        {"name": "郑润宸", "erp": "zhengrunchen.1", "type": "person"},
        {"name": "朱贺存", "erp": "zhuhecun1", "type": "person"},
        {"name": "毛宇君", "erp": "maoyujun.3", "type": "person"},
        {"name": "祝显荣", "erp": "zhuxianrong.0328", "type": "person"},
        {"name": "沈延德", "erp": "shenyande.1", "type": "person"},
        {"name": "🍚 干饭人", "erp": "group:10218887128", "type": "group"},
        {"name": "🏢 基础产品部", "erp": "group:1024703302", "type": "group"},
    ]
    save_contacts(default, username)
    return default


def save_contacts(contacts, username=None):
    path = user_contacts_path(username) if username else CONTACTS_PATH
    with open(path, "w", encoding="utf-8") as f:
        json.dump(contacts, f, ensure_ascii=False, indent=2)

# ─────────────────────────── User / Session helpers ────────────────


def load_users() -> Dict[str, Any]:
    os.makedirs(DATA_DIR, exist_ok=True)
    if os.path.exists(USERS_PATH):
        with open(USERS_PATH, "r", encoding="utf-8") as f:
            return json.load(f)
    return {}


def save_users(users: Dict[str, Any]) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    with open(USERS_PATH, "w", encoding="utf-8") as f:
        json.dump(users, f, ensure_ascii=False, indent=2)


def hash_password(password: str, salt: str) -> str:
    return hashlib.sha256((salt + password).encode("utf-8")).hexdigest()


def create_session(username: str) -> str:
    token = secrets.token_hex(32)
    with _sessions_lock:
        _sessions[token] = {
            "username": username,
            "expires": time.time() + SESSION_EXPIRY,
        }
    return token


def get_session_user(token: str) -> Optional[str]:
    with _sessions_lock:
        sess = _sessions.get(token)
        if sess and sess["expires"] > time.time():
            return sess["username"]
        if sess:
            del _sessions[token]
    return None


def destroy_session(token: str) -> None:
    with _sessions_lock:
        _sessions.pop(token, None)


def ensure_user_dir(username: str) -> None:
    """Create per-user directory with default tags and contacts."""
    user_dir = os.path.join(DATA_DIR, username)
    os.makedirs(user_dir, exist_ok=True)
    os.makedirs(os.path.join(user_dir, "uploads"), exist_ok=True)

    tags_path = os.path.join(user_dir, "tags.json")
    if not os.path.exists(tags_path):
        with open(tags_path, "w", encoding="utf-8") as f:
            json.dump(TAGS, f, ensure_ascii=False, indent=2)

    contacts_path = os.path.join(user_dir, "contacts.json")
    if not os.path.exists(contacts_path):
        default_contacts = [
            {"name": "陈实", "erp": "chenshi23", "type": "person"},
            {"name": "冯汉禹", "erp": "fenghanyu.3", "type": "person"},
            {"name": "高志华", "erp": "gaozhihua9", "type": "person"},
            {"name": "黄萌", "erp": "huangmeng16", "type": "person"},
            {"name": "姜海燕", "erp": "jianghaiyan1", "type": "person"},
            {"name": "李雪", "erp": "lixue368", "type": "person"},
            {"name": "苏星宇", "erp": "suxingyu7", "type": "person"},
            {"name": "孙雅宜", "erp": "sunyayi.1", "type": "person"},
            {"name": "王峰", "erp": "wangfeng479", "type": "person"},
            {"name": "肖伶俐", "erp": "xiaolingli", "type": "person"},
            {"name": "叶帆", "erp": "yefan23", "type": "person"},
            {"name": "郑犇犇", "erp": "zhengbenben", "type": "person"},
            {"name": "郑润宸", "erp": "zhengrunchen.1", "type": "person"},
            {"name": "朱贺存", "erp": "zhuhecun1", "type": "person"},
            {"name": "毛宇君", "erp": "maoyujun.3", "type": "person"},
            {"name": "祝显荣", "erp": "zhuxianrong.0328", "type": "person"},
            {"name": "沈延德", "erp": "shenyande.1", "type": "person"},
            {"name": "🍚 干饭人", "erp": "group:10218887128", "type": "group"},
            {"name": "🏢 基础产品部", "erp": "group:1024703302", "type": "group"},
        ]
        with open(contacts_path, "w", encoding="utf-8") as f:
            json.dump(default_contacts, f, ensure_ascii=False, indent=2)

    todos_path = os.path.join(user_dir, "todos.json")
    if not os.path.exists(todos_path):
        with open(todos_path, "w", encoding="utf-8") as f:
            json.dump([], f)


# ─────────────────────────── Per-user path helpers ─────────────────


def user_data_path(username: str) -> str:
    return os.path.join(DATA_DIR, username, "todos.json")


def user_tags_path(username: str) -> str:
    return os.path.join(DATA_DIR, username, "tags.json")


def user_contacts_path(username: str) -> str:
    return os.path.join(DATA_DIR, username, "contacts.json")


def user_uploads_dir(username: str) -> str:
    return os.path.join(DATA_DIR, username, "uploads")


# Lock for file I/O safety under ThreadingMixIn
_data_lock = threading.Lock()

# ─────────────────────────── Data helpers ───────────────────────────


def _default_todo_fields(partial: Dict[str, Any]) -> Dict[str, Any]:
    """Ensure a todo dict has every expected field with defaults."""
    defaults = {
        "id": 0,
        "text": "",
        "tag": "",
        "status": "pending",
        "date": datetime.now().strftime("%Y-%m-%d"),
        "note": "",
        "starred": False,
        "reminder": None,
        "reminder_target": "",
        "deadline": None,
        "progress": [],
        "attachments": [],
    }
    defaults.update(partial)
    return defaults


def load_todos(username=None) -> List[Dict[str, Any]]:
    data_path = user_data_path(username) if username else DATA_PATH
    with _data_lock:
        if os.path.exists(data_path):
            with open(data_path, "r", encoding="utf-8") as f:
                todos = json.load(f)
            # Backfill new fields for old records
            for t in todos:
                t.setdefault("starred", False)
                t.setdefault("reminder", None)
                t.setdefault("reminder_target", "")
                t.setdefault("deadline", None)
                t.setdefault("progress", [])
                t.setdefault("attachments", [])
            return todos
    # First run: try importing from legacy data.js (only for global path)
    if not username:
        js_path = os.path.join(TODO_DIR, "data.js")
        if os.path.exists(js_path):
            with open(js_path, "r", encoding="utf-8") as f:
                text = f.read()
            text = text.replace("const INITIAL_DATA = ", "").rstrip().rstrip(";")
            todos = json.loads(text)
            for t in todos:
                t.setdefault("starred", False)
                t.setdefault("reminder", None)
                t.setdefault("reminder_target", "")
                t.setdefault("deadline", None)
                t.setdefault("progress", [])
                t.setdefault("attachments", [])
            save_todos(todos)
            return todos
    return []


def save_todos(todos: List[Dict[str, Any]], username=None) -> None:
    data_path = user_data_path(username) if username else DATA_PATH
    with _data_lock:
        with open(data_path, "w", encoding="utf-8") as f:
            json.dump(todos, f, ensure_ascii=False, indent=2)


def next_id(todos: List[Dict[str, Any]]) -> int:
    return max((t.get("id", 0) for t in todos), default=0) + 1


# ─────────────────────────── DB Shim (Supabase PostgreSQL) ─────────────
_USE_DB = bool(os.environ.get("DATABASE_URL"))
if _USE_DB:
    from db import (
        init_db, migrate_json_to_db, export_db_to_json,
        db_load_users, db_save_users,
        db_load_todos, db_save_todos,
        db_load_tags, db_save_tags,
        db_load_contacts, db_save_contacts,
        db_save_upload, db_load_upload,
    )
    init_db()
    # One-time migration from JSON → DB
    if os.environ.get("MIGRATE_JSON_TO_DB") == "1":
        migrate_json_to_db(DATA_DIR)
        print("[DB] Migration complete. Remove MIGRATE_JSON_TO_DB env var now.")
    # Replace module-level data functions
    load_users = db_load_users
    save_users = db_save_users
    load_todos = db_load_todos
    save_todos = db_save_todos
    load_tags = db_load_tags
    save_tags = db_save_tags
    load_contacts = db_load_contacts
    save_contacts = db_save_contacts


# ─────────────────────────── AI helpers ─────────────────────────────


def _call_ai(prompt: str, max_tokens: int = 2048, api_key: Optional[str] = None) -> Optional[str]:
    """Call AI via OpenAI-compatible API (智谱 GLM). Returns text or None on failure."""
    import urllib.request

    key = api_key or AI_API_KEY
    if not key:
        return None

    url = AI_BASE_URL.rstrip("/") + "/chat/completions"
    payload = json.dumps({
        "model": AI_MODEL,
        "messages": [{"role": "user", "content": prompt}],
        "max_tokens": max_tokens,
        "temperature": 0.3,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=payload, headers={
        "Content-Type": "application/json",
        "Authorization": f"Bearer {key}",
    })
    try:
        with urllib.request.urlopen(req, timeout=60) as resp:
            data = json.loads(resp.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    except Exception as e:
        print(f"AI API error: {e}")
        return None


def ai_parse_todos(text: str, api_key: Optional[str] = None, user_tags: Optional[List[str]] = None) -> List[Dict[str, str]]:
    """Use AI to split free-form text into multiple todos with auto-classification."""
    available_tags = user_tags if user_tags is not None else TAGS
    prompt = (
        "你是一个待办拆解助手。用户会用一段话描述多个待办事项，请你：\n"
        "1. 将其拆分成独立的待办条目\n"
        "2. 为每条待办匹配最合适的项目分类\n"
        "3. 如果文本中包含 URL 链接（http/https），将链接提取到 note 字段\n\n"
        f"可选的项目分类有：{json.dumps(available_tags, ensure_ascii=False)}\n\n"
        "请严格返回 JSON 数组格式，每个元素包含：\n"
        "- text: 待办内容（不含链接）\n"
        "- tag: 项目分类\n"
        "- note: 备注（放链接或补充说明，没有则为空字符串）\n\n"
        "如果无法确定分类就留空字符串。不要输出任何其他内容。\n\n"
        '示例输入："处理消保投诉3个，运通平台商户注册 https://xbp.jd.com/console/pending/123，写绩效周报"\n'
        '示例输出：[{"text":"处理消保投诉3个","tag":"合规整改消保","note":""},'
        '{"text":"运通平台商户注册","tag":"运营工作","note":"https://xbp.jd.com/console/pending/123"},'
        '{"text":"写绩效周报","tag":"绩效汇报","note":""}]\n\n'
        f'用户输入："{text}"'
    )

    raw = _call_ai(prompt, max_tokens=1024, api_key=api_key)
    if raw is None:
        return [{"text": text, "tag": ""}]

    try:
        raw = raw.strip()
        # Strip markdown code fences if present
        if raw.startswith("```"):
            lines = raw.split("\n")
            lines = [l for l in lines if not l.strip().startswith("```")]
            raw = "\n".join(lines).strip()
        start = raw.find("[")
        end = raw.rfind("]") + 1
        if start >= 0 and end > start:
            items = json.loads(raw[start:end])
            result = []
            for item in items:
                if isinstance(item, dict) and item.get("text"):
                    tag = item.get("tag", "")
                    if tag not in available_tags:
                        tag = ""
                    result.append({"text": item["text"], "tag": tag, "note": item.get("note", "")})
            return result if result else [{"text": text, "tag": ""}]
    except Exception as e:
        print(f"AI parse JSON error: {e}")

    return [{"text": text, "tag": ""}]


def ai_generate_summary(
    todos_in_range: List[Dict[str, Any]],
    tag_filter: str,
    range_label: str,
    api_key: Optional[str] = None,
) -> str:
    """Use AI to generate a Chinese work summary from todos."""
    if tag_filter:
        todos_in_range = [t for t in todos_in_range if t.get("tag") == tag_filter]

    if not todos_in_range:
        return "该时间范围内没有待办记录。"

    # Build a readable listing for Claude
    lines = []
    status_names = {
        "done": "已完成",
        "doing": "进行中",
        "pending": "未启动",
        "delayed": "暂缓",
    }
    for t in todos_in_range:
        s = status_names.get(t.get("status", "pending"), "未启动")
        tag = t.get("tag", "未分类") or "未分类"
        starred = " ⭐重点" if t.get("starred") else ""
        note_part = f"  备注: {t['note']}" if t.get("note") else ""
        progress_part = ""
        if t.get("progress"):
            progress_part = "  进展: " + "; ".join(
                p.get("text", "") for p in t["progress"]
            )
        lines.append(
            f"- [{s}][{tag}]{starred} {t.get('text', '')}  "
            f"(日期: {t.get('date', '')}){note_part}{progress_part}"
        )

    todo_list_text = "\n".join(lines)

    prompt = (
        f"你是一位专业的工作总结助手。以下是用户在【{range_label}】期间的待办事项列表：\n\n"
        f"{todo_list_text}\n\n"
        "请根据以上信息撰写一份中文工作总结，要求：\n"
        "1. 按项目分类归纳已完成的工作\n"
        "2. 列出正在进行中的事项\n"
        "3. 突出重点成果和亮点\n"
        "4. 指出暂缓或受阻的事项（如有）\n"
        "5. 语言专业简洁，适合直接复制到京ME或邮件中汇报\n"
        "6. **不要使用 Markdown 格式**，不要用 # ## ** ``` 等符号\n"
        "7. 用中文数字序号（一、二、三）做大标题，用 1. 2. 3. 做小项\n"
        "8. 用缩进和换行来组织层次，不要用任何特殊标记符号\n"
        "9. 段落之间空一行，整体简洁清晰\n\n"
        "示例格式：\n"
        "一、已完成工作\n\n"
        "【运营工作】\n"
        "1. 完成XX配置，保障业务正常运行\n"
        "2. 处理XX问题，及时响应\n\n"
        "二、进行中事项\n\n"
        "1. XX项目正在推进中，预计下周完成\n\n"
        "直接输出总结内容，不需要额外说明。"
    )

    result = _call_ai(prompt, max_tokens=4096, api_key=api_key)
    if result is None:
        return "AI 总结生成失败，请检查 API 配置。"
    return result.strip()


# ─────────────────────────── Export ─────────────────────────────────


def generate_excel_bytes(todos: List[Dict[str, Any]]) -> Optional[bytes]:
    """Generate an Excel file in memory and return its bytes."""
    try:
        import pandas as pd
    except ImportError:
        print("pandas not installed, cannot export Excel")
        return None

    status_map = {
        "done": "已完结",
        "pending": "未启动",
        "delayed": "暂缓",
        "doing": "进行中",
    }
    rows = []
    for t in todos:
        rows.append({
            "待办": t.get("text", ""),
            "项目": t.get("tag", ""),
            "进度": status_map.get(t.get("status", "pending"), "未启动"),
            "情况备注": t.get("note", ""),
            "开始时间": t.get("date", ""),
            "截止日期": t.get("deadline", "") or "",
            "重点": "是" if t.get("starred") else "",
            "提醒时间": t.get("reminder", "") or "",
        })

    df = pd.DataFrame(rows)
    tmp = tempfile.NamedTemporaryFile(delete=False, suffix=".xlsx")
    try:
        df.to_excel(tmp.name, index=False, engine="openpyxl")
        with open(tmp.name, "rb") as f:
            data = f.read()
        return data
    finally:
        try:
            os.unlink(tmp.name)
        except OSError:
            pass


# ─────────────────────────── JoyClaw 京ME ─────────────────────────────


def _get_joyclaw_token():
    """Read gateway auth token from openclaw.json."""
    try:
        with open(JOYCLAW_CONFIG, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return cfg.get("gateway", {}).get("auth", {}).get("token", "")
    except Exception:
        return ""


def send_jme_reminder(targets, todo_text, todo_note="", username=None):
    """Write reminder files for JoyClaw to pick up and send via 京ME.
    targets can be a string (single erp) or list of erps.
    Also pushes to WeChat Work webhook as backup."""
    import urllib.request

    if isinstance(targets, str):
        targets = [t.strip() for t in targets.split(",") if t.strip()]
    if not targets:
        targets = ["sunyayi.1"]  # 默认提醒自己

    # 1. 为每个人/群写一个提醒文件
    for target_id in targets:
        try:
            os.makedirs(REMINDERS_DIR, exist_ok=True)
            ts = datetime.now().strftime("%Y%m%d_%H%M%S")
            safe_id = target_id.replace(":", "_")
            filename = f"reminder_{ts}_{safe_id}.md"
            filepath = os.path.join(REMINDERS_DIR, filename)

            # 找中文名
            name_map = {c["erp"]: c["name"] for c in load_contacts(username)}
            display_name = name_map.get(target_id, target_id)

            # 构建消息内容（含备注）
            msg_body = f"Hi~小孙提醒：⏰ {todo_text}"
            if todo_note:
                msg_body += f"\n备注：{todo_note}"

            # 区分个人和群
            if target_id.startswith("group:"):
                group_id = target_id.replace("group:", "")
                content = f"""---
target: {group_id}
name: {display_name}
type: group
status: pending
created: {datetime.now().isoformat()}
---

请帮我发一条群消息到群号{group_id}，内容是：

{msg_body}
"""
            else:
                content = f"""---
target: {target_id}
name: {display_name}
type: person
status: pending
created: {datetime.now().isoformat()}
---

请帮我发一条信息给{target_id}，内容是：

{msg_body}
"""

            with open(filepath, "w", encoding="utf-8") as f:
                f.write(content)
            print(f"[提醒] 已写入: {filepath}")
        except Exception as e:
            print(f"[提醒] 写文件失败 ({target_id}): {e}")

    # 2. 企业微信 webhook 备份推送
    WECHAT_WEBHOOK = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=b213b241-11cb-424f-9141-8fbf6189cff4"
    name_map = {c["erp"]: c["name"] for c in load_contacts(username)}
    names = [name_map.get(t, t) for t in targets]
    msg = f"## ⏰ 待办提醒\n\n**待办：** {todo_text}\n"
    if todo_note:
        msg += f"**备注：** {todo_note}\n"
    msg += f"\n> 💬 京ME提醒对象: {', '.join(names)}\n> 提醒文件已写入JoyClaw，请在JoyClaw中说「检查提醒」"

    try:
        payload = json.dumps({"msgtype": "markdown", "markdown": {"content": msg}}).encode("utf-8")
        req = urllib.request.Request(WECHAT_WEBHOOK, data=payload,
                                     headers={"Content-Type": "application/json"}, method="POST")
        urllib.request.urlopen(req, timeout=10)
        print(f"[提醒] 企业微信推送完成")
    except Exception as e:
        print(f"[提醒] 企业微信推送失败: {e}")

    return True


# ─────────────────────────── Reminder check ─────────────────────────


def check_reminders(todos: List[Dict[str, Any]], username=None) -> List[Dict[str, Any]]:
    """Return todos whose reminder is due (past 5 min to future 60 sec).
    Also triggers 京ME messages for todos with reminder_target set."""
    now = datetime.now()
    window_start = now - timedelta(minutes=5)  # 也捕获过去5分钟内的
    window_end = now + timedelta(seconds=60)
    due = []
    changed = False
    for t in todos:
        reminder = t.get("reminder")
        if not reminder:
            continue
        if t.get("_reminded"):
            continue  # 已经提醒过了
        try:
            r_time = datetime.fromisoformat(reminder)
        except (ValueError, TypeError):
            continue
        if window_start <= r_time <= window_end:
            due.append(t)
            t["_reminded"] = True
            changed = True
            # Send 京ME if target is set
            target = t.get("reminder_target", "")
            if target:
                send_jme_reminder(target, t.get("text", ""), t.get("note", ""), username=username)
    if changed:
        save_todos(todos, username)
    return due


# ─────────────────────────── HTTP Server ────────────────────────────


class Handler(http.server.BaseHTTPRequestHandler):
    def log_message(self, *args):
        pass

    # ── Auth helpers ──

    def _get_current_user(self) -> Optional[str]:
        """Read session token from Cookie, return username or None."""
        cookie_header = self.headers.get("Cookie", "")
        if not cookie_header:
            return None
        cookie = SimpleCookie()
        try:
            cookie.load(cookie_header)
        except Exception:
            return None
        morsel = cookie.get("session")
        if not morsel:
            return None
        return get_session_user(morsel.value)

    def _require_auth(self) -> Optional[str]:
        """Return username if logged in, otherwise send 401 and return None."""
        user = self._get_current_user()
        if user is None:
            self._json_resp({"error": "unauthorized"}, 401)
        return user

    def _set_session_cookie(self, token: str, max_age: int):
        self.send_header(
            "Set-Cookie",
            f"session={token}; Path=/; HttpOnly; SameSite=Lax; Max-Age={max_age}",
        )

    # ── CORS preflight ──
    def do_OPTIONS(self):
        self.send_response(200)
        self._cors_headers()
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type, X-AI-Key, Cookie")
        self.end_headers()

    # ── GET routes ──
    def do_GET(self):
        path = self.path.split("?")[0]  # strip query string

        if path == "/" or path == "/index.html":
            self._serve_file(HTML_PATH, "text/html")

        elif path == "/api/me":
            user = self._get_current_user()
            if user:
                self._json_resp({"username": user})
            else:
                self._json_resp({"error": "unauthorized"}, 401)

        elif path == "/api/ai-config":
            has_env_key = bool(AI_API_KEY)
            self._json_resp({"has_env_key": has_env_key, "model": AI_MODEL})

        elif path == "/api/todos":
            user = self._require_auth()
            if not user:
                return
            todos = load_todos(user)
            self._json_resp(todos)

        elif path == "/api/export":
            user = self._require_auth()
            if not user:
                return
            self._handle_export(user)

        elif path == "/api/check-reminders":
            user = self._require_auth()
            if not user:
                return
            todos = load_todos(user)
            due = check_reminders(todos, user)
            self._json_resp(due)

        elif path == "/api/contacts":
            user = self._require_auth()
            if not user:
                return
            self._json_resp(load_contacts(user))

        elif path == "/api/tags":
            user = self._require_auth()
            if not user:
                return
            self._json_resp(load_tags(user))

        elif path == "/api/reminders":
            user = self._require_auth()
            if not user:
                return
            todos = load_todos(user)
            name_map = {c["erp"]: c["name"] for c in load_contacts(user)}
            reminders = []
            for t in todos:
                if t.get("reminder"):
                    targets = [x.strip() for x in (t.get("reminder_target") or "").split(",") if x.strip()]
                    target_names = [name_map.get(x, x) for x in targets]
                    reminders.append({
                        "id": t["id"],
                        "text": t["text"],
                        "note": t.get("note", ""),
                        "reminder": t["reminder"],
                        "targets": target_names,
                        "target_erps": targets,
                        "reminded": t.get("_reminded", False),
                    })
            reminders.sort(key=lambda x: x["reminder"])
            self._json_resp(reminders)

        elif path.startswith("/uploads/"):
            user = self._require_auth()
            if not user:
                return
            filename = path[len("/uploads/"):]
            filepath = os.path.join(user_uploads_dir(user), filename)
            if os.path.isfile(filepath):
                self._serve_upload(filepath)
            elif _USE_DB:
                # Try loading from database
                result = db_load_upload(user, filename)
                if result:
                    mime_type, file_data = result
                    self.send_response(200)
                    self.send_header("Content-Type", mime_type)
                    self.send_header("Content-Length", str(len(file_data)))
                    self._cors_headers()
                    self.end_headers()
                    self.wfile.write(file_data)
                else:
                    self.send_error(404)
            else:
                self.send_error(404)

        else:
            self.send_error(404)

    # ── POST routes ──
    def do_POST(self):
        path = self.path

        # ── Auth endpoints (no login required) ──
        if path == "/api/register":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {}
            self._handle_register(body)
            return

        if path == "/api/login":
            length = int(self.headers.get("Content-Length", 0))
            raw = self.rfile.read(length) if length else b"{}"
            try:
                body = json.loads(raw)
            except json.JSONDecodeError:
                body = {}
            self._handle_login(body)
            return

        if path == "/api/logout":
            self._handle_logout()
            return

        # ── Upload needs auth but reads body differently ──
        if path == "/api/upload":
            user = self._require_auth()
            if not user:
                return
            self._handle_upload(user)
            return

        # ── All other POST endpoints require auth ──
        user = self._require_auth()
        if not user:
            return

        length = int(self.headers.get("Content-Length", 0))
        raw = self.rfile.read(length) if length else b"{}"
        try:
            body = json.loads(raw)
        except json.JSONDecodeError:
            body = {}

        if path == "/api/add":
            self._handle_add(body, user)
        elif path == "/api/toggle":
            self._handle_toggle(body, user)
        elif path == "/api/delete":
            self._handle_delete(body, user)
        elif path == "/api/ai-parse":
            self._handle_ai_parse(body, user)
        elif path == "/api/batch-add":
            self._handle_batch_add(body, user)
        elif path == "/api/update":
            self._handle_update(body, user)
        elif path == "/api/cancel-reminder":
            tid = body.get("id")
            todos = load_todos(user)
            for t in todos:
                if t["id"] == tid:
                    t["reminder"] = None
                    t["reminder_target"] = ""
                    t.pop("_reminded", None)
                    break
            save_todos(todos, user)
            self._json_resp({"ok": True})
        elif path == "/api/add-progress":
            self._handle_add_progress(body, user)
        elif path == "/api/add-attachment":
            self._handle_add_attachment(body, user)
        elif path == "/api/remove-attachment":
            self._handle_remove_attachment(body, user)
        elif path == "/api/ai-summary":
            self._handle_ai_summary(body, user)
        elif path == "/api/add-contact":
            self._handle_add_contact(body, user)
        elif path == "/api/remove-contact":
            self._handle_remove_contact(body, user)
        elif path == "/api/add-tag":
            self._handle_add_tag(body, user)
        elif path == "/api/remove-tag":
            self._handle_remove_tag(body, user)
        elif path == "/api/import":
            self._handle_import(body, user)
        else:
            self.send_error(404)

    # ── Auth Handlers ──

    def _handle_register(self, body: dict):
        import re
        username = (body.get("username") or "").strip().lower()
        password = body.get("password") or ""
        if not re.match(r'^[a-zA-Z0-9_]+$', username) or len(username) < 2:
            self._json_resp({"error": "用户名只能包含字母、数字、下划线，且至少2位"}, 400)
            return
        if len(password) < 4:
            self._json_resp({"error": "密码至少4位"}, 400)
            return

        users = load_users()
        if username in users:
            self._json_resp({"error": "用户名已存在"}, 400)
            return

        salt = secrets.token_hex(16)
        users[username] = {
            "salt": salt,
            "hash": hash_password(password, salt),
            "created": datetime.now().isoformat(),
        }
        save_users(users)
        ensure_user_dir(username)

        token = create_session(username)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors_headers()
        self._set_session_cookie(token, SESSION_EXPIRY)
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "username": username}, ensure_ascii=False).encode("utf-8"))

    def _handle_login(self, body: dict):
        username = (body.get("username") or "").strip().lower()
        password = body.get("password") or ""

        users = load_users()
        user_record = users.get(username)
        if not user_record:
            self._json_resp({"error": "用户名或密码错误"}, 401)
            return

        if hash_password(password, user_record["salt"]) != user_record["hash"]:
            self._json_resp({"error": "用户名或密码错误"}, 401)
            return

        ensure_user_dir(username)
        token = create_session(username)
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors_headers()
        self._set_session_cookie(token, SESSION_EXPIRY)
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True, "username": username}, ensure_ascii=False).encode("utf-8"))

    def _handle_logout(self):
        # Read cookie to find token
        cookie_header = self.headers.get("Cookie", "")
        if cookie_header:
            cookie = SimpleCookie()
            try:
                cookie.load(cookie_header)
                morsel = cookie.get("session")
                if morsel:
                    destroy_session(morsel.value)
            except Exception:
                pass

        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors_headers()
        # Clear cookie
        self._set_session_cookie("", 0)
        self.end_headers()
        self.wfile.write(json.dumps({"ok": True}, ensure_ascii=False).encode("utf-8"))

    # ── Data Handlers ──

    def _handle_add(self, body: dict, user: str):
        todos = load_todos(user)
        todo = _default_todo_fields({
            "id": next_id(todos),
            "text": body.get("text", "").strip(),
            "tag": body.get("tag", ""),
            "status": "pending",
            "date": datetime.now().strftime("%Y-%m-%d"),
            "note": body.get("note", ""),
            "starred": body.get("starred", False),
            "reminder": body.get("reminder"),
            "reminder_target": body.get("reminder_target", ""),
            "deadline": body.get("deadline"),
        })
        todos.insert(0, todo)
        save_todos(todos, user)
        self._json_resp({"ok": True, "todo": todo})

    def _handle_toggle(self, body: dict, user: str):
        todos = load_todos(user)
        tid = body.get("id")
        for t in todos:
            if t["id"] == tid:
                t["status"] = "pending" if t["status"] == "done" else "done"
                break
        save_todos(todos, user)
        self._json_resp({"ok": True})

    def _handle_delete(self, body: dict, user: str):
        todos = load_todos(user)
        tid = body.get("id")
        todos = [t for t in todos if t["id"] != tid]
        save_todos(todos, user)
        self._json_resp({"ok": True})

    def _handle_ai_parse(self, body: dict, user: str):
        text = body.get("text", "").strip()
        if not text:
            self._json_resp({"error": "空输入"})
            return
        api_key = self.headers.get("X-AI-Key") or None
        user_tags = load_tags(user)
        parsed = ai_parse_todos(text, api_key=api_key, user_tags=user_tags)
        self._json_resp({"items": parsed})

    def _handle_batch_add(self, body: dict, user: str):
        items = body.get("items", [])
        todos = load_todos(user)
        added = 0
        for item in items:
            todo = _default_todo_fields({
                "id": next_id(todos),
                "text": item.get("text", "").strip(),
                "tag": item.get("tag", ""),
                "status": "pending",
                "date": datetime.now().strftime("%Y-%m-%d"),
                "note": item.get("note", ""),
            })
            todos.insert(0, todo)
            added += 1
        save_todos(todos, user)
        self._json_resp({"ok": True, "added": added})

    def _handle_update(self, body: dict, user: str):
        tid = body.get("id")
        if tid is None:
            self._json_resp({"error": "missing id"})
            return

        # Map frontend field names to backend field names
        if "reminder_time" in body:
            body["reminder"] = body.pop("reminder_time") or None
        # Clean up legacy field
        body.pop("reminder_target_id", None)

        allowed = {"text", "tag", "status", "note", "starred", "reminder", "reminder_target", "deadline"}
        todos = load_todos(user)
        found = False
        for t in todos:
            if t["id"] == tid:
                for key in allowed:
                    if key in body:
                        t[key] = body[key]
                # Clear flags if reminder is re-set
                if "reminder" in body:
                    t.pop("_jme_sent", None)
                    t.pop("_reminded", None)
                found = True
                break
        if not found:
            self._json_resp({"error": "not found"})
            return
        save_todos(todos, user)
        self._json_resp({"ok": True})

    def _handle_add_progress(self, body: dict, user: str):
        tid = body.get("id")
        text = body.get("text", "").strip()
        if tid is None or not text:
            self._json_resp({"error": "missing id or text"})
            return
        todos = load_todos(user)
        found = False
        for t in todos:
            if t["id"] == tid:
                t.setdefault("progress", [])
                entry = {
                    "text": text,
                    "time": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                }
                t["progress"].append(entry)
                found = True
                break
        if not found:
            self._json_resp({"error": "not found"})
            return
        save_todos(todos, user)
        self._json_resp({"ok": True, "entry": entry})

    def _handle_add_attachment(self, body: dict, user: str):
        tid = body.get("id")
        title = body.get("title", "").strip()
        url = body.get("url", "").strip()
        if tid is None or not url:
            self._json_resp({"error": "missing id or url"})
            return
        todos = load_todos(user)
        found = False
        for t in todos:
            if t["id"] == tid:
                t.setdefault("attachments", [])
                attachment = {"title": title or url, "url": url}
                t["attachments"].append(attachment)
                found = True
                break
        if not found:
            self._json_resp({"error": "not found"})
            return
        save_todos(todos, user)
        self._json_resp({"ok": True})

    def _handle_remove_attachment(self, body: dict, user: str):
        tid = body.get("id")
        index = body.get("index")
        if tid is None or index is None:
            self._json_resp({"error": "missing id or index"})
            return
        todos = load_todos(user)
        found = False
        for t in todos:
            if t["id"] == tid:
                attachments = t.get("attachments", [])
                if 0 <= index < len(attachments):
                    attachments.pop(index)
                    found = True
                else:
                    self._json_resp({"error": "index out of range"})
                    return
                break
        if not found:
            self._json_resp({"error": "not found"})
            return
        save_todos(todos, user)
        self._json_resp({"ok": True})

    def _handle_upload(self, user: str):
        content_type = self.headers.get('Content-Type', '')
        if 'multipart/form-data' not in content_type:
            self._json_resp({"error": "需要 multipart/form-data"}, 400)
            return

        # Parse boundary
        boundary = content_type.split('boundary=')[1].encode()
        content_length = int(self.headers.get('Content-Length', 0))
        body = self.rfile.read(content_length)

        # Find file data between boundaries
        parts = body.split(b'--' + boundary)
        for part in parts:
            if b'filename="' in part:
                # Extract filename
                header_end = part.find(b'\r\n\r\n')
                headers = part[:header_end].decode('utf-8', errors='replace')
                file_data = part[header_end+4:]
                if file_data.endswith(b'\r\n'):
                    file_data = file_data[:-2]

                # Get original filename
                import re
                fn_match = re.search(r'filename="([^"]+)"', headers)
                original_name = fn_match.group(1) if fn_match else 'upload'

                # Save with timestamp prefix to avoid conflicts
                ts = datetime.now().strftime('%Y%m%d_%H%M%S')
                safe_name = f"{ts}_{original_name}"
                uploads_dir = user_uploads_dir(user)
                filepath = os.path.join(uploads_dir, safe_name)
                os.makedirs(uploads_dir, exist_ok=True)
                with open(filepath, 'wb') as f:
                    f.write(file_data)

                # Also persist to database if available
                if _USE_DB:
                    import mimetypes
                    mime, _ = mimetypes.guess_type(safe_name)
                    if mime is None:
                        mime = "application/octet-stream"
                    try:
                        db_save_upload(user, safe_name, mime, file_data)
                    except Exception as e:
                        print(f"[DB] Warning: upload save failed: {e}")

                self._json_resp({"ok": True, "filename": safe_name, "url": f"/uploads/{safe_name}"})
                return

        self._json_resp({"error": "未找到上传文件"}, 400)

    def _handle_add_contact(self, body: dict, user: str):
        name = body.get("name", "").strip()
        erp = body.get("erp", "").strip()
        ctype = body.get("type", "person").strip()
        if not name or not erp:
            self._json_resp({"error": "missing name or erp"}, 400)
            return
        contacts = load_contacts(user)
        # Check for duplicate erp
        for c in contacts:
            if c["erp"] == erp:
                self._json_resp({"error": "contact already exists"}, 400)
                return
        contacts.append({"name": name, "erp": erp, "type": ctype})
        save_contacts(contacts, user)
        self._json_resp({"ok": True})

    def _handle_remove_contact(self, body: dict, user: str):
        erp = body.get("erp", "").strip()
        if not erp:
            self._json_resp({"error": "missing erp"}, 400)
            return
        contacts = load_contacts(user)
        new_contacts = [c for c in contacts if c["erp"] != erp]
        if len(new_contacts) == len(contacts):
            self._json_resp({"error": "contact not found"}, 404)
            return
        save_contacts(new_contacts, user)
        self._json_resp({"ok": True})

    def _handle_add_tag(self, body: dict, user: str):
        tag = body.get("tag", "").strip()
        if not tag:
            self._json_resp({"error": "missing tag"}, 400)
            return
        tags = load_tags(user)
        if tag in tags:
            self._json_resp({"error": "tag already exists"}, 400)
            return
        tags.append(tag)
        save_tags(tags, user)
        self._json_resp({"ok": True, "tags": tags})

    def _handle_remove_tag(self, body: dict, user: str):
        tag = body.get("tag", "").strip()
        if not tag:
            self._json_resp({"error": "missing tag"}, 400)
            return
        tags = load_tags(user)
        if tag not in tags:
            self._json_resp({"error": "tag not found"}, 404)
            return
        tags.remove(tag)
        save_tags(tags, user)
        self._json_resp({"ok": True, "tags": tags})

    def _handle_import(self, body: dict, user: str):
        """Batch import todos from a list."""
        items = body.get("items", [])
        if not items:
            self._json_resp({"error": "empty items"}, 400)
            return
        todos = load_todos(user)
        added = 0
        for item in items:
            todo = _default_todo_fields({
                "id": next_id(todos),
                "text": item.get("text", "").strip(),
                "tag": item.get("tag", ""),
                "status": item.get("status", "pending"),
                "date": item.get("date", datetime.now().strftime("%Y-%m-%d")),
                "note": item.get("note", ""),
                "starred": item.get("starred", False),
            })
            if todo["text"]:
                todos.insert(0, todo)
                added += 1
        save_todos(todos, user)
        self._json_resp({"ok": True, "added": added})

    def _handle_ai_summary(self, body: dict, user: str):
        range_type = body.get("range", "week")
        tag_filter = body.get("tag", "")
        today = datetime.now().date()

        if range_type == "week":
            start = today - timedelta(days=today.weekday())
            end = today
            label = f"本周（{start} ~ {end}）"
        elif range_type == "month":
            start = today.replace(day=1)
            end = today
            label = f"本月（{start} ~ {end}）"
        elif range_type == "year":
            start = today.replace(month=1, day=1)
            end = today
            label = f"本年（{start} ~ {end}）"
        elif range_type == "custom":
            try:
                start = datetime.strptime(body.get("start_date", ""), "%Y-%m-%d").date()
                end = datetime.strptime(body.get("end_date", ""), "%Y-%m-%d").date()
            except (ValueError, TypeError):
                self._json_resp({"error": "invalid date format, use YYYY-MM-DD"})
                return
            label = f"自定义（{start} ~ {end}）"
        else:
            self._json_resp({"error": "invalid range type"})
            return

        todos = load_todos(user)
        filtered = []
        for t in todos:
            d = t.get("date", "")
            if not d:
                continue
            try:
                td = datetime.strptime(d, "%Y-%m-%d").date()
            except ValueError:
                continue
            if start <= td <= end:
                filtered.append(t)

        api_key = self.headers.get("X-AI-Key") or None
        summary = ai_generate_summary(filtered, tag_filter, label, api_key=api_key)
        self._json_resp({"summary": summary})

    def _handle_export(self, user: str):
        todos = load_todos(user)
        data = generate_excel_bytes(todos)
        if data is None:
            self.send_error(500, "Excel generation failed (pandas/openpyxl not installed?)")
            return
        filename = "todos_{}.xlsx".format(datetime.now().strftime("%Y%m%d_%H%M%S"))
        self.send_response(200)
        self.send_header(
            "Content-Type",
            "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
        )
        self.send_header(
            "Content-Disposition",
            'attachment; filename="{}"'.format(filename),
        )
        self.send_header("Content-Length", str(len(data)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)

    # ── Response helpers ──

    def _cors_headers(self):
        self.send_header("Access-Control-Allow-Origin", "*")

    def _json_resp(self, data: Any, status: int = 200):
        payload = json.dumps(data, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self._cors_headers()
        self.end_headers()
        self.wfile.write(payload)

    def _serve_file(self, path: str, ct: str):
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
        encoded = content.encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "{}; charset=utf-8".format(ct))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(encoded)

    def _serve_upload(self, filepath: str):
        """Serve an uploaded file with appropriate content-type."""
        import mimetypes
        ct, _ = mimetypes.guess_type(filepath)
        if ct is None:
            ct = "application/octet-stream"
        with open(filepath, "rb") as f:
            data = f.read()
        self.send_response(200)
        self.send_header("Content-Type", ct)
        self.send_header("Content-Length", str(len(data)))
        self._cors_headers()
        self.end_headers()
        self.wfile.write(data)


# ─────────────────────────── Entry point ────────────────────────────

# ─── GitHub Auto-Sync (data persistence) ───
GITHUB_SYNC_INTERVAL = int(os.environ.get("SYNC_INTERVAL", "600"))  # default 10 minutes
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "")
GITHUB_REPO = os.environ.get("GITHUB_REPO", "")  # e.g. "sunyayiii/ccsun-todolist"
GITHUB_BRANCH = os.environ.get("GITHUB_BRANCH", "main")


def _github_sync_loop():
    """Background thread: periodically push data/ to GitHub via git commands.

    When _USE_DB is True, exports DB content to JSON before each push cycle,
    so GitHub always has a fresh snapshot for backup purposes.
    """
    import subprocess
    if not GITHUB_TOKEN or not GITHUB_REPO:
        print("[Sync] GITHUB_TOKEN or GITHUB_REPO not set, auto-sync disabled.")
        return

    # Configure git for the container
    remote_url = f"https://x-access-token:{GITHUB_TOKEN}@github.com/{GITHUB_REPO}.git"
    env = os.environ.copy()
    env["GIT_AUTHOR_NAME"] = "TodoBot"
    env["GIT_AUTHOR_EMAIL"] = "bot@ccsun-todo.app"
    env["GIT_COMMITTER_NAME"] = "TodoBot"
    env["GIT_COMMITTER_EMAIL"] = "bot@ccsun-todo.app"
    cwd = TODO_DIR

    # ── Initial git setup (run once at startup) ──
    try:
        if not os.path.isdir(os.path.join(cwd, ".git")):
            subprocess.run(["git", "init"], cwd=cwd, env=env,
                           capture_output=True, timeout=30)
            subprocess.run(["git", "remote", "add", "origin", remote_url],
                           cwd=cwd, env=env, capture_output=True, timeout=30)
        else:
            subprocess.run(["git", "remote", "set-url", "origin", remote_url],
                           cwd=cwd, env=env, capture_output=True, timeout=10)

        # Always pull latest on startup to get any manual fixes
        subprocess.run(["git", "fetch", "origin", GITHUB_BRANCH],
                       cwd=cwd, env=env, capture_output=True, timeout=60)
        subprocess.run(["git", "checkout", "-B", GITHUB_BRANCH,
                        f"origin/{GITHUB_BRANCH}"],
                       cwd=cwd, env=env, capture_output=True, timeout=30)
        print("[Sync] Initial git pull complete.")
    except Exception as e:
        print(f"[Sync] Initial git setup error: {e}")

    # ── Periodic sync loop ──
    while True:
        time.sleep(GITHUB_SYNC_INTERVAL)
        try:
            # If using DB, export data to JSON files first
            if _USE_DB:
                try:
                    from db import export_db_to_json
                    export_db_to_json(DATA_DIR)
                except Exception as e:
                    print(f"[Sync] DB export error: {e}")

            # Check if data dir has any files
            if not os.path.isdir(DATA_DIR) or not os.listdir(DATA_DIR):
                continue

            # Stage only the data directory
            subprocess.run(["git", "add", "data/"], cwd=cwd, env=env,
                           capture_output=True, timeout=30)

            # Check if there are changes to commit
            diff_result = subprocess.run(
                ["git", "diff", "--cached", "--quiet"],
                cwd=cwd, env=env, capture_output=True, timeout=10
            )
            if diff_result.returncode == 0:
                # No changes
                continue

            # Commit and push
            ts = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
            subprocess.run(
                ["git", "commit", "-m", f"[auto-sync] data backup {ts}"],
                cwd=cwd, env=env, capture_output=True, timeout=30
            )
            push_result = subprocess.run(
                ["git", "push", "origin", GITHUB_BRANCH],
                cwd=cwd, env=env, capture_output=True, timeout=60
            )
            if push_result.returncode == 0:
                print(f"[Sync] Data pushed to GitHub at {ts}")
            else:
                print(f"[Sync] Push failed: {push_result.stderr.decode()[:200]}")

        except Exception as e:
            print(f"[Sync] Error: {e}")


class ThreadedServer(socketserver.ThreadingMixIn, http.server.HTTPServer):
    daemon_threads = True
    allow_reuse_address = True


if __name__ == "__main__":
    # Start GitHub sync background thread
    sync_thread = threading.Thread(target=_github_sync_loop, daemon=True)
    sync_thread.start()

    server = ThreadedServer(("0.0.0.0", PORT), Handler)
    print("待办管理启动: http://0.0.0.0:{}".format(PORT))
    # Only open browser locally (not on cloud deploy)
    if not os.environ.get("RENDER") and not os.environ.get("PORT"):
        import webbrowser
        webbrowser.open("http://127.0.0.1:{}".format(PORT))
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\n已退出")
