"""值班服务的持久化状态：关掉再打开要能接着走。"""
import json
import os
from pathlib import Path


def default_state():
    return {
        "round": 0,
        "read_seq": 0,
        "request_seq": 0,
        "fragments": {},     # 原件键 -> 已持有片段列表
        "totals": {},        # 原件键 -> {"total", "sha256"}
        "conflicts": {},     # 原件键 -> 目录与实际分歧说明
        "originals": {},     # 原件键 -> 已凑齐登记的原件
        "reads": [],         # 每次领取尝试（含失败扣量）
        "rejections": [],    # 质检否定 {"kind", "value", "round"}
        "withdrawn": [],     # 已应用的工程撤回
        "field": [],         # 已登记的现场补证
        "bookings": {},      # 本地预约镜像
        "requests": {},      # 已发出的船务请求（含未见回执的）
        "handovers": [],     # 船次出发时的交接快照（冻结）
        "missed": {},        # 区段 -> 错过复核窗的记录
        "notes": [],         # 已保全但来不及参与的原件说明
        "directory": None,   # 最近一次目录
        "unmet": {},         # 船位不足记录（区段 -> 数量）
        "log": [],           # 事件流，供审计
    }


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.data = default_state()
        if self.path.exists():
            loaded = json.loads(self.path.read_text(encoding="utf-8"))
            for key, value in default_state().items():
                loaded.setdefault(key, value)
            self.data = loaded

    def save(self):
        tmp = self.path.with_name(self.path.name + ".tmp")
        tmp.write_text(json.dumps(self.data, ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self.path)


def log_event(_state, _round, _kind, **detail):
    event = {"round": _round, "kind": _kind}
    event.update(detail)
    _state["log"].append(event)
    return event
