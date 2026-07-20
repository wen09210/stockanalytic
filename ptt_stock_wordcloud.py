# -*- coding: utf-8 -*-
"""
PTT 置底文章爬蟲 + 文字雲 + 台股股價查詢
========================================

功能流程：
  1. 爬取 PTT 指定看板（預設 Stock 板）的「置底文章」（處理滿 18 歲 cookie）
  2. 以 jieba 對文章內文與推文做中文斷詞，過濾停用詞並統計詞頻
  3. 用 wordcloud + matplotlib 繪製文字雲並存檔（自動尋找中文字型，避免亂碼/方框）
  4. 從全台上市／上櫃公司清單（證交所 ISIN 網頁）比對文中出現的公司名稱或股票代碼，
     再用 yfinance 查詢最新收盤價（上市 → 代碼.TW，上櫃 → 代碼.TWO）

需要安裝的第三方套件：
  pip install requests beautifulsoup4 jieba wordcloud matplotlib yfinance

使用方式：
  python ptt_stock_wordcloud.py
"""

import re
import sys
from collections import Counter

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry
from bs4 import BeautifulSoup
import jieba
from wordcloud import WordCloud
import matplotlib
matplotlib.use("Agg")  # 不開視窗，直接輸出圖片檔
import matplotlib.pyplot as plt
import yfinance as yf

# ---------------------------------------------------------------------------
# 全域設定
# ---------------------------------------------------------------------------
PTT_BASE = "https://www.ptt.cc"
BOARD = "Stock"                          # 要爬的看板名稱
WORDCLOUD_OUTPUT = "wordcloud.png"       # 文字雲輸出檔名
REPORT_OUTPUT = "report_live.html"       # HTML 網頁報告輸出檔名
                                         # （report.html 保留給分頁器首頁，避免互相覆蓋）
EXCLUDE_TITLE_KEYWORDS = ["[公告]"]      # 標題含這些關鍵字的置底文不分析（可自行增減）
MIN_WORD_FREQ = 5                        # 報告只顯示出現次數 >= 此值的字詞（濾掉只出現一兩次的雜訊詞）

# 常見中文停用詞（可自行擴充，或改成讀取外部停用詞檔）
STOPWORDS = {
    "的", "了", "在", "是", "我", "有", "和", "就", "不", "人", "都", "一",
    "一個", "上", "也", "很", "到", "說", "要", "去", "你", "會", "著", "沒有",
    "看", "好", "自己", "這", "那", "他", "她", "它", "我們", "你們", "他們",
    "什麼", "怎麼", "還", "跟", "被", "讓", "把", "但", "但是", "因為", "所以",
    "如果", "可以", "這個", "那個", "已經", "現在", "知道", "覺得", "應該",
    "還是", "或是", "或者", "然後", "而且", "只是", "真的", "沒", "又", "再",
    "請", "各位", "大家", "感謝", "謝謝", "如題", "小弟", "版上", "板上",
    # 時間相關詞（今天/明天/昨天…等，對個股分析沒有資訊價值的雜訊）
    "今天", "明天", "昨天", "後天", "前天", "今日", "明日", "昨日",
    "今年", "明年", "去年", "早上", "下午", "晚上", "中午",
    "剛剛", "剛才", "目前", "最近", "之前", "之後", "以前", "以後",
    "等等", "時候",
    # PTT 推文常見雜訊
    "XD", "xd", "推", "噓", "http", "https", "www", "com", "cc", "imgur",
    "jpg", "jpeg", "png", "gif", "mopix",  # 圖片連結產生的雜訊
}

# 公司簡稱剛好是常見中文詞的排除清單（避免誤判，可自行增減）
# 例如「數字」(5287)、「世界」(5347) 這類名稱幾乎每篇文章都會出現
AMBIGUOUS_COMPANY_NAMES = {
    "數字", "世界", "大量", "全新", "中華", "三星", "無敵", "冠軍",
    "安心", "精華", "大將", "聯發", "全台", "萬在", "大樹", "統一",
    "華電", "美亞", "正文", "力士", "熱映",
}

# ---- 股票相關詞彙判斷 ----
# 只要詞中含有這些「字」就視為股市相關（股、盤、漲、跌、噴＝股板行情用語）
STOCK_TERM_CHARS = "股盤漲跌噴崩"
# 不含上述字但仍屬股市用語的完整詞（可自行擴充）
STOCK_TERM_WORDS = {
    "台指", "富台", "小台", "大台", "期貨", "選擇權", "權證", "ETF", "etf",
    "外資", "投信", "自營商", "法人", "主力", "散戶", "韭菜", "大戶",
    "財報", "營收", "法說", "除權", "除息", "配息", "殖利率", "本益比",
    "融資", "融券", "當沖", "隔日沖", "停損", "停利", "套牢", "解套",
    "抄底", "追高", "殺低", "多單", "空單", "空軍", "多軍", "做多", "做空",
    "開高", "開低", "收紅", "收黑", "熔斷", "大盤", "指數", "行情",
    "賭場", "航運", "半導體", "電子", "金融", "台積", "現貨", "零股",
    "市場", "交易", "持股", "買進", "賣出", "進場", "出場", "獲利", "損益",
    "tw", "TW",
}


def classify_words(word_freq: Counter, extra_related=(),
                    min_freq: int = MIN_WORD_FREQ) -> tuple:
    """把詞頻分成（股票相關, 不相關）兩個 Counter。

    判斷順序：公司名稱等額外清單 → 股市詞彙表 → 含股市關鍵字元。
    出現次數 < min_freq 的字詞視為雜訊，直接濾掉、不會進報告。
    """
    extra = set(extra_related)
    related, unrelated = Counter(), Counter()
    for word, freq in word_freq.items():
        if freq < min_freq:
            continue
        if (word in extra or word in STOCK_TERM_WORDS
                or any(ch in word for ch in STOCK_TERM_CHARS)):
            related[word] = freq
        else:
            unrelated[word] = freq
    return related, unrelated


# 可能的中文字型路徑（由上往下找，找到第一個存在的就用）
# macOS 內建：蘋方、黑體-繁；Windows：微軟正黑體；Linux：Noto CJK
FONT_CANDIDATES = [
    "/System/Library/Fonts/PingFang.ttc",                       # macOS 蘋方
    "/System/Library/Fonts/STHeiti Medium.ttc",                 # macOS 黑體
    "/System/Library/Fonts/Supplemental/Songti.ttc",            # macOS 宋體
    "C:/Windows/Fonts/msjh.ttc",                                # Windows 微軟正黑體
    "C:/Windows/Fonts/msjh.ttf",
    "/usr/share/fonts/opentype/noto/NotoSansCJK-Regular.ttc",   # Linux Noto
]


# ---------------------------------------------------------------------------
# 1. 爬取 PTT 置底文章
# ---------------------------------------------------------------------------
def _mount_retry_adapter(session: requests.Session, total: int = 5) -> None:
    """幫 session 掛上會自動重試的 adapter。

    某些雲端主機（例如 GitHub Actions runner 的出口 IP）連到 PTT 時，可能在
    TLS 握手階段就被直接斷線（ConnectionResetError），而非回應 HTTP 錯誤碼，
    通常是來源 IP 被防爬蟲規則封鎖。加上重試 + 指數退避仍值得一試。
    """
    retry = Retry(
        total=total, connect=total, read=total,
        backoff_factor=2,   # 重試間隔：2s, 4s, 8s, 16s, 32s...
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "HEAD"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)


def make_ptt_session() -> requests.Session:
    """建立帶有「滿 18 歲同意」cookie、且會自動重試連線失敗的 requests Session。

    PTT 部分看板（如 Gossiping、Stock 不一定）會先跳出年齡確認頁，
    只要在 cookie 帶上 over18=1 即可跳過。
    """
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
    """抓取看板首頁的置底文章清單。

    PTT 網頁版的文章列表中，置底文章位於分隔線
    <div class="r-list-sep"></div> 之後，一般文章之前沒有這條線。
    回傳 [{"title": ..., "url": ...}, ...]
    """
    index_url = f"{PTT_BASE}/bbs/{board}/index.html"
    resp = session.get(index_url, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    # 找到置底分隔線，其後所有 .r-ent 區塊即為置底文章
    sep = soup.find("div", class_="r-list-sep")
    if sep is None:
        print(f"[警告] {board} 板目前沒有置底文章（找不到 r-list-sep 分隔線）")
        return []

    pinned = []
    for ent in sep.find_all_next("div", class_="r-ent"):
        title_tag = ent.select_one("div.title a")
        if title_tag is None:
            continue  # 文章被刪除時沒有連結，跳過
        title = title_tag.get_text(strip=True)
        # 跳過公告類置底文（板規、罰則公告等），只分析討論性質的文章
        if any(kw in title for kw in EXCLUDE_TITLE_KEYWORDS):
            print(f"  略過公告：{title}")
            continue
        pinned.append({
            "title": title,
            "url": PTT_BASE + title_tag["href"],
        })
    return pinned


def get_article_content(session: requests.Session, url: str) -> dict:
    """抓取單篇文章的內文與推文。

    回傳 {"content": 內文字串, "pushes": [推文字串, ...]}
    """
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    soup = BeautifulSoup(resp.text, "html.parser")

    main = soup.find("div", id="main-content")
    if main is None:
        return {"content": "", "pushes": []}

    # --- 先取出推文（div.push），再從主內容中移除，剩下的就是內文 ---
    pushes = []
    for push in main.find_all("div", class_="push"):
        content_tag = push.find("span", class_="push-content")
        if content_tag:
            # 推文內容開頭是「: 」，去掉它
            text = content_tag.get_text(strip=True).lstrip(":").strip()
            if text:
                pushes.append(text)
        push.extract()  # 從 DOM 中移除，避免混入內文

    # --- 移除文章開頭的作者/標題/時間 metadata 區塊 ---
    for meta in main.find_all("div", class_=["article-metaline", "article-metaline-right"]):
        meta.extract()

    content = main.get_text("\n", strip=True)
    # 去掉簽名檔之後的內容（PTT 慣例以 "--" 單獨一行作為分隔）
    content = re.split(r"\n--\n", content)[0]
    # 去掉「※ 發信站」等系統訊息行
    content = "\n".join(
        line for line in content.split("\n") if not line.startswith("※")
    )
    return {"content": content, "pushes": pushes}


# ---------------------------------------------------------------------------
# 2. jieba 斷詞 + 詞頻統計
# ---------------------------------------------------------------------------
def tokenize_and_count(texts: list[str]) -> Counter:
    """將多段文字斷詞、過濾後統計詞頻。"""
    counter = Counter()
    for text in texts:
        for word in jieba.cut(text):
            word = word.strip()
            if not word:
                continue                      # 過濾空白
            if len(word) < 2:
                continue                      # 過濾單字（多為虛詞/標點）
            if word in STOPWORDS:
                continue                      # 過濾停用詞
            if re.fullmatch(r"[\W\d_]+", word):
                continue                      # 過濾純標點符號、純數字
            counter[word] += 1
    return counter


# ---------------------------------------------------------------------------
# 3. 繪製文字雲
# ---------------------------------------------------------------------------
def find_chinese_font() -> str:
    """找出可用的中文字型檔路徑：先查候選清單，找不到再用萬用字元掃常見字型目錄。

    後者是為了應付 Linux 發行版之間套件安裝路徑／檔名的細微差異
    （例如 fonts-noto-cjk 在不同 Ubuntu 版本可能拆成多個檔案）。
    """
    import glob
    import os
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
    sys.exit(
        "[錯誤] 找不到可用的中文字型，請在 FONT_CANDIDATES 中加入你電腦上的字型路徑"
    )


def draw_wordcloud(word_freq: Counter, output_path: str) -> None:
    """依詞頻繪製文字雲並存成圖片。"""
    if not word_freq:
        print("[警告] 沒有可用的詞頻資料，跳過文字雲繪製")
        return

    font_path = find_chinese_font()
    print(f"[資訊] 使用中文字型：{font_path}")

    wc = WordCloud(
        font_path=font_path,      # 中文字型（沒設定會變成方框亂碼）
        width=1200,
        height=800,
        background_color="#131722",  # 深色底，配合交易平台風格的網頁報告
        colormap="summer",           # 綠色系文字
        max_words=len(word_freq),    # 不設上限，讓所有字詞都能進文字雲
    ).generate_from_frequencies(word_freq)

    plt.figure(figsize=(12, 8))
    plt.imshow(wc, interpolation="bilinear")
    plt.axis("off")               # 不顯示座標軸
    plt.tight_layout()
    plt.savefig(output_path, dpi=150)
    plt.close()
    print(f"[完成] 文字雲已儲存至 {output_path}")


# ---------------------------------------------------------------------------
# 4. 辨識台灣上市櫃公司 + 查詢股價
# ---------------------------------------------------------------------------
def fetch_tw_stock_list() -> dict:
    """從證交所 ISIN 網頁抓取全部上市＋上櫃公司清單。

    回傳 {公司簡稱: (股票代碼, yfinance 後綴)}，
    上市後綴為 .TW、上櫃為 .TWO。
    若抓取失敗則回退到內建的常見公司小清單。
    """
    stock_map = {}
    # strMode=2 為上市、strMode=4 為上櫃
    sources = [(2, ".TW"), (4, ".TWO")]
    session = requests.Session()
    _mount_retry_adapter(session)
    try:
        for mode, suffix in sources:
            url = f"https://isin.twse.com.tw/isin/C_public.jsp?strMode={mode}"
            resp = session.get(url, timeout=20)
            resp.raise_for_status()  # 錯誤頁（403/503...）要能被下面 except 攔到並改用備援清單
            resp.encoding = "big5"  # 該頁面為 Big5 編碼
            soup = BeautifulSoup(resp.text, "html.parser")
            for row in soup.select("table.h4 tr"):
                cells = row.find_all("td")
                if len(cells) < 6:
                    continue
                # 第一欄格式為「代碼　名稱」（全形空白分隔）
                first = cells[0].get_text(strip=True)
                parts = first.split("　")
                if len(parts) != 2:
                    continue
                code, name = parts[0].strip(), parts[1].strip()
                # 證交所名稱有時帶尾碼「*」（標示面額非十元），但鄉民打字不會打這個
                # 符號，不去掉的話這類股票永遠無法用名稱比對命中（只能靠代碼數字比對）
                name = name.rstrip("*").strip()
                # 只保留 4 位數字的一般股票代碼（排除權證、ETF 以外的特殊商品可自行調整）
                if re.fullmatch(r"\d{4}", code):
                    stock_map[name] = (code, suffix)
        if not stock_map:
            # HTTP 狀態碼正常，但一筆都沒解析到 —— 通常是頁面結構被攔截頁取代，
            # 主動視為失敗走備援清單，而不是讓後面流程在空清單上默默失敗
            raise requests.RequestException("回應中解析不到任何股票資料（可能被導向攔截頁）")
        print(f"[資訊] 已載入 {len(stock_map)} 檔上市櫃股票清單")
    except requests.RequestException as e:
        print(f"[警告] 無法取得證交所股票清單（{e}），改用內建小清單")
        # 內建常見公司備援清單
        fallback = {
            "台積電": ("2330", ".TW"), "鴻海": ("2317", ".TW"),
            "聯發科": ("2454", ".TW"), "長榮": ("2603", ".TW"),
            "陽明": ("2609", ".TW"), "萬海": ("2615", ".TW"),
            "台達電": ("2308", ".TW"), "聯電": ("2303", ".TW"),
            "中鋼": ("2002", ".TW"), "國泰金": ("2882", ".TW"),
            "富邦金": ("2881", ".TW"), "中華電": ("2412", ".TW"),
        }
        stock_map.update(fallback)
    return stock_map


def detect_stocks(texts: list[str], stock_map: dict) -> dict:
    """在文字中比對出現過的公司名稱或 4 位數股票代碼，並統計提及次數。

    回傳 {公司名稱: (代碼, 後綴, 提及次數)}
    """
    full_text = "\n".join(texts)
    mention_counter = Counter()  # key: 股票代碼

    # 建立 代碼 -> (名稱, 後綴) 的反查表，用來比對文中出現的代碼
    code_map = {code: (name, suffix) for name, (code, suffix) in stock_map.items()}

    # (a) 公司名稱出現次數（模糊名稱只靠代碼比對，避免誤判）
    for name, (code, suffix) in stock_map.items():
        if name in AMBIGUOUS_COMPANY_NAMES or len(name) < 2:
            continue
        n = full_text.count(name)
        if n > 0:
            mention_counter[code] += n

    # (b) 4 位數代碼出現次數（前後不能緊鄰其他數字，避免抓到年份等）
    for code in re.findall(r"(?<!\d)(\d{4})(?!\d)", full_text):
        if code in code_map:
            mention_counter[code] += 1

    found = {}
    for code, mentions in mention_counter.items():
        name, suffix = code_map[code]
        found[name] = (code, suffix, mentions)
    return found


def query_stock_prices(found_stocks: dict) -> list[dict]:
    """用 yfinance 查詢各公司近一個月股價，印出並回傳結果清單。

    回傳（依提及次數由高到低排序）：
    [{"name": 公司名, "symbol": 代碼.TW, "price": 收盤價, "change_pct": 漲跌幅%,
      "date": 日期字串, "mentions": 提及次數, "closes": [近一月收盤序列]}, ...]
    """
    results = []
    if not found_stocks:
        print("[資訊] 文章中未偵測到上市櫃公司")
        return results

    import math

    print("\n========== 偵測到的公司與最新收盤價 ==========")
    # 依提及次數由高到低處理
    ordered = sorted(found_stocks.items(), key=lambda x: -x[1][2])
    for name, (code, suffix, mentions) in ordered:
        symbol = f"{code}{suffix}"
        try:
            ticker = yf.Ticker(symbol)
            hist = ticker.history(period="1mo")  # 近一個月，供迷你走勢圖使用
            if hist.empty:
                print(f"  {name} ({symbol})：查無資料")
                continue
            # 過濾 NaN/inf 收盤價，避免報告顯示出字面上的「nan」；
            # 日期與收盤價配對過濾，確保 date_str 對應到真正被採用的那筆收盤價
            paired = [(dt, float(c)) for dt, c in hist["Close"].items()
                      if math.isfinite(float(c))]
            if not paired:
                print(f"  {name} ({symbol})：收盤價皆為無效值，略過")
                continue
            closes = [c for _, c in paired]
            last_close = closes[-1]
            # 若有前一個交易日資料則計算漲跌幅
            change_pct = None
            if len(closes) >= 2:
                change_pct = (last_close - closes[-2]) / closes[-2] * 100
            date_str = paired[-1][0].strftime("%Y-%m-%d")
            print(f"  {name} ({symbol})：收盤價 {last_close:.2f} 元（{date_str}）"
                  f"｜提及 {mentions} 次")
            results.append({
                "name": name, "symbol": symbol, "price": last_close,
                "change_pct": change_pct, "date": date_str,
                "mentions": mentions, "closes": closes,
            })
        except Exception as e:
            print(f"  {name} ({symbol})：查詢失敗（{e}）")
    return results


# ---------------------------------------------------------------------------
# 5. 產生 HTML 網頁報告（文字雲 + 股票清單）
# ---------------------------------------------------------------------------
def _sparkline_svg(closes: list[float], up: bool) -> str:
    """把收盤價序列畫成迷你走勢圖（inline SVG，含面積漸層）。

    up=True 用綠色、False 用紅色（台股慣例紅漲綠跌的「顏色」由呼叫端決定，
    這裡的 up 參數單純指定要哪個顏色）。
    """
    if len(closes) < 2:
        return ""
    w, h, pad = 140, 40, 3
    lo, hi = min(closes), max(closes)
    span = (hi - lo) or 1.0  # 避免除以零（一路平盤）
    pts = []
    for i, c in enumerate(closes):
        x = pad + i * (w - 2 * pad) / (len(closes) - 1)
        y = h - pad - (c - lo) * (h - 2 * pad) / span
        pts.append(f"{x:.1f},{y:.1f}")
    line = " ".join(pts)
    color = "#f6465d" if up else "#2ebd85"  # 台股慣例：紅漲綠跌
    gid = f"g{'u' if up else 'd'}"
    # 面積 = 折線 + 右下、左下兩個角點閉合
    area = f"{line} {w - pad},{h} {pad},{h}"
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<defs><linearGradient id="{gid}" x1="0" y1="0" x2="0" y2="1">'
        f'<stop offset="0%" stop-color="{color}" stop-opacity=".45"/>'
        f'<stop offset="100%" stop-color="{color}" stop-opacity="0"/>'
        f'</linearGradient></defs>'
        f'<polygon points="{area}" fill="url(#{gid})"/>'
        f'<polyline points="{line}" fill="none" stroke="{color}" '
        f'stroke-width="1.6" stroke-linejoin="round"/></svg>'
    )


def generate_html_report(
    board: str,
    articles: list[dict],
    word_freq: Counter,
    stock_results: list[dict],
    wordcloud_path: str,
    output_path: str,
    unrelated_words: Counter = None,
) -> None:
    """把文字雲圖片與偵測到的股票整合成一頁深色交易平台風格的 HTML 報告。

    word_freq 應傳入「股票相關」詞頻（文字雲的內容）；
    unrelated_words 傳入被過濾掉的其他話題詞，會另列一區、不進文字雲。
    文字雲圖片以 base64 內嵌，報告為單一檔案、可直接用瀏覽器開啟或分享。
    """
    import base64
    import os
    from datetime import datetime, timezone, timedelta

    # --- 將文字雲圖片轉成 base64 內嵌，讓 HTML 單檔即可攜帶 ---
    img_tag = "<p>（文字雲圖片產生失敗）</p>"
    if os.path.exists(wordcloud_path):
        with open(wordcloud_path, "rb") as f:
            b64 = base64.b64encode(f.read()).decode("ascii")
        img_tag = (
            f'<img src="data:image/png;base64,{b64}" '
            f'alt="文字雲" style="max-width:100%;border-radius:12px;">'
        )

    # --- 置底文章清單 ---
    article_items = "\n".join(
        f'<li><a href="{a["url"]}" target="_blank">{a["title"]}</a></li>'
        for a in articles
    )

    # --- 高頻詞標籤（股票相關，綠色，全部字詞）---
    top_words = "\n".join(
        f'<span class="tag">{word} <b>{freq}</b></span>'
        for word, freq in word_freq.most_common()
    )

    # --- 不相關詞標籤（灰色，另列一區、不進文字雲，全部字詞）---
    offtopic_card = ""
    if unrelated_words:
        offtopic_tags = "\n".join(
            f'<span class="tag dim">{word} <b>{freq}</b></span>'
            for word, freq in unrelated_words.most_common()
        )
        offtopic_card = f"""
  <div class="card">
    <h2>Off-topic — 其他話題（未列入文字雲）</h2>
    {offtopic_tags}
  </div>"""

    def chg_pill(pct) -> str:
        """漲跌幅膠囊標籤：台股慣例紅漲綠跌，平盤灰色。"""
        if pct is None:
            return '<span class="pill flat">—</span>'
        if abs(pct) < 0.005:
            return '<span class="pill flat">0.00%</span>'
        cls = "up" if pct > 0 else "down"
        arrow = "▲" if pct > 0 else "▼"
        return f'<span class="pill {cls}">{arrow} {pct:+.2f}%</span>'

    # --- 頂部熱門標的卡片（提及次數前 5 名） ---
    top_cards = ""
    for s in stock_results[:5]:
        yahoo_url = f'https://tw.stock.yahoo.com/quote/{s["symbol"]}'
        top_cards += f"""
      <a class="ticker-card" href="{yahoo_url}" target="_blank">
        <div class="tc-name">{s['name']}<span class="tc-sym">{s['symbol']}</span></div>
        <div class="tc-price">{s['price']:,.2f}</div>
        <div class="tc-row">{chg_pill(s['change_pct'])}
          <span class="tc-mention">🔥 {s['mentions']}</span></div>
      </a>"""

    # --- 股票表格列（已依提及次數排序） ---
    if stock_results:
        max_mentions = max(s["mentions"] for s in stock_results)
        rows = []
        for s in stock_results:
            yahoo_url = f'https://tw.stock.yahoo.com/quote/{s["symbol"]}'
            up = s["change_pct"] is not None and s["change_pct"] > 0
            spark = _sparkline_svg(s["closes"], up=up)
            bar_w = int(s["mentions"] / max_mentions * 100)
            rows.append(
                f"<tr>"
                f"<td><a href='{yahoo_url}' target='_blank' class='tk'>{s['name']}"
                f"<span class='sym'>{s['symbol']}</span></a></td>"
                f"<td class='num'>{s['price']:,.2f}</td>"
                f"<td>{chg_pill(s['change_pct'])}</td>"
                f"<td class='spark'>{spark}</td>"
                f"<td><div class='mbar-wrap'><div class='mbar' style='width:{bar_w}%'></div>"
                f"<span class='mnum'>{s['mentions']}</span></div></td>"
                f"<td class='dim'>{s['date']}</td></tr>"
            )
        stock_table = f"""
        <table>
          <thead><tr><th>Ticker</th><th>收盤價</th><th>Daily % Chg</th>
          <th>近一月走勢</th><th>PTT 熱度</th><th>日期</th></tr></thead>
          <tbody>{''.join(rows)}</tbody>
        </table>"""
    else:
        stock_table = "<p>文章中未偵測到上市櫃公司。</p>"

    # 用台灣時間（UTC+8）顯示產生時間；GitHub Actions runner 是 UTC，
    # 直接 datetime.now() 會顯示成 UTC 時間，故明確指定時區換算
    tw_now = datetime.now(timezone(timedelta(hours=8)))
    generated_at = tw_now.strftime("%Y-%m-%d %H:%M") + "（台灣時間）"
    html = f"""<!DOCTYPE html>
<html lang="zh-Hant">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>PTT {board} 熱門標的追蹤</title>
<style>
  /* ====== 深色交易平台風格 ====== */
  * {{ box-sizing: border-box; }}
  body {{
    font-family: "PingFang TC", "Microsoft JhengHei", "Noto Sans TC", sans-serif;
    max-width: 1080px; margin: 0 auto; padding: 24px 16px; line-height: 1.6;
    background: #131722; color: #d1d4dc;
  }}
  h1 {{ font-size: 1.35rem; letter-spacing: .12em; color: #eaecef; margin: 0; }}
  h2 {{
    font-size: .8rem; margin: 0 0 14px; color: #848e9c;
    text-transform: uppercase; letter-spacing: .22em;
  }}
  .topbar {{
    display: flex; align-items: baseline; justify-content: space-between;
    flex-wrap: wrap; gap: 8px; padding-bottom: 14px;
    border-bottom: 1px solid #2a2e39; margin-bottom: 18px;
  }}
  .meta {{ color: #5e6673; font-size: .8rem; }}
  .card {{
    background: #1c2230; border: 1px solid #2a2e39; border-radius: 10px;
    padding: 18px 22px; margin: 16px 0;
  }}
  /* --- 頂部熱門標的卡片 --- */
  .cards-row {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(180px, 1fr));
    gap: 12px; margin: 16px 0;
  }}
  .ticker-card {{
    background: #1c2230; border: 1px solid #2a2e39; border-radius: 10px;
    padding: 14px 16px; text-decoration: none; color: inherit;
    transition: border-color .15s;
  }}
  .ticker-card:hover {{ border-color: #4a5568; }}
  .tc-name {{ font-size: .95rem; color: #eaecef; font-weight: 600; }}
  .tc-sym {{ font-size: .7rem; color: #5e6673; margin-left: 6px; font-weight: 400; }}
  .tc-price {{
    font-size: 1.3rem; font-weight: 700; color: #eaecef; margin: 4px 0;
    font-variant-numeric: tabular-nums;
  }}
  .tc-row {{ display: flex; justify-content: space-between; align-items: center; }}
  .tc-mention {{ font-size: .8rem; color: #f0b90b; }}
  /* --- 漲跌幅膠囊（台股慣例：紅漲綠跌） --- */
  .pill {{
    display: inline-block; border-radius: 5px; padding: 1px 8px;
    font-size: .8rem; font-weight: 600; font-variant-numeric: tabular-nums;
  }}
  .pill.up {{ background: rgba(246,70,93,.15); color: #f6465d; }}
  .pill.down {{ background: rgba(46,189,133,.15); color: #2ebd85; }}
  .pill.flat {{ background: rgba(132,142,156,.15); color: #848e9c; }}
  /* --- 高頻詞標籤 --- */
  .tag {{
    display: inline-block; background: rgba(46,189,133,.1);
    border: 1px solid rgba(46,189,133,.25); border-radius: 999px;
    padding: 2px 12px; margin: 3px; font-size: .85rem; color: #d1d4dc;
  }}
  .tag b {{ color: #2ebd85; }}
  .tag.dim {{
    background: rgba(132,142,156,.08); border-color: rgba(132,142,156,.25);
    color: #848e9c;
  }}
  .tag.dim b {{ color: #848e9c; }}
  /* --- 表格 --- */
  .tablewrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .9rem; }}
  th, td {{ padding: 9px 12px; text-align: left; white-space: nowrap; }}
  th {{
    color: #848e9c; font-size: .72rem; text-transform: uppercase;
    letter-spacing: .08em; border-bottom: 1px solid #2a2e39; font-weight: 600;
  }}
  tr {{ border-bottom: 1px solid #21273a; }}
  tbody tr:hover {{ background: #212838; }}
  td.num {{ font-variant-numeric: tabular-nums; color: #eaecef; font-weight: 600; }}
  td.spark svg {{ display: block; }}
  td.dim {{ color: #5e6673; font-size: .8rem; }}
  .tk {{ color: #eaecef; text-decoration: none; font-weight: 600; }}
  .tk:hover {{ color: #f0b90b; }}
  .sym {{ display: block; font-size: .7rem; color: #5e6673; font-weight: 400; }}
  /* --- PTT 熱度長條 --- */
  .mbar-wrap {{ display: flex; align-items: center; gap: 8px; min-width: 120px; }}
  .mbar {{
    height: 6px; border-radius: 3px; min-width: 3px;
    background: linear-gradient(90deg, #f0b90b, #f6465d);
  }}
  .mnum {{ font-size: .8rem; color: #f0b90b; font-variant-numeric: tabular-nums; }}
  ul {{ margin: 0; padding-left: 20px; }}
  a {{ color: #6b9fff; }}
  img {{ max-width: 100%; border-radius: 8px; display: block; }}
</style>
</head>
<body>
  <div class="topbar">
    <h1>📈 PTT {board.upper()} 熱門標的追蹤</h1>
    <span class="meta">產生時間 {generated_at}｜{len(articles)} 個資料來源</span>
  </div>

  <div class="cards-row">{top_cards}
  </div>

  <div class="card">
    <h2>Hot List — 鄉民提及標的</h2>
    <div class="tablewrap">{stock_table}</div>
    <p class="meta">資料：Yahoo Finance（yfinance）收盤價，非即時報價｜熱度 = 內文與推文中的提及次數</p>
  </div>

  <div class="card">
    <h2>Word Cloud — 股票相關話題</h2>
    {img_tag}
  </div>

  <div class="card">
    <h2>Trending — 股票相關高頻詞</h2>
    {top_words}
  </div>
{offtopic_card}

  <div class="card">
    <h2>Sources — 資料來源</h2>
    <ul>{article_items}</ul>
  </div>
</body>
</html>"""

    with open(output_path, "w", encoding="utf-8") as f:
        f.write(html)
    print(f"[完成] HTML 報告已儲存至 {output_path}")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def main():
    session = make_ptt_session()

    # --- 步驟 1：抓置底文章 ---
    print(f"[資訊] 開始爬取 PTT {BOARD} 板置底文章...")
    pinned = get_pinned_articles(session, BOARD)
    if not pinned:
        sys.exit("[結束] 沒有置底文章可分析")

    all_texts = []  # 收集所有內文與推文，供斷詞與公司偵測使用
    for art in pinned:
        print(f"  抓取：{art['title']}")
        data = get_article_content(session, art["url"])
        all_texts.append(data["content"])
        all_texts.extend(data["pushes"])
    print(f"[資訊] 共抓取 {len(pinned)} 篇置底文章")

    # --- 步驟 2：斷詞與詞頻統計 ---
    print("[資訊] 進行 jieba 斷詞與詞頻統計...")
    word_freq = tokenize_and_count(all_texts)

    # --- 步驟 3：載入股票清單、把詞分成「股票相關 / 不相關」---
    print("[資訊] 載入台股上市櫃公司清單...")
    stock_map = fetch_tw_stock_list()
    related, unrelated = classify_words(word_freq, extra_related=stock_map.keys())
    print(f"[資訊] 詞彙 {len(word_freq)} 個 → 股票相關 {len(related)}、"
          f"不相關 {len(unrelated)}")
    print("    相關前 5：", "、".join(f"{w}({c})" for w, c in related.most_common(5)))
    print("    不相關前 5：", "、".join(f"{w}({c})" for w, c in unrelated.most_common(5)))

    # --- 步驟 4：文字雲（只用股票相關詞）+ 偵測公司查股價 ---
    draw_wordcloud(related, WORDCLOUD_OUTPUT)
    found = detect_stocks(all_texts, stock_map)
    stock_results = query_stock_prices(found)

    # --- 步驟 5：產生 HTML 網頁報告 ---
    generate_html_report(
        board=BOARD,
        articles=pinned,
        word_freq=related,
        stock_results=stock_results,
        wordcloud_path=WORDCLOUD_OUTPUT,
        output_path=REPORT_OUTPUT,
        unrelated_words=unrelated,
    )


if __name__ == "__main__":
    main()
