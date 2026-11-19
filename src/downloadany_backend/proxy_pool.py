"""mihomo (Clash Meta) 代理池：为 yt-dlp 解析随机固定一个测活通过的出口节点。

只依赖 External Controller RESTful API（默认 127.0.0.1:9090）：
- GET /proxies              列出策略组与节点
- GET /proxies/{node}/delay 实时测活（非 200 = 节点不可用；成功顺带刷新内核 alive 缓存）
- PUT /proxies/{Selector}   把组出口固定为指定节点（仅 Selector 组接受手动指定）

mihomo 没有"取一个 alive 节点"的端点，所以顺序固定为：洗牌 → 并发测活 →
只在通过者里随机 → PUT。切换失败一律返回 None，由调用方回退 ytdlp_proxy。
"""

import logging
import random
import re
import threading
import urllib.parse
from concurrent.futures import ThreadPoolExecutor

import requests

from .config import Settings

logger = logging.getLogger(__name__)

GROUP_TYPES = {"Selector", "URLTest", "Fallback", "LoadBalance"}
BUILT_IN_POLICIES = {"DIRECT", "REJECT", "PASS", "COMPATIBLE", "TUN"}
# 机场订阅里的流量/到期信息伪节点不是真实出口（服务器实测会被混进组里）。
INFO_NAME = re.compile(r"剩余|流量|到期|过期|套餐|官网|公告|通知|折扣|经验")

_switch_lock = threading.Lock()


def enabled(settings: Settings) -> bool:
    return bool(settings.mihomo_controller_url)


def _base(settings: Settings) -> str:
    return settings.mihomo_controller_url.rstrip("/")


def _headers(settings: Settings) -> dict[str, str]:
    if settings.mihomo_secret:
        return {"Authorization": f"Bearer {settings.mihomo_secret}"}
    return {}


def _candidate_pool(settings: Settings) -> list[str] | None:
    """返回 Selector 组里的真实叶子节点；组缺失/非 Selector/池空时给 None。"""
    response = requests.get(
        f"{_base(settings)}/proxies", headers=_headers(settings), timeout=10
    )
    response.raise_for_status()
    proxies = response.json().get("proxies") or {}
    group = proxies.get(settings.mihomo_group)
    if group is None:
        logger.warning("mihomo 策略组不存在：%s", settings.mihomo_group)
        return None
    if group.get("type") != "Selector":
        logger.warning(
            "mihomo 策略组 %s 类型为 %s，不支持手动固定节点",
            settings.mihomo_group,
            group.get("type"),
        )
        return None
    return [
        name
        for name in group.get("all", [])
        if name not in BUILT_IN_POLICIES
        and not INFO_NAME.search(name)
        and (proxies.get(name) or {}).get("type") not in GROUP_TYPES
    ]


def _probe_delay(settings: Settings, name: str) -> int | None:
    url = f"{_base(settings)}/proxies/{urllib.parse.quote(name, safe='')}/delay"
    try:
        response = requests.get(
            url,
            params={
                "url": settings.mihomo_probe_url,
                "timeout": settings.mihomo_probe_timeout * 1000,
            },
            headers=_headers(settings),
            timeout=settings.mihomo_probe_timeout + 5,
        )
    except requests.RequestException:
        return None
    if response.status_code != 200:
        return None
    try:
        delay = response.json().get("delay")
    except ValueError:
        return None
    return int(delay) if isinstance(delay, (int, float)) and delay >= 0 else None


def _select(settings: Settings, name: str) -> bool:
    url = f"{_base(settings)}/proxies/{urllib.parse.quote(settings.mihomo_group, safe='')}"
    try:
        response = requests.put(
            url, json={"name": name}, headers=_headers(settings), timeout=10
        )
        response.raise_for_status()
    except requests.RequestException as error:
        logger.warning("mihomo 固定节点失败 %s：%s", name, error)
        return False
    return True


def switch_to_random_active(settings: Settings, avoid: str | None = None) -> str | None:
    """测活通过的节点里随机选一个并固定为组出口；返回选中的节点名或 None。

    avoid 用于重试轮换：优先排除上一次钉住的节点（只剩它一个时不拒绝）。
    锁内完成整个"列表-测活-固定"，避免并发解析互相覆盖刚选的出口；
    探测并发规模与 parse_slots 同量级即可，不追求全部节点。
    """
    if not enabled(settings):
        return None
    try:
        with _switch_lock:
            pool = _candidate_pool(settings)
            if not pool:
                return None
            pool = [name for name in pool if name != avoid] or pool
            candidates = random.sample(pool, min(settings.mihomo_probe_limit, len(pool)))
            with ThreadPoolExecutor(max_workers=min(8, len(candidates))) as executor:
                results = list(
                    executor.map(lambda name: (name, _probe_delay(settings, name)), candidates)
                )
            alive = [name for name, delay in results if delay is not None]
            if not alive:
                logger.warning("mihomo 测活通过 0/%d，跳过切换", len(candidates))
                return None
            chosen = random.choice(alive)
            if not _select(settings, chosen):
                return None
            logger.info(
                "mihomo 出口已固定：%s（测活通过 %d/%d）", chosen, len(alive), len(candidates)
            )
            return chosen
    except (requests.RequestException, ValueError) as error:
        logger.warning("mihomo 控制端不可用：%s", error)
        return None
