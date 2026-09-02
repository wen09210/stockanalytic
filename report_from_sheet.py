# -*- coding: utf-8 -*-
"""
從 Google 試算表產生 HTML 分析報告
================================================================

資料來源是「PTT股市熱門標的追蹤」試算表（股票代碼、公司名稱、PTT 提及次數、
高頻詞），近一月走勢與漲跌幅由 yfinance 即時補上，輸出與 ptt_stock_wordcloud.py
相同的 mono-color 單色印刷風格 report.html。

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
import re
import sys
from collections import Counter

import yfinance as yf

# 重用主腳本的字型搜尋、文字雲、走勢圖與報告產生函式，避免兩套樣式不同步
import ptt_stock_wordcloud as wc

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials.json")
CSV_FALLBACK = os.path.join(BASE_DIR, "sheet_export.csv")
SOURCES_FILE = os.path.join(BASE_DIR, "sources.json")  # 每天對應的 PTT 文章連結
SPREADSHEET_NAME = "PTT股市熱門標的追蹤"
SHEET_URL = "https://docs.google.com/spreadsheets/d/1B3rQufouHb6n9z2yTvBpIeFK_Uxv1TcxcimXIGsED1s/edit"
WORDCLOUD_OUTPUT = os.path.join(BASE_DIR, "wordcloud.png")
REPORT_OUTPUT = os.path.join(BASE_DIR, "report.html")


def load_sources() -> dict:
    """讀取 sources.json：{日期: {title, url, pushes}}，作為報告的資料來源連結。"""
    if os.path.exists(SOURCES_FILE):
        import json
        with open(SOURCES_FILE, encoding="utf-8") as f:
            return json.load(f)
    return {}


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
        spreadsheet = client.open(SPREADSHEET_NAME)
        # 合併所有分頁的內容：新版是「每天一個日期分頁」，
        # 舊版（單一分頁、日期在欄位裡）也相容——反正每列都帶檢查日期
        rows = []
        for ws in spreadsheet.worksheets():
            rows.extend(ws.get_all_values())
        print(f"[資訊] 從線上試算表「{SPREADSHEET_NAME}」讀取 "
              f"{len(spreadsheet.worksheets())} 個分頁")
        return rows

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
            # 台股是 4 位數字，美股是英文代碼（NVDA、BRK-B）：兩者都要收，
            # 但要擋掉表頭殘留或空白等雜列
            if not re.fullmatch(r"\d{4}|[A-Z][A-Z0-9.\-]{0,6}", code):
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
_history_cache: dict = {}  # code -> [(YYYY-MM-DD, close), ...]，避免重複查詢


def _get_history(code: str):
    """抓該股票近兩個月的日收盤序列並快取。

    台股試 .TW（上市）失敗再試 .TWO（上櫃）；美股代碼（含英文字母）不加後綴。
    會過濾掉 NaN/inf 的收盤價（yfinance 對盤中無成交、剛上市/上櫃的股票
    偶爾會回傳這種值），避免報告上顯示出字面上的「nan」。
    """
    import math
    if code in _history_cache:
        return _history_cache[code]
    result = (None, [])
    # 美股代碼含英文字母（NVDA、BRK-B），台股是純 4 位數字
    suffixes = ("",) if not code.isdigit() else (".TW", ".TWO")
    for suffix in suffixes:
        try:
            hist = yf.Ticker(code + suffix).history(period="2mo")
            if not hist.empty:
                closes = [
                    (d.strftime("%Y-%m-%d"), float(c))
                    for d, c in hist["Close"].items()
                    if math.isfinite(float(c))
                ]
                if closes:  # 過濾完仍有可用資料才算成功，否則繼續試下一個後綴
                    result = (code + suffix, closes)
                    break
        except Exception:
            continue
    _history_cache[code] = result
    return result


def enrich_with_prices(stocks: list[dict], day: str) -> list[dict]:
    """為每檔股票補上「截至檢查日」的收盤序列、當日價與漲跌幅。

    走勢圖只畫到檢查日為止，才不會出現「7/10 的報告畫到 7/13 的走勢」。
    """
    results = []
    for s in sorted(stocks, key=lambda x: -x["mentions"]):
        symbol, all_closes = _get_history(s["code"])
        # 保留（日期, 收盤價）配對，才能標示出這筆收盤價「真正」的交易日
        paired = [(d, c) for d, c in all_closes if d <= day][-22:]  # 截至當日約一個月
        if not paired:
            print(f"  {s['name']}({s['code']})：查無股價，略過")
            continue
        closes = [c for _, c in paired]
        # 顯示日期取實際收盤日，而非檢查日：檢查日若逢週末/國定假日，
        # 收盤價會是前一個交易日的，日期就該標成那個交易日，避免誤標
        close_date = paired[-1][0]
        change_pct = None
        if len(closes) >= 2:
            change_pct = (closes[-1] - closes[-2]) / closes[-2] * 100
        results.append({
            "name": s["name"], "symbol": symbol, "price": closes[-1],
            "change_pct": change_pct, "date": close_date, "mentions": s["mentions"],
            "closes": closes,
        })
    return results


# ---------------------------------------------------------------------------
# 首頁：日期選擇器（日曆）+ iframe 載入該日報告
# ---------------------------------------------------------------------------
def write_tabbed_index(days: list, output_path: str) -> None:
    """產生首頁：頂端只顯示目前選中的日期，點下去開日曆挑其他日期。

    日曆只讓「有報告的日期」可以點；沒有資料的日子會變灰不可選，
    避免點了之後 iframe 載入不存在的檔案而空白。
    """
    import json

    latest = days[-1]
    # 用 token 取代而非 f-string：底下的 JS/CSS 有大量大括號，
    # 用 f-string 得把每個括號都寫成兩倍，很容易漏改而產生難查的錯誤
    html = _INDEX_TEMPLATE
    html = html.replace("__DAYS_JSON__", json.dumps(days))
    html = html.replace("__LATEST__", latest)
    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[完成] 首頁已儲存至 {output_path}（可選日期 {len(days)} 天）")


_INDEX_TEMPLATE = """<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PTT Stock 熱門標的追蹤（每日）</title>
<style>
  /* mono-color：紙張 Cool Gray #E9E9E5、碳墨 #30343A、紅墨 #C83232。
     與報告內頁同一套配方，見 .claude/skills/mono-color/ATTRIBUTION.md */
  * { box-sizing: border-box; margin: 0; }
  body {
    height: 100vh; display: flex; flex-direction: column;
    background: #E9E9E5; color: #30343A;
    font-family: "Helvetica Neue", Helvetica, "PingFang TC",
                 "Noto Sans TC", "Microsoft JhengHei", sans-serif;
  }
  .tabbar {
    display: flex; align-items: center; gap: 14px; padding: 12px 20px;
    border-bottom: 2px solid #30343A; flex-wrap: wrap;
  }
  .brand {
    font-weight: 700; letter-spacing: .18em; color: #30343A;
    margin-right: 4px; font-size: .78rem; text-transform: uppercase;
  }
  /* --- 日期選擇器 --- */
  .picker { position: relative; }
  .datebtn {
    display: inline-flex; align-items: center; gap: 8px;
    background: #C83232; border: 1px solid #C83232; color: #E9E9E5;
    padding: 6px 14px; font-size: .82rem; font-weight: 700;
    cursor: pointer; font-variant-numeric: tabular-nums;
    font-family: inherit; letter-spacing: .04em;
  }
  .datebtn:hover { background: #30343A; border-color: #30343A; }
  .datebtn .caret { font-size: .66rem; opacity: .75; }
  .today-flag {
    border: 1px solid rgba(233,233,229,.5);
    padding: 0 7px; font-size: .68rem; letter-spacing: .08em;
  }
  /* 透明遮罩：iframe 會吃掉點擊事件，沒有它就無法「點報告區關閉日曆」 */
  .scrim { position: fixed; inset: 0; z-index: 40; }
  .scrim[hidden] { display: none; }
  .cal {
    position: absolute; top: calc(100% + 8px); left: 0; z-index: 50;
    background: #E9E9E5; border: 2px solid #30343A;
    padding: 14px; width: 268px;
  }
  .cal[hidden] { display: none; }
  .cal-head {
    display: flex; align-items: center; justify-content: space-between;
    margin-bottom: 10px; padding-bottom: 8px;
    border-bottom: 1px solid rgba(48,52,58,.28);
  }
  .cal-title {
    font-size: .82rem; font-weight: 700; color: #30343A;
    font-variant-numeric: tabular-nums; letter-spacing: .04em;
  }
  .cal-nav {
    background: transparent; border: 1px solid rgba(48,52,58,.4); color: #30343A;
    width: 26px; height: 26px; cursor: pointer;
    font-size: .9rem; line-height: 1; font-family: inherit;
  }
  .cal-nav:hover:not(:disabled) { background: #30343A; color: #E9E9E5; }
  .cal-nav:disabled { opacity: .25; cursor: default; }
  .cal-grid { display: grid; grid-template-columns: repeat(7, 1fr); gap: 2px; }
  .cal-wd {
    text-align: center; font-size: .66rem; color: #30343A; opacity: .55;
    padding: 4px 0 6px; letter-spacing: .06em;
  }
  .cal-d {
    aspect-ratio: 1; border: 0; background: transparent;
    color: rgba(48,52,58,.3); font-size: .8rem; cursor: default;
    font-variant-numeric: tabular-nums; font-family: inherit;
  }
  /* 有報告的日期才可點 */
  .cal-d.has {
    color: #30343A; border: 1px solid rgba(48,52,58,.28);
    cursor: pointer; font-weight: 700;
  }
  .cal-d.has:hover { background: #30343A; color: #E9E9E5; }
  .cal-d.sel { background: #C83232; border-color: #C83232; color: #E9E9E5; }
  .cal-empty { visibility: hidden; }
  .cal-foot {
    margin-top: 10px; padding-top: 8px; font-size: .68rem;
    border-top: 1px solid rgba(48,52,58,.28);
    color: #30343A; opacity: .55; text-align: center;
  }
  iframe { flex: 1; border: 0; width: 100%; background: #E9E9E5; }
</style>
</head>
<body>
  <div class="scrim" id="scrim" hidden></div>
  <div class="tabbar">
    <span class="brand">PTT STOCK 每日追蹤</span>
    <div class="picker">
      <button class="datebtn" id="datebtn" aria-haspopup="dialog" aria-expanded="false">
        <span id="datelabel"></span><span class="caret">▼</span>
      </button>
      <div class="cal" id="cal" role="dialog" aria-label="選擇日期" hidden>
        <div class="cal-head">
          <button class="cal-nav" id="prev" aria-label="上個月">‹</button>
          <span class="cal-title" id="caltitle"></span>
          <button class="cal-nav" id="next" aria-label="下個月">›</button>
        </div>
        <div class="cal-grid" id="calgrid"></div>
        <div class="cal-foot">只有產出報告的日期可以選取</div>
      </div>
    </div>
  </div>
  <iframe id="frame" src="report___LATEST__.html" title="每日報告"></iframe>
<script>
  var DAYS = __DAYS_JSON__;
  var LATEST = "__LATEST__";
  var avail = new Set(DAYS);
  var selected = LATEST;

  var btn = document.getElementById("datebtn");
  var label = document.getElementById("datelabel");
  var cal = document.getElementById("cal");
  var grid = document.getElementById("calgrid");
  var title = document.getElementById("caltitle");
  var prev = document.getElementById("prev");
  var next = document.getElementById("next");
  var frame = document.getElementById("frame");
  var scrim = document.getElementById("scrim");

  function pad(n) { return String(n).padStart(2, "0"); }
  function iso(y, m, d) { return y + "-" + pad(m + 1) + "-" + pad(d); }
  function parse(s) {
    var p = s.split("-");
    return { y: +p[0], m: +p[1] - 1, d: +p[2] };
  }

  // 可選月份的範圍：不讓使用者翻到完全沒有資料的月份
  var first = parse(DAYS[0]);
  var last = parse(DAYS[DAYS.length - 1]);
  var minIdx = first.y * 12 + first.m;
  var maxIdx = last.y * 12 + last.m;

  var view = parse(selected);
  var viewY = view.y, viewM = view.m;

  function setLabel() {
    label.textContent = selected;
    var flag = document.querySelector(".today-flag");
    if (flag) flag.remove();
    if (selected === LATEST) {
      var s = document.createElement("span");
      s.className = "today-flag";
      s.textContent = "今天";
      label.after(s);
    }
  }

  function renderCal() {
    title.textContent = viewY + " 年 " + (viewM + 1) + " 月";
    var idx = viewY * 12 + viewM;
    prev.disabled = idx <= minIdx;
    next.disabled = idx >= maxIdx;

    grid.textContent = "";
    ["日", "一", "二", "三", "四", "五", "六"].forEach(function (w) {
      var e = document.createElement("div");
      e.className = "cal-wd";
      e.textContent = w;
      grid.appendChild(e);
    });

    var lead = new Date(viewY, viewM, 1).getDay();
    var total = new Date(viewY, viewM + 1, 0).getDate();
    for (var i = 0; i < lead; i++) {
      var pad0 = document.createElement("button");
      pad0.className = "cal-d cal-empty";
      pad0.disabled = true;
      grid.appendChild(pad0);
    }
    for (var d = 1; d <= total; d++) {
      var key = iso(viewY, viewM, d);
      var cell = document.createElement("button");
      cell.className = "cal-d";
      cell.textContent = d;
      if (avail.has(key)) {
        cell.classList.add("has");
        if (key === selected) cell.classList.add("sel");
        cell.dataset.day = key;
      } else {
        cell.disabled = true;      // 沒有報告的日子不可點
      }
      grid.appendChild(cell);
    }
  }

  function openCal(open) {
    cal.hidden = !open;
    scrim.hidden = !open;
    btn.setAttribute("aria-expanded", open ? "true" : "false");
    if (open) {
      var v = parse(selected);
      viewY = v.y; viewM = v.m;
      renderCal();
    }
  }

  btn.addEventListener("click", function (e) {
    e.stopPropagation();
    openCal(cal.hidden);
  });
  cal.addEventListener("click", function (e) {
    e.stopPropagation();
    var day = e.target.dataset && e.target.dataset.day;
    if (!day) return;
    selected = day;
    frame.src = "report_" + day + ".html";
    setLabel();
    openCal(false);
  });
  prev.addEventListener("click", function () {
    if (--viewM < 0) { viewM = 11; viewY--; }
    renderCal();
  });
  next.addEventListener("click", function () {
    if (++viewM > 11) { viewM = 0; viewY++; }
    renderCal();
  });
  scrim.addEventListener("click", function () { openCal(false); });
  document.addEventListener("keydown", function (e) {
    if (e.key === "Escape") openCal(false);
  });

  setLabel();
</script>
</body>
</html>"""


def main():
    rows = load_rows()
    sources = load_sources()

    # 解析出所有日期（parse_sheet 一次取一天，先掃出全部日期清單）
    all_days = sorted({
        cells[0].strip() for cells in rows
        if cells and re.fullmatch(r"\d{4}-\d{2}-\d{2}", cells[0].strip())
    })
    print(f"[資訊] 試算表內共有 {len(all_days)} 天資料：{', '.join(all_days)}")

    for day in all_days:
        _, stocks, word_freq = parse_sheet(rows, day)

        # 詞彙分類：股票相關進文字雲，不相關另列一區
        stock_names = [s["name"] for s in stocks]
        related, unrelated = wc.classify_words(word_freq, extra_related=stock_names)
        print(f"\n=== {day}：{len(stocks)} 檔股票｜詞彙 相關 {len(related)}、"
              f"不相關 {len(unrelated)} ===")

        # 每天各自的文字雲與報告檔
        wc_path = os.path.join(BASE_DIR, f"wordcloud_{day}.png")
        report_path = os.path.join(BASE_DIR, f"report_{day}.html")
        wc.draw_wordcloud(related, wc_path)

        # 資料來源：優先用 sources.json 記錄的當日 PTT 文章連結；沒有才退回試算表
        src = sources.get(day)
        if src:
            articles = [{
                "title": f"PTT Stock 板　{src['title']}（採計 {src['pushes']} 則推文）",
                "url": src["url"],
            }, {"title": f"Google 試算表：{SPREADSHEET_NAME}", "url": SHEET_URL}]
        else:
            articles = [{"title": f"Google 試算表：{SPREADSHEET_NAME}（{day}）", "url": SHEET_URL}]

        stock_results = enrich_with_prices(stocks, day)
        wc.generate_html_report(
            board=wc.BOARD,
            articles=articles,
            word_freq=related,
            stock_results=stock_results,
            wordcloud_path=wc_path,
            output_path=report_path,
            unrelated_words=unrelated,
            # 情緒由爬蟲端算好存在 sources.json；舊資料沒這欄就傳 None 不顯示卡片
            sentiment=(src or {}).get("sentiment"),
        )

    # 分頁器首頁（預設顯示最新一天）
    write_tabbed_index(all_days, REPORT_OUTPUT)


if __name__ == "__main__":
    main()
