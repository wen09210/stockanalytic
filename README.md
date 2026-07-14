# PTT 股市熱門標的追蹤（stockanalytic）

自動爬取 PTT Stock 板置底文章，分析鄉民討論熱度：jieba 斷詞產生文字雲、辨識被提及的台灣上市櫃股票並查詢股價，結果可輸出成深色交易平台風格的網頁報告，或記錄到 Google 試算表觀察每日變化。

**🔗 線上報告：<https://wen09210.github.io/stockanalytic/>**

![報告預覽](https://img.shields.io/badge/style-dark%20trading%20dashboard-131722)

## 專案結構

| 檔案 | 用途 |
|------|------|
| `ptt_stock_wordcloud.py` | 爬 PTT 置底文（排除公告）→ 詞彙分類 → 文字雲（僅股票相關詞）→ 產生 `report_live.html` |
| `ptt_stock_tracker.py` | 每日追蹤版：結果寫入 Google 試算表中「以日期命名的分頁」（同日重跑清空重寫） |
| `report_from_sheet.py` | 從 Google 試算表讀資料，產生每日報告 `report_日期.html` + 分頁器 `report.html` |
| `sheet_export.csv` | 試算表的本地匯出檔（無 `credentials.json` 時的資料來源） |
| `report.html` / `index.html` | 分頁器首頁：日期頁籤切換各日報告（index 為 Pages 發佈版） |

報告中的詞彙會分成「股票相關」（公司名、股市詞彙表、含股/盤/漲/跌等字）與「其他話題」兩區，只有股票相關詞會進文字雲。

## 安裝

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

> macOS 內建 Python 3.9 需要 `cryptography<44`（requirements.txt 已處理）；Python 3.10+ 無此限制。

## 使用

```bash
# 產生即時報告（爬 PTT → report_live.html + wordcloud.png）
.venv/bin/python ptt_stock_wordcloud.py

# 記錄到 Google 試算表的日期分頁（需 credentials.json，否則為乾跑模式）
.venv/bin/python ptt_stock_tracker.py

# 從試算表資料產生各日報告 + 分頁器 report.html
.venv/bin/python report_from_sheet.py

# 本機預覽
python3 -m http.server 8791   # 然後開 http://127.0.0.1:8791/report.html
```

## Google Sheets 連動設定

1. [Google Cloud Console](https://console.cloud.google.com/) 建專案，啟用 **Sheets API** 與 **Drive API**
2. 建立**服務帳戶**，下載 JSON 金鑰改名 `credentials.json` 放到專案根目錄（已被 `.gitignore` 排除，勿 commit）
3. 把金鑰內的 `client_email` 加入試算表「PTT股市熱門標的追蹤」的共用名單（編輯者）

寫入方式：**同一份試算表、每天一個以日期命名的分頁**（例如 `2026-07-13`），分頁內上半部為熱門標的（`檢查日期 | 代碼 | 公司 | 提及次數 | 收盤價`）、下半部為當天全部高頻詞（不設數量上限）。同一天重跑會清空該分頁重寫，不會產生重複資料。

股價預設用 **yfinance 寫入檢查日收盤價的固定數值**（涵蓋上市 `.TW` 與上櫃 `.TWO`，不會有 #N/A）。若想改回 `=GOOGLEFINANCE()` 公式（僅上市），把 `ptt_stock_tracker.py` 的 `USE_GOOGLEFINANCE` 設為 `True`。

用 `sync_to_sheet.py` 可把本地 `sheet_export.csv` 的多天資料一次推成日期分頁（並清除非日期分頁）。

## 已知限制

- Google Finance 的 `TPE:` 前綴僅涵蓋**上市**股票，上櫃標的在試算表會顯示 `#N/A`（HTML 報告改用 yfinance 的 `.TWO` 後綴，不受影響）
- 股票偵測以公司簡稱與 4 位數代碼比對，與常用詞同名的公司（如「世界」「數字」）僅以代碼比對，見程式內 `AMBIGUOUS_COMPANY_NAMES`

## 部署

GitHub Pages 採 **deploy from branch**（`main` / root）。更新報告後：

```bash
cp report.html index.html
git add index.html report_*.html && git commit -m "更新報告" && git push
```
