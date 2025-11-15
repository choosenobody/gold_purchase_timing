#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os
import sys
import time
import json
import argparse
import datetime
from typing import Dict, Any

import requests

try:
    import yfinance as yf
except Exception as e:
    print("Missing yfinance:", e, file=sys.stderr)
    sys.exit(1)

STATE_FILE = "gold_trend_state.json"
OZ_TO_GRAM = 31.1034768

# Strategy config (ASCII-only so workflows can patch/translate safely)
CFG: Dict[str, Any] = {
    "symbol": "GC=F",
    # Only notify once per band / level until price leaves and re-enters
    "notify_once_per_band": True,
    # === 中长期配置层面的假设 ===
    # 黄金在组合中的“计划最大权重”，例如 0.18 = 18%
    "plan_gold_max_pct": 0.18,
    # ChatGPT 对黄金 3–5 年视角“合理价格区间”的估算（结合 M2 扩张 / 实际利率等）
    "fair_value_band": [3600, 4200],
    # 默认的美元兑人民币汇率，用于 USD/oz -> RMB/g 的换算（可被环境变量 FX_USDCNY 覆盖）
    "fx_usdcny_default": 7.2,
    # 上沿确认区间：如果价格在这里站稳，可以考虑先建到 30% 计划仓（≈5.4% 总资产）
    "confirm_zone_breakout": {"upper_confirm": [4080, 4100]},
    "levels": {
        # 回调买入区：target_plan_pct 是相对于 plan_gold_max_pct 的比例
        # 例如 target_plan_pct=0.30，代表 0.30 * 18% ≈ 5.4% 组合总资产
        "buy_bands": [
            {"name": "Band A", "low": 3920, "high": 3960, "target_plan_pct": 0.30},
            {"name": "Band B", "low": 3850, "high": 3920, "target_plan_pct": 0.70},
            {"name": "Band C", "low": 3780, "high": 3850, "target_plan_pct": 1.00},
        ],
        "take_profit": [
            {"name": "TP1", "price": 4600},
            {"name": "TP2", "price": 4850},
            {"name": "TP3", "price": 5050},
        ],
        "stop_levels": [
            {
                "name": "Risk-1 trim to 50%",
                "price": 3650,
                "action": "trim_to_50",
            },
            {
                "name": "Risk-2 cut to 0-30%",
                "price": 3520,
                "action": "cut_to_0_30",
            },
        ],
    },
    "atr": {
        "lookback_days": 14,
        # kept for backward compat; we now show 1.0/1.5/2.0× in text
        "mul_stop": 1.5,
    },
    # === Options lotto 黑天鹅对冲配置 ===
    "options_lotto": {
        "enabled": True,
        # 使用哪个标的的期权，这里默认用 GLD
        "underlying": "GLD",
        # 目标期限（天数），比如 365 = 一年左右
        "target_days": 365,
        # 可以接受的 tenor 偏差范围
        "tenor_tolerance_days": 90,
        # 深度虚值范围：行权价 ≈ 现价 * (1 + otm_low ... 1 + otm_high)
        "otm_low": 0.35,   # +35% OTM
        "otm_high": 0.60,  # +60% OTM
        # 平均隐含波动率阈值（例如 0.25 = 25%）
        # 低于这个阈值才提示“彩票”想法
        "iv_threshold": 0.25,
        # 建议的最大资金占比（总组合），例如 0.005 = 0.5%
        "max_allocation_pct": 0.005,
        # 是否只在第一次满足条件时提醒一次
        "notify_once": True,
    },
}

API = "https://api.telegram.org/bot{}/sendMessage"


def tg(token: str, chat: str, text: str) -> bool:
    """Send a Markdown-formatted message to Telegram."""
    r = requests.post(
        API.format(token),
        json={"chat_id": chat, "text": text, "parse_mode": "Markdown"},
        timeout=30,
    )
    if not r.ok:
        print("Telegram failed", r.status_code, r.text, file=sys.stderr)
    return r.ok


def load_state() -> Dict[str, Any]:
    if os.path.exists(STATE_FILE):
        try:
            with open(STATE_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except Exception:
            return {}
    return {}


def save_state(st: Dict[str, Any]) -> None:
    with open(STATE_FILE, "w", encoding="utf-8") as f:
        json.dump(st, f, ensure_ascii=False, indent=2)


def price_and_atr(symbol: str, look: int = 14):
    """Fetch last close price and a simple ATR-like indicator (avg high-low)."""
    period_days = max(look + 2, 20)
    d = yf.Ticker(symbol).history(period=f"{period_days}d", interval="1d")
    if d.empty:
        raise SystemExit("empty data for symbol %s" % symbol)
    p = float(d["Close"].dropna().iloc[-1])
    try:
        a = float((d["High"] - d["Low"]).rolling(look).mean().dropna().iloc[-1])
    except Exception:
        a = None
    return p, a


def _get_fx_rate(cfg: Dict[str, Any]):
    """
    获取 USD/CNY 汇率：
    优先读环境变量 FX_USDCNY；
    其次使用 cfg['fx_usdcny_default']；
    若都不可用则返回 None。
    """
    env = os.getenv("FX_USDCNY", "").strip()
    if env:
        try:
            v = float(env)
            if v > 0:
                return v
        except ValueError:
            pass
    v = cfg.get("fx_usdcny_default", 7.2)
    try:
        v = float(v)
        if v > 0:
            return v
    except Exception:
        pass
    return None


def fmt_status(cfg: Dict[str, Any], p: float, a: float, title: str) -> str:
    """Format the status / heartbeat / signals header."""
    L = cfg["levels"]
    plan_max = float(cfg.get("plan_gold_max_pct", 0.0))
    fair_band = cfg.get("fair_value_band", [None, None])
    fair_lo, fair_hi = fair_band if len(fair_band) == 2 else (None, None)
    fx_rate = _get_fx_rate(cfg)

    out = [f"*{title}*  ", f"Price: *{p:.2f}* USD/oz"]

    # === ATR 块 ===
    if a:
        ap = a / p * 100.0 if p > 0 else 0.0
        look = int(cfg.get("atr", {}).get("lookback_days", 14))
        m1 = p - 1.0 * a
        m15 = p - 1.5 * a
        m2 = p - 2.0 * a
        out.append(f"ATR({look}): ~*{a:.1f}* (*{ap:.2f}%*)")
        out.append(
            "Dynamic refs (ATR): 1.0x~*{m1:.0f}*, 1.5x~*{m15:.0f}*, 2.0x~*{m2:.0f}*".format(
                m1=m1, m15=m15, m2=m2
            )
        )
        out.append(
            "Stops (pick one):\n"
            f"- Conservative 1.0x: ~*{m1:.0f}* — tight risk / short-term\n"
            f"- Standard    1.5x: ~*{m15:.0f}* — default choice\n"
            f"- Loose       2.0x: ~*{m2:.0f}* — more room / smaller size"
        )
        out.append("How to use: if close < your stop -> cut 50–100% per plan.")

    # === 组合计划 & 合理价区间 ===
    out.append("--- Plan ---")
    out.append(
        "- Plan max gold weight: *{pct:.1f}%* of total portfolio".format(
            pct=plan_max * 100.0
        )
    )
    if fair_lo and fair_hi:
        out.append(
            "- Fair-value band (3–5y view): *{lo:.0f}–{hi:.0f}* USD/oz".format(
                lo=fair_lo, hi=fair_hi
            )
        )

    # === 买入区（相对计划仓位） ===
    out.append("--- Rules ---")
    out.append("*Buy bands*:")
    uc = cfg.get("confirm_zone_breakout", {}).get("upper_confirm", [])
    if isinstance(uc, list) and len(uc) == 2:
        target_plan = 0.30  # 30% 计划仓
        target_portfolio = plan_max * target_plan * 100.0
        out.append(
            "- Upper confirm: {lo:.0f}-{hi:.0f} -> build to *30% plan* (~*{pct:.1f}%* of portfolio, if holds)".format(
                lo=uc[0], hi=uc[1], pct=target_portfolio
            )
        )

    for b in L["buy_bands"]:
        target_plan = float(b.get("target_plan_pct", 0.0))
        plan_percent = target_plan * 100.0
        portfolio_percent = plan_max * target_plan * 100.0
        if fx_rate:
            cny_lo = b["low"] * fx_rate / OZ_TO_GRAM
            cny_hi = b["high"] * fx_rate / OZ_TO_GRAM
            rmb_part = ", ~{lo:.0f}-{hi:.0f} RMB/g".format(lo=cny_lo, hi=cny_hi)
        else:
            rmb_part = ""
        out.append(
            "- {name}: {lo:.0f}-{hi:.0f} -> target *{plan:.0f}% plan* (~*{pf:.1f}%* of portfolio{rmb})".format(
                name=b["name"],
                lo=b["low"],
                hi=b["high"],
                plan=plan_percent,
                pf=portfolio_percent,
                rmb=rmb_part,
            )
        )

    # === 止盈 & 风险位 ===
    tps = ", ".join(str(t["price"]) for t in L["take_profit"])
    out.append("*Take profit*: " + tps)
    risks = "; ".join(f"{s['name']}@{s['price']}" for s in L["stop_levels"])
    out.append("*Risk*: " + risks)
    if isinstance(uc, list) and len(uc) == 2:
        out.append(
            "*Upper confirm*: {lo}-{hi} (if holds, consider add to 70-80%)".format(
                lo=uc[0], hi=uc[1]
            )
        )

    return "\n".join(out)


def in_band(p: float, lo: float, hi: float) -> bool:
    return lo <= p <= hi


def should_once(st: Dict[str, Any], key: str) -> bool:
    return not st.get("notified", {}).get(key, False)


def mark_once(st: Dict[str, Any], key: str) -> None:
    st.setdefault("notified", {})[key] = True


def options_lotto_check(
    cfg: Dict[str, Any],
    st: Dict[str, Any],
    gold_price_spot: float,
) -> str | None:
    """
    检查是否满足“买一年期深度虚值看涨期权”的黑天鹅彩票条件：
    1) 金价落在便宜区（Band B / Band C）
    2) GLD 一年期深 OTM call 的平均隐含波低于阈值
    """
    opt_cfg = cfg.get("options_lotto", {})
    if not opt_cfg or not opt_cfg.get("enabled", False):
        return None

    # 只在 Band B / Band C 这类便宜区间里考虑 options lotto
    buy_bands = cfg["levels"]["buy_bands"]
    cheap_band_names = {"Band B", "Band C"}
    in_cheap_zone = any(
        (b["name"] in cheap_band_names) and in_band(gold_price_spot, b["low"], b["high"])
        for b in buy_bands
    )
    if not in_cheap_zone:
        return None

    # 控制频率：只提醒一次，直到状态重置
    if opt_cfg.get("notify_once", True) and st.get("options_lotto_suggested", False):
        return None

    try:
        underlying = opt_cfg.get("underlying", "GLD")
        t = yf.Ticker(underlying)

        # 1) 挑最近的一年期到期日
        today = datetime.date.today()
        target_days = int(opt_cfg.get("target_days", 365))
        tol = int(opt_cfg.get("tenor_tolerance_days", 90))
        best_exp = None
        best_diff = None

        # yfinance 提供的 options 列表是字符串日期
        for exp_str in t.options:
            try:
                exp = datetime.datetime.strptime(exp_str, "%Y-%m-%d").date()
            except Exception:
                continue
            d = abs((exp - today).days - target_days)
            if best_diff is None or d < best_diff:
                best_diff = d
                best_exp = exp_str

        if best_exp is None or (best_diff is not None and best_diff > tol):
            return None

        # 2) 拉期权链，取 calls
        chain = t.option_chain(best_exp)
        calls = chain.calls
        if "impliedVolatility" not in calls.columns:
            return None

        # 3) 取 GLD 当前价格
        hist = t.history(period="2d", interval="1d")
        if hist.empty:
            return None
        gld_price = float(hist["Close"].dropna().iloc[-1])

        # 4) 过滤 35%–60% OTM 的 call，算平均隐含波
        otm_low = float(opt_cfg.get("otm_low", 0.35))
        otm_high = float(opt_cfg.get("otm_high", 0.60))
        lo_strike = gld_price * (1.0 + otm_low)
        hi_strike = gld_price * (1.0 + otm_high)

        sub = calls[(calls["strike"] >= lo_strike) & (calls["strike"] <= hi_strike)]
        if sub.empty or "impliedVolatility" not in sub:
            return None

        iv_mean = float(sub["impliedVolatility"].mean())
        iv_threshold = float(opt_cfg.get("iv_threshold", 0.25))

        if iv_mean > iv_threshold:
            # 隐含波太高，这时候不适合买彩票
            return None

        max_alloc = float(opt_cfg.get("max_allocation_pct", 0.005))
        msg = (
            "Options lotto idea (tail hedge):\n"
            f"- Underlying: {underlying}, spot ~{gld_price:.2f} USD\n"
            f"- Use ~1Y deep OTM calls (expiry {best_exp}, strikes ≈ {lo_strike:.0f}–{hi_strike:.0f})\n"
            f"- Avg implied vol: ~{iv_mean*100:.1f}% (below threshold {iv_threshold*100:.1f}%)\n"
            f"- Suggested max allocation: ~{max_alloc*100:.2f}% of total portfolio per year\n"
            "Idea: reserve this as a black-swan hedge, not a main P&L driver."
        )

        st["options_lotto_suggested"] = True
        return msg

    except Exception as e:
        # 不要影响主流程
        print("options lotto check failed:", e, file=sys.stderr)
        return None


def check_and_alert(
    cfg: Dict[str, Any],
    p: float,
    a: float,
    token: str,
    chat: str,
    st: Dict[str, Any],
) -> bool:
    """Check all bands/levels and send realtime signals if triggered."""
    msgs = []
    L = cfg["levels"]
    once = bool(cfg.get("notify_once_per_band", True))
    plan_max = float(cfg.get("plan_gold_max_pct", 0.0))
    fx_rate = _get_fx_rate(cfg)

    # Buy bands
    for b in L["buy_bands"]:
        if in_band(p, b["low"], b["high"]):
            k = f"buy_{b['name']}"
            if (not once) or should_once(st, k):
                target_plan = float(b.get("target_plan_pct", 0.0))
                plan_percent = target_plan * 100.0
                portfolio_percent = plan_max * target_plan * 100.0
                if fx_rate:
                    cny_lo = b["low"] * fx_rate / OZ_TO_GRAM
                    cny_hi = b["high"] * fx_rate / OZ_TO_GRAM
                    rmb_part = ", ~{lo:.0f}-{hi:.0f} RMB/g".format(lo=cny_lo, hi=cny_hi)
                else:
                    rmb_part = ""
                msgs.append(
                    "Enter buy band *{name}* {lo}-{hi} | price *{p:.2f}* -> "
                    "target *{plan:.0f}% plan* (~*{pf:.1f}%* of portfolio{rmb}, scale in)".format(
                        name=b["name"],
                        lo=b["low"],
                        hi=b["high"],
                        p=p,
                        plan=plan_percent,
                        pf=portfolio_percent,
                        rmb=rmb_part,
                    )
                )
                mark_once(st, k)

    # Upper confirm zone
    uc = cfg.get("confirm_zone_breakout", {}).get("upper_confirm", [])
    if isinstance(uc, list) and len(uc) == 2 and (uc[0] <= p <= uc[1]):
        k = "upper_confirm"
        if (not once) or should_once(st, k):
            target_plan = 0.30
            portfolio_percent = plan_max * target_plan * 100.0
            msgs.append(
                "In upper confirm {lo}-{hi} | price *{p:.2f}* -> "
                "if holds, consider build to *30% plan* (~*{pf:.1f}%* of portfolio)".format(
                    lo=uc[0], hi=uc[1], p=p, pf=portfolio_percent
                )
            )
            mark_once(st, k)

    # Fixed risk levels
    for s in L["stop_levels"]:
        if p <= s["price"]:
            k = f"stop_{s['name']}"
            if (not once) or should_once(st, k):
                action_map = {
                    "trim_to_50": "Trim total position to 50% and wait",
                    "cut_to_0_30": "Cut position to 0-30%, re-evaluate",
                }
                act = action_map.get(s.get("action", ""), "Risk action")
                msgs.append(
                    "Risk level *{name}* @ {level} | price *{p:.2f}* -> {act}".format(
                        name=s["name"], level=s["price"], p=p, act=act
                    )
                )
                mark_once(st, k)

    # Dynamic stops (3 profiles)
    if a and a > 0:
        m1 = p - 1.0 * a
        m15 = p - 1.5 * a
        m2 = p - 2.0 * a
        msgs.append(
            "Stops (pick one):\n"
            f"- Conservative 1.0x: ~*{m1:.0f}*\n"
            f"- Standard    1.5x: ~*{m15:.0f}*\n"
            f"- Loose       2.0x: ~*{m2:.0f}*"
        )
        msgs.append("Rule: if close < your stop -> cut 50–100% per plan.")

    # Options lotto idea (black-swan hedge)
    lotto_msg = options_lotto_check(cfg, st, gold_price_spot=p)
    if lotto_msg:
        msgs.append(lotto_msg)

    if msgs:
        header = fmt_status(cfg, p, a, title="Gold Trend | Signals")
        body = header + "\n\n--- Realtime signals ---\n" + ("\n\n".join(msgs))
        tg(token, chat, body)
        save_state(st)
        return True
    return False


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["run", "status"], default="run")
    ap.add_argument("--symbol", default=os.getenv("SYMBOL", "GC=F"))
    args = ap.parse_args()

    token = os.getenv("BOT_TOKEN", "").strip()
    chat = os.getenv("CHAT_ID", "").strip()
    if not token or not chat:
        print("Missing BOT_TOKEN/CHAT_ID", file=sys.stderr)
        sys.exit(2)

    st = load_state()
    look = int(CFG.get("atr", {}).get("lookback_days", 14))
    p, a = price_and_atr(args.symbol, look=look)

    if args.mode == "status":
        tg(token, chat, fmt_status(CFG, p, a, title="Gold Trend | Status"))
        st["last_status_ts"] = int(time.time())
        save_state(st)
        return

    # Normal "run" mode: check realtime signals; if none fired, send heartbeat every 6h
    pushed = check_and_alert(CFG, p, a, token, chat, st)
    if not pushed:
        last = st.get("last_summary_ts", 0)
        now = int(time.time())
        if now - last > 6 * 3600:
            tg(token, chat, fmt_status(CFG, p, a, title="Gold Trend | Heartbeat"))
            st["last_summary_ts"] = now
            save_state(st)


if __name__ == "__main__":
    main()
