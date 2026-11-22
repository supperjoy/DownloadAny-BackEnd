#!/usr/bin/env python3
"""admin 统计端点性能冒烟：可选灌种子，随后对 7 个端点各计时 3 次输出 p50。

用法（本地）:
  DATABASE_URL=sqlite+pysqlite:////tmp/admin-smoke.db UPDATE_ADMIN_TOKEN=<token> \\
    uv run uvicorn main:app --port 8010 &
  python scripts/smoke-admin-stats.py --base-url http://127.0.0.1:8010 --token <token> --seed 100000

--seed>0 时直连 DATABASE_URL 批量灌事件（media_url 轮询 8 站点根、500 个标题、
约 2000 台设备、近 60 天随机时间）。任一端点 p50 > 1500ms 退出码 1；top-media 或
events 超时时额外输出建议 EXPLAIN 与加索引的提示（不自动改 schema）。
生产（MySQL）计时请在服务器上以同样命令重跑一遍。
"""
import argparse
import json
import os
import random
import statistics
import sys
import time
import urllib.request
import uuid
from datetime import datetime, timedelta, timezone

ENDPOINTS = [
    "/api/analytics/admin/overview?days=30",
    "/api/analytics/admin/timeseries?days=30",
    "/api/analytics/admin/users?days=30",
    "/api/analytics/admin/downloads?days=30",
    "/api/analytics/admin/top-media?days=30",
    "/api/analytics/admin/events?days=30&limit=50",
    "/api/analytics/admin/system?days=30",
]
SLOW_HINT = (
    "top-media/events 超时：对相应查询跑 EXPLAIN，考虑按设计文档 §9 加索引 "
    "analytics_events(event_type, media_site, created_at)"
)


def seed_database(n: int) -> None:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import Session
    from sqlalchemy import insert

    sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "src"))
    from downloadany_backend.database import Base
    from downloadany_backend.models import AnalyticsEvent

    url = os.environ.get("DATABASE_URL", "")
    if not url:
        sys.exit("需要 DATABASE_URL（与目标服务同库）")
    engine = create_engine(url, future=True)
    Base.metadata.create_all(engine)

    sites = [
        "https://www.youtube.com/", "https://v.douyin.com/", "https://www.xiaohongshu.com/",
        "https://www.bilibili.com/", "https://channels.weixin.qq.com/", "https://www.tiktok.com/",
        "https://vimeo.com/", "https://twitter.com/",
    ]
    formats = ["1080p", "720p", "480p", "MP3", None]
    types = (["download_completed"] * 40 + ["download_started"] * 20 + ["parse_requested"] * 25
             + ["parse_succeeded"] * 15 + ["download_failed"] * 10 + ["app_open"] * 8
             + ["download_cancelled"] * 2)
    devices = [str(uuid.uuid4()) for _ in range(2000)]
    now = datetime.now(timezone.utc).replace(tzinfo=None)

    def make_row() -> dict:
        url_ = random.choice(sites)
        return {
            "event_type": random.choice(types),
            "event_id": str(uuid.uuid4()),
            "device_id": random.choice(devices),
            "client_session_id": str(uuid.uuid4()),
            "app_version": random.choice(["0.1.0", "0.1.1", "0.2.0"]),
            "platform": random.choice(["Win32", "Mac", "Linux"]),
            "media_site": url_.split("/")[2],
            "media_title": f"视频标题-{random.randint(0, 499)}",
            "media_url": url_,
            "format_label": random.choice(formats),
            "ip_address": f"10.{random.randint(0, 255)}.{random.randint(0, 255)}.{random.randint(1, 254)}",
            "created_at": now - timedelta(days=random.randint(0, 59),
                                          seconds=random.randint(0, 86_399)),
        }

    with Session(engine) as session:
        inserted = 0
        while inserted < n:
            batch = [make_row() for _ in range(min(5000, n - inserted))]
            session.execute(insert(AnalyticsEvent.__table__), batch)
            session.commit()
            inserted += len(batch)


def timed(path: str, base_url: str, token: str) -> float:
    req = urllib.request.Request(base_url.rstrip("/") + path,
                                 headers={"Authorization": f"Bearer {token}"})
    start = time.perf_counter()
    with urllib.request.urlopen(req, timeout=120) as resp:
        resp.read()
    return (time.perf_counter() - start) * 1000


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--base-url", required=True)
    ap.add_argument("--token", required=True)
    ap.add_argument("--seed", type=int, default=0)
    args = ap.parse_args()

    if args.seed > 0:
        random.seed(42)
        t0 = time.perf_counter()
        seed_database(args.seed)
        print(f"seeded {args.seed} events in {time.perf_counter() - t0:.1f}s", file=sys.stderr)

    report = {}
    failed = False
    for path in ENDPOINTS:
        samples = [timed(path, args.base_url, args.token) for _ in range(3)]
        p50 = round(statistics.median(samples), 1)
        report[path] = p50
        if p50 > 1500:
            failed = True
            if "top-media" in path or "events?" in path:
                print(SLOW_HINT, file=sys.stderr)

    print(json.dumps(report, indent=2, ensure_ascii=False))
    sys.exit(1 if failed else 0)


if __name__ == "__main__":
    main()
