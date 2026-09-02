# -*- coding: utf-8 -*-
"""
公開資訊觀測站（MOPS）每日重大訊息擷取
================================================================

只抓「當天 PTT 熱門標的」那幾檔的重大訊息，回答一個很具體的問題：
鄉民今天在吵這檔，是不是因為公司剛好發了什麼公告？

設計上的三個原則
----------------------------------------------------------------
1. **絕不拖垮既有流程**：MOPS 對雲端 IP 不友善（本專案爬 PTT 已經踩過同類
   問題），因此所有對外請求都包在 try/except 裡，任何失敗一律回傳空清單並
   印出警告。呼叫端（ptt_stock_tracker）不會因此中斷，每日報告照常產出。
2. **請求量壓到最低**：只查熱門標的、逐檔之間有間隔，不做全市場掃描。
3. **失敗要看得出來**：印出清楚的診斷訊息（狀態碼、筆數），CI log 才能判斷
   是被擋、格式改了、還是當天真的沒有公告。

注意：MOPS 的端點與回傳格式沒有官方穩定保證，欄位有可能變動。本模組刻意
把解析寫得寬鬆（缺欄位就跳過該筆），不讓單筆異常影響整批。
"""

import time

import requests
from requests.adapters import HTTPAdapter
from urllib3.util.retry import Retry

# MOPS 網站本身查詢重大訊息用的 ajax 端點（一般查詢頁 t05st01 的資料來源）
MOPS_AJAX_URL = "https://mops.twse.com.tw/mops/web/ajax_t05st01"
REQUEST_TIMEOUT = 20
SLEEP_BETWEEN = 1.2      # 逐檔查詢之間的間隔，避免被視為攻擊流量
MAX_PER_STOCK = 5        # 單一標的最多收幾筆，避免版面被單一公司洗版


def _make_session() -> requests.Session:
    """建立帶重試與瀏覽器 UA 的 session（與本專案爬 PTT 的做法一致）。"""
    session = requests.Session()
    retry = Retry(
        total=2, connect=2, read=2, backoff_factor=2,
        status_forcelist=[429, 500, 502, 503, 504],
        allowed_methods=frozenset(["GET", "POST"]),
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
        "Referer": "https://mops.twse.com.tw/mops/web/t05st01",
    })
    return session


def _roc_year(iso_day: str) -> str:
    """西元 YYYY-MM-DD 轉民國年（MOPS 查詢用民國年）。"""
    return str(int(iso_day[:4]) - 1911)


def _parse_rows(payload: dict, code: str, name: str) -> list[dict]:
    """把 MOPS 回傳的表格資料整理成統一結構，欄位缺漏就跳過該筆。

    MOPS 的 JSON 會把資料放在 data（二維陣列），欄位順序大致為
    〔公司代號, 公司名稱, 發言日期, 發言時間, 主旨, ...〕。
    這裡刻意用寬鬆解析：長度不足或格式不符就略過，不讓單筆壞掉整批。
    """
    rows = []
    data = payload.get("data") or []
    if not isinstance(data, list):
        return rows
    for item in data:
        if not isinstance(item, (list, tuple)) or len(item) < 5:
            continue
        subject = str(item[4]).strip()
        if not subject:
            continue
        rows.append({
            "code": code,
            "name": name,
            "date": str(item[2]).strip(),   # 發言日期（民國）
            "time": str(item[3]).strip(),   # 發言時間
            "subject": subject,
        })
    return rows


def fetch_material_news(stocks: list[dict], day: str) -> list[dict]:
    """抓取指定標的在 day 當天的重大訊息。

    stocks 傳入 count_stock_mentions() 的結果（需含 code / name / market）；
    美股沒有 MOPS 資料，會自動略過。

    回傳 [{code, name, date, time, subject}, ...]；
    任何一步失敗都只會讓結果變少或變空，不會丟出例外。
    """
    # 只有台股（4 位數字代碼）在 MOPS 有資料，美股略過
    targets = [s for s in stocks if str(s.get("code", "")).isdigit()]
    if not targets:
        print("[資訊] 熱門標的中沒有台股，略過重大訊息查詢")
        return []

    session = _make_session()
    roc_y, month = _roc_year(day), day[5:7]
    results, ok, failed = [], 0, 0

    for s in targets:
        code, name = s["code"], s["name"]
        try:
            resp = session.post(
                MOPS_AJAX_URL,
                data={
                    "encodeURIComponent": "1",
                    "step": "1",
                    "firstin": "1",
                    "off": "1",
                    "TYPEK": "all",
                    "co_id": code,
                    "year": roc_y,
                    "month": month,
                },
                timeout=REQUEST_TIMEOUT,
            )
            if resp.status_code != 200:
                failed += 1
                print(f"  {name}({code})：HTTP {resp.status_code}")
                continue
            rows = _parse_rows(resp.json(), code, name)
            # 只留當天（MOPS 用民國日期，格式如 115/09/02）
            today_roc = f"{roc_y}/{month}/{day[8:10]}"
            rows = [r for r in rows if r["date"].replace("-", "/") == today_roc]
            results.extend(rows[:MAX_PER_STOCK])
            ok += 1
        except Exception as e:      # 網路、JSON、格式問題一律吞掉，不影響主流程
            failed += 1
            print(f"  {name}({code})：查詢失敗（{type(e).__name__}: {e}）")
        time.sleep(SLEEP_BETWEEN)

    print(f"[資訊] 重大訊息：查詢 {len(targets)} 檔"
          f"（成功 {ok}、失敗 {failed}），共取得 {len(results)} 則")
    if failed and not results:
        print("[警告] 重大訊息全數查詢失敗，可能是 MOPS 擋住了執行環境的出口 IP；"
              "本次報告的公開資訊頁會顯示為無資料，不影響每日報告。")
    return results
