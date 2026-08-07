"""GC001 早盘盘口信号引擎（回测与实盘共用）。

只做特征计算与触发判定，不含任何下单逻辑：
- BookFeatures：microprice(1/5档)、标准 OFI、touch-OFI、吃墙/撤墙分解、
  卖墙消失（Cancel Ratio 代理）、价格/微价/卖一变动；
- triggers()：eat / wallgone / jump / ofi 四类触发判定；
- price_to_tick()：GC001 最小变动价位取整。

设计说明（来自 3 天回测实证）：
- 标准 OFI 在 GC001 开盘被"卖墙进场"主导，方向常与脉冲相反；
- 吃墙（价格上行时卖盘深度被消耗）与撤墙（价格回落时卖墙消失）
  分解是有效信号：eat 对应连续扫墙型，wallgone 是撤墙诱空后的领先信号；
- jump（微价一帧跳升>=2tick）与 ofi（OFI 爆发）作为参照组。
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from typing import Any

import numpy as np
import pandas as pd


TICK = 0.005
WALL_ABS = 3_000_000          # 卖墙绝对规模阈值（QMT 手）
WALL_GONE_RATIO = 0.8         # 墙缩水超过该比例视为消失
WALL_TOT_DROP = 0.4           # 同时要求总卖盘下降比例
EAT_FRAC = 0.20               # 吃墙触发阈值
CANCEL_FRAC = 0.30            # 撤墙触发阈值
MICRO_JUMP_TICKS = 2          # jump 触发阈值
OFI_NORM = 0.15               # ofi 触发阈值


def price_to_tick(value: float, *, direction: str = "up") -> float:
    """按 GC001 最小变动价位 0.005 取整。direction=up 用于挂单（不低于目标价）。"""
    ticks = float(value) / TICK
    if direction == "up":
        rounded = math.ceil(ticks - 1e-9)
    else:
        rounded = math.floor(ticks + 1e-9)
    return round(rounded * TICK, 3)


def _level_map(prices: list[float], vols: list[float]) -> dict[float, float]:
    out: dict[float, float] = {}
    for p, v in zip(prices, vols):
        if p is None or v is None:
            continue
        p = round(float(p), 3)
        v = float(v)
        if v > 0:
            out[p] = out.get(p, 0.0) + v
    return out


@dataclass
class BookFeatures:
    ts: pd.Timestamp
    hms: str
    last: float
    ask1: float
    bid1: float
    micro1: float
    micro5: float
    tot_bid: float
    tot_ask: float
    imb: float
    ofi: float
    ofi_norm: float
    touch_ofi: float
    ask_lost_frac: float          # 卖盘同价位深度损失 / 前帧卖盘总量
    ask_eaten_frac: float         # 价格上行时的卖盘消耗比例
    ask_cancel_frac: float        # 价格回落时的卖盘撤单比例
    wall_disappear: bool          # 单帧内大卖墙消失且价格未上行
    d_micro1: float
    d_ask1: float
    d_last: float


def build_book_features(frame: pd.DataFrame) -> list[BookFeatures]:
    """逐帧计算盘口特征。frame 必须含 bid/ask 五档价量列与 lastPrice。"""
    out: list[BookFeatures] = []
    rows = frame.to_dict("records")
    prev: dict[str, Any] | None = None
    prev2: dict[str, Any] | None = None

    for row in rows:
        feat = _features_from_row(row, prev, prev2, out[-1] if out else None)
        out.append(feat)
        prev2 = prev
        prev = row
    return out


def _features_from_row(
    row: dict[str, Any],
    prev: dict[str, Any] | None,
    prev2: dict[str, Any] | None,
    prev_feat: BookFeatures | None,
) -> BookFeatures:
    ask_p = [float(x) for x in row["askPrice"]][:5] if isinstance(row["askPrice"], list) else []
    ask_v = [float(x) for x in row["askVol"]][:5] if isinstance(row["askVol"], list) else []
    bid_p = [float(x) for x in row["bidPrice"]][:5] if isinstance(row["bidPrice"], list) else []
    bid_v = [float(x) for x in row["bidVol"]][:5] if isinstance(row["bidVol"], list) else []

    ask1 = ask_p[0] if ask_p and ask_v and ask_v[0] > 0 else float("nan")
    bid1 = bid_p[0] if bid_p and bid_v and bid_v[0] > 0 else float("nan")
    tot_ask = float(sum(ask_v))
    tot_bid = float(sum(bid_v))
    last = float(row["lastPrice"])
    ts = row["ts"]
    hms = row.get("hms", str(ts)[11:19])

    if np.isfinite(bid1) and np.isfinite(ask1) and (ask_v[0] + bid_v[0]) > 0:
        micro1 = (bid1 * ask_v[0] + ask1 * bid_v[0]) / (ask_v[0] + bid_v[0])
    elif np.isfinite(ask1):
        micro1 = ask1
    else:
        micro1 = last
    tot = tot_ask + tot_bid
    micro5 = (
        (sum(p * v for p, v in zip(ask_p, ask_v)) + sum(p * v for p, v in zip(bid_p, bid_v))) / tot
        if tot > 0
        else last
    )
    imb = (tot_bid - tot_ask) / tot if tot > 0 else 0.0

    ofi = float("nan")
    ofi_norm = 0.0
    touch_ofi = float("nan")
    ask_eaten = 0.0
    ask_cancel = 0.0
    ask_lost = 0.0
    wall_disappear = False

    if prev is not None:
        prev_ask_p = [float(x) for x in prev["askPrice"]][:5]
        prev_ask_v = [float(x) for x in prev["askVol"]][:5]
        prev_bid_p = [float(x) for x in prev["bidPrice"]][:5]
        prev_bid_v = [float(x) for x in prev["bidVol"]][:5]
        prev_ask_map = _level_map(prev_ask_p, prev_ask_v)
        prev_bid_map = _level_map(prev_bid_p, prev_bid_v)
        cur_ask_map = _level_map(ask_p, ask_v)
        cur_bid_map = _level_map(bid_p, bid_v)

        db = sum(
            cur_bid_map.get(p, 0.0) - prev_bid_map.get(p, 0.0)
            for p in set(prev_bid_map) | set(cur_bid_map)
        )
        da = sum(
            cur_ask_map.get(p, 0.0) - prev_ask_map.get(p, 0.0)
            for p in set(prev_ask_map) | set(cur_ask_map)
        )
        ofi = db - da
        prev_tot = float(sum(prev_ask_v)) + float(sum(prev_bid_v))
        ofi_norm = ofi / prev_tot if prev_tot > 0 else 0.0

        # 吃墙/撤墙分解：
        #   同价位损失（两帧都存在该价位）按价格方向分类；
        #   消失价位：价格 <= 当前 lastPrice 视为被成交穿透（吃墙），
        #             高于当前 lastPrice 视为撤走/移出视野（撤墙）。
        lost_same = 0.0
        lost_gone_eaten = 0.0
        lost_gone_cancel = 0.0
        lost_union = 0.0
        for p, v in prev_ask_map.items():
            if p in cur_ask_map:
                lost_same += max(0.0, v - cur_ask_map[p])
            elif p <= last + 1e-9:
                lost_gone_eaten += v
            else:
                lost_gone_cancel += v
        lost_union = lost_same + lost_gone_eaten + lost_gone_cancel
        prev_tot_ask = float(sum(prev_ask_v))
        d_last = last - float(prev["lastPrice"])
        prev_last = float(prev["lastPrice"])
        prev_ask1 = float(prev["askPrice"][0]) if isinstance(prev["askPrice"], list) and prev["askPrice"] else float("nan")
        d_ask1 = ask1 - prev_ask1 if np.isfinite(prev_ask1) and np.isfinite(ask1) else 0.0
        trend2 = d_last + (prev_last - (float(prev2["lastPrice"]) if prev2 else prev_last))
        rising = d_last >= 0 and trend2 >= TICK * 0.5
        falling = d_last <= 0 and trend2 <= -TICK * 0.5
        if prev_tot_ask > 0:
            ask_lost = lost_union / prev_tot_ask
            if rising:
                ask_eaten = (lost_same + lost_gone_eaten) / prev_tot_ask
                ask_cancel = lost_gone_cancel / prev_tot_ask
            elif falling:
                ask_cancel = (lost_same + lost_gone_cancel) / prev_tot_ask
                ask_eaten = lost_gone_eaten / prev_tot_ask
            else:
                ask_eaten = (lost_same + lost_gone_eaten) / prev_tot_ask
                ask_cancel = (lost_same + lost_gone_cancel) / prev_tot_ask
        # 卖墙消失：前帧存在 >= WALL_ABS 的档位，本帧缩水 80% 以上，且价格未上行
        wall = max(prev_ask_map.values(), default=0.0)
        if wall >= WALL_ABS and prev_tot_ask > 0:
            cur_wall = max(cur_ask_map.get(p, 0.0) for p in prev_ask_map) if cur_ask_map else 0.0
            if (
                cur_wall <= WALL_GONE_RATIO * wall
                and (prev_tot_ask - tot_ask) / prev_tot_ask >= WALL_TOT_DROP
                and d_ask1 <= TICK * 0.5
            ):
                wall_disappear = True

    d_micro1 = micro1 - prev_feat.micro1 if prev_feat is not None else 0.0
    d_ask1 = ask1 - prev_feat.ask1 if prev_feat is not None and np.isfinite(ask1) else float("nan")
    d_last = last - prev_feat.last if prev_feat is not None else 0.0

    return BookFeatures(
        ts=ts,
        hms=hms,
        last=last,
        ask1=ask1,
        bid1=bid1,
        micro1=micro1,
        micro5=micro5,
        tot_bid=tot_bid,
        tot_ask=tot_ask,
        imb=imb,
        ofi=ofi,
        ofi_norm=ofi_norm,
        touch_ofi=touch_ofi,
        ask_lost_frac=ask_lost,
        ask_eaten_frac=ask_eaten,
        ask_cancel_frac=ask_cancel,
        wall_disappear=wall_disappear,
        d_micro1=d_micro1,
        d_ask1=d_ask1,
        d_last=d_last,
    )


def triggers(feat: BookFeatures) -> list[str]:
    """返回本帧命中的触发类型。eat/wallgone 是主信号，jump/ofi 是参照。"""
    fired: list[str] = []
    # eat 确认：微价上行 OR 卖一抬价（借钱方抬价扫货的直接可见特征）
    if feat.ask_eaten_frac >= EAT_FRAC and (feat.d_micro1 >= TICK or feat.d_ask1 >= TICK):
        fired.append("eat")
    if feat.wall_disappear and feat.ask_cancel_frac >= CANCEL_FRAC:
        fired.append("wallgone")
    if feat.d_micro1 >= MICRO_JUMP_TICKS * TICK:
        fired.append("jump")
    if feat.ofi_norm >= OFI_NORM and feat.d_micro1 >= TICK:
        fired.append("ofi")
    return fired


def pick_trigger(fired: list[str]) -> str | None:
    """复合策略优先级：eat > wallgone > jump > ofi。"""
    for name in ("eat", "wallgone", "jump", "ofi"):
        if name in fired:
            return name
    return None


class IncrementalSignalEngine:
    """实盘逐帧增量信号引擎：喂入一帧 tick（dict），返回该帧特征。

    row 需要包含：ts(pd.Timestamp/datetime)、hms(可选)、lastPrice、
    askPrice、askVol、bidPrice、bidVol（各为五档 list）。
    """

    def __init__(self) -> None:
        self._prev: dict[str, Any] | None = None
        self._prev2: dict[str, Any] | None = None
        self._prev_feat: BookFeatures | None = None
        self.frames: list[BookFeatures] = []

    def update(self, row: dict[str, Any]) -> BookFeatures:
        feat = _features_from_row(row, self._prev, self._prev2, self._prev_feat)
        self.frames.append(feat)
        self._prev2 = self._prev
        self._prev = row
        self._prev_feat = feat
        return feat

    @property
    def prev_feat(self) -> BookFeatures | None:
        return self._prev_feat
