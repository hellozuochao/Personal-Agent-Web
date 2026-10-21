#!/usr/bin/env python3
"""云端记忆（Letta 式三层，影子模式）：
- 观察层 observations.jsonl：append-only，每轮 tick 一条摘要，是事实来源；
- 记忆块 memory_blocks.json：user_profile / active_threads / lessons /
  agent_self 四块，有字符上限、版本号、修改历史，是"当前真相"，
  会注入 decide() 的决策上下文；
- 离线蒸馏：每积累 >=12 条未蒸馏观察、且距上次尝试 >=12 轮，
  调模型产出 {编辑指令, 反思}。影子模式：编辑不直接落库，
  而是变成一条带 exec 载荷（memory_apply）的审批——用户批准后
  由执行器应用（agent.tick 里编排，本模块只提供原语）。

蒸馏时把已消费的观察从文件里剪掉并归零水位，文件不会无限长。
"""
import datetime
import json
import os
import urllib.request

BASE = os.path.dirname(os.path.abspath(__file__))
OBS_FILE = os.path.join(BASE, "observations.jsonl")
BLOCKS_FILE = os.path.join(BASE, "memory_blocks.json")
REFLECTIONS_FILE = os.path.join(BASE, "reflections.jsonl")
MSTATE_FILE = os.path.join(BASE, "memory_state.json")

OPENROUTER_URL = "https://openrouter.ai/api/v1/chat/completions"
DISTILL_EVERY_OBS = 12     # 未蒸馏观察达到这个数才尝试蒸馏
DISTILL_MIN_GAP = 12       # 两次蒸馏尝试至少间隔这么多轮（失败也不狂重试）

BLOCK_SEED = {
    "user_profile": {
        "description": "用户画像与明确表达过的偏好（用户亲口说的优先级最高）",
        "limit": 600,
        "value": "用户在开发一个常开个人智能体（就是本 agent），偏好简短直接的沟通。",
    },
    "active_threads": {
        "description": "正在跟进、还没收尾的事项（邮件、项目线索）",
        "limit": 800,
        "value": "（暂无）",
    },
    "lessons": {
        "description": "从运行事故和纠正里学到的教训，要能改变后续行为",
        "limit": 800,
        "value": "工具的数据源不在本机时不要注册它——曾因水位工具在云上读不到文件，"
                 "一夜提出上百条重复审批。提审批前先查重，播报要去重。",
    },
    "agent_self": {
        "description": "我是谁、我的环境与硬约束（哪些工具在哪可用、哪些事绝不做）",
        "limit": 600,
        "value": "我是常开个人智能体，运行在广州腾讯云服务器，每 5 分钟一轮 tick；"
                 "高风险动作必须先审批；已接入 163 邮箱的感知与执行"
                 "（标记已读/加星标）；Google 服务因机房网络不通未接入。",
    },
}


# ---------- 观察层 ----------
def obs_append(record):
    with open(OBS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def obs_all():
    if not os.path.exists(OBS_FILE):
        return []
    with open(OBS_FILE, encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def obs_trim(consumed):
    """蒸馏消费掉前 consumed 条后，把它们从文件剪掉（水位随之归零）。"""
    rest = obs_all()[consumed:]
    tmp = OBS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        for rec in rest:
            f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    os.replace(tmp, OBS_FILE)


# ---------- 记忆块 ----------
def blocks_load():
    if not os.path.exists(BLOCKS_FILE):
        blocks = {label: {"description": b["description"], "limit": b["limit"],
                          "value": b["value"], "version": 1, "history": []}
                  for label, b in BLOCK_SEED.items()}
        blocks_save(blocks)
        return blocks
    with open(BLOCKS_FILE, encoding="utf-8") as f:
        return json.load(f)


def blocks_save(blocks):
    tmp = BLOCKS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(blocks, f, ensure_ascii=False, indent=2)
    os.replace(tmp, BLOCKS_FILE)


def apply_edits(edits, ts):
    """应用一批编辑指令（执行器调用）。逐条校验，返回 (applied, rejected)。"""
    blocks = blocks_load()
    applied, rejected = [], []
    for edit in edits or []:
        label, op = edit.get("label"), edit.get("op")
        text = (edit.get("text") or "").strip()
        if label not in blocks:
            rejected.append(f"块不存在: {label}")
            continue
        block = blocks[label]
        if op == "replace":
            new_value = text
        elif op == "append":
            new_value = (block["value"] + "\n" + text) if block["value"] else text
        else:
            rejected.append(f"未知操作: {op}")
            continue
        if len(new_value) > block["limit"]:
            rejected.append(f"{label} 超限（{len(new_value)}>{block['limit']}）")
            continue
        block["value"] = new_value
        block["version"] += 1
        block["history"].append({"ts": ts, "op": op,
                                 "note": edit.get("note", "")})
        applied.append(f"{label}（v{block['version']}）")
    blocks_save(blocks)
    return applied, rejected


def summary_for_decide():
    """给 decide() 的记忆摘要：四块的当前内容。"""
    try:
        return {label: b["value"] for label, b in blocks_load().items()}
    except Exception:
        return {}


# ---------- 蒸馏状态 ----------
def _mstate_load():
    if os.path.exists(MSTATE_FILE):
        with open(MSTATE_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {"last_attempt_tick": -10 ** 9}


def _mstate_save(st):
    with open(MSTATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False)


# ---------- 蒸馏（只产出提案，不落库） ----------
DISTILL_SYSTEM = """你是常开个人智能体的记忆蒸馏器。输入：当前的记忆块，
和上次蒸馏以来新增的运行观察记录（每条是一轮 tick 的摘要）。
你的任务是把流水账提炼成长期记忆，只输出 JSON，不要解释。

输出格式：
{"edits": [{"label": "块名", "op": "replace|append", "text": "新内容", "note": "为什么改"}],
 "reflection": "一段反思：这段时间最重要的模式、变化和风险，3-5 句"}

提炼规则：
- 只记跨轮次仍然有用的信息：用户明确的偏好、反复出现的故障模式、
  未收尾的跟进事项、关于环境/工具的硬事实。一次性的琐事不要记。
- 同一故障反复出现，要提炼成一条可执行的教训（下次该怎么做），不要逐条罗列。
- 每个块有字符上限（limit），append 要克制；信息过时就 replace 掉旧的。
- 没有值得记的就不编辑（edits 为空），reflection 照写。"""


def _chat(payload):
    key = os.environ.get("OPENROUTER_API_KEY", "")
    if not key:
        raise RuntimeError("没有 OPENROUTER_API_KEY")
    body = json.dumps({
        "model": os.environ.get("OPENROUTER_MODEL", "openrouter/auto"),
        "messages": [
            {"role": "system", "content": DISTILL_SYSTEM},
            {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
        ],
        "temperature": 0.2,
        "max_tokens": 900,
    }).encode("utf-8")
    req = urllib.request.Request(
        OPENROUTER_URL, data=body,
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        data = json.load(resp)
    return data["choices"][0]["message"]["content"]


def _parse_json_loose(text):
    t = text.strip()
    if t.startswith("```"):
        t = t.strip("`")
        if t.lower().startswith("json"):
            t = t[4:]
    return json.loads(t[t.index("{"): t.rindex("}") + 1])


def distill_propose(tick_count):
    """该蒸馏时产出提案。返回 (proposal, consumed) 或 None。
    proposal = {"edits": [...], "reflection": str}。
    满足三个条件才尝试：未蒸馏观察 >= 12；距上次尝试 >= 12 轮；
    调用方确认队列里没有待批的 memory_apply（防堆积，由 agent 层检查后调用）。
    尝试失败（模型/解析）也记下时间，按间隔重试，不狂刷。"""
    st = _mstate_load()
    obs = obs_all()
    if len(obs) < DISTILL_EVERY_OBS:
        return None
    if tick_count - st.get("last_attempt_tick", -10 ** 9) < DISTILL_MIN_GAP:
        return None
    st["last_attempt_tick"] = tick_count
    _mstate_save(st)
    blocks = blocks_load()
    payload = {
        "blocks": {label: {"description": b["description"],
                           "limit": b["limit"], "value": b["value"]}
                   for label, b in blocks.items()},
        "new_observations": obs,
    }
    last_err = None
    for attempt in (1, 2):
        text = _chat(payload)
        try:
            result = _parse_json_loose(text)
            break
        except Exception as e:
            last_err = e
            payload = dict(payload)
            payload["correction"] = ("上一次输出不是合法 JSON，"
                                     "这次只输出合法的 JSON 对象本身。")
    else:
        raise RuntimeError(f"蒸馏输出两次都不是合法 JSON: {last_err}")
    proposal = {"edits": result.get("edits", []),
                "reflection": result.get("reflection", "")}
    # 归档这次提案（无论后续批准与否都留痕），并剪掉已消费的观察
    with open(REFLECTIONS_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps({
            "ts": datetime.datetime.now().isoformat(timespec="seconds"),
            "covers_observations": len(obs),
            "reflection": proposal["reflection"],
            "edits_proposed": proposal["edits"],
        }, ensure_ascii=False) + "\n")
    obs_trim(len(obs))
    return proposal, len(obs)
