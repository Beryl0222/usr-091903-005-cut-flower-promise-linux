"""仅追加事件存储。

所有状态变化都以事件落盘，当前状态随时可由事件流完整重放——
这是"管理层回放订单完整决定过程"的基础。
"""

import json
import os
import threading


class EventStore:
    def __init__(self, path=None):
        self._path = path
        self._events = []
        self._lock = threading.RLock()
        if path and os.path.exists(path):
            with open(path, "r", encoding="utf-8") as handle:
                for line in handle:
                    line = line.strip()
                    if line:
                        self._events.append(json.loads(line))

    @property
    def lock(self):
        return self._lock

    def append(self, event_type, payload, at, actor=None, seq=None):
        """追加一条事件。seq 缺省时自增；at 为事件发生时间。"""
        with self._lock:
            event = {
                "seq": seq if seq is not None else len(self._events) + 1,
                "type": event_type,
                "at": at,
                "actor": actor,
                "payload": payload,
            }
            self._events.append(event)
            if self._path:
                os.makedirs(os.path.dirname(os.path.abspath(self._path)), exist_ok=True)
                with open(self._path, "a", encoding="utf-8") as handle:
                    handle.write(json.dumps(event, ensure_ascii=False) + "\n")
            return event

    def all(self):
        with self._lock:
            return list(self._events)

    def since(self, seq):
        with self._lock:
            return [e for e in self._events if e["seq"] > seq]

    def reset(self):
        with self._lock:
            self._events = []
            if self._path and os.path.exists(self._path):
                os.remove(self._path)
