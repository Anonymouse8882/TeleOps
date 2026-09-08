"""忙循环检测。

规格的要求很具体也很克制：允许永久循环、不许用固定跳数上限一刀切、要区分
「合法长期循环」（每圈之间有等待或新输入、内容在变）和「非法忙循环」（同一条
消息无变化地高速重复过同一个环），跳闸只暂停那条分支、不许杀掉整个工作流。

判据的口径必须和「被保护的资源」对齐，这是第一版栽过的跟头：第一版按「同一节点
两次重访之间的平均圈时 < 200ms」判高速，而全局限速把整张图压在 20 跳/秒，一条链的
圈时下界就是 环长 × 同时在环里的链数 / 20 秒 —— 只要这个乘积 ≥5，判据永远不成立，
检测器等于不存在。而生产工作流一次采 5 条就是 5 条并发链。

现在的三条判据：

  1. 真的绕回来了 —— path（含当前节点）里出现重复 node_id。宽扇出的图里，同一条
     因果链合法地多次经过同一个汇聚节点，只看次数必然误判。
  2. 这条**因果链**自己在 60 秒窗口里吃掉了 > 300 跳（≈5 跳/秒，全局预算的四分之一）。
     按链算而不是按节点算，与环长、并发链数都无关。
  3. 没有实质进展。两种形态都算：
     a) 60 秒窗口内这条链在这个节点上只见过 ≤5 个不同的内容签名 —— 同时覆盖
        「内容恒定」「A/B 两态翻转」「小周期轮转」；只看"连续相同"会被翻转绕过。
     b) payload 的长度连续增长 ≥20 次 —— 正文只涨不停是没有进展的铁证，
        典型来源是环里放了个不幂等的排版节点（每圈往正文后面再追加一次页脚）。

跳闸之前先软刹车，退避 2^n 秒封顶 30 秒。刹车的判定不带「高速」条件：
高速正是刹车会破坏掉的东西，带上它就会刹一次之后再也刹不动，兜底路径够不着。
"""
from __future__ import annotations

import hashlib
import json
import logging
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any

log = logging.getLogger(__name__)

WINDOW_SECONDS = 60.0        # 滑动窗口
# 一条因果链在窗口里最多吃多少跳（3 跳/秒）。标定依据：一条内容走完一张 10 节点的图
# 才 10 跳，正常工作流一条链一辈子也用不到 180 跳；而全局预算被 K 条链分摊时
# 每条只有 20/K 跳/秒，定太高（比如 5 跳/秒）会让"多条链同时绕环"整体逃过检测——
# 生产的采集一次就产出 5 条，正是这个形态。
CHAIN_HOPS_MAX = 180
DISTINCT_SIG_MAX = 5         # 窗口内不同内容签名不超过这个数，就算"没有实质变化"
GROW_STREAK_MAX = 20         # payload 连续增长多少次算失控
MIN_REVISITS = 10            # 重访太少不判，避免刚起步就误伤
SOFT_BRAKE_AT = 0.5          # 跳数预算用掉一半开始软刹车
MAX_BRAKES = 5               # 刹这么多次还是原地打转就不用再取证了
MAX_TRACKED = 5000           # 跟踪表上限
# 速率兜底：一条链持续超预算这么久，无论内容有没有变都先挂起。
# 针对的是"内容每跳都变、但根本没有进展"的形态（比如 payload 里带个时间戳）——
# 这种签名永远不重复，靠内容判据抓不到。用"持续速率"而不是"累计跳数"是有意的：
# 规格不许用固定跳数上限禁掉合法的长期循环，而慢速的长期循环永远不会碰到速率线。
SUSTAIN_SECONDS = 600.0


@dataclass
class Verdict:
    action: str = "ok"                     # ok / slow / trip
    delay: float = 0.0
    detail: dict[str, Any] = field(default_factory=dict)


@dataclass
class _Chain:
    """一条因果链（origin）的跳数预算。"""
    hops: deque = field(default_factory=lambda: deque(maxlen=CHAIN_HOPS_MAX * 2))
    over_since: float = 0.0      # 从什么时候开始一直超预算
    last_seen: float = 0.0


@dataclass
class _Spot:
    """某条链在某个节点上的内容变化情况。"""
    sigs: deque = field(default_factory=lambda: deque(maxlen=400))   # (时刻, 签名)
    last_size: int = -1
    grow_streak: int = 0
    brakes: int = 0
    last_seen: float = 0.0


def signature(payload: dict[str, Any]) -> str:
    try:
        return hashlib.sha1(
            json.dumps(payload or {}, sort_keys=True, ensure_ascii=False, default=str)
            .encode("utf-8", "ignore")).hexdigest()[:16]
    except Exception:
        return "?"


def payload_size(payload: dict[str, Any]) -> int:
    try:
        return len(json.dumps(payload or {}, ensure_ascii=False, default=str))
    except Exception:
        return 0


def has_real_cycle(path: list[Any]) -> bool:
    seen: set[str] = set()
    for n in path or []:
        if n in seen:
            return True
        seen.add(n)
    return False


def _evict(d: dict[Any, Any]) -> None:
    if len(d) >= MAX_TRACKED:
        oldest = min(d, key=lambda k: d[k].last_seen)
        d.pop(oldest, None)


class RunawayDetector:
    """状态放内存：只有一个消费者在写，重启之后失控的环几十秒就会重新暴露，
    不值得为它每跳写一次库。"""

    def __init__(self) -> None:
        self._chains: dict[tuple[int, str], _Chain] = {}
        self._spots: dict[tuple[int, str, str], _Spot] = {}
        # 用户点过"恢复"的因果链：速率兜底对它放行。
        # 恢复这个动作本身就是用户在说"这条链是合法的"，再按速率拦一次没有意义
        # （拦了它十分钟后还会再被拦，用户只会陷入恢复-again 的循环）。
        # 只豁免速率这一条，"内容原地打转"仍然照拦；进程重启后重新武装。
        self._rate_exempt: set[tuple[int, str]] = set()

    def observe(self, msg: dict[str, Any]) -> Verdict:
        origin = msg.get("origin_id") or ""
        node = msg.get("node_id") or ""
        wid = msg.get("workflow_id") or 0
        if not origin:
            return Verdict()
        now = time.monotonic()

        # —— 链级：这条因果链在窗口里吃了多少跳 ——
        ckey = (wid, origin)
        chain = self._chains.get(ckey)
        if chain is None:
            _evict(self._chains)
            chain = self._chains[ckey] = _Chain()
        chain.last_seen = now
        chain.hops.append(now)
        while chain.hops and now - chain.hops[0] > WINDOW_SECONDS:
            chain.hops.popleft()
        chain_hops = len(chain.hops)
        if chain_hops > CHAIN_HOPS_MAX:
            chain.over_since = chain.over_since or now
        else:
            chain.over_since = 0.0
        sustained = chain.over_since and (now - chain.over_since) > SUSTAIN_SECONDS

        # —— 节点级：内容有没有实质变化 ——
        skey = (wid, node, origin)
        spot = self._spots.get(skey)
        if spot is None:
            _evict(self._spots)
            spot = self._spots[skey] = _Spot()
        spot.last_seen = now
        payload = msg.get("payload") or {}
        spot.sigs.append((now, signature(payload)))
        while spot.sigs and now - spot.sigs[0][0] > WINDOW_SECONDS:
            spot.sigs.popleft()
        distinct = len({s for _, s in spot.sigs})
        revisits = len(spot.sigs)

        size = payload_size(payload)
        if spot.last_size >= 0 and size > spot.last_size:
            spot.grow_streak += 1
        elif size != spot.last_size:
            spot.grow_streak = 0
        spot.last_size = size

        # 真环判定要把当前节点算进去：刚绕回起点的那一跳，path 里还没有它
        cycle = has_real_cycle(list(msg.get("path") or []) + [node])
        no_progress = (revisits >= MIN_REVISITS and distinct <= DISTINCT_SIG_MAX) \
            or spot.grow_streak >= GROW_STREAK_MAX
        stuck = cycle and no_progress

        detail = {
            "chain_hops": chain_hops, "revisits": revisits, "distinct_sigs": distinct,
            "grow_streak": spot.grow_streak, "real_cycle": cycle, "brakes": spot.brakes,
            "path_tail": (list(msg.get("path") or []) + [node])[-12:],
        }

        detail["over_seconds"] = round(now - chain.over_since, 1) if chain.over_since else 0
        if stuck and (chain_hops > CHAIN_HOPS_MAX or spot.brakes >= MAX_BRAKES):
            self._spots.pop(skey, None)
            self._chains.pop(ckey, None)
            return Verdict("trip", detail=detail)
        # 速率兜底：内容一直在变、抓不到"没有进展"，但这条链已经持续几分钟
        # 吃掉四分之一的全局预算了。慢速的合法长环永远碰不到这条线。
        if cycle and sustained and ckey not in self._rate_exempt:
            self._spots.pop(skey, None)
            self._chains.pop(ckey, None)
            detail["reason"] = "rate"
            return Verdict("trip", detail=detail)

        # 软刹车不带"高速"条件：高速正是刹车会破坏掉的东西，
        # 带上它就会刹一次之后再也刹不动，上面那条 brakes 兜底路径永远够不着。
        if stuck and chain_hops > CHAIN_HOPS_MAX * SOFT_BRAKE_AT:
            spot.brakes += 1
            detail["brakes"] = spot.brakes
            return Verdict("slow", delay=min(2 ** min(spot.brakes, 5), 30.0), detail=detail)
        return Verdict(detail=detail)

    def forget(self, workflow_id: int, origin_id: str, *, rate_exempt: bool = False) -> None:
        if rate_exempt:
            if len(self._rate_exempt) > MAX_TRACKED:
                self._rate_exempt.clear()
            self._rate_exempt.add((workflow_id, origin_id))
        self._chains.pop((workflow_id, origin_id), None)
        for k in [k for k in self._spots if k[0] == workflow_id and k[2] == origin_id]:
            self._spots.pop(k, None)
