"""来源服务（网关/船务台）的 HTTP 客户端。"""
import json
from urllib.error import HTTPError
from urllib.request import Request, urlopen


class SourceClient:
    def __init__(self, base, timeout=5):
        self.base = base.rstrip("/")
        self.timeout = timeout

    def get(self, path):
        return self._call("GET", path, None)

    def post(self, path, body):
        return self._call("POST", path, body)

    def _call(self, method, path, body):
        data = json.dumps(body).encode() if body is not None else None
        request = Request(self.base + path, data=data, method=method,
                          headers={"Content-Type": "application/json"})
        try:
            response = urlopen(request, timeout=self.timeout)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read()
        try:
            return response.code, json.loads(raw)
        except ValueError:
            return response.code, {}
