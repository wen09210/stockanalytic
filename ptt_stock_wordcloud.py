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
import jieba.posseg as pseg
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
    # 網域碎片保險（正常情況已由 strip_urls 在斷詞前移除整串網址，
    # 這裡防的是沒有 http/www 開頭的裸網域，例如「tw.stock.yahoo.com」）
    "tw", "TW", "yahoo", "quote", "stock", "html", "php", "net", "org",
}

# 公司簡稱剛好是常見中文詞的排除清單（避免誤判，可自行增減）
# 例如「數字」(5287)、「世界」(5347) 這類名稱幾乎每篇文章都會出現
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

# ---- 股票相關詞彙判斷 ----
# 只要詞中含有這些「字」就視為股市相關（股、盤、漲、跌、噴＝股板行情用語）
# 註：這是輔助規則，容易誤判的詞另列 NON_STOCK_WORDS 排除
STOCK_TERM_CHARS = "股盤漲跌噴崩"

# 含 STOCK_TERM_CHARS 但其實與股市無關的常見詞（優先於字元規則）
# 例如「漲價」講的是物價、「崩潰」「跌倒」是情緒或動作
NON_STOCK_WORDS = {
    "漲價", "漲租", "調漲", "崩潰", "崩壞", "山崩",
    "跌倒", "摔跌", "跌落", "盤子", "盤古", "地盤", "盤點",
    "盤問", "盤據", "全盤", "通盤", "算盤", "棋盤", "盤腿",
    "股溝", "屁股", "噴飯", "噴漆", "噴水",
}
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
    # 註：原本這裡有 "tw"/"TW"，但它們其實是股票連結網址被斷詞後的碎片，
    # 不是股市術語，會讓雜訊被誤判成相關詞，已移除（網址改在斷詞前移除）
    # 股板常見術語與鄉民用語（同時掛進 jieba 詞典，見 register_stock_words）
    "存股", "當沖客", "隔日沖", "沖銷", "違約交割", "融資追繳", "斷頭",
    "除權息", "填息", "貼息", "現增", "減資", "庫藏股", "可轉債",
    "護國神山", "航海王", "鋼鐵人", "少年股神", "股海", "韭菜田",
    "多方", "空方", "軋空", "回補", "攤平", "加碼", "減碼", "停利點",
    "支撐", "壓力", "均線", "季線", "年線", "月線", "爆量", "量縮",
    "跳空", "漲停", "跌停", "當日沖銷", "現股當沖", "當沖降稅",
    "本淨比", "毛利率", "淨利", "eps", "EPS", "殖利", "配股",
    "台股", "美股", "陸股", "港股", "日股", "費半", "那斯達克", "道瓊",
    "標普", "台幣", "美元", "匯率", "升息", "降息", "聯準會", "央行",
}

# ---- 市場情緒（樂觀／悲觀）詞庫 ----
# 用詞典規則而非機器學習模型：股板用語有很強的固定句式，規則法就有不錯的
# 效果，且不必為了跑 CI 而安裝深度學習套件、下載大模型。
BULLISH_WORDS = {
    "看多", "做多", "多方", "多單", "多軍", "買進", "買爆", "進場", "加碼",
    "抄底", "撿便宜", "上漲", "漲停", "大漲", "飆漲", "噴出", "噴發", "起飛",
    "突破", "新高", "反彈", "落底", "止跌", "轉強", "強勢", "領漲", "紅通通",
    "樂觀", "看好", "有機會", "值得", "獲利", "賺錢", "賺翻", "大賺", "翻倍",
    "軋空", "填息", "利多", "回溫", "復甦", "成長", "續強", "抱緊", "存股",
}
BEARISH_WORDS = {
    "看空", "做空", "空方", "空單", "空軍", "賣出", "賣光", "出場", "減碼",
    "停損", "認賠", "下跌", "跌停", "大跌", "崩盤", "崩跌", "暴跌", "重挫",
    "破底", "新低", "轉弱", "弱勢", "領跌", "綠油油", "套牢", "住套房",
    "悲觀", "看壞", "看衰", "危險", "小心", "賠錢", "賠慘", "虧損", "慘賠",
    "逃命", "斷頭", "融資追繳", "違約交割", "利空", "衰退", "泡沫", "恐慌",
    "血流成河", "韭菜", "接刀", "躺平", "認輸", "GG", "崩",
}
# 否定詞：出現在情緒詞前面時要翻轉極性（例如「不看好」其實是悲觀）
_NEGATION_PREFIX = ("不", "沒", "別", "未", "難", "無", "沒有", "不會", "不要")


def _is_negated(text: str, pos: int) -> bool:
    """判斷 text[pos] 開頭的情緒詞前面是否有否定詞。"""
    before = text[max(0, pos - 3):pos]
    return before.endswith(_NEGATION_PREFIX)


def _polarity_of(text: str) -> int:
    """回傳單段文字的情緒傾向：1 樂觀、-1 悲觀、0 中性/看不出來。"""
    bull = bear = 0
    for words, is_bull in ((BULLISH_WORDS, True), (BEARISH_WORDS, False)):
        for word in words:
            for m in re.finditer(re.escape(word), text):
                # 被否定就翻轉極性（不看好 → 悲觀；不看壞 → 樂觀）
                positive = is_bull != _is_negated(text, m.start())
                if positive:
                    bull += 1
                else:
                    bear += 1
    if bull > bear:
        return 1
    if bear > bull:
        return -1
    return 0


def analyze_sentiment(texts: list[str]) -> dict:
    """統計整體市場情緒，回傳樂觀／悲觀的則數與百分比。

    以「每則推文（或內文）」為單位判斷極性，再統計比例。百分比的分母是
    「有明確情緒的則數」（scored），不含中性則——這樣「今天樂觀 60%」指的是
    有表態的人裡有六成偏多，而不會被大量閒聊稀釋掉。
    """
    bullish = bearish = neutral = 0
    for text in texts:
        p = _polarity_of(strip_urls(text))
        if p > 0:
            bullish += 1
        elif p < 0:
            bearish += 1
        else:
            neutral += 1

    scored = bullish + bearish
    return {
        "bullish": bullish,
        "bearish": bearish,
        "neutral": neutral,
        "total": len(texts),
        "scored": scored,
        "bullish_pct": round(bullish / scored * 100, 1) if scored else 0.0,
        "bearish_pct": round(bearish / scored * 100, 1) if scored else 0.0,
    }


def classify_words(word_freq: Counter, extra_related=(),
                    min_freq: int = MIN_WORD_FREQ) -> tuple:
    """把詞頻分成（股票相關, 不相關）兩個 Counter。

    判斷順序（白名單優先於字元啟發式，減少誤判）：
      1. 公司名稱等額外清單、股市詞彙表 → 相關
      2. NON_STOCK_WORDS 排除清單 → 不相關（即使含股市關鍵字元）
      3. 含股市關鍵字元 → 相關
    出現次數 < min_freq 的字詞視為雜訊，直接濾掉、不會進報告。
    """
    extra = set(extra_related)
    related, unrelated = Counter(), Counter()
    for word, freq in word_freq.items():
        if freq < min_freq:
            continue
        if word in extra or word in STOCK_TERM_WORDS:
            related[word] = freq          # 白名單：最高優先
        elif word in NON_STOCK_WORDS:
            unrelated[word] = freq        # 排除清單：擋掉字元規則的誤判
        elif any(ch in word for ch in STOCK_TERM_CHARS):
            related[word] = freq          # 字元啟發式：輔助規則
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
def register_stock_words(stock_map: dict) -> None:
    """把上市櫃公司名稱與股市術語掛進 jieba 自訂詞典。

    jieba 內建詞庫以簡體為主，對台股名稱常會拆錯（例如「台積電」被切成
    「台積 / 電」）。把公司名以專有名詞（nz）掛進詞典後，斷詞會把整個名稱
    當成一個詞，POS 過濾時也不會被誤判成時間詞而刪掉。

    股市術語（存股、當沖、除權息、護國神山…）同樣掛進詞典：斷詞更準之外，
    也讓 classify_words() 能靠白名單正確認出它們是股市相關詞。
    """
    for name in stock_map:
        if len(name) >= 2:
            jieba.add_word(name, tag="nz")
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
    """將多段文字斷詞、過濾後統計詞頻。

    斷詞前先移除網址（見 strip_urls），避免網域碎片混進詞頻。
    改用 jieba.posseg 取得詞性，時間詞（詞性 t，如今天／明天／昨天）直接
    依詞性濾掉，不必逐一列進停用詞清單。STOPWORDS 仍保留作為保險
    （jieba 對繁體的詞性判斷偶有誤差）。
    """
    counter = Counter()
    for text in texts:
        for token in pseg.cut(strip_urls(text)):
            word = token.word.strip()
            if not word:
                continue                      # 過濾空白
            if len(word) < 2:
                continue                      # 過濾單字（多為虛詞/標點）
            if token.flag == "t":
                continue                      # 依詞性過濾時間詞（今天/明天/昨天…）
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

    # mono-color 配方：紙張為底、兩塊印版。Oxblood 主版承載絕大多數字詞，
    # 綠色輔版只給最高頻的少數幾個當重點（accent 15%-30%，不做純裝飾）。
    ranked = [w for w, _ in word_freq.most_common()]
    accent_n = max(1, round(len(ranked) * 0.2))
    accent_words = set(ranked[:accent_n])

    def ink(word, **_kwargs):
        # 文字雲是大字，綠墨可用型錄原值（大字不受小字對比門檻限制）
        return "#008A4B" if word in accent_words else "#4A1F1F"

    wc = WordCloud(
        font_path=font_path,      # 中文字型（沒設定會變成方框亂碼）
        width=1200,
        height=800,
        background_color="#F5F1E8",   # Pale Beige 紙張基材
        color_func=ink,               # 兩塊印版：Oxblood 為主、綠墨為重點
        max_words=len(word_freq),     # 不設上限，讓所有字詞都能進文字雲
    ).generate_from_frequencies(word_freq)

    plt.figure(figsize=(12, 8))
    plt.imshow(wc, interpolation="bilinear")
    plt.axis("off")               # 不顯示座標軸
    plt.tight_layout()
    # 存檔時保留紙張底色，避免四周出現白框而破壞紙感
    plt.savefig(output_path, dpi=150, facecolor="#F5F1E8")
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
    # 兩塊印版：漲＝Oxblood、跌＝Botanical Green（恢復台股紅漲綠跌）
    color = "#8F3434" if up else "#00753F"
    # 面積 = 折線 + 右下、左下兩個角點閉合。
    # 用單一低不透明度的平塗而非漸層——mono-color 明列禁止漸層，
    # 印刷上也只有墨量濃淡、沒有漸變。
    area = f"{line} {w - pad},{h} {pad},{h}"
    return (
        f'<svg width="{w}" height="{h}" viewBox="0 0 {w} {h}" '
        f'xmlns="http://www.w3.org/2000/svg">'
        f'<polygon points="{area}" fill="{color}" fill-opacity=".16"/>'
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
    sentiment: dict = None,
) -> None:
    """把文字雲圖片與偵測到的股票整合成一頁 mono-color 單色印刷風格的 HTML 報告。

    word_freq 應傳入「股票相關」詞頻（文字雲的內容）；
    unrelated_words 傳入被過濾掉的其他話題詞，會另列一區、不進文字雲。
    sentiment 傳入 analyze_sentiment() 的結果，會多顯示一張市場情緒卡片；
    傳 None（或舊資料沒有這欄）時就不顯示該卡片，保持向後相容。
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

    def quote_url(symbol: str) -> str:
        """報價連結：台股用 Yahoo 奇摩股市，美股用美國 Yahoo Finance。"""
        if symbol.endswith((".TW", ".TWO")):
            return f"https://tw.stock.yahoo.com/quote/{symbol}"
        return f"https://finance.yahoo.com/quote/{symbol}"

    # --- 市場情緒卡片（樂觀／悲觀比例）---
    # 顏色沿用台股慣例：紅＝樂觀（漲）、綠＝悲觀（跌）
    sentiment_card = ""
    if sentiment and sentiment.get("scored"):
        bull_pct = sentiment["bullish_pct"]
        bear_pct = sentiment["bearish_pct"]
        if bull_pct - bear_pct >= 20:
            mood, mood_cls = "偏樂觀", "up"
        elif bear_pct - bull_pct >= 20:
            mood, mood_cls = "偏悲觀", "down"
        else:
            mood, mood_cls = "分歧／中性", "flat"
        # 一條線分成左右兩段：左悲觀（綠）、右樂觀（紅），百分比直接標在線上。
        # 直接標示數字同時也是必要的「次要編碼」——紅綠對色盲者的區辨度不足
        # （deutan ΔE 6.2），靠標籤才能不依賴顏色也讀得出來。
        # 段落太窄時字塞不下，改標在線下方的說明列，不硬擠。
        # 線上標整數比較好讀；用「100 減去另一邊」確保兩數相加剛好是 100，
        # 不會因為各自四捨五入而出現 49%＋52% 這種看起來很怪的組合
        bear_int = round(bear_pct)
        bull_int = 100 - bear_int
        LABEL_MIN = 18          # 佔比低於此值就不在線上標字
        bear_label = (f'<span class="senti-t">{bear_int}% 悲觀</span>'
                      if bear_pct >= LABEL_MIN else "")
        bull_label = (f'<span class="senti-t">{bull_int}% 樂觀</span>'
                      if bull_pct >= LABEL_MIN else "")
        segs = ""
        if bear_pct > 0:
            segs += (f'<div class="senti-seg senti-bear" '
                     f'style="width:{bear_pct}%">{bear_label}</div>')
        if bull_pct > 0:
            segs += (f'<div class="senti-seg senti-bull">{bull_label}</div>')
        sentiment_card = f"""
  <div class="card">
    <h2>Sentiment — 今日市場情緒 <span class="pill {mood_cls}">{mood}</span></h2>
    <div class="senti-bar">{segs}</div>
    <p class="meta">
      <b class="down">悲觀 {sentiment['bearish']} 則</b>　<b class="up">樂觀 {sentiment['bullish']} 則</b>　｜
      依情緒詞典判讀每則推文的語氣：{sentiment['total']} 則中有 {sentiment['scored']} 則可判讀
      （其餘 {sentiment['neutral']} 則為中性或看不出傾向），百分比以可判讀的則數為分母｜規則法統計，僅供參考</p>
  </div>"""

    # --- 頂部熱門標的卡片（提及次數前 5 名） ---
    top_cards = ""
    for s in stock_results[:5]:
        yahoo_url = quote_url(s["symbol"])
        top_cards += f"""
      <a class="ticker-card" href="{yahoo_url}" target="_blank">
        <div class="tc-name">{s['name']}<span class="tc-sym">{s['symbol']}</span></div>
        <div class="tc-price">{s['price']:,.2f}</div>
        <div class="tc-row">{chg_pill(s['change_pct'])}
          <span class="tc-mention">{s['mentions']} 提及</span></div>
      </a>"""

    # --- 股票表格列（已依提及次數排序） ---
    if stock_results:
        max_mentions = max(s["mentions"] for s in stock_results)
        rows = []
        for s in stock_results:
            yahoo_url = quote_url(s["symbol"])
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
  /* ====== mono-color：ruled information poster ======================
     本頁的視覺配方（依 .claude/skills/mono-color/ 的設計系統解析）：
       substrate : Pale Beige #F5F1E8（型錄指定給 tactile / archive 類主題）
       mode      : complementary duotone（型錄對此 palette 的定義）
       palette   : Botanical Green #008A4B + Oxblood #8F3434
       plate     : Oxblood 為主版（內文/表格/格線，內文用 #4A1F1F 高濃度）；
                   Botanical Green 為輔版（跌、情緒悲觀段），佔比 15%-30%
       layout    : ruled information poster（細格線構成 metadata band）
       type      : Programmatic（數字為錨點、表格數字等寬對齊，字級落差 4:1-9:1）
       gesture   : 僅一種——細格線；不再另加圓角卡片、陰影等裝飾
     這組墨是唯一能容納台股「紅漲綠跌」的雙墨組合：漲＝Oxblood、跌＝綠墨。
     綠墨用 #00753F（比型錄的 #008A4B 略濃）——型錄值在米色紙上只有 3.93:1，
     小字不過 AA；提濃到 5.15:1 才夠。不再更濃是因為綠墨一旦壓深就會與
     Oxblood 的明度重疊（漲跌對比會從 1.75:1 掉到 1.17:1）。紅綠本來就
     難靠明度分辨，所以漲跌另有 ▲▼ 與正負號，不單靠顏色。
     ================================================================= */
  * {{ box-sizing: border-box; }}
  body {{
    /* Programmatic：display 用 grotesk，資訊與數字用等寬 */
    font-family: "Helvetica Neue", Helvetica, "PingFang TC",
                 "Noto Sans TC", "Microsoft JhengHei", sans-serif;
    max-width: 1000px; margin: 0 auto;
    padding: 6% 7% 9%;              /* 外緣留白 5%-9% */
    line-height: 1.65;
    background: #F5F1E8; color: #4A1F1F;
  }}
  /* 字級落差：h1 約為 microcopy 的 5 倍以上 */
  h1 {{
    font-size: 2.6rem; line-height: 1.05; letter-spacing: -.02em;
    font-weight: 700; color: #4A1F1F; margin: 0;
    /* 中文沒有詞間空白，不設 keep-all 會從任意字中間斷行
       （曾出現「PTT STOCK 熱／門標的追蹤」這種斷法）。換行點由 <br> 決定。*/
    word-break: keep-all;
  }}
  h2 {{
    font-size: .72rem; margin: 0 0 16px; color: #4A1F1F; font-weight: 700;
    text-transform: uppercase; letter-spacing: .2em;
  }}
  /* metadata band：標題與事實共用一條規則線 */
  .topbar {{
    display: flex; align-items: flex-end; justify-content: space-between;
    flex-wrap: wrap; gap: 16px; padding-bottom: 12px;
    border-bottom: 2px solid #4A1F1F; margin-bottom: 8px;
  }}
  .meta {{
    color: #4A1F1F; opacity: .62; font-size: .74rem; letter-spacing: .02em;
    font-variant-numeric: tabular-nums;
  }}
  /* 區塊之間靠格線分隔，不用卡片色塊——保持紙張外露 */
  .card {{
    border-top: 1px solid rgba(74,31,31,.28);
    padding: 26px 0 30px; margin: 0;
  }}
  /* --- 頂部熱門標的：規則線分欄，不是卡片 --- */
  .cards-row {{
    display: grid; grid-template-columns: repeat(auto-fit, minmax(150px, 1fr));
    gap: 0; margin: 26px 0 4px;
    border-top: 2px solid #4A1F1F; border-bottom: 1px solid rgba(74,31,31,.28);
  }}
  .ticker-card {{
    padding: 14px 16px 16px; text-decoration: none; color: inherit;
    border-left: 1px solid rgba(74,31,31,.18);
  }}
  .ticker-card:first-child {{ border-left: 0; padding-left: 0; }}
  .ticker-card:hover {{ background: rgba(74,31,31,.05); }}
  .tc-name {{ font-size: .9rem; color: #4A1F1F; font-weight: 700; }}
  .tc-sym {{
    font-size: .66rem; opacity: .55; margin-left: 6px; font-weight: 400;
    letter-spacing: .06em;
  }}
  .tc-price {{
    font-size: 1.55rem; font-weight: 700; color: #4A1F1F; margin: 6px 0 8px;
    font-variant-numeric: tabular-nums; letter-spacing: -.01em;
  }}
  .tc-row {{ display: flex; justify-content: space-between; align-items: center; }}
  .tc-mention {{ font-size: .74rem; color: #8F3434; font-variant-numeric: tabular-nums; }}
  /* --- 漲跌：紅墨＝漲（保留台股紅漲），碳墨＝跌 --- */
  .pill {{
    display: inline-block; padding: 1px 7px; border: 1px solid;
    font-size: .74rem; font-weight: 700; font-variant-numeric: tabular-nums;
  }}
  .pill.up {{ color: #8F3434; border-color: #8F3434; }}
  .pill.down {{ color: #00753F; border-color: #00753F; }}
  .pill.flat {{ color: #4A1F1F; border-color: rgba(74,31,31,.22); opacity: .6; }}
  /* --- 市場情緒：一條線，左悲觀（碳墨）右樂觀（紅墨） --- */
  .senti-bar {{
    display: flex; height: 34px; margin: 8px 0 12px;
    background: rgba(74,31,31,.1); border: 1px solid rgba(74,31,31,.28);
  }}
  .senti-seg {{
    display: flex; align-items: center; justify-content: center;
    min-width: 0; overflow: hidden; white-space: nowrap;
  }}
  /* 悲觀段用行內 width 指定；樂觀段一律吃掉剩餘寬度（單獨存在時就填滿整條）*/
  .senti-bull {{ background: #8F3434; flex: 1; }}
  .senti-bear {{ background: #00753F; }}
  /* 兩段並存時才需要 2px 紙色縫隙分隔（總寬才不會超過 100%）*/
  .senti-seg + .senti-seg {{ margin-left: 2px; }}
  /* 線上的直接標示：紙色字壓在墨色塊上 */
  .senti-t {{
    font-size: .8rem; font-weight: 700; color: #F5F1E8;
    font-variant-numeric: tabular-nums; padding: 0 10px; letter-spacing: .04em;
  }}
  .meta b.up {{ color: #8F3434; }}
  .meta b.down {{ color: #00753F; }}
  /* --- 高頻詞：紅墨為重點，碳墨為其他話題 --- */
  .tag {{
    display: inline-block; border: 1px solid rgba(143,52,52,.45);
    padding: 1px 10px; margin: 3px 4px 3px 0; font-size: .8rem; color: #4A1F1F;
  }}
  .tag b {{ color: #8F3434; font-variant-numeric: tabular-nums; }}
  .tag.dim {{ border-color: rgba(74,31,31,.25); opacity: .72; }}
  .tag.dim b {{ color: #4A1F1F; }}
  /* --- 表格：只用橫向規則線 --- */
  .tablewrap {{ overflow-x: auto; }}
  table {{ width: 100%; border-collapse: collapse; font-size: .85rem; }}
  th, td {{ padding: 9px 14px 9px 0; text-align: left; white-space: nowrap; }}
  th {{
    color: #4A1F1F; font-size: .66rem; text-transform: uppercase;
    letter-spacing: .12em; border-bottom: 2px solid #4A1F1F; font-weight: 700;
  }}
  tr {{ border-bottom: 1px solid rgba(74,31,31,.16); }}
  tbody tr:hover {{ background: rgba(74,31,31,.05); }}
  td.num {{ font-variant-numeric: tabular-nums; color: #4A1F1F; font-weight: 700; }}
  td.spark svg {{ display: block; }}
  td.dim {{ opacity: .55; font-size: .76rem; font-variant-numeric: tabular-nums; }}
  .tk {{ color: #4A1F1F; text-decoration: none; font-weight: 700; }}
  .tk:hover {{ color: #8F3434; }}
  .sym {{
    display: block; font-size: .66rem; opacity: .55; font-weight: 400;
    letter-spacing: .06em;
  }}
  /* --- PTT 熱度長條：純紅墨，無漸層 --- */
  .mbar-wrap {{ display: flex; align-items: center; gap: 8px; min-width: 110px; }}
  .mbar {{ height: 7px; min-width: 2px; background: #8F3434; }}
  .mnum {{ font-size: .76rem; color: #8F3434; font-variant-numeric: tabular-nums; }}
  ul {{ margin: 0; padding-left: 18px; }}
  a {{ color: #8F3434; }}
  img {{ max-width: 100%; display: block; }}
</style>
</head>
<body>
  <div class="topbar">
    <h1>PTT {board.upper()}<br>熱門標的追蹤</h1>
    <span class="meta">產生時間 {generated_at}｜{len(articles)} 個資料來源</span>
  </div>

  <div class="cards-row">{top_cards}
  </div>

{sentiment_card}

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

    # --- 步驟 2：先載入股票清單（順便掛成 jieba 自訂詞典，避免公司名被拆開）---
    print("[資訊] 載入台股上市櫃公司清單...")
    stock_map = fetch_tw_stock_list()
    register_stock_words(stock_map)

    # --- 步驟 3：斷詞與詞頻統計，並把詞分成「股票相關 / 不相關」---
    print("[資訊] 進行 jieba 斷詞與詞頻統計...")
    word_freq = tokenize_and_count(all_texts)
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
        sentiment=analyze_sentiment(all_texts),
    )


if __name__ == "__main__":
    main()
