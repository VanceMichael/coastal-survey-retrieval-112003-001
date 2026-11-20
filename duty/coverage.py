"""工程单、校准卡与覆盖判定。

只使用已知轮次不晚于当前轮的工程修订与校准卡；粗范围不在这里出现，
覆盖只由凑齐原件的读数、有效校准和现场补证构成。
"""

TOL = 2  # 可接受格点位置为工单格点上下 2 米内


def current_lines(orders, round_no):
    """各区段在指定轮次使用的线路号。affected 之外的区段沿用前线号。"""
    lines = {}
    for rev in sorted(orders["revisions"], key=lambda r: r["known"]):
        if rev["known"] > round_no:
            break
        if "affected" in rev:
            for sid in rev["affected"]:
                lines[sid] = rev["line"]
        else:
            for seg in orders["segments"]:
                lines[seg["id"]] = rev["line"]
    return lines


def select_card(cards, epoch, pass_, line, round_no):
    """按设备世代、实际测程和线路修订选卡，取当前轮已知的最新一张。"""
    best = None
    for card in cards:
        if card["round"] > round_no:
            continue
        if card["epoch"] == epoch and card["pass"] == pass_ and card["line"] == line:
            if best is None or card["round"] > best["round"]:
                best = card
    return best


def card_known_ever(cards, epoch, pass_, line):
    return any(c["epoch"] == epoch and c["pass"] == pass_ and c["line"] == line
               for c in cards)


def map_raw(card, x):
    knots = card["knots"]
    xs = [k[0] for k in knots]
    if x < xs[0] or x > xs[-1]:
        return None  # 原始范围不在卡片两端内，无有效标定
    for i in range(len(knots) - 1):
        x0, y0 = knots[i]
        x1, y1 = knots[i + 1]
        if x0 <= x <= x1:
            if x1 == x0:
                return float(y0)
            t = (x - x0) / (x1 - x0)
            return y0 + t * (y1 - y0)
    return float(knots[-1][1])


def map_interval(card, lo, hi):
    """原始闭区间映到工程线路闭区间；逆向标定同样成立。"""
    a = map_raw(card, lo)
    b = map_raw(card, hi)
    if a is None or b is None:
        return None
    return (min(a, b), max(a, b))


def last_segment_id(orders):
    ordered = sorted(orders["segments"], key=lambda s: s["span"][0])
    return ordered[-1]["id"] if ordered else None


def interval_in_segment(seg, a, b, is_last):
    """区段左闭右开，末段含终点。"""
    s0, s1 = seg["span"]
    if a < s0:
        return False
    return b <= s1 if is_last else b < s1


def interval_touches_segment(seg, a, b, is_last):
    s0, s1 = seg["span"]
    if is_last:
        return b >= s0 and a <= s1
    return b >= s0 and a < s1


def interpret_for_segment(original, seg, line, cards, round_no, withdrawn, is_last):
    """把一份原件的读数针对某区段逐条判读。

    返回列表，每项 {index, raw, mapped, status, card, point, points}：
    - hit：定位闭区间完整落在格点±TOL 且在同一区段内；
    - pending：可能位置横跨格点或区段，先待核；
    - miss：落在区段内但不构成命中，或与该区段无关；
    - uncalibrated：没有有效校准卡或原始范围超出卡片两端。
    """
    content = original["content"]
    card = select_card(cards, content["epoch"], content["pass"], line, round_no)
    readings = []
    for idx, row in enumerate(content.get("rows", [])):
        if (original["page"], original["version"], idx) in withdrawn:
            continue
        lo, hi = row[0], row[1]
        base = {"index": idx, "raw": [lo, hi], "card": card["card"] if card else None,
                "point": None, "points": []}
        if card is None:
            readings.append({**base, "status": "uncalibrated", "mapped": None})
            continue
        mapped = map_interval(card, lo, hi)
        if mapped is None:
            readings.append({**base, "status": "uncalibrated", "mapped": None})
            continue
        a, b = mapped
        inside = interval_in_segment(seg, a, b, is_last)
        hit = None
        touched = []
        for p in seg["points"]:
            if inside and a >= p - TOL and b <= p + TOL:
                hit = p
                break
            if b >= p - TOL and a <= p + TOL:
                touched.append(p)
        if hit is not None:
            status = "hit"
        elif touched or (interval_touches_segment(seg, a, b, is_last) and not inside):
            status = "pending"
        else:
            status = "miss"
        readings.append({**base, "status": status, "mapped": [a, b],
                         "point": hit, "points": touched})
    return readings


def compute_coverage(orders, cards, originals, withdrawn, field_entries, round_no):
    """汇总当前轮各区段证据。

    originals 为已凑齐且未被顶替、未被质检否定的原件；withdrawn 为
    (page, version, 行号) 集合；field_entries 为现场补证，只补其实际完成的格点。
    """
    lines = current_lines(orders, round_no)
    last_id = last_segment_id(orders)
    result = {}
    for seg in orders["segments"]:
        sid = seg["id"]
        line = lines.get(sid)
        points = {str(p): {cat: [] for cat in seg["required"]} for p in seg["points"]}
        pending = []
        for orig in originals:
            device = orig["content"].get("device")
            if device not in seg["required"]:
                continue
            for reading in interpret_for_segment(orig, seg, line, cards, round_no,
                                                 withdrawn, sid == last_id):
                if reading["status"] == "hit":
                    points[str(reading["point"])][device].append({
                        "kind": "original", "original": orig["key"],
                        "row": reading["index"], "card": reading["card"],
                        "reads": orig.get("reads", []), "links": orig.get("links", [])})
                elif reading["status"] == "pending":
                    pending.append({"original": orig["key"], "row": reading["index"],
                                    "mapped": reading["mapped"], "card": reading["card"],
                                    "points": reading["points"], "device": device})
        for entry in field_entries:
            if entry["round"] > round_no or entry.get("line") != line:
                continue
            for pt in entry.get("points", []):
                if pt.get("segment") != sid or pt.get("evidence") not in seg["required"]:
                    continue
                for p in seg["points"]:
                    if abs(pt.get("position", p) - p) <= TOL:
                        points[str(p)][pt["evidence"]].append(
                            {"kind": "field", "trip": entry.get("trip"),
                             "receipt": entry.get("receipt")})
        result[sid] = {"line": line, "points": points, "pending": pending}
    return result


def missing_points(seg, seg_cov):
    """仍需现场核对的格点：任一必需设备类别无命中即缺（缺两类仍只占一个船位）。"""
    out = []
    for p in seg["points"]:
        ev = seg_cov["points"].get(str(p), {})
        if any(not ev.get(cat) for cat in seg["required"]):
            out.append(p)
    return out


def missing_categories(seg, seg_cov, point):
    ev = seg_cov["points"].get(str(point), {})
    return [cat for cat in seg["required"] if not ev.get(cat)]
