"""巡检设备、传输台和船务端的联机演练。"""
from argparse import ArgumentParser
from hashlib import sha256
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock
from urllib.parse import parse_qs, urlsplit
import json

ROOT = Path(__file__).resolve().parent


def create_source(port=0, root=ROOT):
    mission = json.loads((root / "mission.json").read_text())
    round_no = 0
    usage = {}
    trips = {}
    requests = {}
    lock = Lock()

    class Source(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def is_active(self, record):
            return record["from"] <= round_no < record["until"]

        def do_GET(self):
            target = urlsplit(self.path)
            query = parse_qs(target.query)
            try:
                with lock:
                    self.read(target.path, query)
            except (KeyError, ValueError):
                self.reply(400, {"reason": "bad_request"})

        def read(self, path, query):
            if path == "/":
                return self.reply(200, {"round": round_no, "paths": ["/directory", "/sailing"]})
            if path in ("/data/orders.json", "/data/calibration.json", "/data/handoffs.json"):
                return self.reply(200, json.loads((root / path.lstrip("/")).read_text()))
            if path in mission["field_returns"]:
                row = mission["field_returns"][path]
                if row["round"] > round_no:
                    return self.reply(404, {"state": "not_arrived"})
                return self.reply(200, json.loads((root / row["file"]).read_text()))
            if path == "/directory":
                pages = [page for page in mission["directory"] if page["published"] <= round_no]
                links = {}
                for name, window in mission["links"].items():
                    capacity = window["capacity"].get(str(round_no), 0)
                    links[name] = {"capacity": capacity, "spent": usage.get((round_no, name), 0)}
                return self.reply(200, {"round": round_no, "pages": pages, "links": links})
            if path == "/sailing":
                return self.reply(200, {"round": round_no, "boats": mission["boats"], "bookings": trips})
            if path == "/sailing/result":
                value = requests.get(query["request"][0])
                if not value or value["visible"] > round_no:
                    return self.reply(404, {"state": "unknown"})
                return self.reply(*value["response"])
            return self.reply(404, {"state": "unknown"})

        def do_POST(self):
            try:
                body = json.loads(self.rfile.read(int(self.headers["Content-Length"])))
                with lock:
                    self.write(urlsplit(self.path).path, body)
            except (KeyError, ValueError, IndexError, TypeError):
                self.reply(400, {"state": "bad_request"})

        def write(self, path, body):
            nonlocal round_no
            if path == "/round":
                target = body["round"]
                if type(target) is not int or target < round_no:
                    return self.reply(409, {"state": "cannot_rewind"})
                round_no = target
                for booking in trips.values():
                    boat = mission["boats"][booking["slot"]]
                    if not booking["spans"]:
                        continue
                    if round_no >= boat["returns"]:
                        booking["state"] = "returned"
                    elif round_no >= boat["departs"]:
                        booking["state"] = "sailing"
                    elif round_no >= boat["locks"]:
                        booking["state"] = "locked"
                return self.reply(200, {"round": round_no})
            if path == "/read":
                return self.transfer(body)
            if path == "/sailing/request":
                return self.book(body)
            if path == "/sailing/decision":
                return self.decide(body)
            return self.reply(404, {"state": "unknown"})

        def transfer(self, body):
            link = body["link"]
            start, end = body["start"], body["end"]
            if type(start) is not int or type(end) is not int or not 0 <= start < end:
                return self.reply(422, {"state": "invalid_range"})
            cost = end - start
            key = (round_no, link)
            spent = usage.get(key, 0)
            if spent + cost > mission["links"][link]["capacity"].get(str(round_no), 0):
                return self.reply(429, {"state": "capacity_exhausted", "spent": spent})
            spent += cost
            usage[key] = spent
            page = next((p for p in reversed(mission["objects"].get(body["page"], []))
                         if p["link"] == link and self.is_active(p)), None)
            if not page:
                return self.reply(410, {"state": "not_available", "spent": spent})
            if page["version"] != body["version"]:
                return self.reply(409, {"state": "edition_changed", "version": page["version"], "spent": spent})
            if start < page["start"] or end > page["end"]:
                return self.reply(416, {"state": "fragment_out_of_range", "spent": spent})
            data = (root / page["file"]).read_bytes()
            return self.reply(200, {"version": page["version"], "page": body["page"],
                                    "start": start, "end": end, "total": len(data),
                                    "sha256": sha256(data).hexdigest(), "hex": data[start:end].hex(),
                                    "spent": spent})

        def book(self, body):
            ident = body["request"]
            previous = requests.get(ident)
            if previous:
                if body != previous["input"]:
                    return self.reply(409, {"state": "request_conflict"})
                if previous["visible"] > round_no:
                    return self.reply(503, {"state": "reply_lost"})
                return self.reply(*previous["response"])
            slot, spans = body["slot"], body["spans"]
            boat = mission["boats"][slot]
            entry = trips.get(body["booking"])
            if entry and entry["slot"] != slot:
                return self.reply(409, {"state": "slot_conflict"})
            if round_no >= boat["departs"]:
                return self.reply(409, {"state": "departed"})
            if any(span not in boat["spans"] for span in spans):
                return self.reply(422, {"state": "route_unavailable"})
            other = sum(item["demand"] for k, item in trips.items()
                        if item["slot"] == slot and k != body["booking"])
            if not 0 <= body["demand"] <= boat["capacity"] - other:
                return self.reply(409, {"state": "capacity_unavailable"})
            if body["demand"] == 0 and spans or body["demand"] > 0 and not spans:
                return self.reply(422, {"state": "invalid_demand"})
            proposal = {"slot": slot, "spans": spans, "demand": body["demand"]}
            if entry and round_no >= boat["locks"]:
                value = {"state": "pending_change", "old": dict(entry), "proposed": proposal}
            elif round_no >= boat["locks"]:
                return self.reply(409, {"state": "booking_closed"})
            else:
                proposal["state"] = "reserved" if spans else "cancelled"
                trips[body["booking"]] = proposal
                value = {"state": proposal["state"], "booking": dict(proposal)}
            lose = boat.get("lose_first_reply") and not any(x["input"]["slot"] == slot for x in requests.values())
            requests[ident] = {"input": body, "visible": round_no + int(lose or 0), "response": (200, value)}
            return self.reply(503, {"state": "reply_lost"}) if lose else self.reply(200, value)

        def decide(self, body):
            ident = body["request"]
            item = requests[ident]
            old_response = item["response"][1]
            if old_response["state"] != "pending_change":
                return self.reply(409, {"state": "no_pending_change"})
            proposed = old_response["proposed"]
            booking = item["input"]["booking"]
            existing = trips[booking]
            if existing["state"] != "locked":
                return self.reply(409, {"state": "too_late"})
            if body["accept"] is True:
                boat = mission["boats"][proposed["slot"]]
                other = sum(record["demand"] for k, record in trips.items()
                            if record["slot"] == proposed["slot"] and k != booking)
                if proposed["demand"] + other > boat["capacity"]:
                    return self.reply(409, {"state": "capacity_unavailable"})
                updated = {**proposed, "state": "locked" if proposed["spans"] else "cancelled"}
                trips[booking] = updated
                final = {"state": "changed", "booking": dict(updated)}
            else:
                final = {"state": "denied", "booking": dict(existing)}
            item["response"] = (200, final)
            return self.reply(200, final)

    return ThreadingHTTPServer(("127.0.0.1", port), Source)


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8040)
    args = parser.parse_args()
    with create_source(args.port) as server:
        print(f"巡线资料服务在 http://127.0.0.1:{server.server_port}", flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
