#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""法人撈底盤中拉回進場監控（GitHub Actions 版）。

取代原 30K MACD scanner（原程式已改名為 scanner_30k_original.py，
完整原版另存於 backup/30k-original 分支）。本程式只做法人撈底，
不做法人以外的選股，不動 quant（E3a）與五大選股的 TG 線。

法人 S1 策略（凍結，與 faren_nightly.py 同口徑）：
  訊號：外資 5 日淨買 >0、30 日最低收盤落在訊號日前 5 日內、
        訊號日收盤 > 前日收盤（訊號判定由夜間 faren-nightly 做，
        本程式只讀夜間報告的價位，不重算訊號）。
  價位：止損＝30 日最低收盤 x 0.99（取 2 位）；
        進場價＝止損 / 0.95（取 1 位，以取位後止損為準算）；
        預警價＝止損 / 0.97（站上只提醒，不進場；本程式不推預警）。
  停利（分批各 1/3）：一段＝252 日最高 −0.382x區間（fib382）、
        二段＝252 日最高 −0.236x區間（fib236）、三段＝252 日最高收盤（wave）。
  持有 30 日；成本來回 0.3%。
  觀察池只限 12 檔（固定，不放寬到池外）：2458、3491、7769、6187、
        2059、3661、2360、6531、2404、2308、6223、2345。
  訊號排除 ETF（代號 00 開頭）。

價位來源（優先順序）：
  1. 夜間報告 JSON：faren-nightly（TSM-srf-sim repo，台北 21:00 產出）
     的 pool 陣列，經 GitHub API 讀取（需 FAREN_REPORT_TOKEN Secret，
     PAT 具 repo 讀取權限）。這是最準的來源，與夜間報告逐字一致。
  2. 本地快取 faren_pool.json（上次成功取得的 pool）。
  3. yfinance 直接算（備援）：以 yfinance 依
     faren_nightly.compute_levels 同公式算。後綴依市場決定
     （3491、6187、6223 為上櫃，走 .TWO；其餘走 .TW），
     TW／TWO 互為備援（照 faren_nightly.yf_backfill）；休市假 bar
     （量為 0 且開高低收全等）會濾除。7769、2360
     的 fib382/fib236 與報告有小差（252 日高低點資料差異），
     進場價／停損則一致。

盤中偵測：
  每 5 分鐘一輪（GitHub Actions cron，週一至週五 08:50-13:45 台北），
  抓證交所 MIS 公開行情（與本機 auto_detect.py 同一條線），
  現價 <= 進場價即觸發（開盤 gap-down 跌破也算，與「碰到就算進場」同口徑）。

TG 規則（使用者硬規則）：
  只在「現在馬上進場」時推播（現價 <= 進場價），預警價不推。
  法人走本 repo 的 send_telegram_chunks（Telegram Bot API，
  TELEGRAM_TOKEN／TELEGRAM_CHAT_ID Secrets），與五大選股的
  touch_event.json＋tg-notify.yml 那條線完全分開。
  去重：同一檔同一天只推一次（記在 faren_notified.json，
  workflow 跑完會 commit 回 repo）。

用法（GitHub Actions 每輪跑一次即退出，非長駐 daemon）：
  python scanner.py                 # 跑一輪（Actions 用）
  DRY_RUN=1 python scanner.py       # 乾跑：不推 TG，只印訊息（驗證用）
  python scanner.py --selftest      # 固定假資料驗觸發邏輯，不碰網路、不推 TG
  python scanner.py --refresh-pool  # 只更新 faren_pool.json，不偵測、不推 TG
"""
import json
import os
import re
import subprocess
import sys
import time
import urllib.request
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path

import requests

TAIPEI = timezone(timedelta(hours=8))
BASE = Path(__file__).resolve().parent
POOL_FILE = BASE / "faren_pool.json"
NOTIFIED_FILE = BASE / "faren_notified.json"
LOG_FILE = BASE / "faren_scanner.log"

POOL_STOCKS = ["2458", "3491", "7769", "6187", "2059", "3661",
               "2360", "6531", "2404", "2308", "6223", "2345"]
POOL_NAMES = {"2458": "義隆", "3491": "昇達科", "7769": "鴻勁",
              "6187": "萬潤", "2059": "川湖", "3661": "世芯-KY",
              "2360": "致茂", "6531": "愛普*", "2404": "漢唐",
              "2308": "台達電", "6223": "旺矽", "2345": "智邦"}

STOP_FACTOR = Decimal("0.99")
ENTRY_DIV = Decimal("0.95")
WARN_DIV = Decimal("0.97")
FIB1 = Decimal("0.382")
FIB2 = Decimal("0.236")

REPORT_OWNER = "wind910715"
REPORT_REPO = "TSM-srf-sim"
REPORT_DIR = "nightly/output"
REPORT_RE = re.compile(r"^(\d{4}-\d{2}-\d{2})_faren_nightly\.json$")

MIS_URL = ("https://mis.twse.com.tw/stock/api/getStockInfo.jsp"
           "?ex_ch={codes}&json=1&delay=0")

DRY_RUN = os.environ.get("DRY_RUN", "0") == "1"


def now():
    return datetime.now(TAIPEI)


def log(msg):
    line = f"{now().strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except Exception:
        pass


def r2(x):
    return float(Decimal(str(x)).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP))


def r1(x):
    return float(Decimal(str(x)).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP))


def dec(x):
    return Decimal(str(x))


def is_etf(sid):
    return str(sid).startswith("00")


def compute_levels(bars, idx):
    closes = [b[4] for b in bars]
    highs = [b[2] for b in bars]
    lows = [b[3] for b in bars]
    if idx < 29:
        return None
    window = closes[idx - 29:idx + 1]
    low30 = min(window)
    sl = r2(dec(low30) * STOP_FACTOR)
    entry = r1(dec(sl) / ENTRY_DIV)
    warn = r1(dec(sl) / WARN_DIV)
    out = {"low30": low30, "sl": sl, "entry": entry, "warn": warn,
           "fib382": None, "fib236": None, "wave": None, "rr": None}
    if idx >= 251:
        h252 = max(highs[idx - 251:idx + 1])
        l252 = min(lows[idx - 251:idx + 1])
        hc252 = max(closes[idx - 251:idx + 1])
        f1 = r2(dec(h252) - FIB1 * (dec(h252) - dec(l252)))
        f2 = r2(dec(h252) - FIB2 * (dec(h252) - dec(l252)))
        out.update({"fib382": f1, "fib236": f2, "wave": hc252})
        if entry > sl:
            out["rr"] = r2((dec(f1) - dec(entry)) / (dec(entry) - dec(sl)))
    return out


# 上櫃（TPEx）標的在 Yahoo 的後綴為 .TWO。scanner 沒有市場欄位，
# 以下為觀察池內已核實的上櫃檔（MIS 實查：otc_ 有價、tse_ 無價）：
# 3491 昇達科、6187 萬潤、6223 旺矽；其餘 9 檔皆為上市。
# 後綴依提示決定優先順序，TW／TWO 互為備援（照 faren_nightly.yf_backfill）。
OTC_STOCKS = frozenset({"3491", "6187", "6223"})


def _yf_suffixes(sid, market_hint=None):
    """回傳 yfinance 後綴嘗試順序。market_hint 為「上櫃」/OTC/TWO 時
    先試 .TWO，否則先試 .TW；呼叫方兩個都試，互為備援。"""
    hint = (market_hint or ("上櫃" if str(sid) in OTC_STOCKS else "")).strip()
    if hint in ("上櫃", "OTC", "TWO", "tpex", "TPEx"):
        return ["TWO", "TW"]
    return ["TW", "TWO"]


def compute_levels_yf(sid, eval_date=None, market_hint=None):
    try:
        import yfinance as yf
        import pandas as pd
    except Exception as e:
        log(f"yfinance 備援不可用：{e}")
        return None
    for suffix in _yf_suffixes(sid, market_hint):
        try:
            t = yf.Ticker(f"{sid}.{suffix}")
            h = t.history(period="2y", auto_adjust=False)
        except Exception as e:
            log(f"yfinance 抓 {sid}.{suffix} 失敗：{e}")
            continue
        if h is None or len(h) == 0:
            log(f"yfinance 無 {sid}.{suffix} 資料，改試另一後綴")
            continue
        if eval_date is not None:
            h = h[h.index.tz_localize(None) <= pd.Timestamp(eval_date)]
        bars = []
        for d, r in h.iterrows():
            try:
                o, hi, lo, c, v = (float(r["Open"]), float(r["High"]),
                                   float(r["Low"]), float(r["Close"]),
                                   float(r["Volume"]))
            except (TypeError, ValueError):
                continue
            if pd.isna(c) or c <= 0 or hi < lo:
                continue
            # 休市假 bar：量為 0 且開高低收全等（faren_nightly.yf_backfill 同款過濾）
            if v == 0 and o == hi == lo == c:
                continue
            bars.append((d.date(), o, hi, lo, c, v))
        if len(bars) < 30:
            log(f"yfinance {sid}.{suffix} 有效歷史不足 30 根，改試另一後綴")
            continue
        lv = compute_levels(bars, len(bars) - 1)
        if lv is None:
            continue
        lv["stock_id"] = sid
        lv["name"] = POOL_NAMES.get(sid, "")
        lv["close"] = bars[-1][4]
        return lv
    log(f"yfinance 無 {sid} 資料（TW／TWO 皆無有效歷史），備援跳過")
    return None


def gh_api_get(path, token):
    url = f"https://api.github.com{path}"
    headers = {"Accept": "application/vnd.github+json",
               "Authorization": f"Bearer {token}",
               "X-GitHub-Api-Version": "2022-11-28"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    return r.json()


def fetch_latest_report(token):
    if not token:
        log("未設 FAREN_REPORT_TOKEN，跳過報告抓取")
        return None
    try:
        items = gh_api_get(
            f"/repos/{REPORT_OWNER}/{REPORT_REPO}/contents/{REPORT_DIR}", token)
    except Exception as e:
        log(f"列報告目錄失敗：{e}")
        return None
    latest = None
    for it in items if isinstance(items, list) else []:
        m = REPORT_RE.match(it.get("name", ""))
        if m and (latest is None or m.group(1) > latest[0]):
            latest = (m.group(1), it["name"])
    if not latest:
        log("報告目錄無 faren_nightly.json")
        return None
    report_date, fname = latest
    try:
        data = gh_api_get(
            f"/repos/{REPORT_OWNER}/{REPORT_REPO}/contents/{REPORT_DIR}/{fname}",
            token)
        import base64
        content = base64.b64decode(data["content"]).decode("utf-8")
        rep = json.loads(content)
    except Exception as e:
        log(f"抓報告 {fname} 失敗：{e}")
        return None
    pool = [p for p in rep.get("pool", []) if not is_etf(p.get("stock_id", ""))]
    if not pool:
        log(f"報告 {fname} pool 為空")
        return None
    log(f"報告取得：{report_date}，pool {len(pool)} 檔")
    return report_date, pool


def load_pool_file():
    if not POOL_FILE.exists():
        return None, None
    try:
        d = json.loads(POOL_FILE.read_text(encoding="utf-8"))
        return d.get("report_date"), d.get("pool")
    except Exception as e:
        log(f"讀 {POOL_FILE.name} 失敗：{e}")
        return None, None


def save_pool_file(report_date, pool):
    POOL_FILE.write_text(json.dumps(
        {"report_date": report_date,
         "updated_at": now().strftime("%Y-%m-%d %H:%M:%S"),
         "pool": pool}, ensure_ascii=False, indent=1), encoding="utf-8")


def load_pool():
    token = os.environ.get("FAREN_REPORT_TOKEN", "").strip()
    fetched = fetch_latest_report(token)
    if fetched:
        report_date, pool = fetched
        cached_date, _ = load_pool_file()
        if cached_date != report_date:
            save_pool_file(report_date, pool)
            log(f"faren_pool.json 已更新至 {report_date}")
        return report_date, pool, "report"
    cached_date, cached_pool = load_pool_file()
    if cached_pool:
        log(f"報告抓不到，用快取 faren_pool.json（{cached_date}）")
        return cached_date, cached_pool, "cache"
    log("無報告、無快取，改用 yfinance 直接算（備援）")
    pool = []
    for sid in POOL_STOCKS:
        lv = compute_levels_yf(sid)
        if lv:
            pool.append(lv)
    if not pool:
        return None, [], "yfinance"
    report_date = now().strftime("%Y-%m-%d")
    save_pool_file(report_date, pool)
    return report_date, pool, "yfinance"


def _num(v):
    try:
        if v is None or v == "-" or v == "":
            return None
        return float(v)
    except (TypeError, ValueError):
        return None


def parse_mis_item(it):
    trade = it.get("trade") or {}
    price = _num(trade.get("z"))
    if price is None:
        price = _num(it.get("z"))
    return {"stock_id": it.get("c"), "name": it.get("n"), "price": price,
            "open": _num(it.get("o")) or _num(trade.get("o")),
            "high": _num(it.get("h")) or _num(trade.get("h")),
            "low": _num(it.get("l")) or _num(trade.get("l")),
            "prev_close": _num(it.get("y"))}


def mis_get(codes):
    req = urllib.request.Request(MIS_URL.format(codes="|".join(codes)),
                                 headers={"User-Agent": "Mozilla/5.0"})
    last = None
    for i in range(3):
        try:
            with urllib.request.build_opener().open(req, timeout=20) as r:
                return json.loads(r.read().decode("utf-8"))
        except Exception as e:
            last = e
            time.sleep(1.5 * (i + 1))
    try:
        out = subprocess.run(["curl", "-sS", "-m", "20", "-A", "Mozilla/5.0",
                              MIS_URL.format(codes="|".join(codes))],
                             capture_output=True, text=True, timeout=30).stdout
        return json.loads(out)
    except Exception:
        raise last


def fetch_quotes(stock_ids):
    ids = [str(s).strip() for s in stock_ids]
    out, seen = {}, set()
    for prefix in ("tse_", "otc_"):
        todo = [s for s in ids if s not in seen]
        if not todo:
            break
        try:
            d = mis_get([f"{prefix}{s}.tw" for s in todo])
        except Exception as e:
            log(f"MIS 抓價失敗 {prefix}：{e}")
            continue
        for it in d.get("msgArray", []):
            if it.get("c"):
                row = parse_mis_item(it)
                if row["price"] is not None or row["open"] is not None:
                    out[row["stock_id"]] = row
                    seen.add(it["c"])
        time.sleep(0.5)
    return out


def faren_trigger(entry, last):
    if last is None or entry is None:
        return False
    return last <= entry


def load_notified():
    if not NOTIFIED_FILE.exists():
        return {}
    try:
        d = json.loads(NOTIFIED_FILE.read_text(encoding="utf-8"))
        return d.get("notified", {}) if isinstance(d, dict) else {}
    except Exception:
        return {}


def save_notified(notified):
    NOTIFIED_FILE.write_text(json.dumps(
        {"updated_at": now().strftime("%Y-%m-%d %H:%M:%S"),
         "notified": notified}, ensure_ascii=False, indent=1), encoding="utf-8")


def send_telegram_chunks(full_text):
    """推播 TG 訊息（分塊）。回傳 True＝全部 chunk 皆 200，False＝失敗。

    只發到 TELEGRAM_CHAT_ID；token 或 chat_id 未設時直接失敗並大聲報錯，
    絕不以 getUpdates 猜測收件人（舊版 fallback 會把進場價／停損／停利
    發給最近私訊過此 bot 的陌生人，2026-11-14 P0 #1 已刪除）。"""
    token = os.environ.get("TELEGRAM_TOKEN", "").strip()
    chat_id = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
    print("================ 訊息預覽 ================")
    print(full_text)
    print("==========================================")
    if not token:
        log("ERROR 推播失敗：未設 TELEGRAM_TOKEN")
        return False
    if not chat_id:
        log("ERROR 推播失敗：未設 TELEGRAM_CHAT_ID，不猜測收件人")
        return False
    chunks, current_chunk = [], ""
    for line in full_text.split("\n"):
        if len(current_chunk) + len(line) + 1 > 3500:
            chunks.append(current_chunk)
            current_chunk = line
        else:
            current_chunk += ("\n" + line if current_chunk else line)
    if current_chunk:
        chunks.append(current_chunk)
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    for idx, chunk in enumerate(chunks):
        try:
            res = requests.post(url, json={"chat_id": chat_id, "text": chunk}, timeout=15)
        except Exception as e:
            log(f"ERROR 推播失敗：第 {idx + 1}/{len(chunks)} chunk 連線例外：{e}")
            return False
        print(f"Telegram 推播狀態碼: {res.status_code}")
        if res.status_code != 200:
            log(f"ERROR 推播失敗：第 {idx + 1}/{len(chunks)} chunk 狀態碼 "
                f"{res.status_code}，不重試、不猜測收件人")
            return False
    return True


def build_entry_message(item, price):
    return (
        f"【法人拉回進場】{item['stock_id']} {item.get('name', '')}："
        f"現價 {price} 已碰到進場價 {item['entry']}，現在進場；"
        f"停損 {item['sl']}；"
        f"停利一段 {item.get('fib382')}／二段 {item.get('fib236')}／三段 {item.get('wave')}"
    )


def in_window(t):
    if t.weekday() >= 5:
        return False
    mins = t.hour * 60 + t.minute
    return 8 * 60 + 50 <= mins <= 13 * 60 + 45


def run_once():
    t = now()
    if not in_window(t):
        log("非交易時段，靜默結束")
        return []
    report_date, pool, source = load_pool()
    if not pool:
        log("無可用價位，本輪結束")
        return []
    log(f"價位來源：{source}（報告日 {report_date}），pool {len(pool)} 檔")
    quotes = fetch_quotes([p["stock_id"] for p in pool])
    log(f"報價取得 {len(quotes)}/{len(pool)} 檔")
    notified = load_notified()
    today = t.strftime("%Y-%m-%d")
    events = []
    changed = False
    for p in pool:
        sid = p["stock_id"]
        if notified.get(sid) == today:
            continue
        q = quotes.get(sid)
        if not q:
            continue
        if faren_trigger(p["entry"], q["price"]):
            msg = build_entry_message(p, q["price"])
            if DRY_RUN:
                log(f"[DRY_RUN] TG 不發，訊息：{msg}")
            elif not send_telegram_chunks(msg):
                # 推播失敗不記已通知、不列事件，下一輪重試，避免漏推。
                log(f"法人 {p.get('name')}({sid}) 推播失敗，本輪不記已通知，下一輪重試")
                continue
            notified[sid] = today
            changed = True
            events.append({"stock_id": sid, "name": p.get("name"),
                           "price": q["price"], "entry": p["entry"],
                           "stop": p["sl"], "tp1": p.get("fib382"),
                           "tp2": p.get("fib236"), "tp3": p.get("wave")})
            log(f"法人 {p.get('name')}({sid}) 拉回進場：現價 {q['price']}／"
                f"進場 {p['entry']}／停損 {p['sl']}")
    if changed:
        save_notified(notified)
    else:
        log("本輪無觸發，靜默")
    return events


def selftest():
    fails = []

    def check(name, got, want):
        ok = got == want
        print(f"{'PASS' if ok else 'FAIL'} {name}: got={got} want={want}")
        if not ok:
            fails.append(name)

    check("faren_touch", faren_trigger(3590.1, 3580.0), True)
    check("faren_no_touch", faren_trigger(3590.1, 3650.0), False)
    check("faren_no_price", faren_trigger(3590.1, None), False)
    check("faren_eq_entry", faren_trigger(120.4, 120.4), True)
    sl = r2(dec(115.5) * STOP_FACTOR)
    check("sl_2458", sl, 114.35)
    check("entry_2458", r1(dec(sl) / ENTRY_DIV), 120.4)
    check("warn_2458", r1(dec(sl) / WARN_DIV), 117.9)
    m = build_entry_message(
        {"stock_id": "2458", "name": "義隆", "entry": 120.4, "sl": 114.35,
         "fib382": 162.5, "fib236": 175.5, "wave": 189.0}, 115.0)
    check("msg_has_sid", "2458" in m, True)
    check("msg_has_entry", "120.4" in m, True)
    check("msg_has_stop", "114.35" in m, True)
    check("msg_has_tp", ("162.5" in m and "175.5" in m and "189.0" in m), True)
    r = parse_mis_item({"c": "2458", "z": "-", "o": "117.0", "h": "118.0",
                        "l": "115.0",
                        "trade": {"z": "115.0", "o": "117.0", "h": "118.0",
                                  "l": "115.0"}})
    check("mis_trade_z", r["price"], 115.0)
    r2x = parse_mis_item({"c": "2458", "z": "-", "o": None, "h": None, "l": None})
    check("mis_no_price", r2x["price"], None)
    check("etf_0050", is_etf("0050"), True)
    check("etf_00991A", is_etf("00991A"), True)
    check("not_etf_2458", is_etf("2458"), False)
    check("window_sat", in_window(datetime(2026, 11, 14, 10, 0, tzinfo=TAIPEI)), False)
    check("window_0850", in_window(datetime(2026, 11, 11, 8, 50, tzinfo=TAIPEI)), True)
    check("window_1345", in_window(datetime(2026, 11, 11, 13, 45, tzinfo=TAIPEI)), True)
    check("window_0849", in_window(datetime(2026, 11, 11, 8, 49, tzinfo=TAIPEI)), False)
    check("window_1346", in_window(datetime(2026, 11, 11, 13, 46, tzinfo=TAIPEI)), False)
    print("SELFTEST " + ("ALL PASS" if not fails else f"FAIL: {fails}"))
    return 1 if fails else 0


def main():
    if "--selftest" in sys.argv:
        return selftest()
    if "--refresh-pool" in sys.argv:
        report_date, pool, source = load_pool()
        if pool:
            log(f"pool 已更新：{report_date}（{source}），{len(pool)} 檔")
            return 0
        log("pool 更新失敗")
        return 1
    log(f"法人監控啟動 pid={os.getpid()} DRY_RUN={DRY_RUN}")
    events = run_once()
    log(f"本輪完成 events={len(events)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
