"""领取计划与执行：目录评估、分片领取、拼装核对。

排队规则：先保仍可能决定复核窗的资料，再处理无法重领的原件，
同等工作以较早失效为先；已经来不及赶上当前窗口的要求往后排，
把余量留给还来得及改变交接的区段。目录粗范围只决定是否值得领。
"""
import json

from .coverage import TOL, card_known_ever, current_lines, missing_points
from .store import log_event


def key_of(entry):
    return f'{entry["epoch"]}|{entry["page"]}|{entry["version"]}'


def rough_covers_point(rough, point):
    return rough[0] - TOL <= point <= rough[1] + TOL


def ranges_overlap(rough, span):
    return rough[0] <= span[1] and span[0] <= rough[1]


class FetchManager:
    def __init__(self, svc):
        self.svc = svc

    # ---- 目录与封版 ----
    def newest_entries(self, directory):
        """每个 (设备世代, 页) 的最新封版目录条目。"""
        groups = {}
        for entry in directory["pages"]:
            groups.setdefault((entry["epoch"], entry["page"]), []).append(entry)
        newest = {}
        for group in groups.values():
            group.sort(key=lambda e: (e["published"], e["version"]))
            newest[key_of(group[-1])] = group[-1]
        return newest

    def mark_superseded(self, directory, round_no, triggers):
        """新的封版显式顶替当前覆盖证据；旧期原件保留以解释旧单。"""
        st = self.svc.state
        newest = self.newest_entries(directory)
        for key, orig in st["originals"].items():
            if orig.get("superseded"):
                continue
            for nkey, entry in newest.items():
                if entry["epoch"] == orig["epoch"] and entry["page"] == orig["page"] \
                        and nkey != key:
                    orig["superseded"] = True
                    log_event(st, round_no, "superseded", original=key, by=nkey)
                    triggers.append(f"新封版 {nkey} 顶替 {key}")

    # ---- 候选评估 ----
    def needed_ranges(self, key, entry):
        st = self.svc.state
        total = st["totals"].get(key, {}).get("total", entry["size"])
        held = sorted(st["fragments"].get(key, []), key=lambda f: f["start"])
        ranges = []
        cursor = 0
        for frag in held:
            if frag["start"] > cursor:
                ranges.append((cursor, frag["start"]))
            cursor = max(cursor, frag["end"])
        if cursor < total:
            ranges.append((cursor, total))
        return ranges

    def completable_by(self, entry, upto_round, now_round):
        """在可判断的时段内能否完整取回（按要约，不计余量竞争）。"""
        key = key_of(entry)
        needed = self.needed_ranges(key, entry)
        if not needed:
            return True
        for a, b in needed:
            cursor = a
            while cursor < b:
                reach = None
                for offer in entry["offers"].values():
                    if offer["start"] <= cursor < offer["end"]:
                        r = max(now_round, offer["from"])
                        if offer["until"] <= r or r > upto_round:
                            continue
                        if reach is None or min(b, offer["end"]) > reach:
                            reach = min(b, offer["end"])
                if reach is None:
                    return False
                cursor = reach
        return True

    def _completion_round(self, entry, needed, round_no):
        """估计凑齐轮次；任一缺段没有任何要约时返回 None。"""
        completion = round_no
        for a, b in needed:
            cursor = a
            while cursor < b:
                best = None
                for offer in entry["offers"].values():
                    if offer["start"] <= cursor < offer["end"]:
                        r = max(round_no, offer["from"])
                        if offer["until"] <= r:
                            continue
                        if best is None or r < best[0]:
                            best = (r, min(b, offer["end"]))
                if best is None:
                    return None
                completion = max(completion, best[0])
                cursor = best[1]
        return completion

    def _plan_reads(self, entry, needed, round_no, remaining):
        """把本轮有效的要约切成领取区间；同一范围两条链路都有时留余量大的。"""
        reads = []
        for a, b in needed:
            cursor = a
            while cursor < b:
                best = None
                for link, offer in entry["offers"].items():
                    if offer["from"] <= round_no < offer["until"] \
                            and offer["start"] <= cursor < offer["end"]:
                        cand = (link, cursor, min(b, offer["end"]), offer)
                        if best is None or remaining.get(link, 0) > remaining.get(best[0], 0):
                            best = cand
                if best is None:
                    break
                reads.append({"link": best[0], "start": best[1], "end": best[2],
                              "until": best[3]["until"]})
                cursor = best[2]
        return reads

    def candidates(self, directory, coverage, round_no, remaining):
        svc = self.svc
        st = svc.state
        lines = current_lines(svc.orders, round_no)
        out = []
        for key, entry in self.newest_entries(directory).items():
            if key in st["originals"] or key in st["conflicts"]:
                continue
            if svc.is_rejected(key) or svc.is_line_rejected(entry["line"]):
                continue
            served, points = [], []
            for seg in svc.orders["segments"]:
                sid = seg["id"]
                if entry["device"] not in seg["required"]:
                    continue
                if entry["line"] != lines.get(sid):
                    continue
                if not ranges_overlap(entry["rough"], seg["span"]):
                    continue
                if not card_known_ever(svc.cards, entry["epoch"], entry["pass"], lines[sid]):
                    continue  # 永远没有校准卡的页不值得领
                want = [p for p in missing_points(seg, coverage[sid])
                        if not coverage[sid]["points"][str(p)].get(entry["device"])
                        and rough_covers_point(entry["rough"], p)]
                if want:
                    served.append(sid)
                    points.extend((sid, p) for p in want)
            if not served:
                continue
            needed = self.needed_ranges(key, entry)
            completion = self._completion_round(entry, needed, round_no)
            if completion is None:
                continue  # 已知要约凑不齐，不空耗余量
            reads = self._plan_reads(entry, needed, round_no, remaining)
            all_offers = list(entry["offers"].values())
            last_chance = any(
                r["until"] <= round_no + 1 and not any(
                    o["start"] <= r["start"] and r["end"] <= o["end"]
                    and o["until"] > round_no + 1 for o in all_offers)
                for r in reads)
            can_decide = False
            for sid in served:
                seg = svc.segment(sid)
                close = max((b["departs"] for b in svc.boats().values()
                             if sid in b["spans"]), default=round_no)
                if completion <= close and (round_no <= seg["last_dispatch"]
                                            or svc.bookman.segment_booked(sid)):
                    can_decide = True
            out.append({
                "key": key, "entry": entry, "served": served, "points": points,
                "needed": needed, "reads": reads, "can_decide": can_decide,
                "last_chance": last_chance,
                "min_until": min((r["until"] for r in reads), default=round_no + 1),
                "priority": max(svc.segment(s)["priority"] for s in served),
                "size": entry["size"],
            })
        out.sort(key=lambda c: (not c["can_decide"], not c["last_chance"],
                                c["min_until"], -c["priority"], c["size"]))
        return out

    def future_paths(self, seg, coverage, round_no):
        """该区段还值得改排的未取页（供查询与改排提示）。"""
        svc = self.svc
        if not svc.directory:
            return []
        lines = current_lines(svc.orders, round_no)
        sid = seg["id"]
        out = []
        for key, entry in self.newest_entries(svc.directory).items():
            if key in svc.state["originals"] or key in svc.state["conflicts"]:
                continue
            if entry["device"] not in seg["required"] or entry["line"] != lines.get(sid):
                continue
            if not card_known_ever(svc.cards, entry["epoch"], entry["pass"], lines[sid]):
                continue
            want = [p for p in missing_points(seg, coverage[sid])
                    if not coverage[sid]["points"][str(p)].get(entry["device"])
                    and rough_covers_point(entry["rough"], p)]
            if not want:
                continue
            offers = {link: o for link, o in entry["offers"].items() if o["until"] > round_no}
            if offers:
                out.append({"page": entry["page"], "epoch": entry["epoch"],
                            "version": entry["version"], "device": entry["device"],
                            "points": want, "offers": offers})
        return out

    # ---- 执行 ----
    def execute(self, directory, round_no):
        svc = self.svc
        st = svc.state
        remaining = {name: max(0, link["capacity"] - link["spent"])
                     for name, link in directory["links"].items()}
        actions = []
        dead = set()  # (key, link) 本轮已确认不可按目录领取
        for cand in self.candidates(directory, svc.current_coverage, round_no, remaining):
            if cand["points"] and all(
                    svc.current_coverage[sid]["points"][str(p)].get(cand["entry"]["device"])
                    for sid, p in cand["points"]):
                continue  # 本轮稍早凑齐的原件已补上这些格点
            for read in cand["reads"]:
                cost = read["end"] - read["start"]
                if (cand["key"], read["link"]) in dead:
                    continue
                if remaining.get(read["link"], 0) < cost:
                    continue
                status, body = svc.client.post("/read", {
                    "page": cand["entry"]["page"], "link": read["link"],
                    "version": cand["entry"]["version"],
                    "start": read["start"], "end": read["end"]})
                rid = self._record(cand, read, status, body, round_no)
                action = {"read": rid, "page": cand["entry"]["page"],
                          "link": read["link"], "status": status}
                if "spent" in body and read["link"] in directory["links"]:
                    remaining[read["link"]] = max(
                        0, directory["links"][read["link"]]["capacity"] - body["spent"])
                if status == 200:
                    self._add_fragment(cand["key"], read, body, rid, round_no)
                    assembled = self._assemble(cand["key"], round_no)
                    if assembled:
                        action["assembled"] = assembled
                        svc.current_coverage = svc.compute_coverage(round_no)
                elif status == 409:
                    dead.add((cand["key"], read["link"]))
                    log_event(st, round_no, "divergence", issue="edition_changed",
                              page=cand["entry"]["page"], link=read["link"],
                              asked=cand["entry"]["version"], actual=body.get("version"),
                              detail="链路实际封版与目录要约分歧，未凑齐不计完整")
                elif status == 410:
                    dead.add((cand["key"], read["link"]))
                    log_event(st, round_no, "divergence", issue="not_available",
                              page=cand["entry"]["page"], link=read["link"],
                              detail="目录在列但实际不可取")
                elif status == 416:
                    dead.add((cand["key"], read["link"]))
                    log_event(st, round_no, "divergence", issue="fragment_out_of_range",
                              page=cand["entry"]["page"], link=read["link"])
                elif status == 429:
                    remaining[read["link"]] = 0
                actions.append(action)
        return actions

    def _record(self, cand, read, status, body, round_no):
        st = self.svc.state
        st["read_seq"] += 1
        rid = f'R{st["read_seq"]}'
        st["reads"].append({
            "id": rid, "round": round_no, "page": cand["entry"]["page"],
            "epoch": cand["entry"]["epoch"], "version": cand["entry"]["version"],
            "link": read["link"], "start": read["start"], "end": read["end"],
            "cost": 0 if status == 429 else read["end"] - read["start"],
            "status": status, "got_version": body.get("version"),
            "segments": cand["served"]})
        return rid

    def _add_fragment(self, key, read, body, rid, round_no):
        st = self.svc.state
        known = st["totals"].setdefault(key, {"total": body["total"],
                                              "sha256": body["sha256"]})
        if known["sha256"] != body["sha256"] or known["total"] != body["total"]:
            st["conflicts"][key] = "同封版内容摘要前后不一致"
            log_event(st, round_no, "divergence", issue="digest_mismatch", original=key)
            return
        st["fragments"].setdefault(key, []).append({
            "link": read["link"], "start": read["start"], "end": read["end"],
            "sha256": body["sha256"], "round": round_no, "hex": body["hex"],
            "read": rid})

    def _assemble(self, key, round_no):
        """同世代、同页、同封版且内容核对一致的片段才凑成完整原件。"""
        svc = self.svc
        st = svc.state
        if key in st["originals"] or key in st["conflicts"]:
            return None
        info = st["totals"].get(key)
        frags = st["fragments"].get(key, [])
        if not info or not frags:
            return None
        if len({f["sha256"] for f in frags}) != 1 or frags[0]["sha256"] != info["sha256"]:
            st["conflicts"][key] = "片段内容核对不一致"
            log_event(st, round_no, "divergence", issue="fragment_mismatch", original=key)
            return None
        data = bytearray()
        covered = 0
        for frag in sorted(frags, key=lambda f: f["start"]):
            if frag["start"] > covered:
                return None  # 还有缺口
            chunk = bytes.fromhex(frag["hex"])
            if frag["start"] < covered:
                overlap = covered - frag["start"]
                if data[frag["start"]:covered] != chunk[:overlap]:
                    st["conflicts"][key] = "重叠片段字节不一致"
                    log_event(st, round_no, "divergence", issue="overlap_mismatch",
                              original=key)
                    return None
                chunk = chunk[overlap:]
            data.extend(chunk)
            covered += len(chunk)
        if covered < info["total"]:
            return None
        epoch, page, version = key.split("|")
        try:
            content = json.loads(bytes(data))
        except ValueError:
            st["conflicts"][key] = "内容无法解析"
            log_event(st, round_no, "divergence", issue="bad_content", original=key)
            return None
        problems = []
        if content.get("epoch") != epoch or content.get("edition") != version:
            problems.append("内容世代或封版与领取标识不符")
        entry = next((p for p in (svc.directory or {"pages": []})["pages"]
                      if p["epoch"] == epoch and p["page"] == page
                      and p["version"] == version), None)
        if entry:
            for field in ("device", "pass", "line"):
                if content.get(field) != entry[field]:
                    problems.append(f"内容 {field} 与目录分歧")
        rows = content.get("rows")
        if not isinstance(rows, list) or any(
                not isinstance(r, list) or len(r) != 2 for r in rows):
            problems.append("观测行格式不符")
        if problems:
            st["conflicts"][key] = "; ".join(problems)
            log_event(st, round_no, "divergence", issue="catalog_mismatch",
                      original=key, detail="; ".join(problems))
            return None
        st["originals"][key] = {
            "key": key, "epoch": epoch, "page": page, "version": version,
            "device": content["device"], "pass": content["pass"],
            "line": content["line"], "completed": round_no,
            "sha256": info["sha256"], "total": info["total"],
            "reads": sorted({f["read"] for f in frags}),
            "links": sorted({f["link"] for f in frags}),
            "content": content, "superseded": False}
        log_event(st, round_no, "assembled", original=key,
                  reads=st["originals"][key]["reads"],
                  links=st["originals"][key]["links"])
        svc.late_note(key, round_no)
        return key
