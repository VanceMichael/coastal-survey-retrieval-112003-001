import json
import unittest
from pathlib import Path
from tempfile import TemporaryDirectory
from threading import Thread
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from source_service import create_source
from duty import coverage as cov
from duty.server import create_duty
from duty.service import DutyService


class DutyBase(unittest.TestCase):
    def setUp(self):
        self.server = create_source()
        thread = Thread(target=self.server.serve_forever,
                        kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)
        self.tmp = TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.state_path = Path(self.tmp.name) / "duty_state.json"
        self.duty = DutyService(self.base, self.state_path)

    def advance(self, value):
        code, _ = self.duty.client.post("/round", {"round": value})
        self.assertEqual(code, 200)
        return self.duty.tick()

    def missing(self, sid):
        seg = self.duty.segment(sid)
        return cov.missing_points(seg, self.duty.current_coverage[sid])

    def sailing(self):
        return self.duty.client.get("/sailing")[1]


class PlaythroughTest(DutyBase):
    def test_shift_story(self):
        # 第 0 轮：L1-02、B1-08 可领；A1-02 没有任何校准卡，粗范围再合也不领
        self.advance(0)
        reads = {r["page"] for r in self.duty.state["reads"]}
        self.assertEqual(reads, {"L1-02", "B1-08"})
        spent = self.duty.client.get("/directory")[1]["links"]["shore"]["spent"]
        self.assertEqual(spent, 226)
        self.assertEqual(self.missing("S1"), [])
        # B1-08 反向测程经 B0 卡映到 125/175，S2 的 boat 已齐；air 还缺
        self.assertEqual(self.missing("S2"), [125, 175])
        # 预测到 sloop-3 出发时 S2 仍缺 air，先占船；第一次回执被弄丢
        self.assertEqual(len(self.duty.state["requests"]), 1)
        req = next(iter(self.duty.state["requests"].values()))
        self.assertIsNone(req["final"])
        self.assertEqual(req["body"]["slot"], "sloop-3")
        self.assertEqual(req["body"]["spans"], ["S2"])
        self.assertEqual(req["body"]["demand"], 2)

        # 第 1 轮：回执可见，预约入账；A2-02 补齐 S2 的 air（T1 时期）
        self.advance(1)
        self.assertEqual(self.sailing()["bookings"]["N4-sloop-3"]["state"], "reserved")
        self.assertEqual(self.missing("S2"), [])
        self.assertEqual(self.missing("S3"), [225, 275])
        # A2-03 v1 的转发片段已持有一半，A2-04 因本轮余量不足往后排
        self.assertIn("A2|A2-03|v1", self.duty.state["fragments"])
        self.assertNotIn("A2|A2-04|v1", self.duty.state["fragments"])

        # 第 2 轮：v1 凑齐但读数落空；sloop-3 锁定，旧安排不动
        self.advance(2)
        self.assertIn("A2|A2-03|v1", self.duty.state["originals"])
        self.assertEqual(self.missing("S3"), [225, 275])
        self.assertEqual(self.sailing()["bookings"]["N4-sloop-3"]["state"], "locked")

        # 第 3 轮：T2 修订、A2-02 撤回、B1a 卡生效；领 B1-09、A2-04 与 v2
        self.advance(3)
        originals = self.duty.state["originals"]
        self.assertTrue(originals["A2|A2-03|v1"]["superseded"])
        self.assertIn("A2|A2-03|v2", originals)
        self.assertEqual(originals["A2|A2-03|v2"]["links"], ["relay", "shore"])
        self.assertEqual(self.missing("S3"), [])
        # S2 改用 T2 后：B1-08/A2-02 的 T1 证据不再算数，只剩 B1-09 的 125/boat
        self.assertEqual(self.duty.current_coverage["S2"]["line"], "T2")
        self.assertEqual(self.missing("S2"), [125, 175])
        # 撤回只影响 A2-02 的第 1 行，不影响其他范围已有覆盖
        self.assertEqual(self.missing("S1"), [])

        # 第 4 轮：sloop-3 出发，交接快照冻结当时的复核提示
        self.advance(4)
        self.assertEqual(len(self.duty.state["handovers"]), 1)
        handover = self.duty.state["handovers"][0]
        self.assertEqual(handover["booking"], "N4-sloop-3")
        self.assertEqual(handover["needs"],
                         {"S2": [{"point": 125, "missing": ["air"]},
                                 {"point": 175, "missing": ["air", "boat"]}]})

        self.advance(5)
        # 第 6 轮：复核班回传只补其实际完成的格点（S2 125/air）
        self.advance(6)
        self.assertEqual(self.missing("S2"), [175])
        # 船已返航、无剩余路径、派工窗已过：记录错过原因
        missed = self.duty.state["missed"]["S2"]
        self.assertEqual(missed["missing"], {"175": ["air", "boat"]})
        self.assertIn("无剩余原件路径", missed["reason"])
        # 交接记录不被后来的校准与补证改写
        self.assertEqual(self.duty.state["handovers"][0]["needs"]["S2"][0]["point"], 125)

        self.advance(7)
        # 迟到原件说明：B1-09/v2 等保全时已过自由调整窗，留有去向说明
        self.assertTrue(any(n["original"] == "B1|B1-09|v1" for n in self.duty.state["notes"]))
        # 审计：S2 的证据链能追到领取、校准和派工
        audit = self.duty.audit("S2")
        boat_ev = [e for e in audit["evidence"] if e["category"] == "boat"]
        self.assertEqual(boat_ev[0]["original"], "B1|B1-09|v1")
        self.assertEqual(boat_ev[0]["card"], "B1a")
        self.assertTrue(boat_ev[0]["reads"])
        self.assertTrue(any(e["kind"] == "booking_request" for e in audit["events"]))

    def test_restart_keeps_booking_without_double_counting(self):
        self.advance(0)
        self.assertEqual(len(self.duty.state["requests"]), 1)
        # 班组关掉值班服务再打开：船已锁、本地还没看到回执
        code, _ = self.duty.client.post("/round", {"round": 2})
        self.assertEqual(code, 200)
        reopened = DutyService(self.base, self.state_path)
        reopened.tick()
        bookings = self.sailing()["bookings"]
        self.assertEqual(list(bookings), ["N4-sloop-3"])  # 没有重复占船
        self.assertEqual(bookings["N4-sloop-3"]["demand"], 2)
        self.assertEqual(bookings["N4-sloop-3"]["state"], "locked")
        self.assertEqual(len(reopened.state["requests"]), 1)  # 没有重发新请求号
        req = next(iter(reopened.state["requests"].values()))
        self.assertIsNotNone(req["final"])  # 回执对上了

    def test_divergent_directory_never_marks_complete(self):
        # 第 2 轮才开班：目录里 relay 的 v1 要约还在，链路实际已封 v2
        self.advance(2)
        relay_reads = [r for r in self.duty.state["reads"]
                       if r["page"] == "A2-03" and r["link"] == "relay"]
        self.assertEqual(relay_reads[0]["status"], 409)
        self.assertEqual(relay_reads[0]["cost"], 57)  # 换版也计入本轮用量
        self.assertNotIn("A2|A2-03|v1", self.duty.state["originals"])
        kinds = [e["kind"] for e in self.duty.state["log"]]
        self.assertIn("divergence", kinds)
        # sloop-3 已锁，S2 改派 launch-5
        self.assertIn("N4-launch-5", self.sailing()["bookings"])
        self.advance(3)
        self.assertIn("A2|A2-03|v2", self.duty.state["originals"])
        self.assertEqual(self.missing("S3"), [])

    def test_locked_change_waits_for_field_decision(self):
        self.advance(0)
        self.advance(1)
        # 质检否定 L1-02：S1 变缺，launch-5 还有窗，预约 S1
        self.duty.qc({"original": "L1|L1-02|v1"})
        self.assertEqual(self.missing("S1"), [25, 75])
        self.duty.tick()
        self.assertEqual(self.sailing()["bookings"]["N4-launch-5"]["demand"], 2)
        # 第 3 轮 launch-5 锁定后撤销质检：S1 恢复，取消只能走变更申请
        self.advance(3)
        self.duty.qc({"original": "L1|L1-02|v1", "undo": True})
        self.duty.tick()
        mirror = self.duty.state["bookings"]["N4-launch-5"]
        self.assertIsNotNone(mirror["pending"])
        # 船班确认前旧安排仍占容量
        self.assertEqual(self.sailing()["bookings"]["N4-launch-5"]["demand"], 2)
        self.duty.tick()  # 未决期间不重复发请求
        cancel = [r for r in self.duty.state["requests"].values()
                  if r["body"]["booking"] == "N4-launch-5" and not r["body"]["spans"]]
        self.assertEqual(len(cancel), 1)
        self.assertEqual(len(self.duty.state["requests"]), 3)  # 建两笔、取消一笔
        rid = cancel[0]["body"]["request"]
        code, decided = self.duty.client.post(
            "/sailing/decision", {"request": rid, "accept": True})
        self.assertEqual(decided["state"], "changed")
        self.duty.tick()
        self.assertEqual(self.sailing()["bookings"]["N4-launch-5"]["state"], "cancelled")

    def test_qc_line_keeps_other_coverage(self):
        self.advance(0)
        self.advance(1)
        self.advance(2)
        self.advance(3)
        self.assertEqual(self.missing("S3"), [])
        result = self.duty.qc({"line": "T1"})
        # T1 被否定：S1、S3 的覆盖消失；T2 上的 B1-09 不受影响
        self.assertEqual(self.missing("S1"), [25, 75])
        self.assertEqual(self.missing("S3"), [225, 275])
        self.assertEqual(self.missing("S2"), [125, 175])
        # 没有还没取的页能补这些缺口
        self.assertTrue(result["replan"])
        self.assertEqual(result["replan"]["S3"]["fetchable"], [])
        self.duty.qc({"line": "T1", "undo": True})
        self.assertEqual(self.missing("S3"), [])


class GeometryTest(unittest.TestCase):
    """判读几何：反向卡、横跨待核、末段含终点、卡片范围外无标定。"""

    def setUp(self):
        self.orders = json.loads(Path("data/orders.json").read_text())
        self.cards = json.loads(Path("data/calibration.json").read_text())

    def interpret(self, rows, sid, round_no=0, line="T1"):
        seg = next(s for s in self.orders["segments"] if s["id"] == sid)
        original = {"page": "X", "version": "v1",
                    "content": {"epoch": "A2", "pass": "return", "rows": rows}}
        return cov.interpret_for_segment(
            original, seg, line, self.cards, round_no, set(),
            sid == cov.last_segment_id(self.orders))

    def test_reversed_card(self):
        seg = next(s for s in self.orders["segments"] if s["id"] == "S2")
        original = {"page": "B1-08", "version": "v1",
                    "content": {"epoch": "B1", "pass": "shoal",
                                "rows": [[25, 25], [75, 75]]}}
        readings = cov.interpret_for_segment(original, seg, "T1", self.cards, 0,
                                             set(), False)
        self.assertEqual([r["point"] for r in readings], [175, 125])

    def test_spanning_reading_is_pending(self):
        # 映到 [270,276]：压着 275 的容许带但不能完整落入，先待核
        readings = self.interpret([[260, 266]], "S3", round_no=2)
        self.assertEqual(readings[0]["status"], "pending")
        self.assertEqual(readings[0]["points"], [275])

    def test_crossing_segment_boundary_is_pending(self):
        # 映到 [195,205]：横跨 S2/S3 边界
        readings = self.interpret([[185, 195]], "S3", round_no=2)
        self.assertEqual(readings[0]["status"], "pending")
        self.assertEqual(readings[0]["points"], [])

    def test_last_segment_includes_endpoint(self):
        readings = self.interpret([[290, 290]], "S3", round_no=2)
        self.assertEqual(readings[0]["mapped"], [300.0, 300.0])
        self.assertEqual(readings[0]["status"], "miss")  # 在区段内但不命中格点

    def test_raw_outside_card_is_uncalibrated(self):
        readings = self.interpret([[50, 50]], "S3", round_no=2)
        self.assertEqual(readings[0]["status"], "uncalibrated")


class ApiTest(DutyBase):
    def test_query_endpoints(self):
        self.advance(0)
        self.advance(1)
        api = create_duty(self.duty, 0)
        thread = Thread(target=api.serve_forever,
                        kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.addCleanup(api.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(api.shutdown)
        base = f"http://127.0.0.1:{api.server_port}"

        def get(path):
            with urlopen(base + path, timeout=2) as resp:
                return json.loads(resp.read())

        def post(path, payload):
            req = Request(base + path, data=json.dumps(payload).encode())
            with urlopen(req, timeout=2) as resp:
                return json.loads(resp.read())

        status = get("/status")
        self.assertEqual(status["round"], 1)
        section = get("/sections/S2")
        self.assertEqual(section["missing"], [])
        self.assertTrue(section["bookings"])
        trip = get("/trips/sloop-3")
        self.assertEqual(trip["bookings"][0]["demand"], 2)
        audit = get("/audit/S2")
        self.assertTrue(audit["evidence"])
        self.assertIn("A2|A2-03|v1", get("/originals") or {})
        result = post("/tick", {})
        self.assertEqual(result["round"], 1)  # 同轮重复 tick 幂等
        with self.assertRaises(HTTPError) as ctx:
            urlopen(base + "/sections/XX", timeout=2)
        self.assertEqual(ctx.exception.code, 404)


if __name__ == "__main__":
    unittest.main()
