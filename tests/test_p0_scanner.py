#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""法人 scanner P0 修復測試（2026-11-14，fix/p0-scanner-20261114）。

全部 mock，不碰真實網路、不發真實 Telegram 訊息。
跑法：python3 -m unittest discover -s tests -v

#1 send_telegram_chunks getUpdates 資安：
  - 首則 sendMessage 非 200 時，舊版呼叫 getUpdates 拿陌生人 chat.id 重發；
    新版絕不呼叫 getUpdates，直接回傳 False。
  - chat_id / token 為空時舊版靜默 return（None）或必走 fallback；
    新版大聲失敗（log ERROR＋回傳 False）。
  - run_once 只在推播成功時記已通知，失敗不記、下一輪重試。

#10 compute_levels_yf .TW 硬編碼：
  - 3491／6187／6223 為上櫃，Yahoo 正確後綴 .TWO；舊版只試 .TW 拿不到價；
    新版依市場提示先試 .TWO，TW／TWO 互為備援。
  - 休市假 bar（量 0 且開高低收全等）濾除，不計入 30／252 日窗口。
  - 上市檔 .TW 正常時不受影響，仍只走 .TW。
"""
import os
import sys
import tempfile
import types
import unittest
from datetime import datetime, timezone, timedelta
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import pandas as pd  # noqa: E402

import scanner  # noqa: E402

TAIPEI = timezone(timedelta(hours=8))


def make_history(n=300, close=100.0, high=101.0, low=99.0, volume=1000):
    """n 根有效日 bar：收盤皆 close，30 日最低＝close。"""
    idx = pd.date_range("2025-01-01", periods=n, freq="B", tz="America/New_York")
    return pd.DataFrame(
        {"Open": [close] * n, "High": [high] * n, "Low": [low] * n,
         "Close": [close] * n, "Volume": [volume] * n},
        index=idx)


def make_fake_holiday_history(n=300, price=100.0):
    """n 根休市假 bar：量 0 且開高低收全等，應全數濾除。"""
    return make_history(n=n, close=price, high=price, low=price, volume=0)


class FakeTicker:
    """yfinance.Ticker 替身：histories[symbol] 為 DataFrame／None／Exception。"""
    histories = {}
    calls = []

    def __init__(self, symbol):
        self.symbol = symbol
        FakeTicker.calls.append(symbol)

    def history(self, period="2y", auto_adjust=False):
        v = FakeTicker.histories.get(self.symbol)
        if isinstance(v, Exception):
            raise v
        return v


def fake_yf_module():
    m = types.ModuleType("yfinance")
    m.Ticker = FakeTicker
    return m


class Resp:
    def __init__(self, status_code):
        self.status_code = status_code


class P0TestBase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.log_file = os.path.join(self.tmp.name, "test.log")
        self._orig_log = scanner.LOG_FILE
        scanner.LOG_FILE = type(scanner.LOG_FILE)(self.log_file)
        self._orig_notified = scanner.NOTIFIED_FILE
        scanner.NOTIFIED_FILE = type(scanner.NOTIFIED_FILE)(
            os.path.join(self.tmp.name, "faren_notified.json"))
        FakeTicker.histories = {}
        FakeTicker.calls = []

    def tearDown(self):
        scanner.LOG_FILE = self._orig_log
        scanner.NOTIFIED_FILE = self._orig_notified
        self.tmp.cleanup()

    def log_text(self):
        try:
            with open(self.log_file, encoding="utf-8") as f:
                return f.read()
        except FileNotFoundError:
            return ""


class TestSendTelegram(P0TestBase):
    ENV = {"TELEGRAM_TOKEN": "fake-token", "TELEGRAM_CHAT_ID": "111"}

    def test_first_chunk_non200_never_calls_getupdates_and_returns_false(self):
        """#1 核心：首則非 200 → 不呼叫 getUpdates、回傳 False。"""
        with mock.patch.dict(os.environ, self.ENV), \
             mock.patch.object(scanner.requests, "post",
                               return_value=Resp(500)) as post, \
             mock.patch.object(scanner.requests, "get") as get:
            ok = scanner.send_telegram_chunks("進場價 120.4；停損 114.35")
            self.assertFalse(ok)
            get.assert_not_called()
            self.assertEqual(post.call_count, 1)
            self.assertEqual(post.call_args.kwargs["json"]["chat_id"], "111")

    def test_success_returns_true_and_never_calls_getupdates(self):
        """#1 正常路徑不變：200 → True，getUpdates 一次都不碰。"""
        with mock.patch.dict(os.environ, self.ENV), \
             mock.patch.object(scanner.requests, "post",
                               return_value=Resp(200)) as post, \
             mock.patch.object(scanner.requests, "get") as get:
            ok = scanner.send_telegram_chunks("進場價 120.4；停損 114.35")
            self.assertTrue(ok)
            get.assert_not_called()
            self.assertEqual(post.call_count, 1)

    def test_second_chunk_failure_returns_false(self):
        """多 chunk：第二 chunk 失敗 → False，不繼續猜、不重發。"""
        long_msg = "x" * 3400 + "\n" + "y" * 500  # 切成 2 個 chunk
        with mock.patch.dict(os.environ, self.ENV), \
             mock.patch.object(scanner.requests, "post",
                               side_effect=[Resp(200), Resp(403)]) as post, \
             mock.patch.object(scanner.requests, "get") as get:
            ok = scanner.send_telegram_chunks(long_msg)
            self.assertFalse(ok)
            get.assert_not_called()
            self.assertEqual(post.call_count, 2)

    def test_empty_chat_id_loud_failure(self):
        """#1 chat_id 為空：不發送、不猜測，大聲失敗回傳 False。"""
        with mock.patch.dict(os.environ, {"TELEGRAM_TOKEN": "fake-token",
                                          "TELEGRAM_CHAT_ID": ""}), \
             mock.patch.object(scanner.requests, "post") as post, \
             mock.patch.object(scanner.requests, "get") as get:
            ok = scanner.send_telegram_chunks("訊息")
            self.assertFalse(ok)
            post.assert_not_called()
            get.assert_not_called()
            self.assertIn("TELEGRAM_CHAT_ID", self.log_text())

    def test_empty_token_loud_failure(self):
        """token 為空：直接失敗回傳 False（舊版靜默 return None）。"""
        with mock.patch.dict(os.environ, {"TELEGRAM_TOKEN": "",
                                          "TELEGRAM_CHAT_ID": "111"}), \
             mock.patch.object(scanner.requests, "post") as post, \
             mock.patch.object(scanner.requests, "get") as get:
            ok = scanner.send_telegram_chunks("訊息")
            self.assertFalse(ok)
            post.assert_not_called()
            get.assert_not_called()

    def test_post_exception_returns_false(self):
        """連線例外也不炸掉呼叫方，回傳 False。"""
        with mock.patch.dict(os.environ, self.ENV), \
             mock.patch.object(scanner.requests, "post",
                               side_effect=ConnectionError("down")) as post, \
             mock.patch.object(scanner.requests, "get") as get:
            ok = scanner.send_telegram_chunks("訊息")
            self.assertFalse(ok)
            get.assert_not_called()

    def test_run_once_does_not_mark_notified_on_push_failure(self):
        """#1 呼叫點：推播失敗 → 不記已通知、不列事件，下一輪可重試。"""
        pool = [
            {"stock_id": "2458", "name": "義隆", "entry": 120.4, "sl": 114.35,
             "fib382": 162.5, "fib236": 175.5, "wave": 189.0},
            {"stock_id": "3491", "name": "昇達科", "entry": 1130.7, "sl": 1074.15,
             "fib382": 1515.65, "fib236": 1777.21, "wave": 2200.0},
        ]
        quotes = {"2458": {"stock_id": "2458", "price": 115.0},
                  "3491": {"stock_id": "3491", "price": 2000.0}}
        wed_10am = datetime(2026, 11, 11, 10, 0, tzinfo=TAIPEI)
        with mock.patch.object(scanner, "now", return_value=wed_10am), \
             mock.patch.object(scanner, "load_pool",
                               return_value=("2026-11-10", pool, "cache")), \
             mock.patch.object(scanner, "fetch_quotes", return_value=quotes), \
             mock.patch.object(scanner, "DRY_RUN", False), \
             mock.patch.object(scanner, "send_telegram_chunks",
                               return_value=False) as send:
            events = scanner.run_once()
            self.assertEqual(events, [])
            send.assert_called_once()
            self.assertEqual(scanner.load_notified(), {})

    def test_run_once_marks_notified_on_push_success(self):
        """呼叫點對照組：推播成功 → 照舊記已通知、列事件。"""
        pool = [{"stock_id": "2458", "name": "義隆", "entry": 120.4,
                 "sl": 114.35, "fib382": 162.5, "fib236": 175.5, "wave": 189.0}]
        quotes = {"2458": {"stock_id": "2458", "price": 115.0}}
        wed_10am = datetime(2026, 11, 11, 10, 0, tzinfo=TAIPEI)
        with mock.patch.object(scanner, "now", return_value=wed_10am), \
             mock.patch.object(scanner, "load_pool",
                               return_value=("2026-11-10", pool, "cache")), \
             mock.patch.object(scanner, "fetch_quotes", return_value=quotes), \
             mock.patch.object(scanner, "DRY_RUN", False), \
             mock.patch.object(scanner, "send_telegram_chunks",
                               return_value=True):
            events = scanner.run_once()
            self.assertEqual(len(events), 1)
            self.assertEqual(events[0]["stock_id"], "2458")
            self.assertEqual(scanner.load_notified(), {"2458": "2026-11-11"})


class TestYfSuffix(P0TestBase):
    def test_otc_3491_uses_two_first(self):
        """#10 核心：3491 .TW 空、.TWO 有資料 → 正確取價（舊版回 None）。"""
        FakeTicker.histories = {
            "3491.TW": pd.DataFrame(),
            "3491.TWO": make_history(),
        }
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf_module()}):
            lv = scanner.compute_levels_yf("3491")
        self.assertIsNotNone(lv)
        self.assertEqual(lv["stock_id"], "3491")
        self.assertEqual(lv["close"], 100.0)
        self.assertEqual(lv["sl"], 99.0)      # 100 x 0.99
        self.assertEqual(lv["entry"], 104.2)  # 99 / 0.95 → 104.2
        self.assertEqual(FakeTicker.calls, ["3491.TWO"])

    def test_otc_all_three(self):
        """#10：6187、6223 同模式，.TW 空、.TWO 有資料 → 皆正確取價。"""
        for sid in ("6187", "6223"):
            FakeTicker.histories = {
                f"{sid}.TW": pd.DataFrame(),
                f"{sid}.TWO": make_history(),
            }
            FakeTicker.calls = []
            with mock.patch.dict(sys.modules, {"yfinance": fake_yf_module()}):
                lv = scanner.compute_levels_yf(sid)
            self.assertIsNotNone(lv, sid)
            self.assertEqual(lv["sl"], 99.0, sid)
            self.assertEqual(FakeTicker.calls, [f"{sid}.TWO"], sid)

    def test_listed_2458_still_tw_only(self):
        """#10 無關路徑不變：上市 2458 .TW 正常 → 只走 .TW，不碰 .TWO。"""
        FakeTicker.histories = {"2458.TW": make_history()}
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf_module()}):
            lv = scanner.compute_levels_yf("2458")
        self.assertIsNotNone(lv)
        self.assertEqual(lv["sl"], 99.0)
        self.assertEqual(lv["entry"], 104.2)
        self.assertEqual(FakeTicker.calls, ["2458.TW"])

    def test_tw_exception_falls_back_to_two(self):
        """上市檔 .TW 拋例外 → 改試 .TWO，不炸掉。"""
        FakeTicker.histories = {
            "2458.TW": RuntimeError("boom"),
            "2458.TWO": make_history(),
        }
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf_module()}):
            lv = scanner.compute_levels_yf("2458")
        self.assertIsNotNone(lv)
        self.assertEqual(FakeTicker.calls, ["2458.TW", "2458.TWO"])

    def test_fake_holiday_bars_filtered_then_fallback(self):
        """休市假 bar（量 0、開高低收全等）濾除後 .TW 有效根數不足 → 改試 .TWO。"""
        FakeTicker.histories = {
            "2458.TW": make_fake_holiday_history(),
            "2458.TWO": make_history(),
        }
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf_module()}):
            lv = scanner.compute_levels_yf("2458")
        self.assertIsNotNone(lv)
        self.assertEqual(lv["close"], 100.0)
        self.assertEqual(FakeTicker.calls, ["2458.TW", "2458.TWO"])

    def test_both_suffixes_empty_returns_none(self):
        """兩後綴皆無資料 → 回 None（不編造價位）。"""
        FakeTicker.histories = {
            "3491.TW": pd.DataFrame(),
            "3491.TWO": pd.DataFrame(),
        }
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf_module()}):
            self.assertIsNone(scanner.compute_levels_yf("3491"))

    def test_market_hint_overrides_order(self):
        """market_hint＝上櫃時上市檔也先試 .TWO；反之先試 .TW。"""
        FakeTicker.histories = {
            "2458.TW": make_history(),
            "2458.TWO": make_history(),
        }
        with mock.patch.dict(sys.modules, {"yfinance": fake_yf_module()}):
            lv = scanner.compute_levels_yf("2458", market_hint="上櫃")
        self.assertIsNotNone(lv)
        self.assertEqual(FakeTicker.calls, ["2458.TWO"])

    def test_suffix_order(self):
        self.assertEqual(scanner._yf_suffixes("3491"), ["TWO", "TW"])
        self.assertEqual(scanner._yf_suffixes("6187"), ["TWO", "TW"])
        self.assertEqual(scanner._yf_suffixes("6223"), ["TWO", "TW"])
        self.assertEqual(scanner._yf_suffixes("2458"), ["TW", "TWO"])
        self.assertEqual(scanner._yf_suffixes("6531"), ["TW", "TWO"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
