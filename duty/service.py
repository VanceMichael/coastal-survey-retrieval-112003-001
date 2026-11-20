"""值班服务：同步轮次、领取原件、判定覆盖、安排船次、回答班组查询。"""
import json

from . import coverage as cov
from .booking import BookingManager
from .client import SourceClient
from .fetch import FetchManager
from .store import Store, log_event


class DutyService:
    def __init__(self, source, state_path):
        self.client = SourceClient(source)
        self.store = Store(state_path)
        self.state = self.store.data
        self.orders = None
        self.cards = []
        self.handoffs = []
        self.directory = self.state.get("directory")
        self.sailing = None
        self.fetchman = FetchManager(self)
        self.bookman = BookingManager(self)
        self.refresh_reference()
        self.current_coverage = self.compute_coverage(self.state["round"])

    # ---- 参考数据 ----
    def refresh_reference(self):
        code, orders = self.client.get("/data/orders.json")
        if code == 200:
            self.orders = orders
        code, cards = self.client.get("/data/calibration.json")
        if code == 200:
            self.cards = cards
        code, handoffs = self.client.get("/data/handoffs.json")
        if code == 200:
            self.handoffs = handoffs
        if self.orders is None:
            raise RuntimeError("无法取得工程单，值班服务不能启动")

    def segment(self, sid):
        return next((s for s in self.orders["segments"] if s["id"] == sid), None)

    def boats(self):
        return self.bookman.boats()

    def is_rejected(self, key):
        return any(r["kind"] == "original" and r["value"] == key
                   for r in self.state["rejections"])

    def is_line_rejected(self, line):
        return any(r["kind"] == "line" and r["value"] == line
                   for r in self.state["rejections"])

    # ---- 主循环 ----
    def tick(self):
        try:
            return self._tick()
        except OSError:
            return {"error": "source_unreachable", "round": self.state["round"]}

    def _tick(self):
        st = self.state
        code, root = self.client.get("/")
        src_round = root["round"]
        prev_round = st["round"]
        if src_round < prev_round:
            log_event(st, prev_round, "round_backward", source=src_round,
                      detail="来源轮次倒退，保持本地轮次")
        else:
            st["round"] = src_round
        round_no = st["round"]
        self.refresh_reference()
        triggers = self._apply_handoffs(round_no)
        lines_before = cov.current_lines(self.orders, prev_round)
        lines_now = cov.current_lines(self.orders, round_no)
        for sid, line in lines_now.items():
            if lines_before.get(sid) != line:
                triggers.append(f"线路修订生效：{sid} 改用 {line}")
        for card in self.cards:
            if prev_round < card["round"] <= round_no:
                triggers.append(f"校准补交：{card['card']} 第{card['round']}轮可知")
        code, directory = self.client.get("/directory")
        self.directory = directory
        st["directory"] = directory
        self.fetchman.mark_superseded(directory, round_no, triggers)
        cov_pre = self.compute_coverage(round_no)
        self._diff_coverage(self.current_coverage, cov_pre, round_no, triggers)
        self.current_coverage = cov_pre
        self.bookman.sync(round_no)
        reads = self.fetchman.execute(directory, round_no)
        cov_post = self.compute_coverage(round_no)
        assembled = [a["assembled"] for a in reads if a.get("assembled")]
        self._diff_coverage(cov_pre, cov_post, round_no,
                            [f"领取完成 {key}" for key in assembled])
        self.current_coverage = cov_post
        bookings = self.bookman.act(round_no)
        self._evaluate_missed(round_no)
        self.store.save()
        return {"round": round_no, "reads": reads, "bookings": bookings,
                "coverage": self.coverage_summary(), "missed": st["missed"],
                "unmet": st["unmet"]}

    def _apply_handoffs(self, round_no):
        st = self.state
        triggers = []
        for handoff in self.handoffs:
            if handoff["round"] > round_no:
                continue
            ident = json.dumps(handoff, sort_keys=True)
            if handoff["action"] == "withdraw_observation":
                if any(w["id"] == ident for w in st["withdrawn"]):
                    continue
                st["withdrawn"].append({"id": ident, "round": handoff["round"],
                                        "page": handoff["page"],
                                        "version": handoff["version"],
                                        "indexes": handoff["indexes"]})
                log_event(st, round_no, "withdraw", page=handoff["page"],
                          version=handoff["version"], indexes=handoff["indexes"])
                triggers.append(
                    f"工程撤回 {handoff['page']}@{handoff['version']} 行{handoff['indexes']}")
            elif handoff["action"] == "field_evidence":
                if any(f["id"] == ident for f in st["field"]):
                    continue
                code, body = self.client.get(handoff["file"])
                if code != 200:
                    continue
                st["field"].append({"id": ident, "round": handoff["round"],
                                    "trip": handoff.get("trip"),
                                    "receipt": body.get("receipt"),
                                    "line": body.get("line"),
                                    "points": body.get("points", [])})
                log_event(st, round_no, "field_evidence",
                          receipt=body.get("receipt"), points=body.get("points"))
                triggers.append(f"现场补证 {body.get('receipt')}")
        return triggers

    # ---- 覆盖 ----
    def compute_coverage(self, round_no):
        st = self.state
        valid = [orig for key, orig in st["originals"].items()
                 if not orig.get("superseded") and not self.is_rejected(key)
                 and not self.is_line_rejected(orig["content"].get("line"))]
        withdrawn = {(w["page"], w["version"], i)
                     for w in st["withdrawn"] for i in w["indexes"]}
        return cov.compute_coverage(self.orders, self.cards, valid, withdrawn,
                                    st["field"], round_no)

    @staticmethod
    def _evidence_id(item):
        if item["kind"] == "original":
            return f'{item["original"]}#{item["row"]}'
        return f'field:{item.get("receipt")}'

    def _diff_coverage(self, before, after, round_no, triggers):
        if not before or not triggers:
            return
        st = self.state
        for sid, seg_cov in after.items():
            for point, cats in seg_cov["points"].items():
                for cat, evs in cats.items():
                    old = {self._evidence_id(e)
                           for e in before.get(sid, {}).get("points", {})
                           .get(point, {}).get(cat, [])}
                    new = {self._evidence_id(e) for e in evs}
                    for gone in sorted(old - new):
                        log_event(st, round_no, "coverage_lost", segment=sid,
                                  point=point, category=cat, evidence=gone,
                                  causes=triggers)
                    for got in sorted(new - old):
                        log_event(st, round_no, "coverage_gained", segment=sid,
                                  point=point, category=cat, evidence=got,
                                  causes=triggers)

    def projected_missing_points(self, seg, upto_round):
        """预测到某轮为止仍缺的格点：已保全证据 + 能完整取回的页按粗范围预估。"""
        st = self.state
        sid = seg["id"]
        lines = cov.current_lines(self.orders, upto_round)
        line = lines.get(sid)
        valid = [orig for key, orig in st["originals"].items()
                 if not orig.get("superseded") and not self.is_rejected(key)
                 and not self.is_line_rejected(orig["content"].get("line"))]
        withdrawn = {(w["page"], w["version"], i)
                     for w in st["withdrawn"] for i in w["indexes"]}
        projected = cov.compute_coverage(self.orders, self.cards, valid, withdrawn,
                                         [], upto_round)
        missing = {(p, cat) for p in cov.missing_points(seg, projected[sid])
                   for cat in cov.missing_categories(seg, projected[sid], p)}
        if self.directory:
            for key, entry in self.fetchman.newest_entries(self.directory).items():
                if key in st["originals"] or key in st["conflicts"]:
                    continue
                if entry["device"] not in seg["required"] or entry["line"] != line:
                    continue
                if not cov.card_known_ever(self.cards, entry["epoch"],
                                           entry["pass"], line):
                    continue
                if not self.fetchman.completable_by(entry, upto_round, st["round"]):
                    continue
                for p in seg["points"]:
                    if entry["rough"][0] - cov.TOL <= p <= entry["rough"][1] + cov.TOL:
                        missing.discard((p, entry["device"]))
        return sorted({p for p, _ in missing})

    def late_note(self, key, round_no):
        """已保全但来不及参与当前安排的原件：留下并说明可能影响哪个后续安排。"""
        st = self.state
        orig = st["originals"][key]
        lines = cov.current_lines(self.orders, round_no)
        for seg in self.orders["segments"]:
            sid = seg["id"]
            if orig["device"] not in seg["required"] or orig["line"] != lines.get(sid):
                continue
            boats = [(name, b) for name, b in self.boats().items() if sid in b["spans"]]
            if not boats or not all(round_no >= b["locks"] for _, b in boats):
                continue
            affects = []
            for name, b in boats:
                if round_no < b["departs"]:
                    affects.append(f"{name} 可在第{b['departs']}轮出发前提交变更申请")
                else:
                    affects.append(f"{name} 已出发")
            affects.append("交班覆盖评估")
            note = {"round": round_no, "original": key, "segment": sid,
                    "reason": "保全时已过自由调整窗", "affects": affects}
            st["notes"].append(note)
            log_event(st, round_no, "late_original", original=key, segment=sid)

    # ---- 质检 ----
    def qc(self, payload):
        st = self.state
        round_no = st["round"]
        undo = bool(payload.get("undo"))
        if "original" in payload:
            target = ("original", payload["original"])
        elif "line" in payload:
            target = ("line", payload["line"])
        else:
            return {"error": "bad_request"}
        if undo:
            st["rejections"] = [r for r in st["rejections"]
                                if (r["kind"], r["value"]) != target]
        else:
            st["rejections"].append({"kind": target[0], "value": target[1],
                                     "round": round_no})
        log_event(st, round_no, "qc_undo" if undo else "qc_reject",
                  kind=target[0], value=target[1])
        after = self.compute_coverage(round_no)
        self._diff_coverage(self.current_coverage, after, round_no,
                            [f"质检{'撤销' if undo else '否定'} {target[1]}"])
        self.current_coverage = after
        replan = self.replan_hints()
        self.store.save()
        return {"coverage": self.coverage_summary(), "replan": replan}

    def replan_hints(self):
        """指出已领资料改变后的缺口，以及哪些还没取的页值得改排。"""
        hints = {}
        for seg in self.orders["segments"]:
            sid = seg["id"]
            if cov.missing_points(seg, self.current_coverage[sid]):
                paths = self.fetchman.future_paths(seg, self.current_coverage,
                                                   self.state["round"])
                hints[sid] = {"missing": cov.missing_points(seg, self.current_coverage[sid]),
                              "fetchable": paths}
        return hints

    # ---- 错过窗口 ----
    def _evaluate_missed(self, round_no):
        st = self.state
        for seg in self.orders["segments"]:
            sid = seg["id"]
            missing = cov.missing_points(seg, self.current_coverage[sid])
            record = st["missed"].get(sid)
            if not missing:
                if record and record.get("resolved") is None:
                    record["resolved"] = round_no
                continue
            if self.fetchman.future_paths(seg, self.current_coverage, round_no):
                continue  # 还有可领取的原件路径
            if self.bookman.segment_booked(sid):
                continue  # 已有船次会去核
            bookable = any(sid in b["spans"] and round_no < b["locks"]
                           and round_no <= seg["last_dispatch"]
                           for b in self.boats().values())
            if bookable:
                continue
            cats = {str(p): cov.missing_categories(seg, self.current_coverage[sid], p)
                    for p in missing}
            parts = [f"格点缺证 {cats}", "无剩余原件路径"]
            for name, b in self.boats().items():
                if sid not in b["spans"]:
                    continue
                mirror = st["bookings"].get(self.bookman.booking_id(name))
                state = (mirror or {}).get("state") or "未预约"
                parts.append(f"{name}（锁{b['locks']}/发{b['departs']}）: {state}")
            parts.append(f"末班派工第{seg['last_dispatch']}轮")
            reason = "；".join(parts)
            if not record or record.get("missing") != cats or record.get("resolved"):
                st["missed"][sid] = {"segment": sid, "round": round_no,
                                     "missing": cats, "reason": reason,
                                     "resolved": None}
                log_event(st, round_no, "missed", segment=sid, missing=cats,
                          reason=reason)

    # ---- 查询 ----
    def info(self):
        return {"service": "duty", "round": self.state["round"],
                "source": self.client.base,
                "paths": ["/status", "/sections", "/sections/{id}", "/trips",
                          "/trips/{slot}", "/originals", "/audit/{id}", "/missed",
                          "/notes", "/log", "/tick", "/qc"]}

    def coverage_summary(self):
        out = {}
        for seg in self.orders["segments"]:
            sid = seg["id"]
            missing = cov.missing_points(seg, self.current_coverage[sid])
            out[sid] = {"line": self.current_coverage[sid]["line"],
                        "points": len(seg["points"]),
                        "covered": len(seg["points"]) - len(missing),
                        "missing": missing, "demand": len(missing)}
        return out

    def status(self):
        return {"round": self.state["round"],
                "links": (self.directory or {}).get("links", {}),
                "segments": self.coverage_summary(),
                "bookings": self.trips(),
                "pending_requests": [rid for rid, r in self.state["requests"].items()
                                     if not r.get("final")]}

    def sections(self):
        return {sid: summary for sid, summary in self.coverage_summary().items()}

    def section(self, sid):
        seg = self.segment(sid)
        if seg is None:
            return None
        seg_cov = self.current_coverage[sid]
        points = []
        for p in seg["points"]:
            ev = seg_cov["points"][str(p)]
            points.append({
                "point": p,
                "status": "covered" if all(ev.get(c) for c in seg["required"])
                          else "missing",
                "evidence": ev,
                "pending": [r for r in seg_cov["pending"] if p in r["points"]]})
        return {"segment": sid, "line": seg_cov["line"], "span": seg["span"],
                "required": seg["required"], "priority": seg["priority"],
                "last_dispatch": seg["last_dispatch"], "points": points,
                "missing": cov.missing_points(seg, seg_cov),
                "awaiting_pages": self.fetchman.future_paths(seg, self.current_coverage,
                                                             self.state["round"]),
                "bookings": [b for b in self.trips() if sid in b["spans"]],
                "missed": self.state["missed"].get(sid),
                "notes": [n for n in self.state["notes"] if n["segment"] == sid]}

    def trips(self):
        out = []
        for bid, mirror in sorted(self.state["bookings"].items()):
            out.append({"booking": bid, "slot": mirror["slot"],
                        "spans": mirror.get("spans", []),
                        "demand": mirror.get("demand", 0),
                        "state": mirror.get("state"),
                        "pending": mirror.get("pending"),
                        "handover": mirror.get("handover") is not None})
        return out

    def trip(self, slot):
        boat = self.boats().get(slot)
        bookings = [b for b in self.trips() if b["slot"] == slot]
        if boat is None and not bookings:
            return None
        return {"slot": slot, "boat": boat, "bookings": bookings,
                "requests": {rid: {k: v for k, v in r.items() if k != "body" or True}
                             for rid, r in self.state["requests"].items()
                             if r["body"]["slot"] == slot},
                "handovers": [h for h in self.state["handovers"] if h["slot"] == slot]}

    def originals(self):
        st = self.state
        out = {}
        for key, orig in st["originals"].items():
            out[key] = {"state": "superseded" if orig.get("superseded") else "complete",
                        "completed": orig["completed"], "links": orig["links"],
                        "reads": orig["reads"], "device": orig["device"],
                        "rejected": self.is_rejected(key)}
        for key, frags in st["fragments"].items():
            if key in out or key in st["conflicts"]:
                continue
            out[key] = {"state": "partial",
                        "held": [[f["start"], f["end"], f["link"]] for f in frags]}
        for key, reason in st["conflicts"].items():
            out[key] = {"state": "conflict", "reason": reason}
        return out

    def audit(self, sid):
        if self.segment(sid) is None:
            return None
        st = self.state
        seg = self.segment(sid)
        seg_cov = self.current_coverage[sid]
        evidence = []
        for p in seg["points"]:
            for cat in seg["required"]:
                for item in seg_cov["points"][str(p)].get(cat, []):
                    entry = {"point": p, "category": cat, **item}
                    if item["kind"] == "original":
                        entry["reads"] = [r for r in st["reads"]
                                          if r["id"] in item.get("reads", [])]
                    evidence.append(entry)
        events = [e for e in st["log"]
                  if e.get("segment") == sid or sid in e.get("segments", [])
                  or sid in (e.get("spans") or [])]
        return {"segment": sid, "line": seg_cov["line"], "evidence": evidence,
                "pending": seg_cov["pending"], "events": events,
                "bookings": [b for b in self.trips() if sid in b["spans"]],
                "handovers": [h for h in st["handovers"] if sid in h["spans"]],
                "missed": st["missed"].get(sid)}

    def missed(self):
        return self.state["missed"]

    def notes(self):
        return self.state["notes"]

    def log(self, segment=None):
        if segment is None:
            return self.state["log"]
        return [e for e in self.state["log"]
                if e.get("segment") == segment
                or segment in e.get("segments", [])
                or segment in (e.get("spans") or [])]
