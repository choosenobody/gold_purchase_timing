#!/usr/bin/env python3
# -*- coding: utf-8 -*-

import os, sys, time, json, argparse, requests
try:
    import yfinance as yf
except Exception as e:
    print('Missing yfinance:', e, file=sys.stderr); sys.exit(1)

STATE_FILE = 'gold_trend_state.json'

# Strategy config (ASCII-only)
CFG = {
    'symbol': 'GC=F',
    'notify_once_per_band': True,
    'confirm_zone_breakout': { 'upper_confirm': [4080, 4100] },
    'levels': {
        'buy_bands': [
            {'name':'Band A','low':3920,'high':3960,'target_pos_pct':30},
            {'name':'Band B','low':3850,'high':3920,'target_pos_pct':70},
            {'name':'Band C','low':3780,'high':3850,'target_pos_pct':100}
        ],
        'take_profit': [
            {'name':'TP1','price':4600},
            {'name':'TP2','price':4850},
            {'name':'TP3','price':5050}
        ],
        'stop_levels': [
            {'name':'Risk-1 trim to 50%','price':3650,'action':'trim_to_50'},
            {'name':'Risk-2 cut to 0-30%','price':3520,'action':'cut_to_0_30'}
        ]
    },
    'atr': {'lookback_days':14, 'mul_stop':1.5}
}

API = 'https://api.telegram.org/bot{}/sendMessage'

def tg(token, chat, text):
    r = requests.post(API.format(token), json={'chat_id':chat,'text':text,'parse_mode':'Markdown'})
    if not r.ok:
        print('Telegram failed', r.status_code, r.text, file=sys.stderr)
    return r.ok

def load_state():
    if os.path.exists(STATE_FILE):
        try:
            return json.load(open(STATE_FILE,'r',encoding='utf-8'))
        except Exception:
            return {}
    return {}

def save_state(st):
    json.dump(st, open(STATE_FILE,'w',encoding='utf-8'), ensure_ascii=False, indent=2)

def price_and_atr(symbol, look=14):
    d = yf.Ticker(symbol).history(period=f"{max(look+2,20)}d", interval='1d')
    if d.empty:
        raise SystemExit('empty data')
    p = float(d['Close'].dropna().iloc[-1])
    try:
        a = float((d['High']-d['Low']).rolling(look).mean().dropna().iloc[-1])
    except Exception:
        a = None
    return p, a

def fmt_status(cfg, p, a, title='Gold Trend | Status'):
    L = cfg['levels']
    out = [f"*{title}*  \nPrice: *{p:.2f}* USD/oz"]
    if a:
        ap = a/p*100.0
        m1 = p - 1.0*a; m15 = p - 1.5*a; m2 = p - 2.0*a
        out.append(f"ATR({cfg.get('atr',{}).get('lookback_days',14)}): ~*{a:.1f}* (*{ap:.2f}%*)")
        out.append(f"Dynamic refs (ATR): 1.0x~*{m1:.0f}*, 1.5x~*{m15:.0f}*, 2.0x~*{m2:.0f}*")
        out.append("Stops (pick one):\n- Conservative 1.0x: ~*%d* — tight risk / short-term\n- Standard    1.5x: ~*%d* — default choice\n- Loose       2.0x: ~*%d* — more room / smaller size" % (m1, m15, m2))
        out.append("How to use: if close < your stop -> cut 50–100% per plan.")
    out.append('--- Rules ---')
    out.append('*Buy bands*:')
    for b in L['buy_bands']:
        out.append(f"- {b['name']}: {b['low']}-{b['high']} -> target {b['target_pos_pct']}%")
    out.append('*Take profit*: ' + ', '.join([str(t['price']) for t in L['take_profit']]))
    out.append('*Risk*: ' + '; '.join([f"{s['name']}@{s['price']}" for s in L['stop_levels']]))
    uc = cfg.get('confirm_zone_breakout',{}).get('upper_confirm',[])
    if isinstance(uc, list) and len(uc)==2:
        out.append(f"*Upper confirm*: {uc[0]}-{uc[1]} (if holds, consider add to 70-80%)")
    return '\n'.join(out)

def in_band(p, lo, hi):
    return lo <= p <= hi

def should_once(st, key):
    return not st.get('notified',{}).get(key, False)

def mark_once(st, key):
    st.setdefault('notified',{})[key] = True

def check_and_alert(cfg, p, a, token, chat, st):
    msgs = []
    L = cfg['levels']
    once = bool(cfg.get('notify_once_per_band', True))

    # Buy bands
    for b in L['buy_bands']:
        if in_band(p, b['low'], b['high']):
            k = f"buy_{b['name']}"
            if (not once) or should_once(st, k):
                msgs.append(f"Enter buy band {b['name']} {b['low']}-{b['high']} | price *{p:.2f}* -> target *{b['target_pos_pct']}%* (scale in)")
                mark_once(st, k)

    # Upper confirm zone
    uc = cfg.get('confirm_zone_breakout',{}).get('upper_confirm', [])
    if isinstance(uc, list) and len(uc)==2 and (uc[0] <= p <= uc[1]):
        k = 'upper_confirm'
        if (not once) or should_once(st, k):
            msgs.append(f"In upper confirm {uc[0]}-{uc[1]} | price *{p:.2f}* -> if holds, consider add to 70-80%")
            mark_once(st, k)

    # Fixed risk levels
    for s in L['stop_levels']:
        if p <= s['price']:
            k = f"stop_{s['name']}"
            if (not once) or should_once(st, k):
                action = {'trim_to_50':'Trim total position to 50% and wait', 'cut_to_0_30':'Cut position to 0-30%, re-evaluate'}
                act = action.get(s.get('action',''), 'Risk action')
                msgs.append(f"Risk level {s['name']} @ {s['price']} | price *{p:.2f}* -> {act}")
                mark_once(st, k)

    # Dynamic stops (3 profiles)
    if a and a > 0:
        m1 = p - 1.0*a; m15 = p - 1.5*a; m2 = p - 2.0*a
        msgs.append("Stops (pick one):\n- Conservative 1.0x: ~*%d*\n- Standard    1.5x: ~*%d*\n- Loose       2.0x: ~*%d*" % (m1, m15, m2))
        msgs.append("Rule: if close < your stop -> cut 50–100% per plan.")

    if msgs:
        header = fmt_status(cfg, p, a, title='Gold Trend | Signals')
        tg(token, chat, header + '\n\n--- Realtime signals ---\n' + ('\n\n'.join(msgs)))
        save_state(st)
        return True
    return False

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('--mode', choices=['run','status'], default='run')
    ap.add_argument('--symbol', default=os.getenv('SYMBOL','GC=F'))
    args = ap.parse_args()

    token = os.getenv('BOT_TOKEN','').strip()
    chat = os.getenv('CHAT_ID','').strip()
    if not token or not chat:
        print('Missing BOT_TOKEN/CHAT_ID', file=sys.stderr); sys.exit(2)

    st = load_state()
    p, a = price_and_atr(args.symbol, look=int(CFG.get('atr',{}).get('lookback_days',14)))

    if args.mode == 'status':
        tg(token, chat, fmt_status(CFG, p, a, title='Gold Trend | Status'))
        st['last_status_ts'] = int(time.time()); save_state(st)
        return

    pushed = check_and_alert(CFG, p, a, token, chat, st)
    if not pushed:
        last = st.get('last_summary_ts', 0); now = int(time.time())
        if now - last > 6*3600:
            tg(token, chat, fmt_status(CFG, p, a, title='Gold Trend | Heartbeat'))
            st['last_summary_ts'] = now; save_state(st)

if __name__ == '__main__':
    main()
