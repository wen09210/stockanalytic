# -*- coding: utf-8 -*-
"""
把本地 sheet_export.csv 同步到 Google 試算表（每天一個日期分頁）
================================================================

用途：
  - 一次把多天資料（例如 backfill_history.py 產生的 7/10、7/13）推上試算表
  - 讓試算表內容與網站報告一致（兩者都以 sheet_export.csv 為準）
  - 清掉非日期命名的多餘分頁（例如建檔時殘留的 Untitled）

需要 credentials.json（Service Account），且該服務帳戶已被加入試算表共用名單。
執行：.venv/bin/python sync_to_sheet.py
"""
import csv
import os
import re
import sys

import gspread
from google.oauth2.service_account import Credentials

BASE_DIR = os.path.dirname(os.path.abspath(__file__))
CREDENTIALS_FILE = os.path.join(BASE_DIR, "credentials.json")
CSV_FILE = os.path.join(BASE_DIR, "sheet_export.csv")
SPREADSHEET_NAME = "PTT股市熱門標的追蹤"
DATE_RE = re.compile(r"\d{4}-\d{2}-\d{2}")


def parse_csv() -> dict:
    """把 sheet_export.csv 解析成 {日期: {"stocks": [...列...], "words": [...列...]}}。"""
    per_day = {}
    mode = None
    with open(CSV_FILE, encoding="utf-8") as f:
        for row in csv.reader(f):
            cells = [c.strip() for c in row]
            if not any(cells):
                continue
            if "股票代碼" in cells:
                mode = "stocks"; continue
            if "詞彙" in cells:
                mode = "words"; continue
            day = cells[0]
            if not DATE_RE.fullmatch(day):
                continue
            per_day.setdefault(day, {"stocks": [], "words": []})[mode].append(cells)
    return per_day


def main():
    if not os.path.exists(CREDENTIALS_FILE):
        sys.exit(f"[錯誤] 找不到 {CREDENTIALS_FILE}")

    scopes = ["https://www.googleapis.com/auth/spreadsheets",
              "https://www.googleapis.com/auth/drive"]
    creds = Credentials.from_service_account_file(CREDENTIALS_FILE, scopes=scopes)
    client = gspread.authorize(creds)
    ss = client.open(SPREADSHEET_NAME)

    per_day = parse_csv()
    print(f"[資訊] sheet_export.csv 有 {len(per_day)} 天：{', '.join(sorted(per_day))}")

    # --- 逐日寫入日期分頁（存在就清空重寫，冪等）---
    for day in sorted(per_day):
        # 股票代碼欄前面加單引號強制存成文字，避免 USER_ENTERED 把 "0050"
        # 這類前導零代碼自動辨識成數字 50、吃掉前導零
        stock_rows_protected = [
            [row[0], f"'{row[1]}"] + row[2:] for row in per_day[day]["stocks"]
        ]
        values = [["檢查日期", "股票代碼", "公司名稱", "PTT提及次數", "股價"]]
        values += stock_rows_protected
        values += [[], []]
        values += [["檢查日期", "排名", "詞彙", "出現次數"]]
        values += per_day[day]["words"]
        try:
            ws = ss.worksheet(day)
            ws.clear()
        except gspread.WorksheetNotFound:
            ws = ss.add_worksheet(title=day, rows=200, cols=10)
        # USER_ENTERED：股價已是純數值，直接寫入
        ws.update(values=values, range_name="A1", value_input_option="USER_ENTERED")
        print(f"[完成] 已寫入分頁「{day}」"
              f"（{len(per_day[day]['stocks'])} 檔股票、{len(per_day[day]['words'])} 詞）")

    # --- 刪除非日期命名的分頁（例如 Untitled）---
    # 注意：試算表至少要保留一個分頁，所以先確保日期分頁都建好了才刪
    for ws in ss.worksheets():
        if not DATE_RE.fullmatch(ws.title):
            ss.del_worksheet(ws)
            print(f"[完成] 已刪除多餘分頁「{ws.title}」")

    print("\n最終分頁：", [ws.title for ws in ss.worksheets()])


if __name__ == "__main__":
    main()
