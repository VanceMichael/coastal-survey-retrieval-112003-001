"""船次预约、锁定后变更与交接快照。

预约未锁定前随证据变化即时调整，腾出的名额交给仍缺证的区段；
锁定后只能提交变更申请，船班确认前旧安排仍占容量；出发后冻结。
重启时凭请求号对账：既不重复占船，也不从两边都把那一段释放。
"""
from .coverage import current_lines, missing_categories, missing_points
from .store import log_event


class BookingManager:
    def __init__(self, svc):
        self.svc = svc

    def booking_id(self, slot):
        return f'{self.svc.orders["shift"]}-{slot}'

    def boats(self):
        return (self.svc.sailing or {}).get("boats", {})

    def has_booking(self, slot):
        mirror = self.svc.state["bookings"].get(self.booking_id(slot))
        return bool(mirror and mirror.get("spans") and mirror.get("state") != "cancelled")

    def segment_booked(self, sid):
        """区段是否有仍会去核的预约（预约中/已锁定/在航）。"""
        for mirror in self.svc.state["bookings"].values():
            if mirror.get("spans") and sid in mirror["spans"] \
                    and mirror.get("state") in ("reserved", "locked", "sailing"):
                return True
        return False

    # ---- 与船务台对账 ----
    def sync(self, round_no):
        svc = self.svc
        st = svc.state
        code, sailing = svc.client.get("/sailing")
        if code != 200:
            return
        svc.sailing = sailing
        for bid, book in sailing.get("bookings", {}).items():
            mirror = st["bookings"].setdefault(
                bid, {"slot": book["slot"], "spans": [], "demand": 0,
                      "state": None, "pending": None, "in_flight": False, "history": []})
            if mirror.get("state") != book["state"]:
                mirror["history"].append({"round": round_no, "state": book["state"]})
                log_event(st, round_no, "booking_state", booking=bid,
                          state=book["state"], spans=book["spans"])
                if book["state"] in ("sailing", "returned") and mirror.get("pending"):
                    # 出发后变更申请已不可能被确认，按失效收尾
                    mirror["pending"] = None
                    mirror["in_flight"] = False
                    for rid, req in st["requests"].items():
                        if req["booking"] == bid and not req.get("final"):
                            req["final"] = {"state": "expired"}
                    log_event(st, round_no, "change_expired", booking=bid,
                              detail="船次已出发，未决变更申请失效")
            mirror["spans"] = list(book["spans"])
            mirror["demand"] = book["demand"]
            mirror["state"] = book["state"]
            if book["state"] == "sailing" and mirror.get("handover") is None:
                self._snapshot(bid, round_no)
        for rid, req in list(st["requests"].items()):
            if req.get("final"):
                continue
            code, body = svc.client.get(f"/sailing/result?request={rid}")
            if code == 404:
                if round_no > req["sent_round"] + 1:
                    # 回执一直没来：按原样内容重发同一请求号（对方幂等）
                    code2, body2 = svc.client.post("/sailing/request", req["body"])
                    req["sent_round"] = round_no
                    if code2 != 503:
                        self._apply_result(req, body2, round_no)
                continue
            self._apply_result(req, body, round_no)

    def _apply_result(self, req, body, round_no):
        st = self.svc.state
        bid = req["booking"]
        mirror = st["bookings"].setdefault(
            bid, {"slot": req["body"]["slot"], "spans": [], "demand": 0,
                  "state": None, "pending": None, "in_flight": False, "history": []})
        state = body.get("state")
        if state in ("reserved", "cancelled"):
            req["final"] = body
            mirror["in_flight"] = False
            book = body["booking"]
            mirror.update(spans=list(book["spans"]), demand=book["demand"],
                          state=book["state"])
            log_event(st, round_no, "booking_confirmed", booking=bid, state=state,
                      spans=book["spans"], demand=book["demand"])
        elif state == "pending_change":
            req["pending"] = body
            mirror["pending"] = body["proposed"]
            log_event(st, round_no, "pending_change", booking=bid,
                      proposed=body["proposed"],
                      detail="船班确认前旧安排仍占容量")
        elif state in ("changed", "denied"):
            req["final"] = body
            mirror["in_flight"] = False
            mirror["pending"] = None
            book = body["booking"]
            mirror.update(spans=list(book["spans"]), demand=book["demand"],
                          state=book["state"])
            log_event(st, round_no, "booking_decided", booking=bid, state=state,
                      spans=book["spans"], demand=book["demand"])
        else:
            req["final"] = body
            mirror["in_flight"] = False
            log_event(st, round_no, "booking_error", booking=bid, state=state)

    def _snapshot(self, bid, round_no):
        """出发时冻结交接记录：原单、复核提示与当时已知的修订和校准。"""
        svc = self.svc
        st = svc.state
        mirror = st["bookings"][bid]
        needs = {}
        for sid in mirror["spans"]:
            seg = svc.segment(sid)
            cov = svc.current_coverage[sid]
            needs[sid] = [{"point": p,
                           "missing": missing_categories(seg, cov, p)}
                          for p in missing_points(seg, cov)]
        st["handovers"].append({
            "booking": bid, "slot": mirror["slot"], "spans": list(mirror["spans"]),
            "demand": mirror["demand"], "round": round_no, "needs": needs,
            "lines": current_lines(svc.orders, round_no),
            "cards": [c["card"] for c in svc.cards if c["round"] <= round_no]})
        mirror["handover"] = len(st["handovers"]) - 1
        log_event(st, round_no, "handover", booking=bid, spans=mirror["spans"],
                  needs=needs)

    # ---- 排船 ----
    def desired(self, round_no):
        """按工程单安全级别、再按最早失去机会安排；能合派且不挤走更高优先级的合派。"""
        svc = self.svc
        boats = self.boats()
        segs = []
        for seg in svc.orders["segments"]:
            sid = seg["id"]
            feasible = [name for name, boat in boats.items()
                        if sid in boat["spans"] and round_no < boat["departs"]
                        and (round_no < boat["locks"] or self.has_booking(name))]
            if not feasible:
                continue
            loss = min([seg["last_dispatch"]] + [boats[f]["locks"] for f in feasible])
            segs.append((seg, feasible, loss))
        segs.sort(key=lambda t: (-t[0]["priority"], t[2], t[0]["id"]))
        free = {name: boat["capacity"] for name, boat in boats.items()}
        for bid, book in (svc.sailing or {}).get("bookings", {}).items():
            slot = book["slot"]
            if slot in free and bid != self.booking_id(slot) and book["spans"]:
                free[slot] = max(0, free[slot] - book["demand"])
        assigned = {name: [] for name in boats}
        unmet = {}
        for seg, feasible, _loss in segs:
            sid = seg["id"]
            upto = min(boats[f]["departs"] for f in feasible)
            points = svc.projected_missing_points(seg, upto)
            need = len(points)
            if need == 0:
                continue
            def pressure(slot):
                return (sum(1 for _s, f2, _l in segs if f2 == [slot]),
                        boats[slot]["locks"], slot)
            for slot in sorted(feasible, key=pressure):
                take = min(need, free[slot])
                if take:
                    assigned[slot].append((sid, take, points))
                    free[slot] -= take
                    need -= take
                if need == 0:
                    break
            if need:
                unmet[sid] = need
        if unmet != svc.state["unmet"]:
            for sid, count in unmet.items():
                log_event(svc.state, round_no, "demand_unmet", segment=sid,
                          count=count, detail="船位不足，仍有格点排不进船次")
        svc.state["unmet"] = unmet
        want = {}
        for slot, items in assigned.items():
            if not items:
                continue
            spans = sorted({sid for sid, _, _ in items},
                           key=lambda s: svc.segment(s)["span"][0])
            want[slot] = {"spans": spans,
                          "demand": sum(take for _, take, _ in items),
                          "points": {sid: pts for sid, _, pts in items}}
        return want

    def act(self, round_no):
        svc = self.svc
        st = svc.state
        boats = self.boats()
        if not boats:
            return []
        want = self.desired(round_no)
        actions = []
        for slot, boat in boats.items():
            bid = self.booking_id(slot)
            target = want.get(slot, {"spans": [], "demand": 0, "points": {}})
            mirror = st["bookings"].get(bid)
            live = mirror and mirror.get("state") != "cancelled"
            cur_spans = mirror["spans"] if live and mirror.get("spans") else []
            cur_demand = mirror["demand"] if live and mirror.get("spans") else 0
            if mirror and mirror.get("state") in ("sailing", "returned"):
                if cur_spans and (cur_spans != target["spans"]
                                  or cur_demand != target["demand"]):
                    if not mirror.get("frozen_logged"):
                        mirror["frozen_logged"] = True
                        log_event(st, round_no, "booking_frozen", booking=bid,
                                  spans=cur_spans, detail="船次已出发，保留原单")
                continue
            if cur_spans == target["spans"] and cur_demand == target["demand"]:
                continue
            if not cur_spans and not target["spans"]:
                continue
            if not cur_spans:
                if round_no >= boat["locks"]:
                    log_event(st, round_no, "booking_failed", booking=bid,
                              reason="locked", spans=target["spans"])
                    continue
                late = [s for s in target["spans"]
                        if round_no > svc.segment(s)["last_dispatch"]]
                if late:
                    log_event(st, round_no, "booking_failed", booking=bid,
                              reason="last_dispatch_passed", spans=late)
                    continue
                action = self._request(bid, slot, target, round_no, "create")
            elif cur_demand < target["demand"]:
                late = [s for s in target["spans"]
                        if round_no > svc.segment(s)["last_dispatch"]]
                if late:
                    log_event(st, round_no, "booking_failed", booking=bid,
                              reason="last_dispatch_passed", spans=late)
                    continue
                action = self._request(bid, slot, target, round_no, "grow")
            else:
                action = self._request(bid, slot, target, round_no,
                                       "cancel" if not target["spans"] else "shrink")
            if action:
                actions.append(action)
        return actions

    def _request(self, bid, slot, target, round_no, kind):
        svc = self.svc
        st = svc.state
        mirror = st["bookings"].setdefault(
            bid, {"slot": slot, "spans": [], "demand": 0, "state": None,
                  "pending": None, "in_flight": False, "history": []})
        if mirror.get("in_flight"):
            log_event(st, round_no, "request_in_flight", booking=bid,
                      detail="已有未决请求，不重复占船也不重复释放")
            return None
        st["request_seq"] += 1
        rid = f'{bid}-{st["request_seq"]}'
        body = {"request": rid, "booking": bid, "slot": slot,
                "spans": target["spans"], "demand": target["demand"]}
        code, resp = svc.client.post("/sailing/request", body)
        req = st["requests"].setdefault(
            rid, {"booking": bid, "body": body, "sent_round": round_no, "final": None})
        mirror["in_flight"] = True
        log_event(st, round_no, "booking_request", booking=bid, slot=slot,
                  action=kind, spans=target["spans"], demand=target["demand"],
                  points=target.get("points"), request=rid)
        if code == 503:
            log_event(st, round_no, "reply_lost", request=rid, booking=bid,
                      detail="回执丢失，按请求号下轮对账")
            return {"request": rid, "state": "reply_lost"}
        self._apply_result(req, resp, round_no)
        return {"request": rid, "state": resp.get("state")}
