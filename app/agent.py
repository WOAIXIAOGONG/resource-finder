# -*- coding: utf-8 -*-
"""Agent 相关：检测本机 Agent / 本地大模型，提供一个本地收件口。

思路：
- 工具本身不替 Agent 思考，只负责「接住结果 + 摆出来」；
- 本机如果有 OpenAI 兼容的本地服务（Ollama / LM Studio / llama.cpp），
  可以用它把「大白话」拆成搜索关键词；
- 没有本地模型也不要紧：Agent 可以用 HTTP 把搜到的资源推进来。
"""
from __future__ import annotations

import json
import copy
import os
import queue
import re
import secrets
import socket
import subprocess
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Dict, List, Optional
from urllib.parse import urlparse, parse_qs

import requests

from .config import APP_NAME, APP_VERSION
from .agent_contract import AGENT_RULES, task_contract
from .models import SearchResult, parse_count, parse_pub_ts, normalize_url

# 常见的「本机 Agent / AI 客户端」进程名（小写，去掉 .exe）
_AGENT_EXES = {
    "claude": "Claude",
    "claude code": "Claude Code",
    "codex": "Codex CLI",
    "cursor": "Cursor",
    "windsurf": "Windsurf",
    "dsh": "DSH",
    "deepseek": "DeepSeek",
    "chatgpt": "ChatGPT",
    "ollama": "Ollama",
    "lm studio": "LM Studio",
    "lmstudio": "LM Studio",
    "cherry": "Cherry Studio",
    "cherrystudio": "Cherry Studio",
    "chatbox": "Chatbox",
    "comfyui": "ComfyUI",
    "workbuddy": "WorkBuddy",
    "trae": "Trae",
    "code": "VS Code",
}

# OpenAI 兼容本地服务的候选端口
_LLM_PORTS = [
    (11434, "Ollama"),
    (1234, "LM Studio"),
    (8080, "llama.cpp"),
    (8000, "本地 AI 服务"),
    (5000, "本地 AI 服务"),
]


def detect_agents() -> List[Dict[str, str]]:
    """列出本机正在运行的、看起来像 Agent / AI 客户端的进程。"""
    found: List[Dict[str, str]] = []
    try:
        out = subprocess.run(["tasklist", "/fo", "csv", "/nh"],
                             capture_output=True, text=True, timeout=6,
                             encoding="mbcs", errors="ignore").stdout
    except Exception:                     # noqa: BLE001
        return found
    seen = set()
    for line in out.splitlines():
        parts = [p.strip('"') for p in line.split('","')]
        if len(parts) < 2:
            continue
        exe = parts[0].strip('"')
        pid = parts[1].strip('"')
        stem = exe.lower()
        if stem.endswith(".exe"):
            stem = stem[:-4]
        for key, label in _AGENT_EXES.items():
            if key in stem:
                if label in seen:
                    break
                seen.add(label)
                found.append({"name": label, "exe": exe, "pid": pid})
                break
    return found


def detect_llm(timeout: float = 0.8) -> Optional[Dict[str, object]]:
    """找本机可以用的 OpenAI 兼容服务（Ollama / LM Studio …）。"""
    for port, name in _LLM_PORTS:
        base = "http://127.0.0.1:%d" % port
        try:
            r = requests.get(base + "/v1/models", timeout=timeout)
            if r.status_code != 200:
                continue
            data = r.json()
            models = [m.get("id") for m in (data.get("data") or []) if m.get("id")]
            if not models:
                continue
            return {"name": name, "base": base, "models": models}
        except Exception:                 # noqa: BLE001
            continue
    return None


def llm_chat(base: str, model: str, prompt: str, timeout: float = 40.0) -> str:
    """调一次 OpenAI 兼容的 /v1/chat/completions，返回文本。"""
    r = requests.post(base.rstrip("/") + "/v1/chat/completions", timeout=timeout,
                      json={"model": model, "temperature": 0.2,
                            "messages": [{"role": "user", "content": prompt}]})
    r.raise_for_status()
    data = r.json()
    return ((data.get("choices") or [{}])[0].get("message") or {}).get("content") or ""


_INTENT_PROMPT = """你是「资源搜寻器」的搜索助手。用户会用大白话描述想找的资源。
请把它拆成可以直接拿去网站搜索的关键词，并给出筛选条件。
只输出 JSON，不要解释，格式：
{{"keywords": ["关键词1", "关键词2"], "time": "any|24h|7d|30d|1y", "min_views": 0, "why": "一句话说明"}}
规则：
- keywords 1~3 个，尽量短，保留主题、作品名和资源类型；不要带「帮我找」这类废话；
- 用户没提时间就用 any，没提播放量就填 0；
- 不要输出除 JSON 以外的任何字符。

用户说：{text}"""


def _json_block(text: str) -> str:
    m = re.search(r"\{.*\}", text, re.S)
    return m.group(0) if m else text


# ---- 没有本地模型时的规则回退：从大白话里抠关键词 ----
_FILLER = re.compile(r"(帮我|帮忙|麻烦|请|我想要|我想|我要|想找|找一下|搜一下|搜索|查找|"
                     r"找|搜|看看|瞧瞧|一些|几个|的视频|视频|资源|相关|那种|有没有|最好|"
                     r"尽量|大概|左右)")
_TIME_WORDS = re.compile(r"(今天|最近24小时|24小时内|最近一周|近一周|这一周|本周|这周|"
                         r"一周内|最近一个月|近一个月|本月|这个月|一个月内|最近一年|今年|去年)")
_QUALITY_WORDS = re.compile(r"(播放量高|播放量|播放多|播放|热门|高一点|高一些|高清|超清|"
                            r"4k|8k|1080p|60帧)", re.I)
_STOP_CHUNKS = {"一点", "一些", "那个", "这个", "那种", "这种", "东西", "内容", "一下"}


def parse_intent(text: str, llm: Optional[Dict[str, object]] = None) -> Dict[str, object]:
    """把大白话解析成「关键词 + 筛选条件」。

    有本地模型就交给模型；没有或失败时退回规则解析，保证功能可用。
    """
    text = (text or "").strip()
    result = {"keywords": [], "time": "any", "min_views": 0, "why": "本地规则解析"}
    if llm:
        try:
            raw = llm_chat(str(llm["base"]), str((llm.get("models") or [""])[0]),
                           _INTENT_PROMPT.format(text=text))
            data = json.loads(_json_block(raw))
            kws = [str(k).strip() for k in (data.get("keywords") or []) if str(k).strip()]
            if kws:
                result.update(keywords=kws[:3],
                              time=str(data.get("time") or "any"),
                              min_views=int(data.get("min_views") or 0),
                              why=str(data.get("why") or "AI 解析"))
                return result
        except Exception as exc:           # noqa: BLE001
            result["why"] = "AI 解析失败（%s），改用本地规则" % str(exc)[:40]

    cleaned = _TIME_WORDS.sub(" ", text)
    cleaned = _QUALITY_WORDS.sub(" ", cleaned)
    cleaned = _FILLER.sub(" ", cleaned)
    cleaned = re.sub(r"[的了呢啊吧呀嘛个点些]", " ", cleaned)
    chunks = [c.strip() for c in re.split(r"[\s,，。、;；!?！？]+", cleaned) if c.strip()]
    chunks = [c for c in chunks if c not in _STOP_CHUNKS and len(c) >= 2]
    if chunks:
        chunks.sort(key=len, reverse=True)
        result["keywords"] = chunks[:2]
    else:
        result["keywords"] = [text] if text else []
    if re.search(r"(今天|24小时|最近24)", text):
        result["time"] = "24h"
    elif re.search(r"(最近一周|近一周|这一周|本周|这周|一周内|7天|七天)", text):
        result["time"] = "7d"
    elif re.search(r"(最近一个月|近一个月|本月|这个月|一个月内|30天)", text):
        result["time"] = "30d"
    elif re.search(r"(最近一年|今年|本年度)", text):
        result["time"] = "1y"
    m = re.search(r"(\d+)\s*万", text)
    if m:
        result["min_views"] = int(m.group(1)) * 10000
    return result


def _to_result(raw: Dict[str, object]) -> Optional[SearchResult]:
    from .covers import cover_url
    url = str(raw.get("url") or "").strip()
    title = str(raw.get("title") or "").strip()
    from .models import is_web_url
    if not is_web_url(url) or not title:
        return None
    kind = str(raw.get('resource_type') or 'video')
    if kind == 'novel':
        kind = 'article'
    if kind not in ('video', 'image', 'article', 'sound'):
        return None
    image_page = isinstance(raw.get('extra'), dict) and raw['extra'].get('image_page') is True
    views = raw.get("views")
    pub = raw.get("pub_ts") or raw.get("pubdate") or raw.get("published")
    return SearchResult(
        title=title,
        resource_type=kind,
        url=url,
        source=str(raw.get("source") or "Agent"),
        source_id=str(raw.get("source_id") or "agent"),
        cover=next((u for u in (cover_url(raw.get(k)) for k in
                   ('cover', 'pic', 'thumbnail', 'thumbnail_url')) if u), url if kind == 'image' and not image_page else ''),
        author=str(raw.get("author") or ""),
        duration=str(raw.get("duration") or ""),
        play=str(raw.get("play") or "") or (("%d" % parse_count(views)) if views else ""),
        desc=str(raw.get("desc") or "")[:300],
        views=parse_count(views),
        pub_ts=parse_pub_ts(pub),
        extra={"from": "agent", 'image_page': image_page, 'page_url': str(raw.get('page_url') or
            (raw['extra'].get('page_url') if isinstance(raw.get('extra'), dict) else '') or '')},
    )


class _LocalHTTPServer(ThreadingHTTPServer):
    """独占端口的本地 HTTP 服务（见 start() 里关于 SO_REUSEADDR 的说明）。"""

    allow_reuse_address = False
    daemon_threads = True


class AgentServer:
    """本地小 HTTP 服务：Agent 把结果 POST 进来，界面立刻摆出来。

    只监听 127.0.0.1，带随机 token，外部网站访问不到。
    """

    def __init__(self, on_results: Callable[[List[SearchResult], str], None],
                 on_clear: Optional[Callable[[Dict[str, object]], None]] = None,
                 on_status: Optional[Callable[[Dict[str, object]], None]] = None,
                 on_error: Optional[Callable[[Dict[str, object]], None]] = None,
                 port: int = 8765, config=None):
        self._on_results = on_results
        self._on_clear = on_clear
        self._on_error = on_error
        self.on_status = on_status
        self._port = port
        self._httpd: Optional[ThreadingHTTPServer] = None
        self._thread: Optional[threading.Thread] = None
        self.token = secrets.token_hex(16)
        self._revoked_tokens = set()
        self._inflight_network = {}
        self._metrics = {}
        from .agent_liveness import RunnerTracker
        self.runners = RunnerTracker()
        self._task_urls = set()
        self._task_received = 0
        self._task_result_rows = []
        self.disconnected = False
        # 任务队列：界面点「搜索」后把任务放进来，Agent 用长轮询 GET /api/task 取走。
        # 只留最新一条——用户连点搜索时旧任务没人要，排队反而会让 Agent 干过时的活。
        self._tasks: "queue.Queue[Dict[str, object]]" = queue.Queue(maxsize=8)
        self._task_lock = threading.RLock()
        self._control_condition = threading.Condition(self._task_lock)
        self._task_cancel = threading.Event()
        self._task_seq = 0
        self._task_prefix = secrets.token_hex(4)
        self._current_task: Optional[Dict[str, object]] = None
        self._task_state = "idle"
        self._delivery_at = 0.0
        self._delivery_attempt = 0
        self._activity_at = 0.0
        self._receipt_timeout = 15.0
        self._get_times: Dict[str, float] = {}
        self._waiting_polls: Dict[str, int] = {}
        self._results_count = 0
        # 谁在接管：第一个来取任务的 Agent 成为主人；之后 new 一个名字的 Agent
        # 会顶掉它（旧的在下次请求时收到 stop）。完全由用户粘贴指令触发，
        # 工具不去扫描本机进程。
        self._owner = ""
        self._seen_agents: set = set()
        self._poll_times: Dict[str, float] = {}
        self._epoch = 0
        self._paused = False
        self._resume_pending = False
        from .agent_access import AgentAccess
        self.access = AgentAccess(self, config) if config is not None else None

    # -- 任务队列 ------------------------------------------------------- #
    def push_task(self, query: str,
                  filters: Optional[Dict[str, object]] = None,
                  kind: str = "search", want: int = 100,
                  exclude: Optional[List[str]] = None) -> Dict[str, object]:
        """把一次搜索请求排进队列，等 Agent 取走。返回任务描述。

        kind="search" 是用户点了搜索；kind="more" 是补足当前数量目标或用户触底
        再找一批新的（exclude 里是已经给过的链接，让它别重复）。
        """
        filters = dict(filters or {})
        if filters.get('resource_type') == 'novel':
            filters['resource_type'] = 'article'
        with self._task_lock:
            previous = self._current_task
            self._task_seq += 1
            task: Dict[str, object] = {
                "id": "%s-t%d" % (self._task_prefix, self._task_seq),
                "query": query,
                "agent_contract": task_contract(),
                "kind": kind,
                "filters": dict(filters or {}),
                "want": max(1, min(1000, int(want or 100))),
                "created": time.time(),
                "execution": {"first_results": 5, "max_parallel": 3,
                              "checkpoint_seconds": 2, "completion_policy": "target_or_verified_exhausted_or_search_limited",
                              "control": "/api/control", "helper": "/api/control/helper",
                              "heartbeat_seconds": 15, "silence_cancels_task": False,
                              "service_unavailable_action": "stop_session_no_auto_restart",
                              "control_failure_retries": 3,
                              "keep_polling_after_completion": True,
                              "requires_agent_reasoning": True,
                              "session_lifecycle": "persistent_worker_or_active_foreground_poll",
                              "wrap_local_commands": True},
                "completion": {},
            }
            if kind == "more":
                task["round"] = int((self._current_task or {}).get("round") or 0) + 1
            if exclude or kind == 'more':
                task["exclude"] = list(exclude or [])
            self.clear_tasks()
            self._task_cancel = threading.Event()
            self._metrics = {'queued_at': time.monotonic(), 'network_ms': 0, 'network_requests': 0}
            self._task_urls = {normalize_url(url) or url for url in (exclude or [])}
            self._task_received = 0
            self._task_result_rows = []
            task['budget'] = {'max_results': task['want'], 'stop_on_limit': True, 'revision': 1,
                              'stop_scope': 'task', 'keep_polling_after_completion': True,
                              'mode': getattr(self.access.config.settings, 'agent_search_mode', 'limited') if self.access else 'limited'}
            self._current_task = task
            self._task_state = "queued"
            self._tasks.put_nowait(task)
            if self.access:
                self.access.prepare(task)
                if previous and kind == "more" and previous["query"] == query:
                    self.access.logins.inherit(previous["id"], task["id"], self._task_cancel)
            return task

    def pop_task(self, wait: float = 20.0, agent: str = "", session_token=None):
        agent = (agent or "").strip()[:48]
        with self._task_lock:
            session_token = self.token if session_token is None else session_token
            if session_token != self.token or not self.touch(agent):
                return None
        deadline = time.monotonic() + max(0, min(float(wait), 25))
        with self._task_lock:
            self._get_times[agent] = time.time()
            self._waiting_polls[agent] = self._waiting_polls.get(agent, 0) + 1
            started_paused = self._paused
        try:
            while True:
                with self._task_lock:
                    if session_token != self.token or not self.is_owner(agent):
                        return None
                    # An active poll reports pause promptly. Subsequent paused polls
                    # wait normally instead of spinning every two seconds.
                    if self._paused and not started_paused:
                        return None
                    if self.access:
                        control = self.access.poll()
                        if control:
                            return control
                    if not self._paused:
                        self._resume_pending = False
                        if self._current_task and self._current_task['completion'].pop('redeliver', False):
                            self._queue_current()
                    # Removing a task from the queue is not proof of delivery.
                    # Until ACK/progress/results arrives, preserve and retry it.
                    if (not self._paused and self._task_state == "delivered" and
                            time.monotonic() - self._delivery_at >= self._receipt_timeout):
                        self._queue_current()
                    try:
                        if self._paused:
                            raise queue.Empty
                        task = self._tasks.get_nowait()
                        if self._current_task and task["id"] == self._current_task["id"]:
                            self._task_state = "delivered"
                            self._delivery_at = time.monotonic()
                            self._delivery_attempt += 1
                            delivery = dict(task, delivery_attempt=self._delivery_attempt,
                                            ack_required=True)
                            if task.get('completion', {}).get('pending'):
                                delivery.update(resume_required=True, received=self._task_received,
                                    remaining=max(0, int(task['want'])-self._task_received),
                                    exclude=list(self._task_urls))
                            if self.access:
                                delivery['network'] = self.access.for_task(task)
                            self._notify_task("delivered", "任务已发送，等待 Agent 确认领取")
                            return delivery
                    except queue.Empty:
                        pass
                if time.monotonic() >= deadline:
                    return None
                time.sleep(min(0.1, max(0, deadline - time.monotonic())))
        finally:
            with self._task_lock:
                self._waiting_polls[agent] = max(0, self._waiting_polls.get(agent, 1) - 1)

    def _queue_current(self):
        """Caller holds _task_lock; requeue only the current unconfirmed task."""
        while not self._tasks.empty():
            self._tasks.get_nowait()
        if self._current_task:
            self._tasks.put_nowait(self._current_task)
            self._task_state = "queued"

    def delivery_failed(self, task_id, attempt):
        with self._task_lock:
            if (self.accepts(task_id) and self._task_state == "delivered"
                    and attempt == self._delivery_attempt):
                self._queue_current()
                self._notify_task("queued", "任务发送中断，已保留，等待重新领取")

    def _notify_task(self, stage, message):
        if self.on_status and self._current_task:
            try:
                self.on_status({"stage": stage, "message": message,
                                "task_id": self._current_task["id"], "agent": self._owner})
            except Exception:
                pass  # A UI notification failure must not lose the delivery.

    def acknowledge(self, task_id, *, activity=False):
        with self._task_lock:
            if self._paused or not self.accepts(task_id):
                return False
            if self._task_state in ("completed", "failed"):
                return True  # ACK retry must not restart completed work.
            first = self._task_state != "working"
            self._task_state = "working"
            while not self._tasks.empty():
                self._tasks.get_nowait()
            if first or activity:
                self._activity_at = time.monotonic()
            if self.access and self.access.pending() and not self.access.plan.get('started'):
                self.access.plan['started'] = time.monotonic()
            if first:
                self._metrics.setdefault('ack_at', time.monotonic())
                message = ('Agent 已确认领取，正在验证外网连接' if self.access and self.access.pending()
                           else 'Agent 已确认领取，开始搜索')
                self._notify_task("acknowledged", message)
            return True

    def finish_task(self, task_id, failed=False):
        with self._task_lock:
            if self.accepts(task_id):
                self._current_task['completion'] = {}
                self._task_state = "failed" if failed else "completed"
                self._metrics.setdefault('finished_at', time.monotonic())
                self.runners.clear()
                self._task_cancel.set()
                self._control_condition.notify_all()
                while not self._tasks.empty():
                    self._tasks.get_nowait()

    def change_budget(self, task_id, want, mode):
        """Change an active task atomically; completed generations never reopen."""
        with self._task_lock:
            if not self.accepts(task_id) or self._task_state in ('completed', 'failed'):
                return None
            task = self._current_task
            task['want'] = max(0, min(1000, int(want)))
            task['budget'].update(max_results=task['want'], mode=mode,
                revision=int(task['budget'].get('revision', 1))+1)
            if (task.get('completion') or {}).get('pending'):
                task['completion']['message'] = '额度已调整，按最新remaining和next_actions继续检索。'
            reached = self._task_received >= task['want']
            if reached:
                self.finish_task(task_id)
            self._control_condition.notify_all()
            return dict(task_id=task_id, received=self._task_received, want=task['want'],
                remaining=max(0, task['want']-self._task_received), budget=dict(task['budget']),
                done=reached, limit_reached=reached, budget_changed=True, stop=False, session_ended=False, next='poll' if reached else 'continue')

    def task_status(self):
        """Read-only snapshot, safe to inspect without consuming /api/task."""
        with self._task_lock:
            task = self._current_task or {}
            return {"task_id": task.get("id", ""), "state": self._task_state,
                    "age": max(0, time.time() - float(task.get("created") or time.time())),
                    "attempts": self._delivery_attempt, "owner": self._owner,
                    "paused": self._paused,
                    "waiting_polls": self._waiting_polls.get(self._owner, 0),
                    "last_poll_age": (time.time() - self._get_times[self._owner]
                                      if self._owner in self._get_times else -1),
                    "queued": self._tasks.qsize(),
                    "connection": (self.access.connection if self.access else 'legacy'),
                    "ready": (self.access.ready() if self.access else self.connected() and not self._resume_pending),
                    "timings": self.timings(),
                    "liveness": self.liveness(),
                    "received": self._task_received,
                    "want": int(task.get('want') or 0),
                    "budget": dict(task.get('budget') or {}),
                    "remaining": max(0, int(task.get('want') or 0)-self._task_received),
                    "completion": dict(task.get('completion') or {}),
                    "network": (self.access.snapshot() if self.access else {}),
                    "login": self.access.logins.snapshot(task.get("id", "")) if self.access else {}}

    def timings(self):
        with self._task_lock:
            m = self._metrics
            def elapsed(a, b):
                return round(1000 * (m[b] - m[a])) if a in m and b in m else None
            now = m.get('finished_at', time.monotonic())
            intervals = list(m.get('network_intervals', []))
            intervals += [(start, min(now, end)) for (task_id, start), end in self._inflight_network.items()
                          if task_id == (self._current_task or {}).get('id')]
            union = 0; end = 0
            for start, stop in sorted(intervals):
                union += max(0, stop-max(start,end));end=max(end,stop)
            work = max(0, now-m['ack_at']) if 'ack_at' in m else 0
            return {'queue_ms': elapsed('queued_at', 'ack_at'),
                    'first_result_ms': elapsed('ack_at', 'first_result_at'),
                    'network_ms': round(m.get('network_ms', 0)),
                    'work_ms': round(1000*work), 'network_wall_ms': round(1000*union),
                    'outside_tool_ms': round(1000*max(0,work-union)),
                    'network_requests': m.get('network_requests', 0)}

    def record_network(self, task_id, started):
        with self._task_lock:
            if self.accepts(task_id):
                self._metrics['network_ms'] = self._metrics.get('network_ms', 0) + 1000 * (time.monotonic() - started)
                self._metrics['network_requests'] = self._metrics.get('network_requests', 0) + 1
                self._metrics.setdefault('network_intervals', []).append((started, time.monotonic()))

    def liveness(self):
        with self._task_lock:
            task_id = (self._current_task or {}).get('id', '')
            active = self._task_state == 'working' and not self._paused and not self._task_cancel.is_set()
            count = self.runners.active(task_id) if active else 0
            age = self.last_poll_age()
            return {'session_retained': self.session_ready(), 'registered_searches': count,
                    'activity_age': round(max(0,age),1) if age>=0 else None,
                    'evidence': 'registered_process' if count else 'request' if self.connected(within=90) else 'unknown',
                    'silence_revokes_session': False}

    def disconnect(self):
        """Revoke this connection, leaving the listening service and cards intact."""
        with self._task_lock:
            self.clear_tasks()
            self._revoked_tokens.add(self.token)
            self.token = secrets.token_hex(16)
            self._owner = ''
            self._seen_agents.clear()
            self._poll_times.clear()
            self._get_times.clear()
            self._epoch += 1
            self._paused = False
            self._resume_pending = False
            self.disconnected = True
            if self.access:
                self.access.owner = ''
                self.access.reset()

    def cancellation(self, task_id):
        """Capture a generation's event, never follow a later task's event."""
        with self._task_lock:
            if self.accepts(task_id):
                return self._task_cancel
            cancelled = threading.Event()
            cancelled.set()
            return cancelled

    def control(self, agent, task_id, wait=0, session_token=None, observe_only=False, budget_revision=None):
        """Read-only cancellation long poll; never claims or consumes new work."""
        with self._task_lock:
            session_token = self.token if session_token is None else session_token
            if session_token != self.token or not self.is_owner(agent):
                return {"ok": False, "stop": True, "cancelled": True}
            cancel = self.cancellation(task_id)
            revision = int((self._current_task or {}).get('budget', {}).get('revision', 0))
            expected = revision if budget_revision is None else budget_revision
            # A leftover cancellation watcher is not evidence of a live Agent.
            if not observe_only and not cancel.is_set() and not self._paused:
                self.touch(agent)
            deadline = time.monotonic() + max(0, min(float(wait), 25))
            while not cancel.is_set() and time.monotonic() < deadline:
                current_revision = int((self._current_task or {}).get('budget', {}).get('revision', 0))
                if current_revision != expected:
                    break
                self._control_condition.wait(min(.1, max(0, deadline-time.monotonic())))
        with self._task_lock:
            stop = session_token != self.token or not self.is_owner(agent)
            stale = not self.accepts(task_id)
            task = self._current_task or {}
            budget = dict(task.get('budget') or {})
            return {"ok": not stop, "stop": stop, "stale": stale,
                    "session_ended": stop, "stop_scope": "session" if stop else "task" if cancel.is_set() or stale else None,
                    "keep_polling": not stop, "task_finished": not stale and self._task_state in ('completed', 'failed'),
                    "budget": budget, "budget_changed": not stale and budget.get('revision', 0) != expected,
                    "want": int(task.get('want') or 0), "received": self._task_received,
                    "remaining": max(0, int(task.get('want') or 0)-self._task_received),
                    "paused": self.paused, "cancelled": cancel.is_set() or stale or stop,
                    "task_id": task_id, "current_task_id": (self._current_task or {}).get('id', ''),
                    "state": self._task_state,
                    "login": self.access.logins.snapshot(task_id) if self.access else {},
                    "completion": dict((self._current_task or {}).get('completion') or {}),
                    "next": "stop" if stop else "poll" if cancel.is_set() or stale else "continue"}

    def retry_task(self):
        with self._task_lock:
            task = self._current_task
            if not task or self._paused:
                return None
            if self._task_state in ("queued", "delivered"):
                self._queue_current()
                return dict(task)
            remaining = int(task['want']) - self._task_received
            if remaining <= 0:
                return None
            return self.push_task(str(task["query"]), task.get("filters"),
                                  str(task["kind"]), remaining, list(self._task_urls))

    def accepts(self, task_id: str, query: str = "") -> bool:
        with self._task_lock:
            task = self._current_task
            return bool(task and task_id == task["id"]
                        and (not query or query == task["query"]))

    def touch(self, agent: str = "") -> bool:
        with self._task_lock:
            """记录一次「Agent 来过」。返回它现在是不是接管者。

            新名字的 Agent 会顶掉旧主人；已经被顶掉的旧 Agent 再来只记一笔，
            不会把接管权抢回去（否则两边会来回抢）。
            """
            name = (agent or "").strip()[:48]
            if not name:
                return False
            now = time.time()
            if not self._owner:
                self.disconnected = False
                self._owner = name
                self._seen_agents.add(name)
                self._poll_times[name] = now
                return True
            if name == self._owner:
                self._poll_times[name] = now
                return True
            self._poll_times[name] = now
            if name in self._seen_agents:
                return False                     # 被顶掉的旧 Agent
            old = self._owner
            self._owner = name
            self._seen_agents.add(name)
            self._epoch += 1
            pending = self._current_task
            active = self._task_state in ("queued", "delivered", "working")
            # Reissue atomically instead of deleting work and relying on a later
            # GUI callback to recreate it (which could invalidate a newer task).
            if pending and active:
                self.push_task(str(pending["query"]), pending.get("filters"),
                               str(pending["kind"]), max(1, int(pending["want"]) - self._task_received), list(self._task_urls))
            if self.on_status:
                try:
                    self.on_status({"stage": "takeover",
                                    "agent": name, "previous": old, "epoch": self._epoch,
                                    "message": "%s 接管了搜索，%s 已退出" % (name, old)})
                except Exception:                # noqa: BLE001
                    pass
            return True

    def is_owner(self, agent: str = "") -> bool:
        name = (agent or "").strip()[:48]
        return bool(name) and name == self._owner

    @property
    def owner(self) -> str:
        return self._owner

    @property
    def epoch(self) -> int:
        return self._epoch

    # -- 暂停 / 继续 ------------------------------------------------------- #
    def pause(self) -> None:
        with self._task_lock:
            """暂停：不再派新任务，Agent 取任务会收到 paused=true；结果也不再入库。"""
            self._paused = True
            self._task_cancel.set()
            if self.on_status:
                try:
                    self.on_status({"stage": "paused", "message": "已暂停 Agent 搜索"})
                except Exception:            # noqa: BLE001
                    pass

    def resume(self) -> None:
        with self._task_lock:
            if self._paused:
                self._resume_pending = True
            self._paused = False
            if self._task_cancel.is_set() and self._current_task and self._task_state in ('queued', 'delivered', 'working'):
                # Paused workers remain cancelled; resumed work gets a fresh generation.
                task = self._current_task
                self.push_task(str(task['query']), task.get('filters'), str(task['kind']),
                               max(1, int(task['want']) - self._task_received), list(self._task_urls))
            if self.on_status:
                try:
                    self.on_status({"stage": "resumed", "message": "已继续 Agent 搜索"})
                except Exception:            # noqa: BLE001
                    pass

    @property
    def paused(self) -> bool:
        return self._paused

    def clear_tasks(self) -> None:
        with self._task_lock:
            self.runners.clear()
            self._task_cancel.set()
            """撤掉还没被取走的任务（用户取消 / 改用本地搜索时调用）。"""
            try:
                while True:
                    self._tasks.get_nowait()
            except queue.Empty:
                pass
            self._current_task = None
            self._metrics = {}
            self._task_urls = set()
            self._task_received = 0
            self._task_state = "idle"
            self._delivery_at = 0.0
            self._delivery_attempt = 0
            self._activity_at = 0.0
            if self.access:
                self.access.plan = {}
                self.access.receipts.clear()

    def report_status(self, info: Dict[str, object]) -> None:
        self.touch(str(info.get("agent") or ""))
        if self.on_status:
            self.on_status(info)

    def report_error(self, info: Dict[str, object]) -> None:
        """Agent 明确说「这单我做不了」时调用：界面必须弹出来，不能干等。"""
        self.touch(str(info.get("agent") or ""))
        if self._on_error:
            self._on_error(info)

    def connected(self, within: Optional[float] = None) -> bool:
        if not self._owner:
            return False
        if within is None and not self._paused and self._task_state == 'working':
            task_id = (self._current_task or {}).get('id')
            with self._task_lock:
                if not self._task_cancel.is_set() and self.runners.active(task_id):return True
                if any(key[0] == task_id and until > time.monotonic()
                       for key, until in self._inflight_network.items()):
                    return True
        last = self._poll_times.get(self._owner) or 0.0
        idle = self._paused or self._task_state not in ('delivered', 'working')
        if within is None:
            within = 45.0 if idle else 90.0
        return bool(last) and (time.time() - last) <= within

    def connected_agent(self, within: Optional[float] = None) -> Dict[str, object]:
        """谁在接管：返回 {connected, name, age}，给界面显示用。"""
        ok = self.connected(within)
        last = self._poll_times.get(self._owner) or 0.0
        age = time.time() - last if last else -1.0
        return {"connected": ok, "name": self._owner if ok else "", "age": age,
                "ready": self.access.ready() if self.access else ok and not self._resume_pending}

    def session_ready(self) -> bool:
        """Handshake authorization, distinct from evidence of recent activity.

        Quiet external agents can still receive queued work on their next poll.
        Silence alone never revokes a session or proves that its process exited.
        """
        with self._task_lock:
            return bool(self._owner and not self.disconnected and not self._resume_pending
                        and (not self.access or self.access.owner == self._owner
                             and self.access.connection == 'ready'))

    def last_poll_age(self) -> float:
        last = self._poll_times.get(self._owner) or 0.0
        return time.time() - last if last else -1.0

    @property
    def current_task(self) -> Optional[Dict[str, object]]:
        return self._current_task

    # ------------------------------------------------------------------ #
    @property
    def running(self) -> bool:
        return self._httpd is not None

    @property
    def port(self) -> int:
        return self._port

    @property
    def url(self) -> str:
        return "http://127.0.0.1:%d" % self._port

    def endpoint(self) -> str:
        return "%s/api/results?token=%s" % (self.url, self.token)

    def instructions(self) -> str:
        base = self.url
        tok = self.token
        return (
            f'资源搜寻器 {APP_VERSION} · Agent 接入协议。每次接入以本次复制的地址和凭据为准。\n'
            + AGENT_RULES + '\n'
            +
            '【断开连接】用户点击“断开 Agent”会撤销接入凭据；收到 stop:true 必须停止所有本轮工作并退出轮询。'
            '不要尝试自行重新连接、读取新凭据或重新接管；只有用户重新粘贴新指令时才再次接入。\n'
            '【终止信号优先级最高】每次请求先读取 JSON，再处理任务。stop:true、session_ended:true、'
            'next:"stop" 或 task.kind:"stop" 任一出现，都表示结束整个接管会话：跳出最外层循环，'
            '停止你启动的搜索子进程和后台轮询，不再 sleep/retry，不再发送 done/error；向用户说明已停止。'
            '不能把此信号当成 task:null、暂停或普通网络失败。GET task 的终止响应使用 HTTP 200，其他接口也须读取错误响应体。'
            '工具返回的 HTTP 403 表示凭据失效，应停止当前接入；禁止自动读取新 token。\n'
            '【资源类型优先】task.filters.resource_type为video/image/article/sound，分别只搜视频/图片/文章/音效，小说归入article。每条结果必须带同名resource_type。图片url可为原图或作品页、cover为原图或缩略图；作品页须带extra.image_page=true和extra.page_url，不要把HTML页面当图片下载。文章（含小说）url为正文或作品页，有封面则提供。音效sound仅返回独立音效资源页或音频文件，优先爱给网等用户选中的来源；不要把视频、音乐介绍或搜索结果页当音效。无封面可省略cover，试听与下载按原站规则进行。旧视频示例仅适用于video，其他类型不能用视频凑数，不下载全文。\n'
            '【3.1 快速搜索与抢占：优先遵守】\n'
            '【平台范围】若 task.filters 含 source_keys/source_names，只搜索用户当前勾选的平台；'
            '明确平台选择优先于原搜索词中的来源描述。network/config.sources 也只提供当前允许站点。'
            '切勿为了凑数量搜索其他平台；没有可用续页时说明原因并收尾。\n'
            '用户随时可能换词，必须让新任务抢占旧任务。执行计划见 task.execution。\n'
            f'GET {base}/api/control?token={tok}&agent=你的名字&task_id=当前任务号&wait=0 '
            '检查当前任务；wait=20 可长轮询等待取消，不领取新任务、不改变接管者。\n'
            '每次联网/翻页/推送之前和之后都检查 control；脚本执行期间至少每 2 秒检查，'
            '或另起控制监听器。先检查会话终止信号；只有 stop 不为 true 的 cancelled:true 才停止旧任务所有子搜索并重新 GET task；'
            'paused:true 先等待恢复；stop:true 退出整个接管循环。\n'
            f'GET {base}/api/control/helper?token={tok} 的 script 是可审阅的标准库执行助手，保存为 rf_agent_run.py。\n'
            '【关闭与闪退保险】工具正常退出时撤销会话，并向仍在等待的请求返回终止信号。闪退无法主动发送消息：'
            '必须独立检查本机控制通道，连续3次连接失败、超时或无有效响应即停止本会话及所有自建子搜索，禁止自动重启、无限重连或继续外网搜索。'
            '控制检查使用短超时（单轮助手约5秒/次，常驻领取6秒/次），不使用资源搜索的180秒超时；正常慢搜索、task:null、paused、完成单轮不是工具不可达。'
            '配套助手会输出stop:true、session_ended:true、next:stop、retryable:false及reason:service_unavailable；外层必须退出，不把退出码0当成继续接单。'
            '更新后重新下载control/helper和network/helper，旧助手不会自动升级。外部模型或云端调用无法由工具强制终止，只能要求调用前后检查并停止后续操作。\n'
            '【持续接管】本轮done/limit_reached/cancelled只停止本轮，绝不等于退出整个接管。只有stop/session_ended/next=stop或上述控制通道持续不可达才退出。'
            '默认由当前Agent保持前台领取任务，完成理解、搜索和相关性审核。只有已有真实模型调用与审核能力的通用工作程序时，才使用后台常驻器。'
            f'满足此前提后命令为：python rf_agent_run.py --base {base} --token {tok} --agent "你的名字" --serve '
            '--run python 具备模型理解和审核的工作程序.py。若已领取当前任务，在--run之前另加--task-id "当前任务号"，否则省略。'
            '禁止使用旧版默认采集脚本替代Agent理解；不要把仅使用工具适配器、没有模型审核的脚本称为AI工作程序。'
            '自定义工作脚本必须从环境变量RF_TASK_FILE读取每次任务JSON，RF_BASE/RF_TOKEN/RF_AGENT/RF_TASK_ID提供接口身份；'
            '不得写死关键词、task_id、额度或排除列表。脚本执行网络检测、筛选并回推，确认done:true后退出；'
            '常驻接管器随后继续长轮询，并为新任务启动同一通用工作脚本。该脚本每轮必须实际调用模型理解和审核；调度器本身不调用模型，不能把普通采集器当作此工作脚本。'
            '只启动一份常驻接管器，不与原人工轮询竞争；先确认它成功执行一轮并仍在等待，不能把脚本创建成功当作持续接管成功。\n'
            f'仅调试单轮时可用 python rf_agent_run.py --base {base} --token {tok} '
            '--agent "你的名字" --task-id "当前任务号" --run python 你的搜索脚本.py 参数 启动，'
            '或自己实现等效取消监听。该单轮用法不提供持续接管，外层必须继续前台GET task；正常后台接管优先使用上面的--serve。助手只终止它自己启动的子进程树；退出码 75 表示仅取消本轮，接着取新任务。'
            '用户断开时助手正常退出（退出码 0）并输出 session_ended:true、next:"stop"，必须据此退出最外层循环，不能再取任务。'
            '既不加--serve也不加--run时只监听取消，不算Agent活跃证据。工具每2秒只检查该助手注册的进程，不扫描其他进程。'
            '所有耗时本地搜索脚本都应通过此助手 --run 执行，自动检测存活并响应中断；不要只启动无人读取输出的后台监听器。\n'
            '内置云端搜索无法用本地助手终止：每次用小批查询并在调用前后检查 control，'
            '不要连续执行过期任务；不能声称已强制终止无法取消的调用。\n'
            '【登录限制与等待】HTTP 200 也可能包含 login_required=true。遇到登录/年龄确认，立即暂停该来源，继续其他来源；'
            '自己联网发现明确登录提示时 POST /api/network/login，带 agent、task_id、source_id、query 和 code=login_required 或 age_verification_required。'
            '不得在此情况下重复换词爬取或绕过平台验证。工具提示用户在专用窗口登录，确认后关闭窗口并在后台复用登录；无需扩展。'
            '持续 GET /api/control 查看 login[source_id].state；waiting/verifying 时低频等待，skipped 时本轮排除该来源，ready 后用 network/search 从 page=1 重新验证并续搜。'
            '有待登录来源时不能提前退出循环；每个新任务都重新查看来源状态。不要索取、输出或转发密码、验证码、Cookie。'
            '登录后必须复用工具 network/search 的同一浏览器会话，自己的匿名请求不会继承登录。'
            '普通搜索请求默认约180秒；调用工具网络接口客户端超时至少210秒。控制轮询仍短周期检查取消信号。\n'
            '【数量目标与硬上限】限制模式本批目标 100 条，满额后由用户向下滚动触底触发下一批；无限模式每个关键词目标 1000 条。'
            '用户切换模式时control会立即唤醒，返回最新budget.revision/mode、want、received、remaining；未满额保持原task_id和游标，按新remaining继续。'
            '常驻助手同步更新RF_TASK_FILE，工作脚本每次请求与回推前应重读；不要把首次want写死。达到新额度则只结束本轮并继续取任务。'
            'want/budget.max_results 既是本任务应完成的有效资源数量，也是硬上限；补足任务的 want 仅包含尚缺的数量。'
            '收到 results.limit_reached 或 control.cancelled 时立即结束本轮所有子搜索，回到取任务等待；'
            '没有新任务不得继续搜索、预取或自行创建续搜循环。先返回前 5 条，随后按已完成来源的小批结果持续回推；'
            '边搜边推，不要因时间已到或只搜完首屏就收尾；用已接受的有效数量判断是否达标，持续翻页或更换关键词补足。'
            '提前 done:true 后工具会自动派发缺额，必须继续轮询领取，不能把 55 条当作完成 100 条。'
            '确实没有更多资源时提交 done:true、exhausted:true、message:"具体检索范围和末页证据"。'
            '工具会核对本任务network/search的分页记录：先看pagination.limited，true时停止该查询，处理其他来源后done:true；只有未受限的exhausted=false才继续下一页。'
            '连续3个成功页无新候选、每查询50页或每任务200个成功页会停止本轮检索；search_limited不代表网站无资源，不得自行重开任务绕过限制。'
            '重复、不相关、筛选后为0、只检查一两页都不代表结束。不能仅凭文字报告收尾；用自有联网搜索时也须通过工具接口核验所选来源的末页或不可用状态。'
            '响应completion_pending:true/done:false表示未接受结束：保持当前task_id、已交付数及页码，按completion.next_actions继续；'
            '再次GET task会返回同一任务resume_required:true及remaining/exclude，不得把它当新批次、丢弃已有进度。'
            '只有响应done:true才算本次任务收尾，完成后仍须前台持续轮询。不要用“待命中”回复结束对话，也不要只启动一个无法调用模型的后台监听器。'
            '单个来源结束不代表全部结束，错误请走 /api/error。连续三次补足无新增时工具暂停补足，避免空转消耗。\n'
            '优先 POST network/search 复用已启用站点的适配器，最多同时请求 3 个来源；同一来源一次推进一个关键词，结束或受限后再换词，不要轮流恢复大量浏览器页；'
            '请求可带 compact:true 省去封面地址等无需模型处理的字段，服务端仍保存完整元数据；'
            '每个来源一返回就筛选回推，不等最慢来源。工具适配器候选可按candidate_id直接回推完整元数据；'
            '已有可靠链接和元数据时，不要逐条重新打开资源页或反复解析同一链接。只有缺少必要信息才补查；不能编造信息。'
            '同站同关键词的有效翻页继续推进；不支持翻页的站点换词，不重复请求相同首屏。\n\n'
            'network/search 单页最多 50 条候选不等于整批目标。请记录各来源页码，补足任务沿用进度继续下一页，'
            '不要每次领取 more 又从已搜索过的第一页开始。\n\n'
        ) + (self.access.instructions() if self.access else '') + (
            f"你现在接手「{APP_NAME}」的搜索工作（它在我的电脑上运行，只监听 127.0.0.1）。\n"
            "下面这些就是全部接口，token 已经写在地址里：\n\n"
            f"  GET  {base}/api/task?token={tok}&wait=20&agent=你的名字\n"
            "        ← 长轮询取任务（没任务会等 20 秒返回 null）。\n"
            "          请把 agent 参数换成你自己的名字（比如 Codex CLI / Claude Code /\n"
            "          DSH / Cursor），这样用户能在界面上看到「谁正在接管」。\n"
            f"  POST {base}/api/results?token={tok}     ← 把搜到的资源推回来\n"
            f"  POST {base}/api/error?token={tok}       ← 搜不了/被拒绝时报告失败原因\n"
            f"  POST {base}/api/progress?token={tok}    ← 可选：回报进度\n"
            f"  POST {base}/api/ack?token={tok}         ← 领取确认（收到任务后立即调用）\n"
            f"  GET  {base}/api/status?token={tok}      ← 只读诊断，不会取走任务\n"
            f"  GET  {base}/api/help?token={tok}                    ← 重新读这份说明\n"
            f"  GET  {base}/api/ping                    ← 自检\n\n"
            "【任务校验】取任务必须带 agent；results/progress/error 必须带接口返回的当前 task_id，不能照抄示例 t3。\n"
            "收到 409 stale:true 表示任务已撤销：放弃旧搜索并重新取任务；stop:true 才是退出接管。\n"
            "搜索期间建议每15秒通过control或progress回报活动；长脚本通过运行助手自动检测。"
            "工具不因静默或耗时较长撤销接管/任务；实际HTTP超时只针对单次请求，失败需报告。\n\n"
            "【你的工作流程】\n"
            "1. 循环取任务：\n"
            f'   curl --max-time 35 -s "{base}/api/task?token={tok}&wait=20&agent=Codex%20CLI"\n'
            "   HTTP 超时应大于 wait（建议 35 秒）。执行工具返回后台 session 时必须继续等待并读完输出。\n"
            "   没输出不等于 task:null；不能只启动一个保存响应但没人处理的后台轮询脚本。\n"
            '   返回 {"ok":true,"task":null} 表示暂时没活，继续轮询；\n'
            '   返回 {"ok":true,"task":{"id":"t3","kind":"search","query":"用户原话",\n'
            '        "want":100,"filters":{"sort":"relevance","time":"7d","min_views":0}}}\n'
            "   就是用户点了一次搜索。\n"
            f"   取到任务后，先 POST {base}/api/ack?token={tok}\n"
            '   {"task_id":"取到的 task.id","agent":"与 GET 相同的名字"}，确认 acknowledged:true 后再搜索。\n'
            "   同一 task_id 可能因网络重发再次出现：仍要确认，但不要并行重复执行同一任务。\n"
            "   未确认的任务会在后续轮询中重发；首次 progress/results/error 也兼容作为领取确认。\n"
            "   如果返回 {\"ok\":false,\"stop\":true}，说明用户已断开或接管者已改变，\n"
            "   你必须立刻停下循环，不要再推结果。\n"
            "   paused:true 只暂停搜索，不结束接管：立即再次 GET task（wait=20），服务端会长轮询等待恢复。\n"
            "   暂停期间不要搜索或推结果，但必须留在当前执行中持续读取轮询响应，不能发送最终答复后结束。\n"
            "   task:null、paused:true、cancelled:true、子任务退出码 75 都不是退出整个接管循环的信号。\n"
            "   只有 stop:true、用户明确要求退出或工具关闭才退出；若运行环境不能持续等待，必须报告原因，不能声称仍在接管。\n"
            "2. 按上方Agent职责理解task.query原话，生成检索词，再用filters.resource_type搜索对应资源，限定用户勾选的站点；逐批审核相关性后才回推。\n"
            "   用 filters 过滤——time=any|24h|7d|30d|1y，min_views 仅用于视频。\n"
            "   只要对应资源的真实地址，不要搜索结果页，不要编造链接。\n"
            "   每轮以 want 为数量目标，边搜边推；未满目标时继续翻页或更换关键词，不能仅因耗时收尾。\n"
            "   完成当前任务后取任务等待；工具会自动补足本批缺额，限制模式满 100 条才等待滚动。\n"
            "   无限模式最多累计 1000 条；确实不足时用 exhausted:true 和具体 message 报告，不编造资源。\n"
            "3. task.kind = \"more\" 表示补足当前批次的缺额，或用户触底后要下一批：\n"
            "   - 继续找新的资源，以 want 为目标；\n"
            "   - **跳过 task.exclude 里已经给过的链接**，不要重复推同一批；\n"
            "   - 推完继续轮询，用户再滚到底会再来一条 more。\n"
            "4. **边搜边推：首批立即提交，随后每个来源的小批结果完成就提交，不要等全部搜索结束。**\n"
            "   用户要能马上看到结果和进度（他会滚着看），所以：\n"
            f"   POST {base}/api/results?token={tok}\n"
            "   Content-Type: application/json\n"
            '   {"task_id":"t3","agent":"你的名字","query":"用户原话","done":false,"results":[\n'
            '     {"title":"标题","url":"https://...","cover":"https://...",\n'
            '      "source":"哔哩哔哩","author":"作者","duration":"12:34",\n'
            '      "views":12345,"pub_ts":1700000000,"desc":"简介"}\n'
            "   ]}\n"
            "   外部搜索结果 title 和 url 必填；有 cover 必须原样保留，不要丢弃封面。\n"
            '   优先使用 network/search 返回的 candidate_id：筛选后 POST results，'
            'JSON 示例 {"task_id":"当前任务号","agent":"你的名字","candidate_ids":["选中的编号"],"done":false}。\n'
            "   工具按编号恢复完整元数据，无需重写标题/封面；编号只在当前任务有效。"
            "第一批结果立即回推，随后每个来源返回一批就提交一次，不必逐条调用工具。\n"
            "   **这一轮全部搜完后，再发一次收尾请求（results 可为空）：**\n"
            '   {"task_id":"t3","agent":"你的名字","done":true,"results":[]}\n'
            "   请求done:true不代表工具已接受结束，必须读取响应done和completion_pending。未达目标且仍有分页时继续当前任务。\n"
            "   一条未找到也不能凭空判定结束：核验分页/来源后报告exhausted，或通过error原样报告无法搜索的原因。\n"
            f"5. 如果拒绝搜索 / 内容审核不通过 / 工具报错，必须告诉我，别沉默：\n"
            f"   POST {base}/api/error?token={tok}\n"
            '   {"task_id":"t3","agent":"你的名字","error":"原样写你收到的报错/拒绝原因"}\n'
            "   界面会立刻把这个错误弹给用户，并结束「搜索中」。\n"
            f"6. 想让我看到进度就 POST {base}/api/progress?token={tok}\n"
            '   {"task_id":"t3","agent":"你的名字","stage":"searching","message":"正在搜 B站…（12/100）"}\n'
            "7. 推完继续轮询下一条，直到我关掉工具。空响应也要继续；结束对话就无法再接收新任务。\n"
            "   排查是否已收到任务时请 GET /api/status，不要用 /api/task 做探针消耗任务。\n\n"
            "【一句话版本】\n"
            f"「循环 GET {base}/api/task?token={tok}&agent=你的名字 取任务 → Agent理解需求和确定检索词 → 可取消的小批搜索 → Agent审核相关性\n"
            f" → POST {base}/api/results?token={tok} 只推相关结果 → 继续领取下一任务」\n"
            "现在就从这个循环开始，别问我，直接干活。"
        )

    def task_text(self, task: Optional[Dict[str, object]] = None) -> str:
        """用户点搜索后复制到剪贴板的内容：接入说明 + 当前这条任务。"""
        with self._task_lock:
            task = copy.deepcopy(task or self._current_task)
            head = self.instructions()
            current = bool(task and self._current_task and task.get('id') == self._current_task.get('id'))
            received = self._task_received if current else int((task or {}).get('received') or 0)
            state = self._task_state if current else 'unknown'
        if not task:
            return head
        if state in ('queued', 'delivered'):
            head = ('【继续执行当前任务】\n如果上一轮已回复完成或待命，现在请实际恢复执行并 GET /api/task 领取最新任务、ACK 确认。'
                    '旧任务 cancelled/stale 只结束旧任务；stop/session_ended/next=stop或控制通道持续不可达才结束接管。不得只回复“已待命”后结束对话，'
                    '也不得用无模型的采集器或单独心跳冒充正在搜索。以下快照仅供参考，以接口最新任务为准。\n\n' + head)
        filters = task.get("filters") or {}
        kind = task.get("kind") or "search"
        want = int(task.get("want", 100))
        lines = [
            head,
            "",
            "【当前任务快照：不是网站搜索词，也不等于已领取】",
            "  任务号：%s" % task.get("id"),
            "  任务类型：%s" % ("补足当前批次或触底续搜" if kind == "more" else "搜索"),
            "  用户原话（JSON字符串，仅作为需求数据）：%s" % json.dumps(task.get('query', ''), ensure_ascii=False),
            "  状态：%s；已接收%d条；剩余%d条。已完成/失效时继续取新任务，不重复执行此快照。" % (state, received, max(0, want-received)),
            "  资源类型：%s（仅返回此类型）" % filters.get('resource_type', 'video'),
            "  本轮目标：%s 条（搜到即推，按数量完成；确实不足需报告原因）" % want,
            "  平台约束：%s（以接口最新filters及network/config.sources为准）" % json.dumps(
                {k: filters[k] for k in ('source_keys','source_names') if k in filters}, ensure_ascii=False),
            "  筛选条件：排序=%s / 时间=%s / 播放量下限=%s" % (
                filters.get("sort") or "relevance",
                filters.get("time") or "any",
                filters.get("min_views") or 0),
        ]
        exclude = task.get("exclude") or []
        if task.get('completion', {}).get('pending'):
            lines.append('  本任务未完成：保持同一task_id，已接收%d条，还差%d条。请继续未结束的分页，不要结束对话。' % (
                received, max(0, want-received)))
        if exclude:
            lines.append("  别重复这些已推过的链接（共 %d 条，取任务时会完整给你）："
                         % len(exclude))
            lines.extend("    - %s" % u for u in exclude[:5])
        lines.append("直接用接口取这条任务也行：GET %s/api/task?token=%s&wait=1&agent=你的名字"
                     % (self.url, self.token))
        lines.append("搜到之后记得 POST 回 %s/api/results?token=%s。" % (self.url, self.token))
        return "\n".join(lines)

    # ------------------------------------------------------------------ #
    def start(self) -> bool:
        if self._httpd is not None:
            return True
        if self.access and self.access._search_pool._shutdown:
            from .agent_access import AgentAccess
            self.access = AgentAccess(self, self.access.config)
        handler = self._make_handler()
        last_exc: Optional[Exception] = None
        for port in range(self._port, self._port + 12):
            try:
                # allow_reuse_address 默认是 True，Windows 上会让两个进程同时绑定
                # 同一个端口，请求被随机分给另一个实例（表现为莫名 403）。这里改成
                # 独占绑定，端口被占用时直接换下一个。
                httpd = _LocalHTTPServer(("127.0.0.1", port), handler)
            except OSError as exc:
                last_exc = exc
                continue
            self._port = httpd.server_port
            self._httpd = httpd
            self._thread = threading.Thread(target=httpd.serve_forever,
                                            name="agent-server", daemon=True)
            self._thread.start()
            return True
        if last_exc:
            return False
        return False

    def stop(self) -> None:
        # Revoke before closing the listener: pending task/control requests can
        # still return a terminal stop instead of timing out and retrying forever.
        self.disconnect()
        if self.access:
            self.access.close()
        if self._httpd is None:
            return
        try:
            self._httpd.shutdown()
            self._httpd.server_close()
        except Exception:                 # noqa: BLE001
            pass
        self._httpd = None
        self._thread = None

    # ------------------------------------------------------------------ #
    def _make_handler(self):
        server = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *a):        # 别往控制台刷日志
                return

            def _send(self, code: int, payload: Dict[str, object]) -> bool:
                if self._request_token() in server._revoked_tokens:
                    code, payload = 409, {"ok": False, "stop": True, "cancelled": True,
                                          "reason": "user_disconnected",
                                          "error": "用户已断开 Agent，请退出接管，等待用户重新提供接入指令"}
                elif code == 403 and self._request_token():
                    payload = {"ok": False, "stop": True, "cancelled": True,
                               "reason": "invalid_token", "error": "接入凭据已失效，请结束当前接管，等待用户提供新指令"}
                if payload.get('stop'):
                    payload = dict(payload, session_ended=True, next='stop', retryable=False,
                                   instruction='停止所有搜索子任务和后台轮询，退出最外层接管循环；不要自动重连。')
                    if urlparse(self.path).path == '/api/task' and self.command == 'GET':
                        # A terminal control message, not a transient HTTP error.
                        # Never return task:null here: older loops may ignore it.
                        payload['task'] = {'kind': 'stop', 'instruction': payload['instruction']}
                        if code == 409:
                            code = 200
                body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
                try:
                    self.send_response(code)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.send_header("Cache-Control", "no-store")
                    self.end_headers()
                    self.wfile.write(body)
                    self.wfile.flush()
                    return True
                except (OSError, ValueError):
                    return False

            def _request_token(self):
                q = parse_qs(urlparse(self.path).query)
                return (q.get("token") or [""])[0] or self.headers.get("X-RF-Token", "")

            def _authed(self) -> bool:
                return self._request_token() == server.token

            def do_OPTIONS(self):             # noqa: N802
                self.send_response(204)
                self.send_header("Cache-Control", "no-store")
                self.send_header("Access-Control-Allow-Headers", "Content-Type, X-RF-Token")
                self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
                self.end_headers()

            def do_GET(self):                 # noqa: N802
                parsed = urlparse(self.path)
                path = parsed.path
                if path == "/api/ping":
                    self._send(200, {"ok": True, "app": APP_NAME,
                                     "version": APP_VERSION,
                                     "instance": server._task_prefix,
                                     "accepting": not server.paused})
                elif path == "/api/status":
                    if not self._authed():
                        self._send(403, {"ok": False, "error": "需要 token"})
                        return
                    self._send(200, {"ok": True, "version": APP_VERSION,
                                     "instance": server._task_prefix,
                                     "task": server.task_status()})
                elif path == '/api/task/context':
                    q = parse_qs(parsed.query)
                    who = (q.get('agent') or [''])[0]
                    task_id = (q.get('task_id') or [''])[0]
                    with server._task_lock:
                        if not self._authed() or not server.is_owner(who):
                            self._send(403, {'ok': False, 'stop': True});return
                        if not server.accepts(task_id):
                            self._send(409, {'ok': False, 'stale': True, 'next': 'poll'});return
                        task = dict(server.current_task, received=server._task_received,
                            remaining=max(0, int(server.current_task['want'])-server._task_received),
                            exclude=list(server._task_urls), state=server._task_state)
                        if server.access:
                            task['network'] = server.access.for_task(server.current_task)
                        self._send(200, {'ok': True, 'task': task, 'paused': server.paused,
                            'stop': False, 'session_ended': False, 'keep_polling': True})
                elif path == "/api/help":
                    if not self._authed():
                        self._send(403, {"ok": False, "error": "请从界面复制带 token 的接入指令"})
                        return
                    self._send(200, {"ok": True, "how": server.instructions()})
                elif path == "/api/control":
                    if not self._authed():
                        self._send(403, {"ok": False, "error": "需要 token"})
                        return
                    q = parse_qs(parsed.query)
                    who, task_id = (q.get('agent') or [''])[0], (q.get('task_id') or [''])[0]
                    try:
                        wait = float((q.get('wait') or ['0'])[0])
                        if not who or not task_id or not 0 <= wait <= 25:
                            raise ValueError()
                    except ValueError:
                        self._send(400, {"ok": False, "error": "需要 agent、task_id，wait 范围 0–25 秒"})
                        return
                    reply = server.control(who, task_id, wait, session_token=self._request_token(),
                                           observe_only=(q.get('observe') or ['0'])[0]=='1',
                                           budget_revision=int((q.get('budget_revision') or ['-1'])[0]) if (q.get('budget_revision') or [''])[0].isdigit() else None)
                    self._send(409 if reply.get('stop') else 200, reply)
                elif path in ("/api/network/helper", "/api/control/helper", "/api/worker/helper"):
                    if not self._authed():
                        self._send(403, {"ok": False, "error": "需要 token"})
                        return
                    from .agent_client import HELPER_SCRIPT, CONTROL_SCRIPT
                    from .agent_worker import WORKER_SCRIPT
                    control = path == '/api/control/helper'
                    worker = path == '/api/worker/helper'
                    self._send(200, {"ok": True, "filename": "rf_worker.py" if worker else "rf_agent_run.py" if control else "rf_agent_net.py",
                                     "script": WORKER_SCRIPT if worker else CONTROL_SCRIPT if control else HELPER_SCRIPT,
                                     "supports_serve": control, "requires_reusable_worker": control,
                                     "requires_model_capable_worker": control,
                                     "provides_agent_reasoning": False,
                                     "service_failure_stops_session": True,
                                     "default_worker": None})
                elif path == "/api/network/config":
                    q = parse_qs(parsed.query)
                    data = {key: values[0] for key, values in q.items()}
                    self._network(path, data)
                elif path == "/api/task":
                    # 长轮询：Agent 没任务时会挂在这里等着，界面上点搜索就能立刻拿到
                    if not self._authed():
                        self._send(403, {"ok": False,
                                         "error": "token 不对，请用界面里显示的那个地址"})
                        return
                    q = parse_qs(parsed.query)
                    try:
                        wait = float((q.get("wait") or ["20"])[0] or 20)
                    except ValueError:
                        wait = 20.0
                    who = ((q.get("agent") or [""])[0]
                           or self.headers.get("X-RF-Agent", "")).strip()
                    if not who:
                        self._send(400, {"ok": False, "error": "请提供 agent 名称"})
                        return
                    task = server.pop_task(wait, agent=who, session_token=self._request_token())
                    if not self._authed() or not server.is_owner(who):
                        self._send(409, {"ok": False, "stop": True})
                        return
                    sent = self._send(200, {"ok": True, "task": task,
                                     "stop": False, "session_ended": False, "keep_polling": True,
                                     "paused": server.paused,
                                     "connected": True, "next": "poll" if task is None else "handle_task",
                                     "agent": server.connected_agent()})
                    if not sent and task and task.get('delivery_attempt'):
                        server.delivery_failed(task["id"], task["delivery_attempt"])
                else:
                    self._send(404, {"ok": False, "error": "未知接口"})

            def do_POST(self):                # noqa: N802
                path = urlparse(self.path).path
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    if length < 0 or length > 8 * 1024 * 1024:
                        self._send(413, {"ok": False, "error": "请求体上限为 8 MB"})
                        return
                    self.connection.settimeout(15)
                    raw = self.rfile.read(length) if length else b"{}"
                except (ValueError, OSError):
                    raw = b"{}"
                # 先把请求体读完再判 token：否则客户端还在发数据时被 403 打断，
                # Windows 上会报 ConnectionAbortedError(10053)。
                if not self._authed():
                    self._send(403, {"ok": False,
                                     "error": "token 不对，请用界面里显示的那个地址"})
                    return
                try:
                    data = json.loads(raw.decode("utf-8", "ignore") or "{}")
                except Exception as exc:      # noqa: BLE001
                    self._send(400, {"ok": False, "error": "JSON 解析失败：%s" % exc})
                    return
                if not isinstance(data, dict):
                    self._send(400, {"ok": False, "error": "请求必须是 JSON 对象"})
                    return
                who = (str(data.get("agent") or "")
                       or self.headers.get("X-RF-Agent", "")).strip()
                if path == '/api/connect':
                    with server._task_lock:
                        if not self._authed():
                            self._send(403, {'ok': False})
                            return
                        if not server.access or not server.is_owner(who):
                            self._send(409, {'ok': False, 'error': '请先 GET task 领取当前握手'})
                            return
                        try:
                            self._send(200, server.access.confirm(data))
                        except ValueError as exc:
                            self._send(409, {'ok': False, 'stale': True, 'error': str(exc)})
                    return
                if path.startswith('/api/network/'):
                    self._network(path, data)
                    return
                if path == '/api/activity/runner':
                    with server._task_lock:
                        task_id = str(data.get('task_id') or '')
                        if not self._authed() or not server.is_owner(who):
                            self._send(403, {'ok': False, 'stop': True});return
                        if not server.accepts(task_id) or server.paused or server._task_cancel.is_set():
                            self._send(409, {'ok': False, 'stale': True, 'cancelled': True});return
                        if not server.session_ready():
                            self._send(428, {'ok': False, 'error': '请先完成接管握手'});return
                        if data.get('runner_id'):
                            server.runners.remove(str(data['runner_id']), task_id)
                            self._send(200, {'ok': True});return
                        try:
                            if data.get('platform',os.name)!=os.name:
                                raise ValueError('不支持跨系统PID检测，请使用控制心跳')
                            if data.get('platform') and (data.get('runner_created') is None or data.get('child_created') is None):
                                raise ValueError('缺少进程创建标识，请使用控制心跳')
                            key = server.runners.register(task_id, data.get('runner_pid'), data.get('child_pid'),
                                data.get('runner_created'),data.get('child_created'))
                            server.touch(who);server.acknowledge(task_id, activity=True)
                            self._send(200, {'ok': True, 'runner_id': key, 'check_seconds': 2})
                        except ValueError as exc:self._send(400, {'ok': False, 'error': str(exc)})
                    return
                with server._task_lock:
                    if not self._authed():
                        self._send(403, {'ok': False})
                        return
                    if who and (not server.owner or server.is_owner(who)):
                        server.touch(who)
                    if who and not server.is_owner(who) and path in (
                            "/api/results", "/api/progress", "/api/error", "/api/ack", "/api/clear"):
                        self._send(409, {"ok": False, "stop": True,
                                         "error": "已被 %s 接管，请停止" % server.owner})
                        return

                    if path in ("/api/results", "/api/progress", "/api/error", "/api/ack"):
                        if server.paused:
                            self._send(200, {"ok": True, "paused": True, "accepted": 0, "ignored": True})
                            return
                        task_id = str(data.get("task_id") or "")
                        if not server.accepts(task_id, str(data.get("query") or "")):
                            self._send(409, {"ok": False, "stale": True,
                                             "error": "任务已失效，请重新取任务"})
                            return
                        if server._task_state in ('completed', 'failed') and path != '/api/ack':
                            self._send(200, {'ok': True, 'accepted': 0, 'ignored': True, 'done': True,
                                             'stop': False, 'session_ended': False, 'keep_polling': True, 'next': 'poll', 'stop_scope': 'task',
                                             'cancelled': True, 'limit_reached': server._task_received >= int(server.current_task['want'])})
                            return
                        if who:
                            server.touch(who)
                        elif server.owner:
                            server.touch(server.owner)

                        if server.access:
                            with server._task_lock:
                                if not server.access.ready():
                                    self._send(428, {'ok': False, 'error': '请先完成 connect 握手并再次轮询'})
                                    return
                                if path == '/api/results' and server.access.pending():
                                    self._send(428, {'ok': False, 'error': '请先完成 network 检测',
                                                     'network': server.access.snapshot()})
                                    return

                    if path == "/api/ack":
                        ok = server.acknowledge(task_id)
                        self._send(200 if ok else 409, {"ok": ok, "acknowledged": ok,
                                                       "task_id": task_id})
                    elif path == "/api/error":
                        info = {"task_id": str(data.get("task_id") or ""),
                                "agent": who,
                                "error": str(data.get("error") or data.get("message")
                                             or "Agent 没有说明原因")}
                        try:
                            if server.access:
                                server.access.cache.clear()
                            server.acknowledge(task_id, activity=True)
                            server.report_error(info)
                            server.finish_task(task_id, failed=True)
                        except Exception as exc:      # noqa: BLE001
                            self._send(500, {"ok": False, "error": str(exc)})
                            return
                        self._send(200, {"ok": True, "reported": True})
                    elif path == "/api/progress":
                        info = {"task_id": str(data.get("task_id") or ""),
                                "agent": who,
                                "stage": str(data.get("stage") or ""),
                                "message": str(data.get("message") or "")}
                        try:
                            server.acknowledge(task_id, activity=True)
                            server.report_status(info)
                        except Exception:     # noqa: BLE001
                            pass
                        self._send(200, {"ok": True})
                    elif path == "/api/results":
                        if server.paused:
                            # 暂停期间的结果直接丢掉，别让界面继续跳
                            self._send(200, {"ok": True, "accepted": 0,
                                             "paused": True, "ignored": True})
                            return
                        items = data.get("results") or data.get("items") or []
                        if isinstance(items, dict):
                            items = [items]
                        if not isinstance(items, list) or not isinstance(data.get("done", False), bool):
                            self._send(400, {"ok": False, "error": "results 必须为数组，done 必须为布尔值"})
                            return
                        exhausted = data.get('exhausted', False)
                        message = str(data.get('message') or '').strip()[:2000]
                        if not isinstance(exhausted, bool) or (exhausted and (not data.get('done') or not message)):
                            self._send(400, {'ok': False, 'error': 'exhausted 必须为布尔值；报告资源不足须同时提供 done:true 和具体 message'})
                            return
                        if 'candidate_ids' in data:
                            try:
                                if not server.access:
                                    raise ValueError('当前连接不支持候选编号')
                                items = items + server.access.resolve_candidates(task_id, data['candidate_ids'])
                            except ValueError as exc:
                                self._send(400, {'ok': False, 'error': str(exc)})
                                return
                        parsed = [r for r in (_to_result(x) for x in items if isinstance(x, dict)) if r]
                        source_keys = server.current_task.get('filters', {}).get('source_keys')
                        if source_keys is not None:
                            from .sources import result_groups
                            sites = server.access.config.sites if server.access else []
                            parsed = [r for r in parsed if set(result_groups(r, sites)) & set(source_keys)]
                        if server.access:
                            from .sources import result_groups, site_group
                            login_states = server.access.logins.snapshot(task_id)
                            skipped = {site_group(s)[0] for s in server.access.config.sites
                                       if login_states.get(s.id, {}).get('state') == 'skipped'}
                            parsed = [r for r in parsed if not (set(result_groups(r, server.access.config.sites)) & skipped)]
                        filters = server.current_task.get('filters') or {}
                        kind = filters.get('resource_type', 'video')
                        parsed = [r for r in parsed if r.resource_type == kind]
                        span = {'24h': 86400, '7d': 604800, '30d': 2592000, '1y': 31536000}.get(filters.get('time'), 0)
                        try:
                            min_views = max(0, int(filters.get('min_views') or 0)) if kind == 'video' else 0
                        except (TypeError, ValueError):
                            min_views = 0
                        now = time.time()
                        parsed = [r for r in parsed if r.views >= min_views
                                  and (not span or r.pub_ts and 0 <= now - r.pub_ts <= span)
                                  and (not server.access or not server.access.config.match_blacklist(
                                      ' '.join((r.title, r.desc, r.url, r.author))))]
                        unique = []
                        for row in parsed:
                            key = normalize_url(row.url) or row.url
                            if key not in server._task_urls and server._task_received < int(server.current_task['want']):
                                server._task_urls.add(key)
                                server._task_received += 1
                                unique.append(row)
                        parsed = unique
                        server._task_result_rows.extend(parsed)
                        limit_reached = server._task_received >= int(server.current_task['want'])
                        query = str(data.get("query") or "")
                        awaiting_login = bool(server.access and any(r['state'] in ('waiting', 'verifying')
                            for r in server.access.logins.snapshot(task_id).values()))
                        completion = {}
                        search_limited = False
                        search_limits = []
                        if data.get('done') and not limit_reached and not awaiting_login and server.access:
                            review = server.access.search_evidence.review(server.current_task, server.access.sources(),
                                server.access.logins.snapshot(task_id))
                            review['inflight'] = any(key[0] == task_id for key in server._inflight_network)
                            if review['inflight']:
                                review['verified'] = False
                                review['can_finish'] = False
                            search_limited = review['can_finish'] and review['limited']
                            search_limits = review['limits']
                            if not review['can_finish'] and (exhausted or review['observed'] or review['inflight']
                                    or (server._task_received == 0 and bool(server.access.sources()))):
                                completion = dict(review, pending=True, redeliver=True,
                                    reason='unverified_exhaustion' if exhausted else 'target_not_reached',
                                    message='本批尚未完成：已接收 %d/%d 条。搜索结束尚未核验，任务和结果已保留，等待 Agent 继续。'
                                    % (server._task_received, int(server.current_task['want'])),
                                    instruction='保持当前task_id和已交付计数，按next_actions继续；不要结束对话或只启动无法调用模型的监听器。')
                                server.current_task['completion'] = completion
                        meta = {"task_id": str(data.get("task_id") or ""),
                                "done": (bool(data.get("done")) and not awaiting_login and not completion) or limit_reached,
                                "received": server._task_received, "limit_reached": limit_reached,
                                "exhausted": exhausted and not completion and not search_limited, "message": message,
                                "search_limited": search_limited, "search_limits": search_limits,
                                "completion": completion,
                                "count": len(parsed),
                                "agent": who}
                        try:
                            server.acknowledge(task_id, activity=True)
                            if parsed:
                                server._metrics.setdefault('first_result_at', time.monotonic())
                            server._on_results(parsed, query, meta)
                            if meta["done"]:
                                server.finish_task(task_id)
                        except Exception as exc:  # noqa: BLE001
                            self._send(500, {"ok": False, "error": str(exc)})
                            return
                        self._send(200, {"ok": True, "accepted": len(parsed), 'done': meta['done'],
                                         'stop': False, 'session_ended': False, 'keep_polling': True,
                                         'stop_scope': 'task' if meta['done'] else None,
                                         'budget': dict(server.current_task.get('budget') or {}),
                                         'exhausted': meta['exhausted'], 'message': message,
                                         'search_limited': search_limited, 'search_limits': search_limits,
                                         'completion_pending': bool(completion), 'completion': completion,
                                         'next': 'continue_current_task' if completion else 'poll' if meta['done'] else 'continue',
                                         'received': server._task_received, 'limit_reached': limit_reached,
                                         'remaining': max(0, int(server.current_task['want']) - server._task_received)})
                    elif path == "/api/clear":
                        if not server.accepts(str(data.get("task_id") or "")) or server.paused:
                            self._send(409, {"ok": False, "stale": True})
                            return
                        if server._on_clear:
                            server._on_clear({"task_id": str(data.get("task_id") or "")})
                        self._send(200, {"ok": True})
                    else:
                        self._send(404, {"ok": False, "error": "未知接口"})

            def _network(self, path, data):
                if not self._authed():
                    self._send(403, {'ok': False, 'error': '需要 token'})
                    return
                who, task_id = str(data.get('agent') or ''), str(data.get('task_id') or '')
                with server._task_lock:
                    if not self._authed():
                        self._send(403, {'ok': False})
                        return
                    access = server.access
                    if not access or not server.is_owner(who):
                        self._send(409, {'ok': False, 'stop': True, 'error': '不是当前接管者'})
                        return
                    if server.paused or not server.accepts(task_id) or server._task_state in ('completed', 'failed'):
                        self._send(409, {'ok': False, 'stale': True, 'error': '已暂停或任务失效'})
                        return
                    # A valid late request can itself report renewed activity.
                    # Heartbeat freshness is UI evidence, not authorization TTL.
                    if not server.session_ready():
                        self._send(428, {'ok': False, 'error': '请先完成接管'})
                        return
                    access.for_task(server.current_task)
                    check_id = access.plan['check_id']
                    if path == '/api/network/login':
                        site = next((s for s in access.sources() if s.id == data.get('source_id')), None)
                        if not site or data.get('code') not in ('login_required', 'age_verification_required'):
                            self._send(400, {'ok': False, 'error': '需要本轮有效 source_id 和明确的登录限制 code'})
                            return
                        row = access.logins.require(task_id, site,
                            str(data.get('query') or server.current_task['query'])[:300], 1,
                            '该平台要求登录或确认年龄；请在官方页面完成验证。', server.cancellation(task_id))
                        self._send(200, {'ok': False, 'login_required': True, 'login': row, 'next': 'other_sources_then_control'})
                        return
                    if path == '/api/network/config':
                        payload = access.config_payload()
                        payload['schema_version'] = 1
                        payload['sources'] = [{'id': s.id, 'name': s.display, 'route': s.route,
                                               'scope_domain': s.scope_domain, 'kind': s.kind,
                                               'search_url': s.search_url} for s in access.sources()]
                        self._send(200, payload)
                        return
                    if path == '/api/network/require':
                        targets = data.get('targets')
                        if not isinstance(targets, list) or not targets or len(targets) > 6:
                            self._send(400, {'ok': False, 'error': 'targets 必须是 1~6 个目标 URL'})
                            return
                        from .agent_access import external_targets
                        targets = external_targets(' '.join(str(u) for u in targets), [])
                        if not targets:
                            self._send(400, {'ok': False, 'error': '没有可检测的公开外网目标'})
                            return
                        merged = list(dict.fromkeys(access.plan['targets'] + targets))
                        if len(merged) > 6:
                            self._send(400, {'ok': False, 'error': '每任务最多检测 6 个外网目标'})
                            return
                        self._send(200, {'ok': True, 'network': access.prepare(server.current_task, merged)})
                        return
                    if path == '/api/network/report':
                        try:
                            usable = access.report(data)
                        except ValueError as exc:
                            self._send(400, {'ok': False, 'error': str(exc)})
                            return
                        server.acknowledge(task_id, activity=True)
                        if usable:
                            server._notify_task('network_ready', 'Agent 网络检测通过，开始搜索')
                        else:
                            server.report_error({'task_id': task_id, 'agent': who,
                                'error': '该 Agent 本次无法访问所需外网目标。\n\n' + access.last_report})
                            server.finish_task(task_id, failed=True)
                        self._send(200, {'ok': True, 'usable': usable, 'network': access.snapshot(),
                                         'report': access.last_report})
                        return
                    if path not in ('/api/network/probe', '/api/network/fetch', '/api/network/search'):
                        self._send(404, {'ok': False, 'error': '未知网络接口'})
                        return
                    if path == '/api/network/search':
                        login = access.logins.snapshot(task_id).get(str(data.get('source_id') or ''))
                        if login and login['state'] != 'ready':
                            self._send(200, {'ok': False, 'login_required': login['state'] != 'skipped',
                                'skipped': login['state'] == 'skipped', 'login': login,
                                'next': 'other_sources_then_control'})
                            return
                    probe = path == '/api/network/probe'
                    url = str(data.get('url') or '')
                    source_keys = server.current_task.get('filters', {}).get('source_keys')
                    if path == '/api/network/fetch' and source_keys is not None:
                        from .sources import result_group
                        if (result_group(SearchResult(title='fetch', url=url), access.config.sites)[0] not in source_keys
                                and not access.candidate_allows_fetch(task_id, url, source_keys)):
                            self._send(400, {'ok': False, 'error': '此网址不在用户本轮勾选的平台范围内'})
                            return
                    if probe and url not in access.plan['targets']:
                        self._send(400, {'ok': False, 'error': '不是本次检测目标'})
                        return
                    if not probe and access.pending():
                        self._send(428, {'ok': False, 'error': '先完成网络检测'})
                        return
                    server.acknowledge(task_id, activity=True)
                    server._notify_task('network_check' if probe else 'searching',
                                        '正在验证工具网络中转' if probe else 'Agent 正在使用工具网络通道')
                    cancel = server.cancellation(task_id)
                # Never hold the task lock during external IO: pause/takeover remain responsive.
                io_started = time.monotonic()
                with server._task_lock:
                    server._inflight_network[(task_id, io_started)] = io_started + access.config.settings.timeout + 30
                try:
                    result = access.search(data, cancel=cancel) if path.endswith('/search') else access.fetch(url, probe=probe, cancel=cancel)
                except Exception as exc:
                    if cancel.is_set() or server.paused or not server.is_owner(who) or not server.accepts(task_id):
                        self._send(409, {'ok': False, 'stale': True, 'error': '请求期间任务已失效'})
                        return
                    from .login import LoginRequired
                    if isinstance(exc, LoginRequired):
                        site = next((s for s in access.sources() if s.id == data.get('source_id')), None)
                        if site:
                            row = access.logins.require(task_id, site, str(data.get('query') or '')[:300],
                                int(data.get('page') or 1), exc, cancel)
                            self._send(200, {'ok': False, 'login_required': True, 'code': exc.code,
                                'login': row, 'next': 'other_sources_then_control'})
                            return
                    self._send(502, {'ok': False, 'error': type(exc).__name__ + ': ' + str(exc)[:2000]})
                    return
                finally:
                    with server._task_lock:
                        server._inflight_network.pop((task_id, io_started), None)
                    server.record_network(task_id, io_started)
                with server._task_lock:
                    if (cancel.is_set() or server.paused or not server.is_owner(who) or not server.accepts(task_id)
                            or access.plan.get('check_id') != check_id):
                        self._send(409, {'ok': False, 'stale': True, 'error': '请求期间任务已失效'})
                        return
                    if probe:
                        receipt = secrets.token_hex(16)
                        access.receipts[receipt] = (check_id, url)
                        result = {'ok': True, 'status': result['status'], 'receipt': receipt}
                    if path.endswith('/search'):
                        if not (result.get('pagination') or {}).get('limited'):
                            access.search_evidence.record(task_id, data, result)
                        limited = access.search_evidence.limit(data)
                        if limited:
                            result.setdefault('pagination', {}).update(limited=True, limit_reason=limited)
                            result['note'] = '本轮该检索已停止推进。保留本页候选，处理其他未检查来源后提交done:true；不要继续请求该查询，不代表网站已无资源。'
                        server.current_task['completion'] = {}
                        access.remember_candidates(task_id, result.get('results', []))
                        if data.get('compact') is True:
                            fields = ('candidate_id', 'title', 'url', 'resource_type', 'source', 'author', 'duration', 'views', 'pub_ts', 'desc', 'extra')
                            result['results'] = [{k: r[k] for k in fields if k in r} for r in result['results']]
                    result['elapsed_ms'] = round(1000 * (time.monotonic() - io_started))
                    self._send(200, result)

        return Handler


def free_port(start: int = 8765) -> int:
    """给界面显示用的端口探测（真正绑定在 AgentServer.start 里做）。"""
    for port in range(start, start + 12):
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as s:
            try:
                s.bind(("127.0.0.1", port))
                return port
            except OSError:
                continue
    return start
