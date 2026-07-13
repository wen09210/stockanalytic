# PTT 股市熱門標的追蹤（stockanalytic）

自動爬取 PTT Stock 板置底文章，分析鄉民討論熱度：jieba 斷詞產生文字雲、辨識被提及的台灣上市櫃股票並查詢股價，結果可輸出成深色交易平台風格的網頁報告，或記錄到 Google 試算表觀察每日變化。

**🔗 線上報告：<https://wen09210.github.io/stockanalytic/>**

![報告預覽](https://img.shields.io/badge/style-dark%20trading%20dashboard-131722)

## 專案結構

| 檔案 | 用途 |
|------|------|
| `ptt_stock_wordcloud.py` | 爬 PTT 置底文（排除公告）→ 文字雲 → 股價/走勢 → 產生 `report.html` |
| `ptt_stock_tracker.py` | 每日追蹤版：分析結果 Append 到 Google 試算表（股票追蹤、熱門詞彙兩分頁） |
| `report_from_sheet.py` | 反向流程：從 Google 試算表讀資料重建 HTML 報告（支援指定日期回放） |
| `sheet_export.csv` | 試算表的本地匯出檔（無 `credentials.json` 時的資料來源） |
| `index.html` | 發佈到 GitHub Pages 的報告快照 |

## 安裝

```bash
python3 -m venv .venv
.venv/bin/pip install -r requirements.txt
```

> macOS 內建 Python 3.9 需要 `cryptography<44`（requirements.txt 已處理）；Python 3.10+ 無此限制。

## 使用

```bash
# 產生今日報告（爬 PTT → report.html + wordcloud.png）
.venv/bin/python ptt_stock_wordcloud.py

# 記錄到 Google 試算表（需 credentials.json，否則為乾跑模式）
.venv/bin/python ptt_stock_tracker.py

# 從試算表資料重建報告（可指定日期）
.venv/bin/python report_from_sheet.py 2026-07-13

# 本機預覽報告
python3 -m http.server 8791   # 然後開 http://127.0.0.1:8791/report.html
```

## Google Sheets 連動設定

1. [Google Cloud Console](https://console.cloud.google.com/) 建專案，啟用 **Sheets API** 與 **Drive API**
2. 建立**服務帳戶**，下載 JSON 金鑰改名 `credentials.json` 放到專案根目錄（已被 `.gitignore` 排除，勿 commit）
3. 把金鑰內的 `client_email` 加入試算表「PTT股市熱門標的追蹤」的共用名單（編輯者）

寫入格式：`檢查日期 | 股票代碼 | 公司名稱 | PTT提及次數 | =GOOGLEFINANCE("TPE:代碼")`。

## 已知限制

- Google Finance 的 `TPE:` 前綴僅涵蓋**上市**股票，上櫃標的在試算表會顯示 `#N/A`（HTML 報告改用 yfinance 的 `.TWO` 後綴，不受影響）
- 股票偵測以公司簡稱與 4 位數代碼比對，與常用詞同名的公司（如「世界」「數字」）僅以代碼比對，見程式內 `AMBIGUOUS_COMPANY_NAMES`

## 部署

GitHub Pages 採 **deploy from branch**（`main` / root）。更新報告後：

```bash
cp report.html index.html
git add index.html && git commit -m "更新報告" && git push
```
