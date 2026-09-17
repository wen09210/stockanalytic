# -*- coding: utf-8 -*-
"""
公開資訊觀測站每日重大訊息（走 TWSE OpenAPI）
================================================================

只顯示「當天 PTT 熱門標的」的重大訊息，回答一個很具體的問題：
鄉民今天在吵這檔，是不是因為公司剛好發了什麼公告？

為什麼用 OpenAPI 而不是爬 MOPS 網頁
----------------------------------------------------------------
MOPS 的查詢頁是 POST 表單 + session 狀態，部分頁面有驗證碼，而且對雲端 IP
不友善（本專案爬 PTT 已經踩過同類問題）。TWSE OpenAPI 回傳 JSON，而且是
**一次給整批**，所以這裡改成「抓一次、自己過濾」：

  - 對外請求從「熱門標的數量」次降到 1 次，被視為攻擊流量的風險大幅降低
  - 不需要逐檔 sleep，整體快很多
  - 沒有表單、session、驗證碼的問題

防禦性設計
----------------------------------------------------------------
1. **絕不拖垮既有流程**：所有對外請求與解析都包在 try/except，任何失敗一律
   回傳空清單。呼叫端（ptt_stock_tracker）不會中斷，每日報告照常產出。
2. **欄位用別名比對**：OpenAPI 各資料集的欄位命名不完全一致，直接寫死
   key 名稱很脆弱。這裡用別名清單找欄位，命名有出入也還能解析。
3. **失敗要看得出來**：印出實際用到的端點、取得筆數、以及第一筆的欄位名稱，
   CI log 才足以判斷是端點錯、格式變了、還是當天真的沒有公告。

注意：端點與欄位名稱無法在開發環境驗證（出口 proxy 封鎖 openapi.twse.com.tw），
以下候選清單是依 TWSE OpenAPI 慣例推定，實際以第一次 CI 執行的 log 為準。
"""

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# 依序嘗試的候選資料集（第一個成功且有資料的就採用）。
# 之所以是清單而不是單一端點：這裡無法連外驗證，先讓它在 CI 上自己找到對的
# 那個並印出來，比猜錯一次就整個沒資料好。確認後可以收斂成單一端點。
CANDIDATE_ENDPOINTS = [
    "https://openapi.twse.com.tw/v1/opendata/t187ap04_L",   # 上市 重大訊息
    "https://openapi.twse.com.tw/v1/opendata/t187ap04_O",   # 上櫃 重大訊息
]

REQUEST_TIMEOUT = 20

# 欄位別名：OpenAPI 各資料集命名不完全一致，用別名找比寫死 key 穩
FIELD_ALIASES = {
    "code": ("公司代號", "證券代號", "Code", "CompanyCode"),
    "name": ("公司名稱", "證券名稱", "Name", "CompanyName"),
    "date": ("發言日期", "出表日期", "Date", "AnnounceDate"),
    "time": ("發言時間", "Time", "AnnounceTime"),
    "subject": ("主旨", "標題", "Subject", "Title"),
}


def _make_session() -> requests.Session:
    """建立帶重試的 session（沿用本專案爬 PTT 的做法）。"""
    session = requests.Session()
    retry = Retry(
        total=2, connect=2, read=2, backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET"]),
        raise_on_status=False,
    )
    session.mount("https://", HTTPAdapter(max_retries=retry))
    session.headers.update({"User-Agent": "stockanalytic/1.0 (+github actions)"})
    return session


def _pick(item: dict, key: str) -> str:
    """依別名清單從一筆資料裡取值，取不到回空字串。"""
    for alias in FIELD_ALIASES[key]:
        if alias in item and item[alias] is not None:
            return str(item[alias]).strip()
    return ""


def _digits(s: str) -> str:
    """只留數字，用來比對日期（民國日期可能寫成 1150902 或 115/09/02）。"""
    return "".join(ch for ch in s if ch.isdigit())


def _fmt_time(raw: str) -> str:
    """發言時間可能是 083100 / 08:31:00 / 0831，統一成 HH:MM 好讀。

    取不到或格式看不懂就原樣回傳，寧可顯示原始值也不要顯示錯的時間。
    """
    d = _digits(raw)
    if len(d) >= 4:
        return f"{d[:2]}:{d[2:4]}"
    return raw


def _roc_digits(iso_day: str) -> str:
    """西元 YYYY-MM-DD 轉成民國日期的純數字形式，例如 2026-09-02 → 1150902。"""
    return f"{int(iso_day[:4]) - 1911}{iso_day[5:7]}{iso_day[8:10]}"


def _fetch_dataset(session: requests.Session, url: str) -> list:
    """抓單一資料集，失敗回空清單並印出原因。"""
    try:
        resp = session.get(url, timeout=REQUEST_TIMEOUT)
    except Exception as e:
        print(f"  {url} → 連線失敗（{type(e).__name__}: {e}）")
        return []
    if resp.status_code != 200:
        print(f"  {url} → HTTP {resp.status_code}")
        return []
    try:
        data = resp.json()
    except Exception:
        print(f"  {url} → 回應不是 JSON（前 120 字：{resp.text[:120]!r}）")
        return []
    if not isinstance(data, list):
        print(f"  {url} → 預期為陣列，實得 {type(data).__name__}")
        return []
    print(f"  {url} → {len(data)} 筆")
    if data and isinstance(data[0], dict):
        # 印出實際欄位名，端點或格式有變時能直接從 CI log 看出來
        print(f"    欄位：{list(data[0].keys())}")
    return data


def fetch_material_news(stocks: list[dict], day: str) -> list[dict]:
    """抓取指定標的在 day 當天的重大訊息。

    stocks 傳入 count_stock_mentions() 的結果（需含 code / name）；
    美股沒有這裡的資料，會自動略過。

    做法是「整批抓一次、在本地過濾」——OpenAPI 一次回傳全市場，
    不需要逐檔查詢。

    回傳 [{code, name, date, time, subject}, ...]；
    任何一步失敗都只會讓結果變少或變空，不會丟出例外。
    """
    wanted = {str(s.get("code", "")): s.get("name", "")
              for s in stocks if str(s.get("code", "")).isdigit()}
    if not wanted:
        print("[資訊] 熱門標的中沒有台股，略過重大訊息查詢")
        return []

    session = _make_session()
    target = _roc_digits(day)
    results = []

    print(f"[資訊] 查詢重大訊息（目標日期民國 {target}，"
          f"比對 {len(wanted)} 檔熱門標的）")
    for url in CANDIDATE_ENDPOINTS:
        for item in _fetch_dataset(session, url):
            if not isinstance(item, dict):
                continue
            code = _pick(item, "code")
            if code not in wanted:
                continue
            # 日期比對用純數字，避免 1150902 / 115/09/02 兩種寫法對不上
            if _digits(_pick(item, "date")) != target:
                continue
            subject = _pick(item, "subject")
            if not subject:
                continue
            results.append({
                "code": code,
                "name": _pick(item, "name") or wanted[code],
                "date": _pick(item, "date"),
                "time": _fmt_time(_pick(item, "time")),
                "subject": subject,
            })

    print(f"[資訊] 重大訊息：命中 {len(results)} 則")
    if not results:
        print("[提示] 沒有命中不一定是錯誤——多數個股在多數日子本來就沒有公告。"
              "若連續多日皆為空，再依上面印出的筆數與欄位名檢查端點是否正確。")
    return results
