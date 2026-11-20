import json
from threading import Thread
import unittest
from urllib.error import HTTPError
from urllib.request import Request, urlopen

from source_service import create_source


class GatewayTest(unittest.TestCase):
    def setUp(self):
        self.server = create_source()
        thread = Thread(target=self.server.serve_forever, kwargs={"poll_interval": .01}, daemon=True)
        thread.start()
        self.base = f"http://127.0.0.1:{self.server.server_port}"
        self.addCleanup(self.server.server_close)
        self.addCleanup(thread.join, 2)
        self.addCleanup(self.server.shutdown)

    def exchange(self, path, payload=None, headers=None):
        request = Request(self.base + path, data=json.dumps(payload).encode() if payload is not None else None,
                          headers=headers or {})
        try:
            response = urlopen(request, timeout=2)
        except HTTPError as error:
            response = error
        with response:
            body = response.read()
            if response.headers["Content-Type"].startswith("application/json"):
                body = json.loads(body)
            return response.code, response.headers, body

    def advance(self, value):
        self.assertEqual(self.exchange("/round", {"round": value})[0], 200)

    def test_page_edition_changes_before_catalog_and_still_costs_capacity(self):
        self.advance(1)
        request = {"page": "A2-03", "link": "relay", "version": "v1", "start": 0, "end": 20}
        first = self.exchange("/read", request)[2]
        self.assertEqual(first["version"], "v1")
        self.assertEqual(len(bytes.fromhex(first["hex"])), 20)
        self.advance(2)
        page = [p for p in self.exchange("/directory")[2]["pages"] if p["page"] == "A2-03"]
        self.assertEqual([row["version"] for row in page], ["v1"])
        status, _, replaced = self.exchange("/read", request)
        self.assertEqual((status, replaced["version"], replaced["spent"]), (409, "v2", 20))
        request["end"] = 111
        self.assertEqual(self.exchange("/read", request)[0], 429)
        relay = self.exchange("/directory")[2]["links"]["relay"]
        self.assertEqual(relay, {"capacity": 110, "spent": 20})
        self.advance(3)
        request["version"], request["end"] = "v2", 20
        second = self.exchange("/read", request)[2]
        self.assertNotEqual(second["sha256"], first["sha256"])

    def test_evicted_page_cannot_be_read_through_an_originals_fallback(self):
        self.advance(1)
        request = {"page": "A1-02", "link": "shore", "version": "v1", "start": 0, "end": 20}
        self.assertEqual(self.exchange("/read", request)[0], 410)
        self.assertEqual(self.exchange("/originals/pages/A1-02-v1.json")[0], 404)
        self.assertEqual(self.exchange("/field/H37")[0], 404)
        self.advance(6)
        self.assertEqual(self.exchange("/field/H37")[2]["points"][0]["segment"], "S2")

    def test_ship_lock_and_lost_receipt_preserve_capacity_until_confirmation(self):
        self.advance(1)
        book = {"slot": "sloop-3", "booking": "H1", "request": "K1", "demand": 3, "spans": ["S2", "S3"]}
        self.assertEqual(self.exchange("/sailing/request", book)[0], 503)
        self.assertEqual(self.exchange("/sailing/result?request=K1")[0], 404)
        other = {**book, "booking": "H2", "request": "K2", "demand": 2}
        self.assertEqual(self.exchange("/sailing/request", other)[2]["state"], "capacity_unavailable")
        self.advance(2)
        self.assertEqual(self.exchange("/sailing/result?request=K1")[2]["booking"]["state"], "reserved")
        change = {**book, "request": "K3", "spans": ["S2"], "demand": 2}
        self.assertEqual(self.exchange("/sailing/request", change)[2]["state"], "pending_change")
        self.assertEqual(self.exchange("/sailing")[2]["bookings"]["H1"]["demand"], 3)
        self.assertEqual(self.exchange("/sailing/decision", {"request": "K3", "accept": True})[2]["booking"]["demand"], 2)
        self.advance(4)
        self.assertEqual(self.exchange("/sailing")[2]["bookings"]["H1"]["state"], "sailing")
        self.assertEqual(self.exchange("/sailing/request", {**change, "request": "K4"})[2]["state"], "departed")
