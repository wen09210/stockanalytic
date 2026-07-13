# -*- coding: utf-8 -*-
"""
PTT 股市熱門標的自動追蹤系統
================================================================

功能流程：
  1. 爬取 PTT 指定看板（預設 Stock 板）的置底文章（自動略過 [公告]），
     擷取內文與所有推文（處理 over18=1 cookie）
  2. jieba 中文斷詞 + 停用詞過濾 + 詞頻統計，
     生成當天文字雲圖片 wordcloud_today.png
  3. 比對全台上市櫃公司清單（證交所 ISIN 網頁），
     統計每檔股票被提及的次數（熱門度）
  4. 透過 gspread 將結果 Append 到 Google 試算表，每列格式：
     [檢查日期, 股票代碼, 公司名稱, PTT提及次數, GoogleFinance股價公式]

----------------------------------------------------------------
【一】Google Cloud Console 開通步驟（Service Account 憑證）
----------------------------------------------------------------
  1. 前往 https://console.cloud.google.com/ 並登入 Google 帳號
  2. 上方專案選單 →「新增專案」→ 取名（例如 ptt-stock-tracker）→ 建立
  3. 左側選單「API 和服務」→「程式庫」：
     - 搜尋「Google Sheets API」→ 啟用
     - 搜尋「Google Drive API」→ 啟用（gspread 用名稱開啟試算表時需要）
  4. 「API 和服務」→「憑證」→「建立憑證」→「服務帳戶 (Service Account)」
     - 取名後一路「建立並繼續」→「完成」（角色可留空）
  5. 點進剛建立的服務帳戶 →「金鑰」分頁 →「新增金鑰」→「建立新的金鑰」
     → 選「JSON」→ 下載的檔案改名為 credentials.json，放到本程式同目錄
  6. ★ 最重要的一步 ★
     打開 credentials.json，複製裡面的 "client_email"
     （長得像 xxx@xxx.iam.gserviceaccount.com），
     到你的 Google 試算表按「共用」，把這個 email 加入為「編輯者」。
     沒做這步程式會報 PERMISSION_DENIED / SpreadsheetNotFound。

----------------------------------------------------------------
【二】需要安裝的第三方套件
----------------------------------------------------------------
  pip install requests beautifulsoup4 jieba wordcloud matplotlib gspread google-auth

----------------------------------------------------------------
使用方式：
  python ptt_stock_tracker.py
  （若同目錄找不到 credentials.json，會改用「乾跑模式」：
    只把要寫入的資料印在終端機，方便先測試爬蟲與分析部分）
"""

import os
import re
import sys
import time
from collections import Counter
from datetime import date

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
import jieba
from wordcloud import WordCloud
import matplotlib
matplotlib.use("Agg")  # 不開視窗，直接輸出圖片檔
import matplotlib.pyplot as plt

# ---------------------------------------------------------------------------
# 全域設定（依需求自行修改）
# ---------------------------------------------------------------------------
PTT_BASE = "https://www.ptt.cc"
BOARD = "Stock"                            # 要爬的看板
EXCLUDE_TITLE_KEYWORDS = ["[公告]"]        # 標題含這些關鍵字的置底文不分析

# 所有輸出/憑證都以「腳本所在目錄」為基準，這樣用 cron/launchd 排程時不會因
# 工作目錄不同而找不到檔案
BASE_DIR = os.path.dirname(os.path.abspath(__file__))
WORDCLOUD_OUTPUT = os.path.join(BASE_DIR, "wordcloud_today.png")  # 文字雲輸出
TOP_N_WORDS = 200                          # 文字雲最多顯示詞數
MIN_MENTIONS = 1                           # 提及次數低於此值的股票不寫入試算表

# --- Google Sheets 設定 ---
CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials.json")  # Service Account 憑證檔
SPREADSHEET_NAME = "PTT股市熱門標的追蹤"    # 你的 Google 試算表「名稱」
# 每天的資料寫入「以日期命名的分頁」（例如 2026-07-13），永遠用同一份試算表；
# 同一天重跑會清空該分頁重寫（冪等），不會產生重複資料或新檔案
TOP_WORDS_TO_SHEET = 20                    # 每天記錄前幾名高頻詞

# 股價寫入方式：
#   USE_GOOGLEFINANCE = False（預設）→ 用 yfinance 寫入「檢查日收盤價」的固定數值，
#       涵蓋上市＋上櫃，不會有 #N/A，數字凍結不漂移。
#   USE_GOOGLEFINANCE = True → 改寫 GOOGLEFINANCE 公式（僅涵蓋上市，上櫃會 #N/A）。
USE_GOOGLEFINANCE = False
USE_HISTORICAL_CLOSE = False   # 僅在 USE_GOOGLEFINANCE=True 時有效（見 build_price_formula）

# 常見中文停用詞（可自行擴充）
STOPWORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一個", "上", "也", "很", "到", "說", "要", "去", "你", "會", "著", "沒有",
    "看", "好", "自己", "這", "那", "他", "她", "它", "我們", "你們", "他們",
    "什麼", "怎麼", "還", "跟", "被", "讓", "把", "但", "但是", "因為", "所以",
    "如果", "可以", "這個", "那個", "已經", "現在", "知道", "覺得", "應該",
    "還是", "或是", "或者", "然後", "而且", "只是", "真的", "沒", "又", "再",
    "請", "各位", "大家", "感謝", "謝謝", "如題", "小弟", "版上", "板上",
    # PTT 推文與圖片連結常見雜訊
    "XD", "xd", "推", "噓", "http", "https", "www", "com", "cc", "imgur",
    "jpg", "jpeg", "png", "gif", "mopix",
}

# 公司簡稱剛好是常見中文詞的排除清單（避免誤判，可自行增減）
# 這些股票只有在文中出現「4 位數代碼」時才會被計入
AMBIGUOUS_COMPANY_NAMES = {
    "數字", "世界", "大量", "全新", "中華", "三星", "無敵", "冠軍",
    "安心", "精華", "大將", "聯發", "全台", "萬在", "大樹", "統一",
    "華電", "美亞", "正文", "力士", "熱映",
}

# 中文字型候選路徑（由上往下找，找到第一個存在的就用）
FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",                       # macOS 蘋方
    "/System/Library/Fonts/STHeiti Medium.ttc",                 # macOS 黑體
    "C:/Windows/Fonts/msjh.ttc",                                # Windows 微軟正黑體
    "C:/Windows/Fonts/msjh.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",   # Linux Noto
]


# ---------------------------------------------------------------------------
# 1. 爬取 PTT 置底文章
# ---------------------------------------------------------------------------
def _mount_retry_adapter(session: requests.Session, total: int = 5) -> None:
    """幫 session 掛上會自動重試的 adapter。

    某些雲端主機（例如 GitHub Actions runner 的 Azure IP）在連到 PTT 時，
    可能在 TLS 握手階段就被直接斷線（ConnectionResetError），而不是回應
    HTTP 錯誤碼。這通常是來源 IP 被防爬蟲規則封鎖，重試「同一個」IP 不一定
    保證成功，但仍可能因為 PTT 端規則是機率性節流、或中間節點是負載平衡
    (多台前端只有部分有封鎖規則) 而在幾次重試後就打通，所以仍值得加上。
    """
    retry = Retry(
        total=total,
        connect=total,   # 涵蓋 TLS 握手被重置這類「連線建立階段」的失敗
        read=total,
        backoff_factor=2,   # 重試間隔：2s, 4s, 8s, 16s, 32s...
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


def make_ptt_session() -> requests.Session:
    """建立帶有「滿 18 歲同意」cookie、且會自動重試連線失敗的 requests Session。"""
    session = requests.Session()
    session.cookies.set("over18", "1", domain=".ptt.cc")
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        )
    })
    _mount_retry_adapter(session)
    return session


def get_pinned_articles(session: requests.Session, board: str) -> list[dict]:
    """抓取看板首頁的置底文章清單（略過公告類）。

    PTT 列表頁中，置底文章位於 <div class="r-list-sep"> 分隔線之後。
    """
    index_url = f"{PTT_BASE}/bbs/{board}/index.html"
    resp = session.get(index_url, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    sep = soup.find("div", class_="r-list-sep")
    if sep is None:
        print(f"[警告] {board} 板目前沒有置底文章")
        return []

    pinned = []
    for ent in sep.find_all_next("div", class_="r-ent"):
        title_tag = ent.select_one("div.title a")
        if title_tag is None:
            continue  # 被刪除的文章沒有連結
        title = title_tag.get_text(strip=True)
        if any(kw in title for kw in EXCLUDE_TITLE_KEYWORDS):
            print(f"  略過公告：{title}")
            continue
        pinned.append({"title": title, "url": PTT_BASE + title_tag["href"]})
    return pinned


def get_article_content(session: requests.Session, url: str) -> dict:
    """抓取單篇文章的內文與推文，回傳 {"content": str, "pushes": [str, ...]}"""
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    main = soup.find("div", id="main-content")
    if main is None:
        return {"content": "", "pushes": []}

    # 先取出推文再從 DOM 移除，剩下的才是內文
    pushes = []
    for push in main.find_all("div", class_="push"):
        content_tag = push.find("span", class_="push-content")
        if content_tag:
            text = content_tag.get_text(strip=True).lstrip(":").strip()
            if text:
                pushes.append(text)
        push.extract()

    # 移除文章開頭 metadata（作者/標題/時間）
    for meta in main.find_all("div", class_=["article-metaline", "article-metaline-right"]):
        meta.extract()

    content = main.get_text("\n", strip=True)
    content = re.split(r"\n--\n", content)[0]  # 去掉簽名檔
    content = "\n".join(
        line for line in content.split("\n") if not line.startswith("※")
    )
    return {"content": content, "pushes": pushes}


# ---------------------------------------------------------------------------
# 2. jieba 斷詞、詞頻統計與文字雲
# ---------------------------------------------------------------------------
def tokenize_and_count(texts: list[str]) -> Counter:
    """斷詞、過濾後統計詞頻。"""
    counter = Counter()
    for text in texts:
        for word in jieba.cut(text):
            word = word.strip()
            if not word or len(word) < 2:
                continue                      # 過濾空白與單字詞
            if word in STOPWORDS:
                continue                      # 過濾停用詞
            if re.fullmatch(r"[\W\d_]+", word):
                continue                      # 過濾純標點、純數字
            counter[word] += 1
    return counter


def find_chinese_font() -> str:
    """找出可用的中文字型檔路徑：先查候選清單，找不到再用萬用字元掃常見字型目錄。

    後者是為了應付 Linux 發行版之間套件安裝路徑／檔名的細微差異
    （例如 fonts-noto-cjk 在不同 Ubuntu 版本可能拆成多個檔案）。
    """
    import glob
    for path in FONT_CANDIDATES:
        if os.path.exists(path):
            return path
    for pattern in (
        "/usr/share/fonts/**/*CJK*",
        "/usr/share/fonts/**/*NotoSansTC*",
        "/usr/share/fonts/**/*WenQuanYi*",
    ):
        matches = glob.glob(pattern, recursive=True)
        if matches:
            return matches[0]
    sys.exit("[錯誤] 找不到中文字型，請在 FONT_CANDIDATES 加入你電腦上的字型路徑")


def draw_wordcloud(word_freq: Counter, output_path: str) -> None:
    """依詞頻繪製文字雲並存檔。"""
    if not word_freq:
        print("[警告] 沒有詞頻資料，跳過文字雲")
        return
    font_path = find_chinese_font()
    wc = WordCloud(
        font_path=font_path, width=1200, height=800,
        background_color="white", max_words=TOP_N_WORDS, colormap="tab10",
    ).generate_from_frequencies(word_freq)

    plt.figure(figsize=(12, 8))
    plt.imshow(wc, interpolation="bilinear")
    plt.axis("off")
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()
    print(f"[完成] 文字雲已儲存至 {output_path}")


# ---------------------------------------------------------------------------
# 3. 台股標的辨識與熱門度統計
# ---------------------------------------------------------------------------
def fetch_tw_stock_list() -> dict:
    """從證交所 ISIN 網頁抓全部上市＋上櫃公司。

    回傳 {公司簡稱: (代碼, 市場別)}，市場別為 "上市" 或 "上櫃"。
    失敗時回退到內建常見公司小清單。
    """
    stock_map = {}
    sources = [(2, "上市"), (4, "上櫃")]  # strMode=2 上市、4 上櫃
    session = requests.Session()
    _mount_retry_adapter(session)
    try:
        for mode, market in sources:
            url = f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}"
            resp = session.get(url, timeout=20)
            resp.raise_for_status()  # 錯誤頁（403/503...）要能被下面 except 攔到並改用備援清單
            resp.encoding = "big5"  # 該頁為 Big5 編碼
            soup = BeautifulSoup(resp.text, "html.parser")
            for row in soup.select("table.h4 tr"):
                cells = row.find_all("td")
                if len(cells) < 6:
                    continue
                # 第一欄格式：「代碼　名稱」（全形空白分隔）
                parts = cells[0].get_text(strip=True).split("　")
                if len(parts) != 2:
                    continue
                code, name = parts[0].strip(), parts[1].strip()
                if re.fullmatch(r"\d{4}", code):  # 只留 4 位數一般股票
                    stock_map[name] = (code, market)
        if not stock_map:
            # HTTP 狀態碼正常，但一筆都沒解析到 —— 通常是頁面結構被攔截頁取代，
            # 主動視為失敗走備援清單，而不是讓後面流程在空清單上默默失敗
            raise requests.RequestException("回應中解析不到任何股票資料（可能被導向攔截頁）")
        print(f"[資訊] 已載入 {len(stock_map)} 檔上市櫃股票清單")
    except requests.RequestException as e:
        print(f"[警告] 無法取得證交所清單（{e}），改用內建小清單")
        fallback = {
            "台積電": ("2330", "上市"), "鴻海": ("2317", "上市"),
            "聯發科": ("2454", "上市"), "長榮": ("2603", "上市"),
            "陽明": ("2609", "上市"), "萬海": ("2615", "上市"),
            "台達電": ("2308", "上市"), "聯電": ("2303", "上市"),
            "中鋼": ("2002", "上市"), "國泰金": ("2882", "上市"),
        }
        stock_map.update(fallback)
    return stock_map


def count_stock_mentions(texts: list[str], stock_map: dict) -> list[dict]:
    """統計每檔股票在文本中被提及的次數（名稱出現次數 + 代碼出現次數）。

    回傳依提及次數由高到低排序的
    [{"code": 代碼, "name": 名稱, "market": 市場別, "mentions": 次數}, ...]
    """
    full_text = "\n".join(texts)
    code_map = {code: (name, market) for name, (code, market) in stock_map.items()}

    mention_counter = Counter()  # key: 股票代碼

    # (a) 公司名稱出現次數（排除易誤判的模糊名稱）
    for name, (code, _market) in stock_map.items():
        if name in AMBIGUOUS_COMPANY_NAMES or len(name) < 2:
            continue
        n = full_text.count(name)
        if n > 0:
            mention_counter[code] += n

    # (b) 4 位數代碼出現次數（前後不能緊鄰數字，避免抓到年份等）
    for code in re.findall(r"(?<!\d)(\d{4})(?!\d)", full_text):
        if code in code_map:
            mention_counter[code] += 1

    results = []
    for code, mentions in mention_counter.most_common():
        if mentions < MIN_MENTIONS:
            continue
        name, market = code_map[code]
        results.append({
            "code": code, "name": name, "market": market, "mentions": mentions,
        })
    return results


# ---------------------------------------------------------------------------
# 4. 寫入 Google Sheets
# ---------------------------------------------------------------------------
def build_price_formula(code: str, check_date: date) -> str:
    """產生 GoogleFinance 股價公式字串（僅在 USE_GOOGLEFINANCE=True 時使用）。

    注意：GOOGLEFINANCE 的 "TPE:" 前綴只涵蓋台灣「上市」股票，
    上櫃（TPEx）標的多半抓不到、會顯示 #N/A。預設改用 yfinance 靜態收盤價
    （見 fetch_close_price），涵蓋上市＋上櫃。
    """
    if USE_HISTORICAL_CLOSE:
        d = check_date
        return (
            f'=INDEX(GOOGLEFINANCE("TPE:{code}","close",'
            f'DATE({d.year},{d.month},{d.day})),2,2)'
        )
    return f'=GOOGLEFINANCE("TPE:{code}")'


def fetch_close_price(code: str, check_date: date):
    """用 yfinance 取「檢查日（含）以前最近交易日」的收盤價（涵蓋上市 .TW 與上櫃 .TWO）。

    回傳浮點數；查不到（或資料是 NaN，例如剛上市、停牌）回傳 None。
    寫成固定數值，數字不會隨時間漂移。
    """
    import math
    import yfinance as yf
    target = check_date.isoformat()
    for suffix in (".TW", ".TWO"):  # 先試上市再試上櫃
        try:
            hist = yf.Ticker(code + suffix).history(start="2026-06-01")
            if hist.empty:
                continue
            best = None
            for dt, close in hist["Close"].items():
                if dt.strftime("%Y-%m-%d") <= target:
                    c = float(close)
                    if math.isfinite(c):  # 過濾 NaN/inf，避免寫入試算表時 JSON 序列化失敗
                        best = c
            if best is not None:
                return best
        except Exception:
            continue
    return None


def write_to_google_sheets(stock_rows: list[list], word_rows: list[list],
                           day: str) -> None:
    """把當天的資料寫入「同一份試算表」中以日期命名的分頁（例如 2026-07-13）。

    分頁版面：上半部為熱門標的表格、隔兩列後是高頻詞表格。
    - 永遠寫同一份試算表（SPREADSHEET_NAME），不會建立新檔案
    - 該日期分頁已存在時會先清空再重寫（同一天重跑不會累積重複資料）
    若找不到憑證檔則改用「乾跑模式」，只印出資料不實際寫入。
    """
    # 組出該分頁的完整儲存格內容（兩個區塊）
    values = [["檢查日期", "股票代碼", "公司名稱", "PTT提及次數", "股價"]]
    values += stock_rows
    values += [[], []]
    values += [["檢查日期", "排名", "詞彙", "出現次數"]]
    values += word_rows

    if not os.path.exists(CREDENTIALS_FILE):
        print(f"\n[乾跑模式] 找不到 {CREDENTIALS_FILE}，"
              f"以下為「將要寫入分頁『{day}』」的資料：")
        for r in values:
            print("  " + " | ".join(str(c) for c in r))
        print("\n[提示] 放好 credentials.json 並設定 SPREADSHEET_NAME 後重跑即可實際寫入")
        return

    import gspread
    from google.oauth2.service_account import Credentials

    # Service Account 授權（Sheets 讀寫 + Drive 依名稱開啟試算表）
    scopes = [
        "https://www.googleapis.com/auth/spreadsheets",
        "https://www.googleapis.com/auth/drive",
    ]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)

    try:
        spreadsheet = client.open(SPREADSHEET_NAME)
    except gspread.SpreadsheetNotFound:
        sys.exit(
            f"[錯誤] 找不到試算表「{SPREADSHEET_NAME}」。\n"
            "請確認：1) 名稱拼寫正確 2) 已把 credentials.json 裡的 client_email "
            "加入試算表的共用名單（編輯者權限）"
        )

    # 取得（或建立）以日期命名的分頁；已存在就先清空，確保同日重跑冪等
    try:
        ws = spreadsheet.worksheet(day)
        ws.clear()
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=day, rows=200, cols=10)

    # USER_ENTERED 讓 =GOOGLEFINANCE(...) 被當成公式而非文字
    ws.update(values=values, range_name="A1", value_input_option="USER_ENTERED")
    print(f"[完成] 已寫入「{SPREADSHEET_NAME}」的分頁「{day}」"
          f"（{len(stock_rows)} 檔股票、{len(word_rows)} 個詞）")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _record_source(day: str, article: dict, push_count: int) -> None:
    """把當天分析的 PTT 文章連結寫入 sources.json（報告的參考資料來源）。"""
    import json
    path = os.path.join(BASE_DIR, "sources.json")
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    data[day] = {"title": article["title"], "url": article["url"],
                 "pushes": push_count}
    with open(path, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def _scrape_pinned(today_str: str) -> tuple:
    """跑一次完整的置底文章爬取，回傳 (pinned, all_texts, push_total)。"""
    session = make_ptt_session()
    print(f"[資訊] 爬取 PTT {BOARD} 板置底文章（{today_str}）...")
    pinned = get_pinned_articles(session, BOARD)
    if not pinned:
        sys.exit("[結束] 沒有可分析的置底文章")

    all_texts, push_total = [], 0
    for art in pinned:
        print(f"  抓取：{art['title']}")
        data = get_article_content(session, art["url"])
        all_texts.append(data["content"])
        all_texts.extend(data["pushes"])
        push_total += len(data["pushes"])
    print(f"[資訊] 共分析 {len(pinned)} 篇置底文章")
    return pinned, all_texts, push_total


def main():
    today = date.today()

    # --- 步驟 1：爬置底文章（外層重試：某些雲端主機的出口 IP 可能被 PTT
    # 的防爬蟲規則在 TLS 層直接斷線，_mount_retry_adapter 處理的是單次連線的
    # 立即重試；這裡再包一層「整個流程重來」、間隔拉長到 30 秒，讓重試橫跨
    # 較長時間、更有機會避開節流窗口） ---
    ATTEMPTS = 3
    for attempt in range(1, ATTEMPTS + 1):
        try:
            pinned, all_texts, push_total = _scrape_pinned(today.isoformat())
            break
        except requests.exceptions.RequestException as e:
            print(f"[警告] 第 {attempt}/{ATTEMPTS} 次嘗試連線 PTT 失敗：{e}")
            if attempt == ATTEMPTS:
                sys.exit(
                    "[錯誤] 連續多次無法連線 PTT，可能是目前執行環境的出口 IP "
                    "被 PTT 的防爬蟲規則封鎖（常見於雲端主機／CI runner）。"
                )
            time.sleep(30)

    # 記錄今天的資料來源（PTT 文章連結）到 sources.json，供報告的參考資料使用
    _record_source(today.isoformat(), pinned[0], push_total)

    # --- 步驟 2：斷詞統計 + 文字雲 ---
    print("[資訊] jieba 斷詞與詞頻統計...")
    word_freq = tokenize_and_count(all_texts)
    print(f"[資訊] 有效詞彙 {len(word_freq)} 個，前 5 高頻：", end=" ")
    print("、".join(f"{w}({c})" for w, c in word_freq.most_common(5)))
    draw_wordcloud(word_freq, WORDCLOUD_OUTPUT)

    # --- 步驟 3：股票辨識與熱門度 ---
    print("[資訊] 載入台股上市櫃清單並統計提及次數...")
    stock_map = fetch_tw_stock_list()
    hot_stocks = count_stock_mentions(all_texts, stock_map)
    if not hot_stocks:
        sys.exit("[結束] 文中未偵測到任何上市櫃股票")

    print(f"\n========== 今日熱門標的（共 {len(hot_stocks)} 檔） ==========")
    for s in hot_stocks:
        print(f"  {s['name']}({s['code']}, {s['market']})：提及 {s['mentions']} 次")

    # --- 步驟 4：組資料列並寫入 Google Sheets ---
    print("[資訊] 查詢各標的收盤價（yfinance，涵蓋上市＋上櫃）...")
    stock_rows = []
    for s in hot_stocks:
        if USE_GOOGLEFINANCE:
            price = build_price_formula(s["code"], today)   # 寫公式（僅上市）
        else:
            p = fetch_close_price(s["code"], today)         # 寫固定數值（含上櫃）
            price = round(p, 2) if p is not None else "#N/A"
        stock_rows.append([
            today.isoformat(),   # 檢查日期 YYYY-MM-DD
            s["code"],           # 股票代碼
            s["name"],           # 公司名稱
            s["mentions"],       # PTT 提及次數（熱門度）
            price,               # 收盤價（數值）或 GoogleFinance 公式
        ])
    word_rows = [
        [today.isoformat(), rank, word, freq]
        for rank, (word, freq) in enumerate(
            word_freq.most_common(TOP_WORDS_TO_SHEET), start=1
        )
    ]
    write_to_google_sheets(stock_rows, word_rows, day=today.isoformat())


if __name__ == "__main__":
    main()
