# -*- coding: utf-8 -*-
"""
歷史資料回填工具（可重現、全部來自實際爬取）
================================================================

PTT Stock 板的「盤後閒聊」文只在交易日發，週末沿用前一交易日那篇。
本工具針對每個「檢查日期」，抓取當天對應的實際文章，並「只採計該日期
（含）以前的推文」，因此每一天的數字都能從真實 PTT 文章重現，不是手動填的。

輸出：
  - sheet_export.csv：三天的股票熱度與高頻詞（股價為各日當天收盤價）
  - sources.json：每天對應的 PTT 文章標題、網址、採計推文數（供報告的參考資料連結）

只保留「能從實際文章重現」的交易日。實測 PTT 文章推文日期分佈為
7/10 文＝{07/10, 07/13}、7/13 文＝{07/13}，07/11、07/12 週末無推文，
故不製造 7/12 資料（避免與 7/10 完全重複、失去真實性）。

日期 → 來源文章對照：
  2026-07-10 → 7/10 盤後閒聊，採計 07/10（含）以前推文
  2026-07-13 → 7/13 盤後閒聊，採計 07/13（含）以前推文
"""
import csv
import json
import os
import re

from bs4 import BeautifulSoup
import yfinance as yf

import ptt_stock_tracker as t

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CSV_OUT = os.path.join(BASE_DIR, "sheet_export.csv")
SOURCES_OUT = os.path.join(BASE_DIR, "sources.json")

# 各檢查日 → (文章網址, 採計推文的截止 MM/DD)
URL_0710 = "https://www.ptt.cc/bbs/Stock/M.1783643408.A.71D.html"
URL_0713 = "https://www.ptt.cc/bbs/Stock/M.1783922445.A.C87.html"
PLAN = {
    "2026-07-10": (URL_0710, "07/10"),
    "2026-07-13": (URL_0713, "07/13"),
}


def scrape_day(session, url: str, cutoff: str) -> tuple:
    """抓取文章，回傳（文字清單, 文章標題, 採計推文數）。

    只保留推文時間戳 <= cutoff（MM/DD）的推文，讓歷史快照可重現。
    """
    resp = session.get(url, timeout=15)
    soup = BeautifulSoup(resp.text, "html.parser")
    main = soup.find("div", id="main-content")

    # 文章標題（metaline 中「標題」那列）
    title = ""
    for meta in main.find_all("div", class_="article-metaline"):
        tag = meta.find("span", class_="article-meta-tag")
        val = meta.find("span", class_="article-meta-value")
        if tag and val and tag.get_text(strip=True) == "標題":
            title = val.get_text(strip=True)

    pushes = []
    for push in main.find_all("div", class_="push"):
        dt_tag = push.find("span", class_="push-ipdatetime")
        content_tag = push.find("span", class_="push-content")
        if dt_tag and content_tag:
            m = re.search(r"(\d{2}/\d{2})", dt_tag.get_text())
            if m and m.group(1) <= cutoff:
                txt = content_tag.get_text(strip=True).lstrip(":").strip()
                if txt:
                    pushes.append(txt)
        push.extract()
    for meta in main.find_all("div", class_=["article-metaline", "article-metaline-right"]):
        meta.extract()
    content = main.get_text("\n", strip=True)
    content = re.split(r"\n--\n", content)[0]
    content = "\n".join(l for l in content.split("\n") if not l.startswith("※"))
    return [content] + pushes, title, len(pushes)


def main():
    session = t.make_ptt_session()
    stock_map = t.fetch_tw_stock_list()

    per_day = {}     # date -> {"stocks": [...], "words": [...]}
    sources = {}     # date -> {"title", "url", "pushes"}
    for day, (url, cutoff) in PLAN.items():
        texts, title, n_push = scrape_day(session, url, cutoff)
        word_freq = t.tokenize_and_count(texts)
        hot = t.count_stock_mentions(texts, stock_map)
        per_day[day] = {
            "stocks": [(s["code"], s["name"], s["mentions"]) for s in hot],
            "words": [(i, w, c) for i, (w, c) in enumerate(word_freq.most_common(), 1)],
        }
        sources[day] = {"title": title, "url": url, "pushes": n_push}
        print(f"{day}｜{title}｜採計 {n_push} 則推文｜"
              f"{len(hot)} 檔股票、{len(word_freq)} 個詞")

    # 各檢查日的當天（或最近交易日）收盤價
    all_codes = sorted({c for d in per_day.values() for c, _, _ in d["stocks"]})
    closes = {}
    for code in all_codes:
        for suffix in (".TW", ".TWO"):
            try:
                hist = yf.Ticker(code + suffix).history(start="2026-06-20")
                if not hist.empty:
                    closes[code] = [(dt.strftime("%Y-%m-%d"), float(c))
                                    for dt, c in hist["Close"].items()]
                    break
            except Exception:
                continue

    def close_on_or_before(code, day):
        best = None
        for d, c in closes.get(code, []):
            if d <= day:
                best = c
        return best

    # 寫出 CSV
    with open(CSV_OUT, "w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(["檢查日期", "股票代碼", "公司名稱", "PTT提及次數", "股價"])
        for day in sorted(per_day):
            for code, name, mentions in sorted(per_day[day]["stocks"], key=lambda x: -x[2]):
                price = close_on_or_before(code, day)
                w.writerow([day, code, name, mentions,
                            f"{price:.2f}" if price is not None else "#N/A"])
        w.writerow([]); w.writerow([])
        w.writerow(["檢查日期", "排名", "詞彙", "出現次數"])
        for day in sorted(per_day):
            for rank, word, freq in per_day[day]["words"]:
                w.writerow([day, rank, word, freq])
    print(f"[完成] {CSV_OUT}")

    with open(SOURCES_OUT, "w", encoding="utf-8") as f:
        json.dump(sources, f, ensure_ascii=False, indent=2)
    print(f"[完成] {SOURCES_OUT}")


if __name__ == "__main__":
    main()
