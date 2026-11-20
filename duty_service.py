"""海缆巡线值班服务：在实际领取原件时把船次和区段一起跟住。

用法：
    python3 source_service.py --port 8040          # 先起演练台
    python3 duty_service.py --source http://127.0.0.1:8040 --port 8050

状态写入 --state 指定的文件；关掉再打开会从文件恢复，不会重复占船。
"""
from argparse import ArgumentParser
from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent))

from duty.server import create_duty
from duty.service import DutyService


def main():
    parser = ArgumentParser(description=__doc__)
    parser.add_argument("--source", default="http://127.0.0.1:8040",
                        help="演练台（巡检资料服务）地址")
    parser.add_argument("--port", type=int, default=8050, help="本服务端口")
    parser.add_argument("--state", default="duty_state.json", help="状态文件")
    args = parser.parse_args()
    svc = DutyService(args.source, args.state)
    with create_duty(svc, args.port) as server:
        print(f"值班服务在 http://127.0.0.1:{server.server_port}（来源 {args.source}）",
              flush=True)
        server.serve_forever()


if __name__ == "__main__":
    main()
