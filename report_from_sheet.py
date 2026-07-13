# -*- coding: utf-8 -*-
"""
從 Google 試算表產生 HTML 分析報告
================================================================

資料來源是「PTT股市熱門標的追蹤」試算表（股票代碼、公司名稱、PTT 提及次數、
高頻詞），近一月走勢與漲跌幅由 yfinance 即時補上，輸出與 ptt_stock_wordcloud.py
相同的深色交易平台風格 report.html。

讀取方式（自動擇一）：
  1. 同目錄有 credentials.json → 用 gspread 直接讀線上試算表
  2. 沒有憑證 → 讀本地匯出檔 sheet_export.csv
     （Google 試算表可用「檔案 → 下載 → CSV」匯出）

使用方式：
  .venv/bin/python report_from_sheet.py            # 用最新一天的資料
  .venv/bin/python report_from_sheet.py 2026-07-13 # 指定某一天

需要套件（同 requirements.txt）：
  pip install -r requirements.txt
"""

import csv
import os
import sys
from collections import Counter

import yfinance as yf

# 重用主腳本的字型搜尋、文字雲、走勢圖與報告產生函式，避免兩套樣式不同步
import ptt_stock_wordcloud as wc

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials.json")
CSV_FALLBACK = os.path.join(BASE_DIR, "sheet_export.csv")
SPREADSHEET_NAME = "PTT股市熱門標的追蹤"
SHEET_URL = "https://docs.google.com/spreadsheets/d/1G8vxy9pjASnoSN6SzO_qWyOJQ8fIRtBwi6EMhTw64Pw/edit"
WORDCLOUD_OUTPUT = os.path.join(BASE_DIR, "wordcloud.png")
REPORT_OUTPUT = os.path.join(BASE_DIR, "report.html")


# ---------------------------------------------------------------------------
# 讀取試算表資料（線上 gspread 或本地 CSV）
# ---------------------------------------------------------------------------
def load_rows() -> list[list[str]]:
    """讀取試算表全部儲存格，回傳列的清單（每列是字串清單）。"""
    if os.path.exists(CREDENTIALS_FILE):
        import gspread
        from google.oauth2.service_account import Credentials

        scopes = [
            "https://www.googleapis.com/auth/spreadsheets.readonly",
            "https://www.googleapis.com/auth/drive.readonly",
        ]
        creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
        client = gspread.authorize(creds)
        ws = client.open(SPREADSHEET_NAME).sheet1
        print(f"[資訊] 從線上試算表「{SPREADSHEET_NAME}」讀取資料")
        return ws.get_all_values()

    if not os.path.exists(CSV_FALLBACK):
        sys.exit(
            f"[錯誤] 找不到 {CREDENTIALS_FILE} 也找不到 {CSV_FALLBACK}。\n"
            "請把試算表用「檔案 → 下載 → CSV」匯出成 sheet_export.csv，"
            "或放入 credentials.json 直接讀線上資料。"
        )
    print(f"[資訊] 從本地匯出檔 {os.path.basename(CSV_FALLBACK)} 讀取資料")
    with open(CSV_FALLBACK, encoding="utf-8") as f:
        return [row for row in csv.reader(f)]


def parse_sheet(rows: list[list[str]], target_date) -> tuple:
    """把試算表列解析成 (日期, 股票清單, 詞頻 Counter)。

    表內有兩個區塊：股票追蹤（表頭含「股票代碼」）與熱門詞彙（表頭含「詞彙」），
    以表頭列切換解析模式；資料會累積多天，依「檢查日期」欄過濾出目標日期。
    """
    stocks_by_date: dict[str, list[dict]] = {}
    words_by_date: dict[str, Counter] = {}
    mode = None  # "stock" 或 "word"

    for row in rows:
        cells = [c.strip() for c in row]
        if not any(cells):
            continue  # 空白列
        # 表頭列 → 切換解析模式
        if "股票代碼" in cells:
            mode = "stock"
            continue
        if "詞彙" in cells:
            mode = "word"
            continue
        if mode == "stock" and len(cells) >= 4:
            d, code, name, mentions = cells[0], cells[1], cells[2], cells[3]
            if not code.isdigit():
                continue
            stocks_by_date.setdefault(d, []).append({
                "code": code, "name": name,
                "mentions": int(mentions) if mentions.isdigit() else 0,
            })
        elif mode == "word" and len(cells) >= 4:
            d, _rank, word, freq = cells[0], cells[1], cells[2], cells[3]
            if word:
                words_by_date.setdefault(d, Counter())[word] = (
                    int(freq) if freq.isdigit() else 0
                )

    if not stocks_by_date:
        sys.exit("[錯誤] 試算表中找不到股票資料")

    # 未指定日期就取最新一天
    day = target_date or max(stocks_by_date)
    if day not in stocks_by_date:
        sys.exit(f"[錯誤] 試算表中沒有 {day} 的資料，"
                 f"可用日期：{', '.join(sorted(stocks_by_date))}")
    return day, stocks_by_date[day], words_by_date.get(day, Counter())


# ---------------------------------------------------------------------------
# 補上 yfinance 股價與走勢，組成報告需要的資料結構
# ---------------------------------------------------------------------------
def enrich_with_prices(stocks: list[dict]) -> list[dict]:
    """為每檔股票補上近一月收盤序列、最新價與漲跌幅（同時試 .TW 與 .TWO）。"""
    results = []
    for s in sorted(stocks, key=lambda x: -x["mentions"]):
        closes, symbol = [], None
        for suffix in (".TW", ".TWO"):  # 先試上市再試上櫃
            try:
                hist = yf.Ticker(s["code"] + suffix).history(period="1mo")
                if not hist.empty:
                    closes = [float(c) for c in hist["Close"].tolist()]
                    symbol = s["code"] + suffix
                    break
            except Exception:
                continue
        if not closes:
            print(f"  {s['name']}({s['code']})：查無股價，略過")
            continue
        change_pct = None
        if len(closes) >= 2:
            change_pct = (closes[-1] - closes[-2]) / closes[-2] * 100
        results.append({
            "name": s["name"], "symbol": symbol, "price": closes[-1],
            "change_pct": change_pct, "date": "", "mentions": s["mentions"],
            "closes": closes,
        })
        print(f"  {s['name']}({symbol})：{closes[-1]:.2f} 元｜提及 {s['mentions']} 次")
    return results


def main():
    target_date = sys.argv[1] if len(sys.argv) > 1 else None
    rows = load_rows()
    day, stocks, word_freq = parse_sheet(rows, target_date)
    print(f"[資訊] 使用 {day} 的資料：{len(stocks)} 檔股票、{len(word_freq)} 個詞")

    # 文字雲（試算表只存 Top 20 詞，雲會比即時爬蟲版稀疏一些）
    wc.draw_wordcloud(word_freq, WORDCLOUD_OUTPUT)

    # 補股價與走勢
    print("[資訊] 透過 yfinance 補上股價與近一月走勢...")
    stock_results = enrich_with_prices(stocks)
    for r in stock_results:
        r["date"] = day  # 表格日期欄顯示資料所屬日

    # 產生報告（資料來源卡片指向 Google 試算表）
    articles = [{"title": f"Google 試算表：{SPREADSHEET_NAME}（{day}）", "url": SHEET_URL}]
    wc.generate_html_report(
        board=wc.BOARD,
        articles=articles,
        word_freq=word_freq,
        stock_results=stock_results,
        wordcloud_path=WORDCLOUD_OUTPUT,
        output_path=REPORT_OUTPUT,
    )


if __name__ == "__main__":
    main()
