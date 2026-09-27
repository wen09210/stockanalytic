# -*- coding: utf-8 -*-
"""
從 Disp BBS（disp.cc）取得 PTT Stock 板的每日盤後閒聊
================================================================

為什麼不直接爬 PTT
----------------------------------------------------------------
2026-09-16 起 PTT 對 GitHub Actions runner 的出口 IP 一律回 403，連續多日、
每次重試都一樣，是穩定的 IP 層封鎖，不是 headers 或重試能繞過的。disp.cc
是 PTT 的鏡像站，Stock 板每天的「[閒聊] YYYY/MM/DD 盤後閒聊」連同推文都有
轉錄，內容就是同一篇文章，所以換來源不會改變資料的意義。

選文邏輯與原本一致
----------------------------------------------------------------
原本分析的是 PTT 置底文（實際上每天就是最新一篇盤後閒聊；週末沿用週五那篇）。
這裡改成在 disp.cc 看板列表找標題符合「[閒聊] 日期 盤後閒聊」的文章，取日期
最新的一篇——結果跟以前抓置底文相同。

防禦性設計（HTML 結構無法在開發環境驗證）
----------------------------------------------------------------
開發環境的出口 proxy 封鎖 disp.cc，所以這裡**刻意不依賴任何 class 名稱**：

1. **找文章靠網址樣式＋標題文字**：列表頁上所有 href 符合 disp.cc 文章網址
   （/b/Stock/xxxx、/b/205-xxxx、/amp/Stock/xxxx）的連結都拿來比對標題。
2. **找推文靠 PTT 推文本身的格式**：「推/噓/→ 帳號: 內容 日期時間」這個格式
   不論鏡像站怎麼包 HTML 都會保留。做法是找「文字符合推文格式、且底下沒有
   更小的元素也符合」的最小元素，每個就是一則推文。這套規則對 PTT 原站的
   HTML 同樣成立。
3. **失敗要看得出來**：每一步都印出抓到幾個連結、幾則推文、內文取自哪個元素，
   CI log 足以判斷是被擋、結構不符還是推文被截斷。

實際結構以第一次 CI 執行的 log 為準；若解析不到，把 log 貼給維護者即可調整。
"""

import re
from datetime import date
from urllib.parse import urljoin

import requests
from bs4 import BeautifulSoup, Tag
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

DISP_BASE = "https://disp.cc"
BOARD = "Stock"
BOARD_URL = f"{DISP_BASE}/b/{BOARD}"
BOARD_NUMBER = "205"          # disp.cc 內部的 Stock 板編號（網址 /b/205-xxxx）
REQUEST_TIMEOUT = 20
MAX_LIST_PAGES = 12           # 列表首頁找不到時，最多往前翻幾頁

# disp.cc 文章網址的幾種寫法（皆出現在搜尋引擎收錄的實際網址中）
_ARTICLE_HREF_RE = re.compile(
    rf"(?:^|disp\.cc)/?(?:b|amp|m)/(?:{BOARD}/|{BOARD_NUMBER}-)[A-Za-z0-9]+/?$"
)
# 目標文章標題：[閒聊] 2026/09/25 盤後閒聊
_TITLE_RE = re.compile(r"\[閒聊\]\s*(\d{4})/(\d{1,2})/(\d{1,2})\s*盤後閒聊")
# 翻頁連結的文字（往較舊的文章）
_PREV_PAGE_TEXTS = ("上頁", "上一頁", "前頁", "前一頁", "較舊", "舊文章")

# PTT 推文格式：「推 帳號: 內容 09/25 13:45」（帳號後的冒號可能是全形）
_PUSH_RE = re.compile(r"^(推|噓|→)\s*([A-Za-z0-9_]{2,14})\s*[:：]\s?(.*)$", re.S)
# 推文結尾的樓層／IP／日期時間，要從內容剝掉，否則「1F」「09」「13」會混進
# 詞頻。disp.cc 的實際格式是「: 內容 1F 09/25 08:32」（樓層在日期前）
_PUSH_TAIL_RE = re.compile(
    r"\s*(?:\d+F\s+)?(?:\d{1,3}(?:\.\d{1,3}){3}\s*)?"
    r"\d{1,2}/\d{1,2}(?:\s+\d{1,2}:\d{2})?\s*$"
)
_FLOOR_RE = re.compile(r"(\d+)F\s+\d{1,2}/\d{1,2}")
# 文章開頭的 metadata 標籤（看板／作者／標題／時間），各自下一行是值
_META_LABELS = ("看板", "作者", "標題", "時間")
_PUSH_MAX_LEN = 400           # 一則推文不會這麼長；超過的元素一定是外層容器

# 推文數低於此值就視為可能被截斷（盤後閒聊每天約 1,500 則）
_SUSPICIOUSLY_FEW_PUSHES = 200


def make_session() -> requests.Session:
    """帶重試、用一般瀏覽器 UA 的 session。"""
    session = requests.Session()
    retry = Retry(
        total=4, connect=4, read=4, backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    adapter = HTTPAdapter(max_retries=retry)
    session.mount("https://", adapter)
    session.mount("http://", adapter)
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
        ),
        "Accept-Language": "zh-TW,zh;q=0.9",
    })
    return session


def _get_soup(session: requests.Session, url: str) -> BeautifulSoup:
    resp = session.get(url, timeout=REQUEST_TIMEOUT)
    resp.raise_for_status()
    if not resp.encoding or resp.encoding.lower() == "iso-8859-1":
        resp.encoding = resp.apparent_encoding   # 沒宣告編碼時避免中文變亂碼
    return BeautifulSoup(resp.text, "html.parser")


# ---------------------------------------------------------------------------
# 1. 從看板列表找出最新一篇盤後閒聊
# ---------------------------------------------------------------------------
def _article_links(soup: BeautifulSoup, page_url: str) -> list[dict]:
    """列出頁面上所有 disp.cc Stock 板文章連結（去重，保留出現順序）。"""
    seen, links = set(), []
    for a in soup.find_all("a", href=True):
        href = a["href"].split("#")[0].split("?")[0]
        if not _ARTICLE_HREF_RE.search(href):
            continue
        url = urljoin(page_url, href)
        title = a.get_text(" ", strip=True) or a.get("title", "").strip()
        if url in seen:
            # 同一篇可能有多個連結（圖示＋標題），補上較完整的標題
            for item in links:
                if item["url"] == url and len(title) > len(item["title"]):
                    item["title"] = title
            continue
        seen.add(url)
        links.append({"title": title, "url": url})
    return links


def _prev_page_url(soup: BeautifulSoup, page_url: str):
    for a in soup.find_all("a", href=True):
        text = a.get_text(strip=True)
        if any(t in text for t in _PREV_PAGE_TEXTS):
            return urljoin(page_url, a["href"])
    return None


def _title_date(title: str):
    m = _TITLE_RE.search(title)
    if not m:
        return None
    try:
        return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
    except ValueError:
        return None


def find_latest_after_market_chat(session: requests.Session) -> dict:
    """回傳 {"title", "url", "date"}；找不到時丟出 LookupError。"""
    url, visited = BOARD_URL, set()
    for page in range(1, MAX_LIST_PAGES + 1):
        if not url or url in visited:
            break
        visited.add(url)
        soup = _get_soup(session, url)
        links = _article_links(soup, url)
        candidates = [dict(item, date=d) for item in links
                      if (d := _title_date(item["title"]))]
        print(f"  disp.cc 列表第 {page} 頁：{len(links)} 篇文章連結、"
              f"{len(candidates)} 篇盤後閒聊（{url}）")
        if page == 1 and not links:
            # 第一頁就一個文章連結都沒有，代表結構跟預期不同（或被導去驗證頁），
            # 印出頁面開頭幫助判斷，再翻頁也沒意義
            title = soup.title.get_text(strip=True) if soup.title else ""
            print(f"    頁面標題：{title!r}；前 200 字："
                  f"{soup.get_text(' ', strip=True)[:200]!r}")
            break
        if candidates:
            best = max(candidates, key=lambda c: c["date"])
            print(f"  選用：{best['title']}（{best['url']}）")
            return best
        url = _prev_page_url(soup, url)
    raise LookupError("disp.cc 列表中找不到「[閒聊] 日期 盤後閒聊」文章")


# ---------------------------------------------------------------------------
# 2. 解析文章：推文與內文
# ---------------------------------------------------------------------------
def _clean_push_content(raw: str) -> str:
    return _PUSH_TAIL_RE.sub("", raw).strip()


def extract_pushes(soup: BeautifulSoup) -> tuple[list[str], list[Tag]]:
    """找出所有推文，回傳 (推文內容清單, 推文所在元素清單)。

    規則：元素文字符合推文格式，且子元素中沒有也符合的——也就是「最小的」
    推文容器。外層（整串推文的容器）文字也可能以「推 帳號:」開頭，但它底下
    有更小的元素符合，所以會被排除，不會重複計算。
    """
    matched = {}
    for tag in soup.find_all(True):
        if tag.name in ("script", "style", "head", "title"):
            continue
        text = tag.get_text(" ", strip=True)
        if len(text) > _PUSH_MAX_LEN:
            continue
        if _PUSH_RE.match(text):
            matched[id(tag)] = tag

    pushes, nodes = [], []
    for tag in matched.values():
        # 有更小的符合元素 → 這是外層容器，略過
        if any(id(d) in matched for d in tag.find_all(True)):
            continue
        m = _PUSH_RE.match(tag.get_text(" ", strip=True))
        content = _clean_push_content(m.group(3))
        nodes.append(tag)
        if content:
            pushes.append(content)
    return pushes, nodes


def extract_pushes_by_line(soup: BeautifulSoup) -> list[str]:
    """備援：把整頁當純文字逐行比對推文格式。

    有些 BBS 鏡像把整篇（含推文）放在同一個 <pre> 裡，這時上面「找最小元素」
    的做法會因為整塊太長而一則都找不到；逐行比對可以補上。
    """
    pushes = []
    for line in soup.get_text("\n").split("\n"):
        m = _PUSH_RE.match(line.strip())
        if m:
            content = _clean_push_content(m.group(3))
            if content:
                pushes.append(content)
    return pushes


def _parse_pushes(soup: BeautifulSoup) -> tuple[list[str], list[Tag]]:
    """兩種解析取推文較多者；逐行版沒有對應元素可移除（內文端另行過濾）。"""
    pushes, nodes = extract_pushes(soup)
    if len(pushes) < _SUSPICIOUSLY_FEW_PUSHES:
        by_line = extract_pushes_by_line(soup)
        if len(by_line) > len(pushes):
            print(f"    元素解析 {len(pushes)} 則、逐行解析 {len(by_line)} 則，採逐行")
            return by_line, []
    return pushes, nodes


def _strip_meta_header(content: str) -> str:
    """去掉開頭的「看板 / Stock / 作者 / 帳號 (暱稱) / 標題 / … / 時間 / …」。

    PTT 原站用 article-metaline 標記這段，disp.cc 沒有，只能靠文字：若開頭
    幾行內出現「時間」標籤，就把它和它的值（下一行）以前的內容整段去掉。
    """
    lines = content.split("\n")
    head = lines[:12]
    if "時間" in head and sum(label in head for label in _META_LABELS) >= 3:
        cut = head.index("時間") + 2
        return "\n".join(lines[cut:])
    return content


def _extract_content(soup: BeautifulSoup) -> tuple[str, str]:
    """取內文，回傳 (內文, 取自哪個元素的描述)。須在移除推文元素後呼叫。

    先試 PTT 原站的 #main-content；否則挑「直屬文字最多」的區塊——BBS 內文
    通常是一個 div 裡一大串文字節點夾 <br>，導覽列、側欄的文字則分散在
    許多小元素裡，直屬文字很少。
    """
    for tag in soup.find_all(["script", "style", "nav", "header", "footer",
                              "noscript", "form"]):
        tag.decompose()

    main = soup.find(id="main-content")
    if main is None:
        best, best_len = None, 0
        for tag in soup.find_all(["div", "article", "section", "pre"]):
            direct = "".join(s for s in tag.find_all(string=True, recursive=False))
            n = len(direct.strip())
            if n > best_len:
                best, best_len = tag, n
        main = best
    if main is None:
        return "", "（找不到）"

    where = f"<{main.name} id={main.get('id')!r} class={main.get('class')!r}>"
    for meta in main.find_all("div", class_=["article-metaline",
                                             "article-metaline-right"]):
        meta.extract()
    content = main.get_text("\n", strip=True)
    content = re.split(r"\n--\n", content)[0]   # 去掉簽名檔
    content = _strip_meta_header(content)
    # 推文行（逐行解析時沒有元素可先移除）與 ※ 系統訊息都不算內文
    content = "\n".join(line for line in content.split("\n")
                        if not line.startswith("※")
                        and not _PUSH_RE.match(line.strip()))
    return content, where


def get_article_content(session: requests.Session, url: str) -> dict:
    """抓單篇文章，回傳 {"content": str, "pushes": [str, ...]}（與原 PTT 版相同）。"""
    soup = _get_soup(session, url)
    pushes, nodes = _parse_pushes(soup)

    # 推文少得不合理時試 AMP 版：AMP 頁面不能跑自訂 JS，內容必須在伺服器端
    # 直接輸出，如果一般版是用 JS 分批載入推文，AMP 版比較可能是完整的
    if len(pushes) < _SUSPICIOUSLY_FEW_PUSHES:
        m = re.search(rf"/b/(?:{BOARD}/|{BOARD_NUMBER}-)([A-Za-z0-9]+)/?$", url)
        if m:
            amp_url = f"{DISP_BASE}/amp/{BOARD}/{m.group(1)}"
            try:
                amp_soup = _get_soup(session, amp_url)
                amp_pushes, amp_nodes = _parse_pushes(amp_soup)
                print(f"    一般版 {len(pushes)} 則推文偏少，AMP 版 {len(amp_pushes)} 則")
                if len(amp_pushes) > len(pushes):
                    soup, pushes, nodes = amp_soup, amp_pushes, amp_nodes
            except requests.exceptions.RequestException as e:
                print(f"    AMP 版抓取失敗（{e}），沿用一般版")

    # 樓層號是 disp.cc 標的連續編號：最高樓層明顯大於抓到的則數，代表有推文
    # 沒出現在頁面上（例如分批載入）；兩者相近就是完整的
    floors = [int(m.group(1)) for n in nodes
              if (m := _FLOOR_RE.search(n.get_text(" ", strip=True)))]
    floor_note = f"，最高樓層 {max(floors)}F" if floors else ""

    for node in nodes:
        node.extract()
    content, where = _extract_content(soup)
    print(f"    推文 {len(pushes)} 則{floor_note}；內文 {len(content)} 字，取自 {where}")
    if floors and max(floors) > len(nodes) * 1.05:
        print(f"    [提示] 最高樓層 {max(floors)}F 比抓到的 {len(nodes)} 則多，"
              "頁面上可能沒有列出全部推文")
    if len(pushes) < _SUSPICIOUSLY_FEW_PUSHES:
        print(f"    [提示] 盤後閒聊通常約 1,500 則推文，只抓到 {len(pushes)} 則，"
              "可能被截斷或推文格式與預期不同")
    return {"content": content, "pushes": pushes}
