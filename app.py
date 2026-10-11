# -*- coding: utf-8 -*-
"""
老人智能药盒 · 最小 Demo 后端

单文件 FastAPI 服务：托管静态页面 + 代理 DeepSeek 调用（API Key 只留在后端）。
状态全部存内存，进程重启即清空。

内部结构（按区块阅读）：
    CONFIG    常量与配置
    STATE     内存状态 + 虚拟时钟
    AI        DeepSeek 调用与响应解析（全服务唯一的网络出口）
    VALIDATE  校验器：items -> issues[]（纯函数，无副作用）
    DOMAIN    频次展开、Dose 生成、状态流转（纯函数）
    ROUTES    HTTP 接口

设计要点：
  * 频次到时段（早/中/晚）的展开是后端写死的确定性规则，不由模型决定。
  * plan 分 draft / active 两态，"子女确认"就是这两态的边界；老人端只读 active。
  * overdue 不是存储状态，是 compute_status() 算出来的，因此虚拟时钟能天然驱动超时。
  * prompt 只负责降低出错率，校验器才是防线；AI 的任何异常都走同一条 issues 通道。
"""

import json
import math
import os
import uuid
from datetime import date, datetime, time as dtime, timedelta
from typing import Any, List, Optional

import httpx
from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from pydantic import BaseModel

# ══════════════════════════════════ CONFIG ══════════════════════════════════

APP_DIR = os.path.dirname(os.path.abspath(__file__))
STATIC_DIR = os.path.join(APP_DIR, "static")
ENV_FILE = os.path.join(APP_DIR, ".env")


def _load_dotenv(path: str) -> None:
    """把 .env 里的键值对读进 os.environ。

    手写而不引 python-dotenv：只有十几行，省掉一个依赖，也省掉
    "忘了 pip install 导致服务起不来"这类问题。真实环境变量优先，
    所以 export 过的值会盖住 .env，符合 dotenv 惯例。
    """
    if not os.path.exists(path):
        return
    with open(path, encoding="utf-8-sig") as fh:   # utf-8-sig：容忍记事本存出来的 BOM
        for line in fh:
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, value = line.partition("=")
            key, value = key.strip(), value.strip()
            if key.startswith("export "):          # 容忍 shell 写法
                key = key[len("export "):].strip()
            if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
                value = value[1:-1]                # 去掉成对引号
            if key and key not in os.environ:
                os.environ[key] = value


_load_dotenv(ENV_FILE)

DEEPSEEK_BASE_URL = os.environ.get("DEEPSEEK_BASE_URL", "https://api.deepseek.com")
DEEPSEEK_MODEL = os.environ.get("DEEPSEEK_MODEL", "deepseek-chat")
DEEPSEEK_API_KEY = os.environ.get("DEEPSEEK_API_KEY", "")

PATIENT_NAME = "张桂英"

# 时段表：一天最多四次。顺序即时间顺序。
SLOTS = [
    ("morning", "早上", "08:00"),
    ("noon", "中午", "12:00"),
    ("evening", "晚上", "18:00"),
    ("bedtime", "睡前", "21:30"),
]
SLOT_ORDER = [k for k, _, _ in SLOTS]
SLOT_LABEL = {k: label for k, label, _ in SLOTS}
SLOT_AT = {k: dtime(int(t[:2]), int(t[3:])) for k, _, t in SLOTS}

# 每天次数 -> 具体时段。确定性展开，模型无权决定这件事。
FREQ_TO_SLOTS = {
    1: ["morning"],
    2: ["morning", "evening"],
    3: ["morning", "noon", "evening"],
    4: ["morning", "noon", "evening", "bedtime"],
}
MAX_TIMES_PER_DAY = 4

GRACE_MINUTES = 30  # 超过计划时间多久算超时

MOCK_OCR_TEXT = """××市第一医院  门诊处方
姓名：张桂英    开方日期：2026-10-08

1. 阿莫西林胶囊 0.25g  口服 一日三次 每次2粒 疗程7天
2. 硝苯地平控释片 30mg 口服 一日一次 每次1片
3. 布洛芬缓释胶囊 口服 一次1粒 遵医嘱
"""

STATUS_LABEL = {
    "pending": "待服用",
    "taken": "已服用",
    "overdue": "已超时",
    "suspended": "已带出（不计漏服）",
    "missed": "已漏服",
}

RESOLVE_ACTIONS = ("carried_out", "make_up", "missed")
PARSE_MODES = ("normal", "missing_fields", "malformed")

# ══════════════════════════════════ STATE ═══════════════════════════════════


def _fresh_state() -> dict:
    return {
        "plans": {},           # plan_id -> plan
        "doses": {},           # dose_id -> dose
        "notifications": [],   # 漏服通知（模拟推送）
        "clock_offset_minutes": 0,
        "parse_mode": "normal",
    }


STATE = _fresh_state()


def _now() -> datetime:
    """虚拟时钟。全服务所有"现在几点"都走这里，advance_time 改的是偏移量。

    这样超时判定是真的在跑，而不是被 dev 开关短路掉。
    """
    return datetime.now() + timedelta(minutes=STATE["clock_offset_minutes"])


def _reset_state() -> None:
    STATE.clear()
    STATE.update(_fresh_state())


# ══════════════════════════════════ AI ══════════════════════════════════════

# 注意：DeepSeek 的 json_object 模式要求 prompt 里必须出现 "json" 这个词，
# 否则接口直接返回 400。下面这段里有，别删。
SYSTEM_PROMPT = """你是一个处方文字的信息抽取器，不是医生。

你的唯一任务：把处方原文中【已经写明】的信息转成 json。禁止推断，禁止补全，禁止使用任何医学常识。

规则：
1. 原文没有明确写出的字段，一律填 null。填 null 是正确输出，不是失败。
2. 出现「遵医嘱」「按说明书」「适量」「必要时」「随餐」「按需」这类模糊表述时，对应的数值字段必须为 null，并把原词原样存进对应的 _raw 字段。
3. 绝对禁止输出原文中没有出现过的药名，也禁止把一种药拆成多条。
4. times_per_day 只能是 1 到 4 的整数，或者 null。原文写「一日三次」「每日1次」「bid」这类可以确定的，换算成整数；其余无法确定的写法一律 null。
5. 只输出 json 对象本身。不要 Markdown，不要代码围栏，不要任何解释文字。

json 结构：
{
  "medications": [
    {
      "name": "药名，string 或 null",
      "dose_amount": "单次剂量数值，number 或 null",
      "dose_unit": "剂量单位，string 或 null",
      "times_per_day": "每天次数，1-4 的整数或 null",
      "frequency_raw": "频次原文，string",
      "duration_days": "疗程天数，整数或 null",
      "duration_raw": "疗程原文，string"
    }
  ]
}

示例：
输入：
1. 阿司匹林肠溶片 100mg 口服 一日一次 疗程30天
2. 布洛芬缓释胶囊 口服 一次1粒 遵医嘱

输出：
{"medications": [{"name": "阿司匹林肠溶片", "dose_amount": 100, "dose_unit": "mg", "times_per_day": 1, "frequency_raw": "一日一次", "duration_days": 30, "duration_raw": "疗程30天"}, {"name": "布洛芬缓释胶囊", "dose_amount": 1, "dose_unit": "粒", "times_per_day": null, "frequency_raw": "遵医嘱", "duration_days": null, "duration_raw": ""}]}"""

# dev 模式 missing_fields 的固定返回：一条剂量缺失、一条频次模糊、
# 一条疗程缺失、一条原文里根本没有的药（用来演示防幻觉规则）。
_FAKE_AI_RESULT = {
    "medications": [
        {"name": "阿莫西林胶囊", "dose_amount": None, "dose_unit": "g",
         "times_per_day": 3, "frequency_raw": "一日三次",
         "duration_days": 7, "duration_raw": "疗程7天"},
        {"name": "硝苯地平控释片", "dose_amount": 30, "dose_unit": "mg",
         "times_per_day": 1, "frequency_raw": "一日一次",
         "duration_days": None, "duration_raw": ""},
        {"name": "布洛芬缓释胶囊", "dose_amount": 1, "dose_unit": "粒",
         "times_per_day": None, "frequency_raw": "遵医嘱",
         "duration_days": None, "duration_raw": ""},
        {"name": "维生素C片", "dose_amount": 1, "dose_unit": "片",
         "times_per_day": 2, "frequency_raw": "一日两次",
         "duration_days": 14, "duration_raw": "14天"},
    ]
}


class DeepSeekError(RuntimeError):
    """DeepSeek 调用失败。message 一定非空，可以直接拿去展示给用户。"""


def _redact(text: str) -> str:
    """万一报错信息里回显了 key，别让它进终端日志或前端。"""
    if DEEPSEEK_API_KEY and DEEPSEEK_API_KEY in text:
        return text.replace(DEEPSEEK_API_KEY, "sk-***")
    return text


async def _chat_completion(messages: List[dict]) -> str:
    """单次 DeepSeek 调用。全服务唯一的对外网络出口。

    失败一律抛 DeepSeekError，message 里带上具体原因（状态码 / 错误 body /
    异常类型），终端同时打一份完整日志。原来这里把 str(exc) 直接拼给前端，
    碰到 str() 为空的异常（部分超时、TLS 握手失败就是）只剩一个光秃秃的冒号。
    """
    payload = {
        "model": DEEPSEEK_MODEL,
        "messages": messages,
        "temperature": 0,                                   # 抽取任务不需要创造性
        "max_tokens": 2000,                                 # 防止 json 被截断
        "response_format": {"type": "json_object"},         # 必须是对象，不能是字符串
    }
    headers = {
        "Authorization": f"Bearer {DEEPSEEK_API_KEY}",
        "Content-Type": "application/json",
    }
    url = f"{DEEPSEEK_BASE_URL}/chat/completions"

    # trust_env=False：绕开系统代理设置。
    # Windows 的「设置 → 网络 → 代理」是写在注册表里的。装过 Clash / v2ray 之类
    # 又没开着时，httpx 会照着注册表把请求发给那个本地端口（例如
    # https://127.0.0.1:7897），等于对一个不讲 TLS 的本地代理做 TLS 握手，报
    # ConnectError: EOF occurred in violation of protocol——看着像 DeepSeek 挂了，
    # 其实跟 API Key 毫无关系。curl 不读注册表，所以会出现"curl 能通、Python 不通"。
    # 真要挂代理上网，把这里改回 True，或先设好 HTTPS_PROXY 再改。
    try:
        async with httpx.AsyncClient(timeout=30.0, trust_env=False) as client:
            resp = await client.post(url, headers=headers, json=payload)
    except httpx.TimeoutException as exc:
        print(f"[DeepSeek] 超时（30s）：{type(exc).__name__}: {exc!r}", flush=True)
        raise DeepSeekError(f"请求超时（30 秒）。目标 {url}") from exc
    except httpx.HTTPError as exc:
        print(f"[DeepSeek] 网络错误：{type(exc).__name__}: {exc!r}", flush=True)
        raise DeepSeekError(f"网络错误 {type(exc).__name__}：{exc}") from exc

    if resp.status_code != 200:
        body = _redact(resp.text[:500])
        hint = {401: "API Key 无效，请重新生成",
                402: "账户余额不足，请充值",
                429: "请求过于频繁，稍后再试"}.get(resp.status_code, "")
        print(f"[DeepSeek] HTTP {resp.status_code}"
              f"（{hint or '见响应体'}）；响应体：{body}", flush=True)
        raise DeepSeekError(f"HTTP {resp.status_code}"
                            + (f"（{hint}）" if hint else "") + f"：{body}")

    try:
        data = resp.json()
        return data["choices"][0]["message"]["content"] or ""
    except Exception as exc:                    # 200，但结构不是预期的那样
        body = _redact(resp.text[:500])
        print(f"[DeepSeek] HTTP 200 但结构异常："
              f"{type(exc).__name__}: {exc!r}；响应体：{body}", flush=True)
        raise DeepSeekError(f"返回结构异常 {type(exc).__name__}：{exc}") from exc


async def extract_plan_json(raw_text: str) -> dict:
    """调 AI 拿结构化用药数据。

    永远不抛异常——所有失败都转成 {ok: False, code, message}，
    由调用方塞进 issues 通道，前端统一按"高亮 + 人工补全"处理。
    """
    out: dict = {"ok": False, "data": None, "code": None, "message": "", "debug_raw": ""}

    # dev 模式是纯本地的模拟结果，不走网络，所以放在 API Key 检查之前——
    # 否则没有 key 时连异常路径都演示不了。
    if STATE["parse_mode"] == "malformed":
        out.update(code="ai_malformed", debug_raw="<dev 注入：非 json 文本>",
                   message="AI 返回的内容不是合法 json，已重试一次仍失败，请手动录入")
        return out

    if STATE["parse_mode"] == "missing_fields":
        out.update(ok=True, data=_FAKE_AI_RESULT,
                   debug_raw=json.dumps(_FAKE_AI_RESULT, ensure_ascii=False))
        return out

    if not DEEPSEEK_API_KEY:
        out.update(code="no_api_key",
                   message="后端未配置 DEEPSEEK_API_KEY，无法调用 AI，请手动录入")
        return out

    messages = [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": f"请从下面的处方文字中抽取信息，输出 json：\n\n{raw_text}"},
    ]

    last_err = ""
    for _ in range(2):  # 失败重试一次，第二次把错误信息回喂
        try:
            content = await _chat_completion(messages)
        except Exception as exc:  # DeepSeekError / 其它意外，都归到这里
            # 兜底：就算 str(exc) 是空的，也要让前端看到异常类型，不能再出现光秃秃的冒号
            reason = str(exc).strip() or f"{type(exc).__name__}（异常信息为空）"
            print(f"[DeepSeek] 调用失败：{type(exc).__name__}: {exc!r}", flush=True)
            out.update(code="ai_unreachable",
                       message=f"调用 DeepSeek 失败：{_redact(reason)}")
            return out

        out["debug_raw"] = content

        if not content.strip():
            last_err = "AI 返回内容为空"
        else:
            try:
                data = json.loads(content)
            except json.JSONDecodeError as exc:
                last_err = f"json 解析失败（{exc}）"
            else:
                items = _medications_of(data)
                if not items:
                    last_err = "json 里没有找到 medications 数组"
                else:
                    out.update(ok=True, data=data, code=None, message="")
                    return out

        messages.append({"role": "assistant", "content": content})
        messages.append({
            "role": "user",
            "content": f"上面的内容不能用（{last_err}）。请只返回一个合法的 json 对象，"
                       f"不要包含代码围栏或任何解释文字。",
        })

    out.update(code="ai_malformed", message=f"AI 返回内容无法使用：{last_err}")
    return out


def _medications_of(data: Any) -> List[dict]:
    """从 AI 返回里取出药品数组，容忍常见的包裹差异。"""
    if isinstance(data, list):
        return data
    if isinstance(data, dict):
        meds = data.get("medications")
        if isinstance(meds, list):
            return meds
        # 容错：模型偶尔会包一层 {"result": [...]}
        lists = [v for v in data.values() if isinstance(v, list)]
        if len(lists) == 1:
            return lists[0]
    return []


# ════════════════════════════════ VALIDATE ══════════════════════════════════

# 这些 code 会卡住确认按钮；其余只提示。
_BLOCKING_CODES = {"missing", "out_of_range", "empty", "ai_malformed",
                   "ai_unreachable", "no_api_key"}

_DOSE_SUFFIXES = (
    "缓释胶囊", "缓释片", "控释片", "肠溶胶囊", "肠溶片", "分散片", "咀嚼片",
    "滴眼液", "喷雾剂", "口服液", "软膏", "贴剂", "颗粒", "胶囊", "片剂", "片", "丸",
)


def _issue(item_index: Optional[int], field: str, code: str,
           message: str) -> dict:
    return {
        "item_index": item_index,
        "field": field,
        "code": code,
        "message": message,
        "blocking": code in _BLOCKING_CODES,
    }


def _squash(s: Any) -> str:
    return "".join(str(s).split())


def _name_in_text(name: str, squashed_text: str) -> bool:
    """宽松判断药名是否真的出自原文（用于防幻觉提示，允许去掉剂型后缀）。"""
    n = _squash(name)
    if not n:
        return False
    if n in squashed_text:
        return True
    for suf in _DOSE_SUFFIXES:
        if n.endswith(suf) and len(n) > len(suf) and n[: -len(suf)] in squashed_text:
            return True
    return False


def validate_items(items: List[dict], raw_text: str) -> List[dict]:
    """校验器：AI 的错、人工编辑的错，都在这里被抓住。纯函数。

    规则：
      1. 药名为空            -> 阻塞
      2. 剂量数值/单位为空   -> 阻塞
      3. 频次为空（含"遵医嘱"）-> 阻塞，不猜
      4. 频次超出 1-4        -> 阻塞
      5. 疗程为空            -> 只提示（长期药本来就没有疗程）
      6. 药名在原文中找不到  -> 只提示（疑似 AI 编造）
    """
    if not items:
        return [_issue(None, "_plan", "empty", "没有解析出任何药品，请手动录入后再确认")]

    issues: List[dict] = []
    text = _squash(raw_text)

    for i, it in enumerate(items):
        name = it.get("name")
        if not name:
            issues.append(_issue(i, "name", "missing", "未识别到药名，请填写"))
        elif not _name_in_text(name, text):
            issues.append(_issue(i, "name", "fabricated",
                                 "原文中找不到这个药名，AI 可能编造，请核对"))

        if it.get("dose_amount") is None:
            issues.append(_issue(i, "dose_amount", "missing", "未识别到单次剂量，请填写"))
        if not it.get("dose_unit"):
            issues.append(_issue(i, "dose_unit", "missing", "未识别到剂量单位，请填写"))

        tpd = it.get("times_per_day")
        if tpd is None:
            raw = (it.get("frequency_raw") or "").strip()
            msg = (f"原文是「{raw}」，无法确定每天吃几次，请手动选择" if raw
                   else "未识别到用药频次，请手动选择")
            issues.append(_issue(i, "times_per_day", "missing", msg))
        elif not (1 <= tpd <= MAX_TIMES_PER_DAY):
            issues.append(_issue(i, "times_per_day", "out_of_range",
                                 f"每天次数必须在 1-{MAX_TIMES_PER_DAY} 之间，请修正"))

        if it.get("duration_days") is None:
            issues.append(_issue(i, "duration_days", "soft_missing",
                                 "未识别到疗程，请确认是否为长期服用"))

    return issues


def has_blocking(issues: List[dict]) -> bool:
    return any(x["blocking"] for x in issues)


# ═════════════════════════════════ DOMAIN ═══════════════════════════════════


def _as_str(v: Any) -> Optional[str]:
    if v is None:
        return None
    s = str(v).strip()
    return s or None


def _as_float(v: Any) -> Optional[float]:
    """只放行有限数。nan / inf 一律降级为 None。

    原因：starlette 的 JSONResponse 用 json.dumps(allow_nan=False) 序列化，
    放进 nan 或 inf 会抛 ValueError 直接变成 500。降级成 None 之后，
    校验器会把它当成"剂量缺失"报 blocking，走人工补全——这才是设计要的行为。
    超大整数（如 400 位）调 float() 会抛 OverflowError，同样要接住。
    """
    if v is None or isinstance(v, bool):
        return None
    try:
        f = float(v) if isinstance(v, (int, float)) else float(str(v).strip())
    except (TypeError, ValueError, OverflowError):
        return None
    return f if math.isfinite(f) else None


def _as_int(v: Any) -> Optional[int]:
    if v is None or isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v
    if isinstance(v, float):
        return int(v) if v.is_integer() else None
    try:
        return int(str(v).strip())
    except (TypeError, ValueError):
        return None


def normalize_item(raw: Any) -> dict:
    """把任何来源（AI 输出 / 子女编辑）的一条药归一成标准形状，坏值一律降级为 None。

    降级而不是报错：坏值随后会被校验器抓成 issue，前端统一高亮。
    """
    if not isinstance(raw, dict):
        raw = {}
    return {
        "name": _as_str(raw.get("name")),
        "dose_amount": _as_float(raw.get("dose_amount")),
        "dose_unit": _as_str(raw.get("dose_unit")),
        "times_per_day": _as_int(raw.get("times_per_day")),
        "frequency_raw": _as_str(raw.get("frequency_raw")) or "",
        "duration_days": _as_int(raw.get("duration_days")),
        "duration_raw": _as_str(raw.get("duration_raw")) or "",
    }


def dose_text(item: dict) -> str:
    """给老人看的剂量文本，例如 "0.25g"。"""
    unit = item.get("dose_unit") or ""
    amount = item.get("dose_amount")
    if amount is None:
        return unit
    return f"{amount:g}{unit}"


def current_slot(now: datetime) -> str:
    """now 落在哪个时段窗口内。窗口 = 本时段起点 到 下一时段起点。"""
    cur = SLOT_ORDER[0]
    for key in SLOT_ORDER:
        if now.time() >= SLOT_AT[key]:
            cur = key
        else:
            break
    return cur


def compute_status(dose: dict) -> str:
    """真实状态 = 存储状态 + 超时推导。overdue 从不落库，只在这里产生。"""
    if dose["status"] != "pending":
        return dose["status"]
    scheduled = datetime.fromisoformat(dose["scheduled_at"])
    if _now() > scheduled + timedelta(minutes=GRACE_MINUTES):
        return "overdue"
    return "pending"


def ensure_doses(day: date) -> None:
    """幂等地为所有 active plan 生成 day 当天的 Dose。

    剂量 id 由 (plan, 条目, 日期, 时段) 决定，所以重复调用不会重复生成。
    """
    for plan in STATE["plans"].values():
        if plan["status"] != "active":
            continue
        start = plan["activated_at"].date()
        day_index = (day - start).days
        if day_index < 0:
            continue

        for idx, item in enumerate(plan["items"]):
            duration = item.get("duration_days")
            if duration is not None and day_index >= duration:
                continue  # 疗程已结束
            for slot in FREQ_TO_SLOTS.get(item["times_per_day"], []):
                dose_id = f"{plan['id']}_{idx}_{day:%Y%m%d}_{slot}"
                if dose_id in STATE["doses"]:
                    continue
                scheduled = datetime.combine(day, SLOT_AT[slot])
                STATE["doses"][dose_id] = {
                    "id": dose_id,
                    "plan_id": plan["id"],
                    "item_index": idx,
                    "item": item,
                    "date": day.isoformat(),
                    "slot": slot,
                    "scheduled_at": scheduled.isoformat(timespec="minutes"),
                    "status": "pending",  # pending | taken | suspended | missed
                    "taken_at": None,
                    "resolved_action": None,
                    "resolved_at": None,
                }


def dose_view(dose: dict) -> dict:
    status = compute_status(dose)
    item = dose["item"]
    return {
        "id": dose["id"],
        "name": item["name"],
        "dose_text": dose_text(item),
        "slot": dose["slot"],
        "slot_label": SLOT_LABEL[dose["slot"]],
        "scheduled_at": dose["scheduled_at"],
        "status": status,
        "status_label": STATUS_LABEL[status],
        "taken_at": dose["taken_at"],
        "resolved_action": dose["resolved_action"],
        "frequency_raw": item.get("frequency_raw") or "",
    }


def plan_view(plan: dict) -> dict:
    return {
        "plan_id": plan["id"],
        "status": plan["status"],
        "raw_text": plan["raw_text"],
        "items": plan["items"],
        "issues": plan["issues"],
        "blocking": has_blocking(plan["issues"]),
        "ai_ok": plan.get("ai_ok"),
        "ai_debug_raw": plan.get("ai_debug_raw") or "",
        "created_at": plan["created_at"],
        "activated_at": plan["activated_at"],
    }


# ═════════════════════════════════ ROUTES ═══════════════════════════════════

app = FastAPI(title="老人智能药盒 Demo")


# ── 请求体 ──────────────────────────────────────────────────────────────────


class ParseReq(BaseModel):
    raw_text: Optional[str] = None


class ConfirmReq(BaseModel):
    items: List[Any] = []


class ResolveReq(BaseModel):
    action: str


class AdvanceReq(BaseModel):
    minutes: int


class ParseModeReq(BaseModel):
    mode: str


# ── 页面 ────────────────────────────────────────────────────────────────────


@app.get("/")
async def index():
    path = os.path.join(STATIC_DIR, "index.html")
    if not os.path.exists(path):
        return JSONResponse(
            {"detail": "static/index.html 还没写。后端接口已可用（见 /docs）。"},
            status_code=404,
        )
    return FileResponse(path)


@app.get("/api/health")
async def health():
    return {
        "ok": True,
        "model": DEEPSEEK_MODEL,
        "base_url": DEEPSEEK_BASE_URL,
        "api_key_configured": bool(DEEPSEEK_API_KEY),
        "parse_mode": STATE["parse_mode"],
        "now": _now().isoformat(timespec="seconds"),
        "clock_offset_minutes": STATE["clock_offset_minutes"],
    }


# ── 主链路：拍照 -> 解析 -> 子女确认 ────────────────────────────────────────


@app.post("/api/ocr/scan")
async def ocr_scan():
    """Mock 拍照识别：返回固定药盒文字。"""
    return {
        "source": "mock",
        "raw_text": MOCK_OCR_TEXT,
        "scanned_at": _now().isoformat(timespec="seconds"),
    }


@app.post("/api/parse")
async def parse(req: ParseReq):
    """调 AI 把药盒文字抽成结构化数据，落成 draft plan。不生成生效计划。"""
    raw_text = (req.raw_text or "").strip() or MOCK_OCR_TEXT
    ai = await extract_plan_json(raw_text)

    items: List[dict] = []
    issues: List[dict] = []

    if ai["ok"]:
        items = [normalize_item(x) for x in _medications_of(ai["data"])]
    else:
        issues.append(_issue(None, "_plan", ai["code"], ai["message"]))

    issues.extend(validate_items(items, raw_text))

    plan_id = uuid.uuid4().hex[:12]
    plan = {
        "id": plan_id,
        "status": "draft",
        "raw_text": raw_text,
        "items": items,
        "issues": issues,
        "ai_ok": ai["ok"],
        "ai_debug_raw": (ai.get("debug_raw") or "")[:2000],
        "created_at": _now().isoformat(timespec="seconds"),
        "activated_at": None,
    }
    STATE["plans"][plan_id] = plan
    return plan_view(plan)


@app.get("/api/plans")
async def list_plans():
    return {"plans": [plan_view(p) for p in STATE["plans"].values()]}


@app.get("/api/plans/{plan_id}")
async def get_plan(plan_id: str):
    plan = STATE["plans"].get(plan_id)
    if not plan:
        raise HTTPException(404, f"plan {plan_id} 不存在")
    return plan_view(plan)


def _norm_name(name: Optional[str]) -> str:
    """判重用的药名归一化：去首尾空白 + 全角转半角 + 统一小写。

    到此为止——「布洛芬」和「布洛芬缓释胶囊」算不算同一种药交给用户判断，
    代码不猜。
    """
    s = (name or "").strip()
    s = "".join(
        chr(ord(c) - 0xFEE0) if 0xFF01 <= ord(c) <= 0xFF5E    # 全角 ！-～ 转半角
        else (" " if c == "　" else c)                     # 全角空格转普通空格
        for c in s
    )
    return s.strip().lower()


def _dup_key(item: dict) -> Optional[tuple]:
    """语义判重键 = 药名（归一化） + 剂量 + 频次。

    没有药名的行不参与判重——那种行会被校验器报成「未识别到药名」，
    在这里再报一次「重复药品：」只会让人看不懂。
    """
    name = _norm_name(item.get("name"))
    if not name:
        return None
    return (name, item.get("dose_amount"), item.get("dose_unit"), item.get("times_per_day"))


def reject_duplicate_items(items: List[dict], plan_id: str) -> None:
    """确认前的语义去重，两道关：本次提交内部、与已有生效计划之间。

    为什么放在 confirm 而不是 ensure_doses 里：ensure_doses 是按
    (plan, 条目下标) 幂等的，它挡不住「两个 plan 各有一条同名药」，
    也不该由它来挡——重复药品是数据准入问题，不是展开问题。
    """
    seen: dict = {}
    for it in items:
        key = _dup_key(it)
        if key is None:
            continue
        if key in seen:
            raise HTTPException(
                409, f"用药计划中存在重复药品：{it['name']}，请删除重复条目后重试")
        seen[key] = it

    for other in STATE["plans"].values():
        if other["status"] != "active" or other["id"] == plan_id:
            continue
        for it in other.get("items") or []:
            key = _dup_key(it)
            if key is not None and key in seen:
                raise HTTPException(
                    409, f"{seen[key]['name']} 已在当前用药计划中，请勿重复添加")


@app.post("/api/plans/{plan_id}/confirm")
async def confirm_plan(plan_id: str, req: ConfirmReq):
    """子女确认：第二次校验（因为是人工编辑过的），通过才转 active 并展开今日 Dose。"""
    plan = STATE["plans"].get(plan_id)
    if not plan:
        raise HTTPException(404, f"plan {plan_id} 不存在")
    if plan["status"] == "active":
        raise HTTPException(409, "该计划已确认生效，不能重复确认")

    items = [normalize_item(x) for x in req.items]
    reject_duplicate_items(items, plan_id)
    issues = validate_items(items, plan["raw_text"])

    plan["items"] = items
    plan["issues"] = issues

    if has_blocking(issues):
        return JSONResponse(
            {
                "detail": "仍有必填项未补全，未生成用药计划",
                "plan_id": plan_id,
                "status": "draft",
                "blocking": True,
                "items": items,
                "issues": issues,
            },
            status_code=400,
        )

    plan["status"] = "active"
    plan["activated_at"] = _now()
    ensure_doses(_now().date())
    return plan_view(plan)


# ── 老人端：今日药历 + 打卡 + 超时处理 ──────────────────────────────────────


@app.get("/api/doses/today")
async def doses_today():
    """今日药历，后端已经分好组、标好当前时段——前端不做任何时间判断。"""
    now = _now()
    day = now.date()
    ensure_doses(day)
    cur = current_slot(now)

    groups = []
    for key, label, at in SLOTS:
        doses = [
            dose_view(d)
            for d in STATE["doses"].values()
            if d["date"] == day.isoformat() and d["slot"] == key
        ]
        doses.sort(key=lambda d: d["name"] or "")
        groups.append({
            "slot": key,
            "label": label,
            "at": at,
            "is_current": key == cur,
            "doses": doses,
        })

    overdue = [d for g in groups for d in g["doses"] if d["status"] == "overdue"]

    return {
        "now": now.isoformat(timespec="seconds"),
        "today": day.isoformat(),
        "current_slot": cur,
        "current_slot_label": SLOT_LABEL[cur],
        "grace_minutes": GRACE_MINUTES,
        "slots": groups,
        "overdue_doses": overdue,  # 需要老人做状态选择的就是这些
    }


@app.post("/api/doses/{dose_id}/take")
async def take_dose(dose_id: str):
    """打卡"已服用"。超时后直接打卡等同于补服。"""
    dose = STATE["doses"].get(dose_id)
    if not dose:
        raise HTTPException(404, f"dose {dose_id} 不存在")

    status = compute_status(dose)
    if status != "pending" and status != "overdue":
        raise HTTPException(409, f"这次服药已经处理过了：{STATUS_LABEL[status]}")

    stamp = _now().isoformat(timespec="minutes")
    dose["status"] = "taken"
    dose["taken_at"] = stamp
    dose["resolved_at"] = stamp
    dose["resolved_action"] = "on_time" if status == "pending" else "make_up"
    return dose_view(dose)


@app.post("/api/doses/{dose_id}/resolve")
async def resolve_dose(dose_id: str, req: ResolveReq):
    """超时未服的三选一出口。

    carried_out -> 挂起，不计漏服、不通知子女
    make_up     -> 正常流转为已服用
    missed      -> 标记漏服 + 模拟通知子女
    """
    dose = STATE["doses"].get(dose_id)
    if not dose:
        raise HTTPException(404, f"dose {dose_id} 不存在")

    status = compute_status(dose)
    if status != "overdue":
        raise HTTPException(
            409,
            f"这次服药当前状态是「{STATUS_LABEL[status]}」，"
            f"只有超时未服的药才需要选择处理方式",
        )

    if req.action not in RESOLVE_ACTIONS:
        raise HTTPException(400, f"action 必须是 {list(RESOLVE_ACTIONS)} 之一")

    stamp = _now().isoformat(timespec="minutes")
    dose["resolved_action"] = req.action
    dose["resolved_at"] = stamp

    if req.action == "carried_out":
        dose["status"] = "suspended"
    elif req.action == "make_up":
        dose["status"] = "taken"
        dose["taken_at"] = stamp
    else:
        dose["status"] = "missed"
        STATE["notifications"].append({
            "id": uuid.uuid4().hex[:8],
            "dose_id": dose["id"],
            "level": "warning",
            "text": (f"{PATIENT_NAME} {dose['date']} {SLOT_LABEL[dose['slot']]}"
                     f" 的「{dose['item']['name']}」没有服用"),
            "created_at": stamp,
        })

    return dose_view(dose)


# ── 子女端：通知 ────────────────────────────────────────────────────────────


@app.get("/api/notifications")
async def list_notifications():
    return {
        "count": len(STATE["notifications"]),
        "items": list(reversed(STATE["notifications"])),
    }


# ── 演示辅助 ────────────────────────────────────────────────────────────────


@app.post("/api/dev/advance_time")
async def advance_time(req: AdvanceReq):
    """把虚拟时钟拨快/拨慢，用来演示超时。"""
    STATE["clock_offset_minutes"] += req.minutes
    return {
        "clock_offset_minutes": STATE["clock_offset_minutes"],
        "now": _now().isoformat(timespec="seconds"),
    }


@app.post("/api/dev/parse_mode")
async def set_parse_mode(req: ParseModeReq):
    """注入 AI 异常，用来演示异常路径（正常调 API 很难稳定复现）。"""
    if req.mode not in PARSE_MODES:
        raise HTTPException(400, f"mode 必须是 {list(PARSE_MODES)} 之一")
    STATE["parse_mode"] = req.mode
    return {"parse_mode": STATE["parse_mode"]}


@app.post("/api/dev/reset")
async def reset():
    _reset_state()
    return {"ok": True, "parse_mode": STATE["parse_mode"]}
