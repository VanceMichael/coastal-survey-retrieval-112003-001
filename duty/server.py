"""值班查询接口：按区段查证据与待补页，按船次核对已确认和待变更安排。"""
import json
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlsplit


def create_duty(svc, port=0):
    class Duty(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass

        def reply(self, status, value):
            body = json.dumps(value, ensure_ascii=False).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            try:
                if not parts:
                    return self.reply(200, svc.info())
                if parts == ["status"]:
                    return self.reply(200, svc.status())
                if parts == ["sections"]:
                    return self.reply(200, svc.sections())
                if len(parts) == 2 and parts[0] == "sections":
                    value = svc.section(parts[1])
                    return self.reply(200, value) if value else self.reply(404, {"state": "unknown"})
                if parts == ["trips"]:
                    return self.reply(200, svc.trips())
                if len(parts) == 2 and parts[0] == "trips":
                    value = svc.trip(parts[1])
                    return self.reply(200, value) if value else self.reply(404, {"state": "unknown"})
                if parts == ["originals"]:
                    return self.reply(200, svc.originals())
                if len(parts) == 2 and parts[0] == "audit":
                    value = svc.audit(parts[1])
                    return self.reply(200, value) if value else self.reply(404, {"state": "unknown"})
                if parts == ["missed"]:
                    return self.reply(200, svc.missed())
                if parts == ["notes"]:
                    return self.reply(200, svc.notes())
                if parts == ["log"]:
                    return self.reply(200, svc.log())
                return self.reply(404, {"state": "unknown"})
            except (KeyError, ValueError, TypeError) as error:
                return self.reply(400, {"state": "bad_request", "detail": str(error)})

        def do_POST(self):
            parts = [p for p in urlsplit(self.path).path.split("/") if p]
            try:
                length = int(self.headers.get("Content-Length") or 0)
                body = json.loads(self.rfile.read(length)) if length else {}
                if parts == ["tick"]:
                    return self.reply(200, svc.tick())
                if parts == ["qc"]:
                    return self.reply(200, svc.qc(body))
                return self.reply(404, {"state": "unknown"})
            except (KeyError, ValueError, TypeError) as error:
                return self.reply(400, {"state": "bad_request", "detail": str(error)})

    return ThreadingHTTPServer(("127.0.0.1", port), Duty)
