#!/usr/bin/env python3
# -*- coding: utf-8 -*-
import os, sys, argparse, requests
try:
    import yfinance as yf
except Exception:
    print("Missing yfinance", file=sys.stderr); sys.exit(1)

def send_telegram(token, chat_id, text):
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    r = requests.post(url, json={"chat_id": chat_id, "text": text, "parse_mode": "Markdown"})
    return r.ok

def get_price_and_atr(symbol="GC=F"):
    t = yf.Ticker(symbol)
    d = t.history(period="30d", interval="1d")
    if d.empty:
        raise SystemExit("empty data")
    price = float(d["Close"].dropna().iloc[-1])
    atr = None
    try:
        atr = float((d["High"] - d["Low"]).rolling(14).mean().dropna().iloc[-1])
    except Exception:
        pass
    return price, atr

def format_msg(price, atr):
    text = f"*Gold Trend | Status*  \n价格: *{price:.2f}* USD/oz"
    if atr is not None:
        atr_pct = (atr / price) * 100.0
        m1 = price - 1.0 * atr
        m15 = price - 1.5 * atr
        m2 = price - 2.0 * atr
        text += f"\nATR(14): ~*{atr:.1f}* (*{atr_pct:.2f}%*)"
        text += f"\n动态参考(ATR基准): 1.0×≈*{m1:.0f}*, 1.5×≈*{m15:.0f}*, 2.0×≈*{m2:.0f}*"
    return text

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["run","status"], default="status")
    ap.add_argument("--symbol", default=os.getenv("SYMBOL","GC=F"))
    a = ap.parse_args()
    bot = os.getenv("BOT_TOKEN","")
    chat = os.getenv("CHAT_ID","")
    if not bot or not chat:
        print("Missing BOT_TOKEN/CHAT_ID", file=sys.stderr); sys.exit(2)
    price, atr = get_price_and_atr(a.symbol)
    msg = format_msg(price, atr)
    ok = send_telegram(bot, chat, msg)
    if not ok:
        print("failed to send", file=sys.stderr); sys.exit(1)

if __name__ == "__main__":
    main()
