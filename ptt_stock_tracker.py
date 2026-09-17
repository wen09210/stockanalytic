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
import jieba.posseg as pseg
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
MIN_MENTIONS = 1                           # 提及次數低於此值的股票不寫入試算表
MIN_WORD_FREQ_TO_SHEET = 5                 # 出現次數低於此值的字詞視為雜訊，不寫入試算表

# --- Google Sheets 設定 ---
CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials.json")  # Service Account 憑證檔
SPREADSHEET_NAME = "PTT股市熱門標的追蹤"    # 你的 Google 試算表「名稱」
# 每天的資料寫入「以日期命名的分頁」（例如 2026-07-13），永遠用同一份試算表；
# 同一天重跑會清空該分頁重寫（冪等），不會產生重複資料或新檔案

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
    # 時間相關詞（今天/明天/昨天…等，對個股分析沒有資訊價值的雜訊）
    "今天", "明天", "昨天", "後天", "前天", "今日", "明日", "昨日",
    "今年", "明年", "去年", "早上", "下午", "晚上", "中午",
    "剛剛", "剛才", "目前", "最近", "之前", "之後", "以前", "以後",
    "等等", "時候",
    # PTT 推文與圖片連結常見雜訊
    "XD", "xd", "推", "噓", "http", "https", "www", "com", "cc", "imgur",
    "jpg", "jpeg", "png", "gif", "mopix",
    # 網域碎片保險（正常情況已由 strip_urls 在斷詞前移除整串網址，
    # 這裡防的是沒有 http/www 開頭的裸網域，例如「tw.stock.yahoo.com」）
    "tw", "TW", "yahoo", "quote", "stock", "html", "php", "net", "org",
}

# 公司簡稱剛好是常見中文詞的排除清單（避免誤判，可自行增減）
# 這些股票只有在文中出現「4 位數代碼」時才會被計入。
# 實例：2026-08-23 的報告把「台南」排到熱門第 5 名（7 次），但同一天
# 「台北」「高雄」各出現 6 次列在其他話題區——鄉民講的是城市，不是台南紡織；
# 「幸福」同理是一般用詞而非幸福水泥。這類詞只認代碼才不會灌水。
AMBIGUOUS_COMPANY_NAMES = {
    "數字", "世界", "大量", "全新", "中華", "三星", "無敵", "冠軍",
    "安心", "精華", "大將", "聯發", "全台", "萬在", "大樹", "統一",
    "華電", "美亞", "正文", "力士", "熱映",
    # 縣市／地名（鄉民聊地點的頻率遠高於聊同名的那幾檔股票）
    "台南", "台中", "台北", "高雄", "新竹", "桃園", "基隆", "嘉義",
    "彰化", "南投", "宜蘭", "花蓮", "台東", "屏東", "雲林", "苗栗",
    "台灣", "中國", "美國", "日本", "上海", "南港", "士林", "萬華",
    # 一般用詞（刻意只收「幾乎不會是在講那檔股票」的詞：像大同、東元、
    # 農林這種雖然也是常用字但確實常被討論的個股就不列入，以免反而漏抓）
    "幸福", "全國", "宏觀", "大華", "和益", "新代", "亞洲", "國際",
    "現代", "光明", "自強", "大方", "第一", "全家",
}

# ---- 美股精選清單 ----
# PTT 股板真正常被討論的美股。刻意「不」抓全美股六千檔代碼清單：ON/IT/ALL/
# SO/NOW/AI 這類短代碼會跟推文裡的英文縮寫大量誤撞，熱門度會嚴重灌水。
# 格式：代碼 -> (顯示名稱, [中文暱稱與別名, ...])
# 比對以中文暱稱為主（鄉民多半打中文），代碼為輔（需全大寫且獨立成詞）。
US_STOCKS = {
    "NVDA": ("輝達", ["輝達", "輝達股", "黃仁勳"]),
    "TSLA": ("特斯拉", ["特斯拉", "特斯拉股"]),
    "AAPL": ("蘋果", ["蘋果", "蘋果公司"]),
    "MSFT": ("微軟", ["微軟"]),
    "GOOGL": ("谷歌", ["谷歌", "google", "Google", "字母"]),
    "AMZN": ("亞馬遜", ["亞馬遜"]),
    "META": ("Meta", ["臉書", "祖克柏"]),
    "AMD": ("超微", ["超微", "蘇姿丰"]),
    "INTC": ("英特爾", ["英特爾"]),
    "MU": ("美光", ["美光"]),
    "AVGO": ("博通", ["博通"]),
    "QCOM": ("高通", ["高通"]),
    "TSM": ("台積電ADR", ["台積電ADR", "台積ADR"]),
    "ASML": ("艾司摩爾", ["艾司摩爾", "艾斯摩爾"]),
    "ARM": ("安謀", ["安謀"]),
    "MRVL": ("邁威爾", ["邁威爾"]),
    "SMCI": ("美超微", ["美超微"]),
    "DELL": ("戴爾", ["戴爾"]),
    "ORCL": ("甲骨文", ["甲骨文"]),
    "CRM": ("賽富時", ["賽富時"]),
    "ADBE": ("奧多比", ["奧多比"]),
    "NFLX": ("網飛", ["網飛"]),
    "DIS": ("迪士尼", ["迪士尼"]),
    "KO": ("可口可樂", ["可口可樂"]),
    "PLTR": ("Palantir", ["帕蘭泰爾"]),
    "COIN": ("Coinbase", ["幣安基地"]),
    "MSTR": ("microstrategy", ["微策略"]),
    "BRK-B": ("波克夏", ["波克夏", "巴菲特"]),
    "JPM": ("摩根大通", ["摩根大通"]),
    "GS": ("高盛", ["高盛"]),
    "BA": ("波音", ["波音"]),
    "F": ("福特", ["福特"]),
    "GM": ("通用汽車", ["通用汽車"]),
    "PFE": ("輝瑞", ["輝瑞"]),
    "LLY": ("禮來", ["禮來"]),
    "NVO": ("諾和諾德", ["諾和諾德"]),
    "UBER": ("優步", ["優步"]),
    "ABNB": ("Airbnb", ["愛彼迎"]),
    "SPY": ("標普500ETF", ["標普500", "SPY"]),
    "QQQ": ("那斯達克100ETF", ["QQQ"]),
    "VOO": ("VOO", ["VOO"]),
    "SOXL": ("半導體3倍做多", ["SOXL"]),
    "TQQQ": ("那斯達克3倍做多", ["TQQQ"]),
}

# 美股代碼若剛好是這些字串就不比對代碼（避免與英文縮寫/一般用字誤撞）
# 只留「代碼本身夠獨特」的才用代碼比對；其餘僅靠中文暱稱辨識
US_AMBIGUOUS_TICKERS = {"F", "GM", "GS", "BA", "KO", "MU", "DIS", "ARM", "META"}

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
def register_stock_words(stock_map: dict) -> None:
    """把上市櫃公司名稱與股市術語掛進 jieba 自訂詞典。

    jieba 內建詞庫以簡體為主，對台股名稱常會拆錯（例如「台積電」被切成
    「台積 / 電」）。把公司名以專有名詞（nz）掛進詞典後，斷詞會把整個名稱
    當成一個詞，POS 過濾時也不會被誤判成時間詞而刪掉。

    股市術語（存股、當沖、除權息、護國神山…）沿用 ptt_stock_wordcloud 的
    STOCK_TERM_WORDS，避免兩邊各自維護一份而不同步。
    """
    for name in stock_map:
        if len(name) >= 2:
            jieba.add_word(name, tag="nz")
    # 美股中文暱稱（輝達、超微、美光…）也掛進詞典，否則會被拆開而比對不到
    for _name, aliases in US_STOCKS.values():
        for alias in aliases:
            if len(alias) >= 2:
                jieba.add_word(alias, tag="nz")
    try:
        from ptt_stock_wordcloud import STOCK_TERM_WORDS
    except ImportError:      # 單獨執行且找不到該模組時，僅掛公司名即可
        return
    for term in STOCK_TERM_WORDS:
        if len(term) >= 2:
            jieba.add_word(term, tag="n")


def strip_urls(text: str) -> str:
    """把網址整串移除，避免斷詞後留下 tw / yahoo / quote 之類的網域碎片。

    鄉民常在推文貼股票連結（https://tw.stock.yahoo.com/quote/2330.TW），
    整串丟進 jieba 會被切成一堆無意義片段混進文字雲，逐一加停用詞補不完，
    直接在斷詞前移除最乾淨。
    """
    return re.sub(r"https?://\S+|www\.\S+", " ", text)


def tokenize_and_count(texts: list[str]) -> Counter:
    """斷詞、過濾後統計詞頻。

    斷詞前先移除網址（見 strip_urls），避免網域碎片混進詞頻。
    改用 jieba.posseg 取得詞性，時間詞（詞性 t，如今天／明天／昨天）直接
    依詞性濾掉，不必逐一列進停用詞清單。STOPWORDS 仍保留作為保險
    （jieba 對繁體的詞性判斷偶有誤差）。
    """
    counter = Counter()
    for text in texts:
        for token in pseg.cut(strip_urls(text)):
            word = token.word.strip()
            if not word or len(word) < 2:
                continue                      # 過濾空白與單字詞
            if token.flag == "t":
                continue                      # 依詞性過濾時間詞（今天/明天/昨天…）
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
        background_color="white", max_words=len(word_freq), colormap="tab10",
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
                # 證交所名稱有時帶尾碼「*」（標示面額非十元），但鄉民打字不會打這個
                # 符號，不去掉的話這類股票永遠無法用名稱比對命中（只能靠代碼數字比對）
                name = name.rstrip("*").strip()
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


# 4 位數若「後面」緊跟這些單位，多半是年份/價格/數量，不是股票代碼
_NON_CODE_SUFFIX = ("年", "元", "點", "塊", "％", "%", "萬", "億")
# 「股」「張」當數量單位時同理，但後面若還接著字（2330股票／2330張力）就不算單位，
# 需要單獨處理：只有「數字+股/張」剛好結尾或接非中文字時才視為數量
_NON_CODE_UNIT = ("股", "張")
# 4 位數若「前面」緊跟這些字，多半是價格或民國年，不是股票代碼
_NON_CODE_PREFIX = ("民國", "西元", "收在", "漲到", "跌到", "破", "$", "＄")


def _is_cjk(ch: str) -> bool:
    """是否為中日韓統一表意文字（用來判斷量詞後面還有沒有接字）。"""
    return bool(ch) and "一" <= ch <= "鿿"


def _looks_like_non_code(text: str, match: re.Match) -> bool:
    """判斷這個 4 位數字比較像年份/價格而不是股票代碼。

    例如「2025 年」「收在 1500 點」裡的數字剛好等於某檔股票代碼時，
    不該被算成一次提及。
    """
    num = int(match.group(1))
    if 1900 <= num <= 2100:      # 明顯的西元年份區間
        return True
    after = text[match.end():match.end() + 2]
    if after.startswith(_NON_CODE_SUFFIX):
        return True
    # 「股」「張」只有單獨當量詞時才算（2330股 → 數量；2330股票 → 是代碼）
    if after[:1] in _NON_CODE_UNIT and not _is_cjk(after[1:2]):
        return True
    before = text[max(0, match.start() - 2):match.start()]
    if before.endswith(_NON_CODE_PREFIX):
        return True
    return False


def count_stock_mentions(texts: list[str], stock_map: dict) -> list[dict]:
    """統計每檔股票在文本中被提及的次數（名稱出現次數 + 代碼出現次數）。

    名稱比對以「詞元」為單位（斷詞後整個詞剛好等於公司名才算），而不是子字串
    比對，避免「中華」被「中華電信／中華民國／中華隊」灌水這類誤判。
    （公司名已由 register_stock_words() 掛進 jieba 詞典，會被切成完整一個詞。）

    回傳依提及次數由高到低排序的
    [{"code": 代碼, "name": 名稱, "market": 市場別, "mentions": 次數}, ...]
    """
    full_text = "\n".join(texts)
    code_map = {code: (name, market) for name, (code, market) in stock_map.items()}

    mention_counter = Counter()  # key: 股票代碼

    # (a) 公司名稱出現次數：以斷詞後的詞元比對，整個詞相等才算一次
    token_freq = Counter(
        tok for text in texts for tok in jieba.cut(text) if len(tok.strip()) >= 2
    )
    for name, (code, _market) in stock_map.items():
        if name in AMBIGUOUS_COMPANY_NAMES or len(name) < 2:
            continue
        n = token_freq.get(name, 0)
        if n > 0:
            mention_counter[code] += n

    # (b) 4 位數代碼出現次數（前後不能緊鄰數字，避免抓到年份等）
    for m in re.finditer(r"(?<!\d)(\d{4})(?!\d)", full_text):
        code = m.group(1)
        if code not in code_map:
            continue
        if _looks_like_non_code(full_text, m):
            continue
        mention_counter[code] += 1

    results = []
    for code, mentions in mention_counter.most_common():
        if mentions < MIN_MENTIONS:
            continue
        name, market = code_map[code]
        results.append({
            "code": code, "name": name, "market": market, "mentions": mentions,
        })
    results.extend(count_us_mentions(texts))
    results.sort(key=lambda s: -s["mentions"])
    return results


def count_us_mentions(texts: list[str]) -> list[dict]:
    """統計美股精選清單被提及的次數，回傳與台股相同結構的清單。

    中文暱稱以詞元比對（同台股做法，避免子字串灌水）；英文代碼則要求
    「全大寫、且前後不是英數字」才算，並排除 F/GM/KO 這類容易與英文縮寫
    誤撞的短代碼（見 US_AMBIGUOUS_TICKERS，那些只靠中文暱稱辨識）。
    """
    full_text = strip_urls("\n".join(texts))   # 網址裡常有 AAPL 之類的字串，先清掉
    token_freq = Counter(
        tok for text in texts
        for tok in jieba.cut(strip_urls(text)) if len(tok.strip()) >= 2
    )

    results = []
    for ticker, (name, aliases) in US_STOCKS.items():
        mentions = sum(token_freq.get(alias, 0) for alias in aliases)
        if ticker not in US_AMBIGUOUS_TICKERS:
            # 代碼需獨立出現（前後非英數字），避免抓到 NVDAxx 這種黏在一起的字串
            mentions += len(re.findall(
                rf"(?<![A-Za-z0-9]){re.escape(ticker)}(?![A-Za-z0-9])", full_text
            ))
        if mentions >= MIN_MENTIONS:
            results.append({
                "code": ticker, "name": name, "market": "美股",
                "mentions": mentions,
            })
    return results


# ---------------------------------------------------------------------------
# 4. 寫入 Google Sheets
# ---------------------------------------------------------------------------
def build_price_formula(code: str, check_date: date) -> str:
    """產生 GoogleFinance 股價公式字串（僅在 USE_GOOGLEFINANCE=True 時使用）。

    注意：GOOGLEFINANCE 的 "TPE:" 前綴只涵蓋台灣「上市」股票，
    上櫃（TPEx）標的多半抓不到、會顯示 #N/A。預設改用 yfinance 靜態收盤價
    （見 fetch_close_price），涵蓋上市＋上櫃＋美股。

    美股代碼不加 "TPE:" 前綴，GOOGLEFINANCE 直接吃代碼即可。
    """
    symbol = f"TPE:{code}" if code.isdigit() else code
    if USE_HISTORICAL_CLOSE:
        d = check_date
        return (
            f'=INDEX(GOOGLEFINANCE("{symbol}","close",'
            f'DATE({d.year},{d.month},{d.day})),2,2)'
        )
    return f'=GOOGLEFINANCE("{symbol}")'


def fetch_close_price(code: str, check_date: date):
    """用 yfinance 取「檢查日（含）以前最近交易日」的收盤價。

    台股試 .TW（上市）與 .TWO（上櫃）；美股代碼不加後綴，yfinance 直接支援。
    注意美股收盤在台灣時間隔天凌晨，本程式在台灣時間 00:00 執行時美股尚在
    盤中，因此美股取到的會是「前一個交易日」的收盤價（報告的日期欄會據實
    標示該筆價格對應的交易日）。

    回傳浮點數；查不到（或資料是 NaN，例如剛上市、停牌）回傳 None。
    寫成固定數值，數字不會隨時間漂移。
    """
    import math
    import yfinance as yf
    target = check_date.isoformat()
    # 美股代碼含英文字母（NVDA、BRK-B），台股是純 4 位數字
    suffixes = ("",) if not code.isdigit() else (".TW", ".TWO")
    for suffix in suffixes:
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
                           day: str, news_rows: list[list] = None) -> None:
    """把當天的資料寫入「同一份試算表」中以日期命名的分頁（例如 2026-07-13）。

    分頁版面：熱門標的表格 → 高頻詞表格 → 重大訊息表格，各隔兩列。
    - 永遠寫同一份試算表（SPREADSHEET_NAME），不會建立新檔案
    - 該日期分頁已存在時會先清空再重寫（同一天重跑不會累積重複資料）
    若找不到憑證檔則改用「乾跑模式」，只印出資料不實際寫入。
    """
    # 組出該分頁的完整儲存格內容（三個區塊）
    values = [["檢查日期", "股票代碼", "公司名稱", "PTT提及次數", "股價"]]
    values += stock_rows
    values += [[], []]
    values += [["檢查日期", "排名", "詞彙", "出現次數"]]
    values += word_rows
    if news_rows:
        values += [[], []]
        # 表頭第二欄刻意不叫「股票代碼」：report_from_sheet 的 parse_sheet
        # 是靠表頭字串切換解析模式，重複用同一個詞會讓兩個區塊混在一起
        values += [["檢查日期", "重大訊息", "公司名稱", "發言時間", "主旨"]]
        values += news_rows

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

    # 分頁列數需能容納完整詞頻清單（不再只留前 20 名），抓實際列數加緩衝
    needed_rows = len(values) + 10

    # 取得（或建立）以日期命名的分頁；已存在就先清空，確保同日重跑冪等
    try:
        ws = spreadsheet.worksheet(day)
        ws.clear()
        if ws.row_count < needed_rows:
            ws.resize(rows=needed_rows)
    except gspread.WorksheetNotFound:
        ws = spreadsheet.add_worksheet(title=day, rows=needed_rows, cols=10)

    # USER_ENTERED 讓 =GOOGLEFINANCE(...) 被當成公式而非文字
    ws.update(values=values, range_name="A1", value_input_option="USER_ENTERED")
    print(f"[完成] 已寫入「{SPREADSHEET_NAME}」的分頁「{day}」"
          f"（{len(stock_rows)} 檔股票、{len(word_rows)} 個詞）")


# ---------------------------------------------------------------------------
# 主流程
# ---------------------------------------------------------------------------
def _record_source(day: str, article: dict, push_count: int,
                   sentiment: dict = None) -> None:
    """把當天分析的 PTT 文章連結與市場情緒寫入 sources.json。

    情緒是從當天的推文原文算出來的，只有爬蟲這一端拿得到；報告是由
    report_from_sheet.py 讀試算表重建的，所以要在這裡順便存下來傳過去。
    """
    import json
    path = os.path.join(BASE_DIR, "sources.json")
    data = {}
    if os.path.exists(path):
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
    data[day] = {"title": article["title"], "url": article["url"],
                 "pushes": push_count}
    if sentiment:
        data[day]["sentiment"] = sentiment
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

    # 市場情緒（樂觀／悲觀比例）：只有這裡拿得到推文原文，算好一起存進
    # sources.json，之後 report_from_sheet.py 重建報告時才有得用
    from ptt_stock_wordcloud import analyze_sentiment
    sentiment = analyze_sentiment(all_texts)
    print(f"[資訊] 市場情緒：樂觀 {sentiment['bullish_pct']}%"
          f"（{sentiment['bullish']} 則）、悲觀 {sentiment['bearish_pct']}%"
          f"（{sentiment['bearish']} 則），可判讀 {sentiment['scored']}"
          f"/{sentiment['total']} 則")

    # 記錄今天的資料來源（PTT 文章連結）到 sources.json，供報告的參考資料使用
    _record_source(today.isoformat(), pinned[0], push_total, sentiment)

    # --- 步驟 2：先載入台股清單（順便掛成 jieba 自訂詞典，避免公司名被拆開）---
    print("[資訊] 載入台股上市櫃清單...")
    stock_map = fetch_tw_stock_list()
    register_stock_words(stock_map)

    # --- 步驟 3：斷詞統計 + 文字雲 ---
    print("[資訊] jieba 斷詞與詞頻統計...")
    word_freq = tokenize_and_count(all_texts)
    print(f"[資訊] 有效詞彙 {len(word_freq)} 個，前 5 高頻：", end=" ")
    print("、".join(f"{w}({c})" for w, c in word_freq.most_common(5)))
    draw_wordcloud(word_freq, WORDCLOUD_OUTPUT)

    # --- 步驟 4：股票辨識與熱門度（重用 stock_map）---
    print("[資訊] 統計各標的提及次數...")
    hot_stocks = count_stock_mentions(all_texts, stock_map)
    if not hot_stocks:
        sys.exit("[結束] 文中未偵測到任何上市櫃股票")

    print(f"\n========== 今日熱門標的（共 {len(hot_stocks)} 檔） ==========")
    for s in hot_stocks:
        print(f"  {s['name']}({s['code']}, {s['market']})：提及 {s['mentions']} 次")

    # --- 步驟 5：組資料列並寫入 Google Sheets ---
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
            # 股票代碼：前面加單引號強制存成文字，避免 Google Sheets 用
            # value_input_option=USER_ENTERED 寫入時把 "0050" 這類前導零代碼
            # 自動辨識成數字 50、吃掉前導零，導致之後查價對不到代碼
            f"'{s['code']}",
            s["name"],           # 公司名稱
            s["mentions"],       # PTT 提及次數（熱門度）
            price,               # 收盤價（數值）或 GoogleFinance 公式
        ])
    word_rows = [
        [today.isoformat(), rank, word, freq]
        for rank, (word, freq) in enumerate(word_freq.most_common(), start=1)
        if freq >= MIN_WORD_FREQ_TO_SHEET
    ]
    # --- 步驟 6：公開資訊觀測站的當日重大訊息（只查熱門標的）---
    # 整段包在 try 裡：MOPS 對雲端 IP 不友善，就算完全抓不到也只是這一區沒
    # 資料，絕不能讓既有的每日報告跟著失敗
    news_rows = []
    try:
        from mops_tracker import fetch_material_news
        print("[資訊] 查詢公開資訊觀測站當日重大訊息...")
        for n in fetch_material_news(hot_stocks, today.isoformat()):
            news_rows.append([today.isoformat(), f"'{n['code']}", n["name"],
                              n["time"], n["subject"]])
    except Exception as e:
        print(f"[警告] 重大訊息查詢整段失敗（{type(e).__name__}: {e}），"
              "略過此區塊，不影響每日報告")

    write_to_google_sheets(stock_rows, word_rows, day=today.isoformat(),
                           news_rows=news_rows)


if __name__ == "__main__":
    main()
