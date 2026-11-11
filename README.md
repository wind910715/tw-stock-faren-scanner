# tw-stock-30k-scanner → 法人撈底盤中監控

> 2026-11-11 改版：本 repo 已由 30K MACD scanner 覆寫為法人撈底盤中拉回進場監控。
> 原 30K 程式完整保留於 `scanner_30k_original.py` 與 `backup/30k-original` 分支。
> 本 repo 只做法人撈底，不動 quant（E3a）與五大選股的 TG 線。

## 法人 S1 策略（凍結）

- 訊號：外資 5 日淨買 >0、30 日最低收盤落在訊號日前 5 日內、訊號日收盤 > 前日收盤
  （訊號判定由夜間 faren-nightly 做，本程式只讀夜間報告的價位）。
- 價位：止損＝30 日最低收盤 × 0.99（取 2 位）；進場價＝止損 / 0.95（取 1 位）；
  預警價＝止損 / 0.97（站上只提醒，不進場；本程式不推預警）。
- 停利（分批各 1/3）：一段＝252 日最高 −0.382×區間、二段＝−0.236×區間、三段＝252 日最高收盤。
- 持有 30 日；成本來回 0.3%。
- 觀察池只限 12 檔：2458、3491、7769、6187、2059、3661、2360、6531、2404、2308、6223、2345。
- 訊號排除 ETF（代號 00 開頭）。

## 運作方式

- `scanner.py`：法人監控主程式（每輪跑一次即退出，非長駐 daemon）。
  - 價位來源：夜間報告 JSON（TSM-srf-sim，`FAREN_REPORT_TOKEN`）→
    快取 `faren_pool.json` → yfinance 直接算（備援）。
  - 盤中偵測：證交所 MIS 公開行情，現價 <= 進場價即觸發。
  - TG：只在「現在馬上進場」時推播（`send_telegram_chunks`，Telegram Bot API），
    預警價不推。去重：同一檔同一天只推一次（`faren_notified.json`）。
- `.github/workflows/main.yml`：每 5 分鐘一輪，週一至週五 UTC 00:00–05:59
  （台北 08:00–13:59；程式內再限 08:50–13:45，時段外靜默退出）。

## Secrets（repo Settings → Secrets and variables → Actions）

- `TELEGRAM_TOKEN`、`TELEGRAM_CHAT_ID`：沿用原 30K repo 的設定（法人 TG 走這條線）。
- `FAREN_REPORT_TOKEN`：需新增。PAT（classic，scope `repo` 讀取），用來讀取
  私有 repo `wind910715/TSM-srf-sim` 的夜間報告 JSON。
  未設時程式改用快取 `faren_pool.json`，再無快取才用 yfinance 備援
  （Yahoo 無 3491、6187、6223 三檔，備援時跳過）。

## 本機驗證

```bash
python scanner.py --selftest   # 固定假資料驗邏輯，不碰網路、不推 TG
DRY_RUN=1 python scanner.py    # 乾跑：不推 TG，只印訊息
python scanner.py --refresh-pool  # 只更新 faren_pool.json
```
