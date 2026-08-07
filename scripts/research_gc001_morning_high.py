"""GC001 morning-high prediction research.

Goal: using only information observable from the call-auction print through the
first five minutes of continuous trading (09:31-09:35), decide a limit sell
price for GC001 that captures the early-morning spike with a known fill
probability.

The script is research-only: it never connects to a broker and places no
orders. Fill logic uses 1-minute OHLC bars as a proxy (a limit sell at price P
is deemed filled when a later minute high >= P). Historical bid1 is not
available, so this proxy is the documented limitation of the backtest.
"""

from __future__ import annotations

import argparse
import json
from datetime import date
from pathlib import Path

import numpy as np
import pandas as pd


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_INPUT = Path(
    r"D:\gitee\miniQMT\data\gc001_kronos\gc001_1m.parquet"
)
DEFAULT_OUTPUT = ROOT / "reports" / "gc001_intraday" / "morning_high_predict"

TICK = 0.005
F5_MINUTES = ["09:31", "09:32", "09:33", "09:34", "09:35"]
SPLIT_RATIO = 0.70
FALLBACK_MINUTE = "10:00"

CAL_COLS = [
    "dow", "is_dow_0", "is_dow_1", "is_dow_2", "is_dow_3", "is_dow_4",
    "month", "week_of_month", "days_to_month_end",
    "td_to_month_end", "td_to_quarter_end",
    "is_last1_td_month", "is_last2_td_month", "is_last3_td_month",
    "is_last1_td_quarter", "is_last2_td_quarter",
]
PRIOR_COLS = [
    "prev_morning_high", "prev_auction", "prev_day_close",
    "prev_f5_high", "prev_f5_close", "prev_spike",
    "mh_ma5", "mh_ma10", "mh_ma20",
    "spike_ma5", "spike_ma10", "spike_ma20",
    "auction_ma5", "auction_ma10", "auction_ma20",
]
T1_COLS = [
    "auction", "auction_vol", "auction_vol_log",
    "auc_minus_prev_close", "auc_minus_prev_mh",
]
T2_COLS = [
    "f5_open", "f5_high", "f5_low", "f5_close",
    "f5_vol", "f5_amount", "f5_vol_log", "f5_amount_log",
    "f5_high_minus_auction", "f5_close_minus_auction",
    "f5_open_minus_auction", "f5_high_minus_open", "f5_range",
    "f5_high_minus_close", "f5_close_minus_open",
]
FEATURES_T1 = CAL_COLS + PRIOR_COLS + T1_COLS
FEATURES_T2 = CAL_COLS + PRIOR_COLS + T1_COLS + T2_COLS


def setup_matplotlib() -> None:
    """Use Microsoft YaHei so CJK labels render on Windows."""
    import matplotlib
    from matplotlib import font_manager

    matplotlib.use("Agg")
    yahei = Path(r"C:\Windows\Fonts\msyh.ttc")
    if yahei.is_file():
        font_manager.fontManager.addfont(str(yahei))
    import matplotlib.pyplot as plt

    plt.rcParams["font.family"] = "Microsoft YaHei"
    plt.rcParams["axes.unicode_minus"] = False


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="GC001 morning-high prediction research (no orders)."
    )
    parser.add_argument("--input", type=Path, default=DEFAULT_INPUT)
    parser.add_argument("--output", type=Path, default=DEFAULT_OUTPUT)
    parser.add_argument("--split-ratio", type=float, default=SPLIT_RATIO)
    parser.add_argument(
        "--seed", type=int, default=20260806, help="rng seed for xgboost"
    )
    return parser.parse_args()


def load_bars(path: Path) -> pd.DataFrame:
    df = pd.read_parquet(path)
    df.index = pd.to_datetime(df.index)
    df = df.sort_index()
    df["hm"] = df.index.strftime("%H:%M")
    if df.index.has_duplicates:
        raise ValueError("duplicate timestamps in input")
    return df


def trading_day_flags(idx: pd.DatetimeIndex) -> pd.DataFrame:
    """Calendar features known before the open (no market data used)."""
    cal = pd.DataFrame(index=idx)
    cal["dow"] = idx.dayofweek
    for d in range(5):
        cal[f"is_dow_{d}"] = (idx.dayofweek == d).astype(int)
    cal["month"] = idx.month
    cal["week_of_month"] = ((idx.day - 1) // 7) + 1
    cal["days_to_month_end"] = (
        (idx + pd.offsets.MonthEnd(0)) - idx
    ).days
    # trading-day distance to month/quarter end (0 = last trading day)
    positions = np.arange(len(idx))
    month_groups = idx.to_period("M")
    quarter_groups = idx.to_period("Q")
    td_to_month_end = pd.Series(np.nan, index=idx)
    td_to_quarter_end = pd.Series(np.nan, index=idx)
    for period in month_groups.unique():
        mask = month_groups == period
        last_pos = positions[mask].max()
        td_to_month_end[mask] = last_pos - positions[mask]
    for period in quarter_groups.unique():
        mask = quarter_groups == period
        last_pos = positions[mask].max()
        td_to_quarter_end[mask] = last_pos - positions[mask]
    cal["td_to_month_end"] = td_to_month_end
    cal["td_to_quarter_end"] = td_to_quarter_end
    cal["is_last1_td_month"] = (cal["td_to_month_end"] == 0).astype(int)
    cal["is_last2_td_month"] = (cal["td_to_month_end"] <= 1).astype(int)
    cal["is_last3_td_month"] = (cal["td_to_month_end"] <= 2).astype(int)
    cal["is_last1_td_quarter"] = (cal["td_to_quarter_end"] == 0).astype(int)
    cal["is_last2_td_quarter"] = (cal["td_to_quarter_end"] <= 1).astype(int)
    return cal


def build_session_table(df: pd.DataFrame) -> pd.DataFrame:
    """One row per trading day with decision-time features and targets."""
    rows: list[dict] = []
    for day, g in df.groupby("trade_date", sort=True):
        auc = g[g["hm"] == "09:30"]
        if len(auc) != 1:
            continue
        auc = auc.iloc[0]
        cont = g[(g.index.hour < 12) & (g["hm"] != "09:30")]  # 09:31..11:30
        f5 = g[g["hm"].isin(F5_MINUTES)]
        if len(f5) != len(F5_MINUTES) or len(cont) < 100:
            continue
        aft = g[g.index.hour >= 13]  # 13:01..15:30
        mh_idx = cont["high"].idxmax()
        rows.append(
            {
                "date": day,
                # auction (09:30 bar is the flat call-auction print)
                "auction": auc["open"],
                "auction_vol": auc["volume"],
                # first five continuous minutes
                "f5_open": f5["open"].iloc[0],
                "f5_high": f5["high"].max(),
                "f5_low": f5["low"].min(),
                "f5_close": f5["close"].iloc[-1],
                "f5_vol": f5["volume"].sum(),
                "f5_amount": f5["amount"].sum(),
                "f5_high_min": f5["high"].idxmin(),
                # targets
                "morning_high": cont["high"].max(),
                "morning_low": cont["low"].min(),
                "morning_close": cont["close"].iloc[-1],
                "mh_time": cont.loc[mh_idx, "hm"],
                "mh_after_f5": g.loc[
                    (g.index >= f5.index[-1] + pd.Timedelta(minutes=1))
                    & (g.index.hour < 12),
                    "high",
                ].max()
                if len(g.loc[
                    (g.index >= f5.index[-1] + pd.Timedelta(minutes=1))
                    & (g.index.hour < 12),
                    "high",
                ])
                else np.nan,
                "day_high": max(
                    cont["high"].max(), aft["high"].max() if len(aft) else 0
                ),
                "day_low": min(
                    cont["low"].min(), aft["low"].min() if len(aft) else np.inf
                ),
                "day_close": aft["close"].iloc[-1]
                if len(aft)
                else cont["close"].iloc[-1],
                "fallback_close": g.loc[
                    g["hm"] == FALLBACK_MINUTE, "close"
                ].iloc[0]
                if (g["hm"] == FALLBACK_MINUTE).any()
                else cont["close"].iloc[-1],
                "first_open": f5["open"].iloc[0],
            }
        )
    s = pd.DataFrame(rows).set_index("date")
    s.index = pd.to_datetime(s.index)
    return s


def add_prior_day_features(s: pd.DataFrame) -> pd.DataFrame:
    """Lag / rolling features using strictly prior trading days."""
    s = s.sort_index()
    out = s.copy()
    cols = [
        "auction",
        "morning_high",
        "morning_close",
        "day_high",
        "day_close",
        "f5_high",
        "f5_close",
    ]
    for c in cols:
        out[f"prev_{c}"] = out[c].shift(1)
    out["prev_spike"] = (
        out["prev_morning_high"] - out["prev_auction"]
    )
    out["auc_minus_prev_close"] = out["auction"] - out["prev_day_close"]
    out["auc_minus_prev_mh"] = out["auction"] - out["prev_morning_high"]
    for window in (5, 10, 20):
        out[f"mh_ma{window}"] = out["morning_high"].shift(1).rolling(window).mean()
        out[f"spike_ma{window}"] = (
            out["morning_high"].shift(1) - out["auction"].shift(1)
        ).rolling(window, min_periods=3).mean()
        out[f"auction_ma{window}"] = (
            out["auction"].shift(1).rolling(window, min_periods=3).mean()
        )
    return out


def add_t1_features(s: pd.DataFrame) -> pd.DataFrame:
    """Features observable at ~09:30:42 (auction + calendar + prior days)."""
    cal = trading_day_flags(s.index)
    out = pd.concat([s, cal], axis=1)
    out["auction_vol_log"] = np.log1p(out["auction_vol"])
    return out


def add_t2_features(s: pd.DataFrame) -> pd.DataFrame:
    """Features observable at 09:35 (adds first-five-minute OHLCV)."""
    out = s.copy()
    out["f5_high_minus_auction"] = out["f5_high"] - out["auction"]
    out["f5_close_minus_auction"] = out["f5_close"] - out["auction"]
    out["f5_open_minus_auction"] = out["f5_open"] - out["auction"]
    out["f5_high_minus_open"] = out["f5_high"] - out["f5_open"]
    out["f5_range"] = out["f5_high"] - out["f5_low"]
    out["f5_high_minus_close"] = out["f5_high"] - out["f5_close"]
    out["f5_close_minus_open"] = out["f5_close"] - out["f5_open"]
    out["f5_vol_log"] = np.log1p(out["f5_vol"])
    out["f5_amount_log"] = np.log1p(out["f5_amount"])
    return out


def time_split(s: pd.DataFrame, ratio: float):
    n = len(s)
    cut = int(n * ratio)
    return s.index[:cut], s.index[cut:]


def bp(x: pd.Series | float) -> pd.Series | float:
    """Convert rate in % to basis points (0.01%)."""
    return x * 100


def main() -> int:
    args = parse_args()
    df = load_bars(args.input)
    s0 = build_session_table(df)
    s = add_prior_day_features(s0)
    s = add_t1_features(s)
    s_t2 = add_t2_features(s)
    # rows with any feature missing (early sessions) are dropped consistently
    s = s.dropna(subset=FEATURES_T1)
    s_t2 = s_t2.dropna(subset=FEATURES_T2)

    args.output.mkdir(parents=True, exist_ok=True)
    s.to_csv(args.output / "sessions_t1.csv", encoding="utf-8-sig")
    s_t2.to_csv(args.output / "sessions_t2.csv", encoding="utf-8-sig")
    print(f"sessions: {len(s)}  range: {s.index.min().date()} .. {s.index.max().date()}")

    _descriptive(s_t2, args.output)
    _modeling(s_t2, args)
    _backtest(s_t2, args)
    print(f"output={args.output}")
    return 0


def _descriptive(s: pd.DataFrame, out: Path) -> None:
    """Print and save descriptive stats on the morning spike."""
    lines: list[str] = []
    lines.append("# GC001 早盘上冲描述统计")
    lines.append("")
    lines.append(f"- 样本交易日: {len(s)}")
    lines.append(f"- 区间: {s.index.min().date()} ~ {s.index.max().date()}")
    lines.append("- 价格单位: % 年化利率; 1 tick = 0.005% = 0.5bp")
    lines.append("")

    s = s.copy()
    s["spike_auc"] = s["morning_high"] - s["auction"]
    s["spike_f5"] = s["morning_high"] - s["f5_high"]
    s["spike_f5c"] = s["morning_high"] - s["f5_close"]
    s["resid_after_f5"] = s["mh_after_f5"] - s["f5_close"]

    table = pd.DataFrame(
        {
            "morning_high - auction": bp(s["spike_auc"]),
            "morning_high - f5_high": bp(s["spike_f5"]),
            "morning_high - f5_close": bp(s["spike_f5c"]),
            "after-09:35 residual (high-f5_close)": bp(s["resid_after_f5"]),
        }
    )
    desc = table.describe(percentiles=[0.1, 0.25, 0.5, 0.75, 0.9]).round(2)
    lines.append("## 早盘上冲幅度 (bp)")
    lines.append("")
    lines.append(desc.to_markdown())
    lines.append("")
    lines.append(
        f"- morning_high > auction 占比: {(s['spike_auc'] > 0).mean():.1%}"
    )
    lines.append(
        f"- morning_high > f5_high (09:35后还有新高) 占比: "
        f"{(s['spike_f5'] > 0).mean():.1%}"
    )
    lines.append("")

    mh_min = (
        pd.to_datetime(s["mh_time"], format="%H:%M").dt.hour * 60
        + pd.to_datetime(s["mh_time"], format="%H:%M").dt.minute
    )
    lines.append("## 早盘高点出现时间")
    lines.append("")
    lines.append(f"- 09:31-09:35 内出现: {(mh_min <= 575).mean():.1%}")
    lines.append(f"- 09:40 前出现: {(mh_min <= 570 + 10).mean():.1%}")
    lines.append(f"- 10:00 前出现: {(mh_min <= 600).mean():.1%}")
    lines.append("")
    hist = pd.cut(mh_min, bins=range(570, 695, 5)).value_counts().sort_index()
    lines.append("| 时间窗 | 天数 |")
    lines.append("|---|---:|")
    for bucket, count in hist.items():
        lo = bucket.left
        hi = bucket.right
        lines.append(
            f"| {int(lo)//60:02d}:{int(lo)%60:02d}-{int(hi)//60:02d}:{int(hi)%60:02d} | {count} |"
        )
    lines.append("")

    wk = s.groupby(s.index.dayofweek)["spike_auc"].agg(["mean", "median", "count"])
    wk.index = ["周一", "周二", "周三", "周四", "周五"]
    lines.append("## 按星期: morning_high - auction (bp)")
    lines.append("")
    wk_out = wk.copy()
    wk_out[["mean", "median"]] = bp(wk[["mean", "median"]])
    lines.append(wk_out.round(2).to_markdown())
    lines.append("")

    me = s.groupby(s["is_last3_td_month"])["spike_auc"].agg(["mean", "median", "count"])
    me.index = ["非月末", "月末(最后3交易日)"]
    lines.append("## 月末效应: morning_high - auction (bp)")
    lines.append("")
    me_out = me.copy()
    me_out[["mean", "median"]] = bp(me[["mean", "median"]])
    lines.append(me_out.round(2).to_markdown())
    lines.append("")

    wait = s.copy()
    wait["wait_gain_bp"] = bp(wait["f5_close"] - wait["first_open"])
    wk_wait = wait.groupby(wait.index.dayofweek)["wait_gain_bp"].agg(
        ["mean", "median", "count"]
    )
    wk_wait.index = ["周一", "周二", "周三", "周四", "周五"]
    lines.append("## 按星期: 等到09:35市价 vs 09:31市价 的增益 (bp)")
    lines.append("")
    lines.append(wk_wait.round(2).to_markdown())
    lines.append("")
    me_wait = wait.groupby(wait["is_last3_td_month"])["wait_gain_bp"].agg(
        ["mean", "median", "count"]
    )
    me_wait.index = ["非月末", "月末(最后3交易日)"]
    lines.append("## 月末效应: 等到09:35 vs 09:31 的增益 (bp)")
    lines.append("")
    lines.append(me_wait.round(2).to_markdown())
    lines.append("")

    corr = s[
        [
            "spike_auc",
            "auction",
            "auction_vol",
            "prev_spike",
            "prev_morning_high",
            "prev_day_close",
        ]
    ].corr()
    lines.append("## 与 spike 的相关性")
    lines.append("")
    lines.append(corr.round(3).to_markdown())
    lines.append("")
    (out / "descriptive.md").write_text("\n".join(lines), encoding="utf-8")

    setup_matplotlib()
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(2, 2, figsize=(13, 9))
    # 1. time of morning high
    ax = axes[0, 0]
    ax.hist(mh_min, bins=range(570, 695, 5), color="#4C72B0", edgecolor="white")
    ax.set_title("早盘高点出现时间分布 (09:30-11:30)")
    ax.set_xlabel("分钟 (570=09:30)")
    ax.set_ylabel("天数")
    # 2. spike from auction
    ax = axes[0, 1]
    ax.hist(bp(s["spike_auc"]), bins=30, color="#55A868", edgecolor="white")
    ax.axvline(0, color="black", lw=0.8)
    ax.set_title("morning_high - auction (bp)")
    # 3. auction vs morning high
    ax = axes[1, 0]
    ax.scatter(s["auction"], s["morning_high"], s=14, alpha=0.6, color="#C44E52")
    lims = [min(s["auction"].min(), s["morning_high"].min()) - 0.05,
            max(s["auction"].max(), s["morning_high"].max()) + 0.05]
    ax.plot(lims, lims, "k--", lw=1)
    ax.set_xlabel("auction")
    ax.set_ylabel("morning_high")
    ax.set_title("竞价价 vs 早盘高点")
    # 4. residual after 09:35
    ax = axes[1, 1]
    ax.hist(bp(s["resid_after_f5"]), bins=30, color="#8172B3", edgecolor="white")
    ax.axvline(0, color="black", lw=0.8)
    ax.set_title("09:35后剩余上冲 (bp)")
    fig.tight_layout()
    fig.savefig(out / "descriptive.png", dpi=130)
    plt.close(fig)

    print("descriptive written")


def _modeling(s: pd.DataFrame, args: argparse.Namespace) -> None:
    """Train models on T2 features; predict morning high and residual."""
    from sklearn.ensemble import GradientBoostingRegressor
    from sklearn.linear_model import LinearRegression
    from sklearn.metrics import mean_absolute_error, r2_score
    from xgboost import XGBRegressor

    s = s.copy()
    train_idx, test_idx = time_split(s, args.split_ratio)
    train, test = s.loc[train_idx], s.loc[test_idx]
    print(f"model train={len(train)} test={len(test)}")

    targets = {
        "morning_high": s["morning_high"],
    }
    feature_sets = {
        "t1": FEATURES_T1,
        "t2": FEATURES_T2,
    }
    models = {
        "ols_level": LinearRegression(),
        "gbm_increment": GradientBoostingRegressor(
            n_estimators=200, max_depth=2, learning_rate=0.05, random_state=args.seed
        ),
        "xgb_increment": XGBRegressor(
            n_estimators=300, max_depth=2, learning_rate=0.05,
            subsample=0.8, colsample_bytree=0.8, random_state=args.seed,
            verbosity=0,
        ),
    }
    y = targets["morning_high"]

    rows = []
    for fname, cols in feature_sets.items():
        Xtr, Xte = train[cols], test[cols]
        # naive baselines
        for name, pred in [
            ("naive_prev_mh", test["prev_morning_high"]),
            ("naive_auction", test["auction"]),
            ("naive_auction_plus_mean_spike",
             test["auction"] + train["morning_high"].sub(train["auction"]).mean()),
        ]:
            yte = test["morning_high"]
            rows.append(_model_row(fname, name, pred, yte))
        for name, model in models.items():
            if name.endswith("increment"):
                ytr = train["morning_high"] - train["auction"]
                pred = model.fit(Xtr, ytr).predict(Xte) + test["auction"]
            else:
                ytr = train["morning_high"]
                pred = model.fit(Xtr, ytr).predict(Xte)
            rows.append(_model_row(fname, name, pred, test["morning_high"]))

    res = pd.DataFrame(rows)
    res.to_csv(args.output / "model_results.csv", index=False, encoding="utf-8-sig")
    print(res[["feature_set", "model", "mae_bp", "r2"]].round(3).to_string(index=False))

    # residual-after-09:35 classification: does f5_high remain the day's morning high?
    resid_target = (s["morning_high"] > s["f5_high"]).astype(int)
    cols = FEATURES_T2
    from sklearn.linear_model import LogisticRegression

    clf = LogisticRegression(max_iter=1000, C=1.0)
    ytr = resid_target.loc[train_idx]
    yte = resid_target.loc[test_idx]
    proba = clf.fit(train[cols], ytr).predict_proba(test[cols])[:, 1]
    pred_bin = (proba >= 0.5).astype(int)
    acc = (pred_bin == yte).mean()
    base = yte.mean()
    print(
        f"residual>0 classifier: acc={acc:.3f} base={base:.3f} "
        f"precision@50%={((pred_bin==1)&(yte==1)).sum()/max((pred_bin==1).sum(),1):.3f}"
    )
    precision = ((pred_bin == 1) & (yte == 1)).sum() / max(
        (pred_bin == 1).sum(), 1
    )
    clf_res = pd.DataFrame(
        [
            {"feature_set": "t2", "model": "logit_residual_gt0",
             "metric": "accuracy", "value": acc},
            {"feature_set": "t2", "model": "logit_residual_gt0",
             "metric": "base_rate", "value": base},
            {"feature_set": "t2", "model": "logit_residual_gt0",
             "metric": "precision_at_threshold_0.5", "value": precision},
        ]
    )
    clf_res.to_csv(
        args.output / "classifier_results.csv", index=False, encoding="utf-8-sig"
    )

    # save holdout predictions for the backtest
    model = XGBRegressor(
        n_estimators=300, max_depth=2, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8, random_state=args.seed, verbosity=0,
    )
    cols = FEATURES_T2
    Xtr = train[cols]
    ytr = train["morning_high"] - train["auction"]
    Xte = test[cols]
    pred_inc = model.fit(Xtr, ytr).predict(Xte)
    holdout = test[
        ["auction", "morning_high", "f5_high", "f5_close", "fallback_close",
         "first_open", "mh_after_f5", "day_high"]
    ].copy()
    holdout["pred_mh"] = test["auction"] + pred_inc
    holdout["pred_inc_bp"] = bp(pred_inc)
    holdout["actual_inc_bp"] = bp(test["morning_high"] - test["auction"])
    holdout.to_csv(args.output / "holdout_predictions.csv", encoding="utf-8-sig")

    # charts
    setup_matplotlib()
    import matplotlib.pyplot as plt

    fig, axes = plt.subplots(1, 2, figsize=(12, 4.5))
    ax = axes[0]
    ax.scatter(
        holdout["actual_inc_bp"], holdout["pred_inc_bp"],
        s=18, alpha=0.7, color="#4C72B0",
    )
    lims = [holdout["actual_inc_bp"].min() - 2, holdout["actual_inc_bp"].max() + 2]
    ax.plot(lims, lims, "k--", lw=1)
    ax.set_xlabel("actual morning_high - auction (bp)")
    ax.set_ylabel("predicted increment (bp)")
    ax.set_title("Holdout: 预测上冲幅度 vs 实际")
    ax = axes[1]
    err = bp(holdout["pred_mh"] - holdout["morning_high"])
    ax.hist(err, bins=25, color="#55A868", edgecolor="white")
    ax.axvline(0, color="black", lw=0.8)
    ax.set_xlabel("prediction error (bp)")
    ax.set_title("Holdout 预测误差分布 (pred - actual)")
    fig.tight_layout()
    fig.savefig(args.output / "model_holdout.png", dpi=130)
    plt.close(fig)


def _model_row(feature_set: str, model: str, pred, actual) -> dict:
    from sklearn.metrics import mean_absolute_error, r2_score

    pred = pd.Series(pred, index=actual.index)
    err_bp = bp(pred - actual)
    return {
        "feature_set": feature_set,
        "model": model,
        "mae_bp": float(mean_absolute_error(actual, pred) * 100),
        "rmse_bp": float(np.sqrt(np.mean((pred - actual) ** 2)) * 100),
        "r2": float(r2_score(actual, pred)),
        "median_err_bp": float(np.median(pred - actual) * 100),
        "pct_within_2bp": float((err_bp.abs() <= 2).mean()),
        "pct_within_5bp": float((err_bp.abs() <= 5).mean()),
    }


def _backtest(s: pd.DataFrame, args: argparse.Namespace) -> None:
    """Simulate limit-sell strategies with a sensible cancel-and-market
    fallback, and compare against sell-now / wait-5-minute baselines.

    Fill convention: a sell limit at price L placed at time t fills when a
    later minute high >= L (1-minute OHLC proxy; no historical bid1).
    If L <= the current market proxy, the order is marketable and fills at
    the market proxy (never worse than selling immediately).
    """
    from xgboost import XGBRegressor

    s = s.copy()
    train_idx, test_idx = time_split(s, args.split_ratio)
    train, test = s.loc[train_idx], s.loc[test_idx]
    holdout = pd.read_csv(
        args.output / "holdout_predictions.csv", parse_dates=["date"], index_col="date"
    )
    holdout = holdout.reindex(test.index)

    # T2 placement at 09:35: fill window = 09:36..11:30; fallback = market
    # at 09:35 (cancel the limit and sell) if the limit never fills.
    max_after_f5 = test["mh_after_f5"]
    # T1 placement right after the auction: fill window = 09:31..11:30;
    # fallback = market at 09:35 close if the limit never fills.
    morning_max = test["morning_high"]
    fallback_t2 = test["f5_close"]

    rows: list[dict] = []

    def add(
        name: str,
        achieved: pd.Series,
        fill_rate: float,
        desc: str,
    ) -> None:
        rows.append(
            {
                "strategy": name,
                "desc": desc,
                "mean_rate_bp": float(bp(achieved.mean())),
                "median_rate_bp": float(bp(achieved.median())),
                "fill_rate": float(fill_rate),
                "mean_gain_bp_vs_b1": float(
                    bp((achieved - test["first_open"]).mean())
                ),
                "mean_gain_bp_vs_b2": float(
                    bp((achieved - test["f5_close"]).mean())
                ),
                "pct_not_worse_than_b2": float(
                    (achieved >= test["f5_close"]).mean()
                ),
            }
        )

    # baselines
    add("B1_market_0931_open", test["first_open"], 1.0,
        "当前策略近似: 09:31 开盘价市价卖出")
    add("B2_wait_0935_market", test["f5_close"], 1.0,
        "等到 09:35 收盘价市价卖出")

    # T2 chase rules: at 09:35, limit = f5_close + k ticks (bet on the
    # residual spike after 09:35), fallback = cancel & market at 09:35.
    for k in (1, 2, 3, 5, 10):
        limit = test["f5_close"] + k * TICK
        filled = max_after_f5 >= limit
        achieved = np.where(filled, limit, fallback_t2)
        add(
            f"t2_limit_f5close_plus_{k}t",
            pd.Series(achieved, index=test.index),
            filled.mean(),
            f"09:35 挂 f5_close+{k}tick, 未成交则09:35市价",
        )

    # T2 model-based limits: limit = pred_mh - buffer, fallback 09:35 market
    for buffer_t in (1, 2, 3, 5):
        limit = holdout["pred_mh"] - buffer_t * TICK
        filled = max_after_f5 >= limit
        achieved = np.where(filled, np.maximum(limit, fallback_t2), fallback_t2)
        add(
            f"t2_model_predmh_minus_{buffer_t}t",
            pd.Series(achieved, index=test.index),
            filled.mean(),
            f"09:35 挂 预测高点-{buffer_t}tick, 未成交则09:35市价",
        )

    # T1 model-based limits placed right after the auction (09:31),
    # fallback = 09:35 market. If the limit is below the opening market it
    # fills at the opening price (never worse than selling at 09:31).
    model = XGBRegressor(
        n_estimators=300, max_depth=2, learning_rate=0.05,
        subsample=0.8, colsample_bytree=0.8, random_state=args.seed, verbosity=0,
    )
    ytr = train["morning_high"] - train["auction"]
    pred_t1 = (
        test["auction"]
        + model.fit(train[FEATURES_T1], ytr).predict(test[FEATURES_T1])
    )
    for buffer_t in (1, 2, 3, 5):
        limit = pred_t1 - buffer_t * TICK
        filled = morning_max >= limit
        achieved = np.where(
            filled,
            np.maximum(limit, test["first_open"]),
            test["f5_close"],
        )
        add(
            f"t1_predmh_minus_{buffer_t}t",
            pd.Series(achieved, index=test.index),
            filled.mean(),
            f"09:31 挂 预测高点-{buffer_t}tick, 未成交09:35市价",
        )

    # oracle
    add("oracle_morning_high", test["morning_high"], 1.0,
        "事后知道早盘高点(上限参照)")

    res = pd.DataFrame(rows)
    res.to_csv(args.output / "backtest_results.csv", index=False, encoding="utf-8-sig")
    print(
        res[
            [
                "strategy", "mean_rate_bp", "fill_rate",
                "mean_gain_bp_vs_b1", "mean_gain_bp_vs_b2",
                "pct_not_worse_than_b2",
            ]
        ].round(3).to_string(index=False)
    )

    # direction decision: can the T1 model predict whether waiting to 09:35
    # beats selling at 09:31 (f5_close > first_open)?
    direction_tr = (train["f5_close"] > train["first_open"]).astype(int)
    direction_te = (test["f5_close"] > test["first_open"]).astype(int)
    base_dir = direction_te.mean()
    from sklearn.linear_model import LogisticRegression

    clf = LogisticRegression(max_iter=1000, C=0.3)
    proba = clf.fit(train[FEATURES_T1], direction_tr).predict_proba(
        test[FEATURES_T1]
    )[:, 1]
    pred_dir = pd.Series((proba >= 0.5).astype(int), index=test.index)
    acc_dir = (pred_dir == direction_te).mean()
    # strategy: sell at 09:31 if model says decline, else wait to 09:35
    achieved_dir = np.where(
        pred_dir == 1, test["f5_close"], test["first_open"]
    )
    dir_row = {
        "strategy": "dir_model_wait_if_up",
        "desc": "模型预测09:31→09:35上涨则等09:35市价, 否则09:31市价",
        "mean_rate_bp": float(bp(pd.Series(achieved_dir, index=test.index).mean())),
        "fill_rate": float(np.nan),
        "mean_gain_bp_vs_b1": float(
            bp(pd.Series(achieved_dir, index=test.index).sub(test["first_open"]).mean())
        ),
        "mean_gain_bp_vs_b2": float(
            bp(pd.Series(achieved_dir, index=test.index).sub(test["f5_close"]).mean())
        ),
        "pct_not_worse_than_b2": float(
            (pd.Series(achieved_dir, index=test.index) >= test["f5_close"]).mean()
        ),
    }
    res = pd.concat([res, pd.DataFrame([dir_row])], ignore_index=True)
    res.to_csv(args.output / "backtest_results.csv", index=False, encoding="utf-8-sig")
    print(
        f"direction base_rate={base_dir:.3f} acc={acc_dir:.3f} "
        f"precision_up={(pred_dir==1).mean():.3f}"
    )
    print(
        res[
            [
                "strategy", "mean_rate_bp", "fill_rate",
                "mean_gain_bp_vs_b1", "mean_gain_bp_vs_b2",
            ]
        ].round(3).to_string(index=False)
    )

    setup_matplotlib()
    import matplotlib.pyplot as plt

    names = [
        "B1_market_0931_open",
        "B2_wait_0935_market",
        "t2_limit_f5close_plus_1t",
        "t2_limit_f5close_plus_2t",
        "t2_limit_f5close_plus_5t",
        "t2_model_predmh_minus_2t",
        "t2_model_predmh_minus_5t",
        "t1_predmh_minus_2t",
        "t1_predmh_minus_1t",
        "dir_model_wait_if_up",
        "oracle_morning_high",
    ]
    fig, ax = plt.subplots(figsize=(12, 5))
    sub = res.set_index("strategy").loc[names]
    x = np.arange(len(sub))
    ax.bar(x - 0.2, sub["mean_rate_bp"], width=0.38, label="平均成交利率(bp)", color="#4C72B0")
    ax.bar(x + 0.2, sub["mean_gain_bp_vs_b1"], width=0.38, label="相对09:31市价增益(bp)", color="#55A868")
    ax.set_xticks(x)
    ax.set_xticklabels(sub.index, rotation=35, ha="right")
    ax.axhline(0, color="black", lw=0.8)
    ax.set_title("Holdout 策略对比 (平均利率与相对基准增益)")
    ax.legend()
    fig.tight_layout()
    fig.savefig(args.output / "backtest_compare.png", dpi=130)
    plt.close(fig)


if __name__ == "__main__":
    raise SystemExit(main())
