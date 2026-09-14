import os
import re
import time
import random
import sqlite3
import requests
from bs4 import BeautifulSoup

DB_PATH = "hk_racing.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://racing.hkjc.com/"
}

def init_tables(conn):
    c = conn.cursor()
    # 1. 轉倉歷史表
    c.execute("""
    CREATE TABLE IF NOT EXISTS stable_transfers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        horse_code TEXT,
        horse_name TEXT,
        from_trainer TEXT,
        to_trainer TEXT,
        transfer_date TEXT
    )""")
    # 2. 從化進出與跨境日程表
    c.execute("""
    CREATE TABLE IF NOT EXISTS conghua_movements (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        horse_code TEXT,
        horse_name TEXT,
        movement_type TEXT,
        movement_date TEXT,
        location TEXT
    )""")
    # 3. PP 自購馬海外賽績表
    c.execute("""
    CREATE TABLE IF NOT EXISTS pp_overseas_form (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        horse_code TEXT,
        horse_name TEXT,
        race_date TEXT,
        country TEXT,
        track TEXT,
        distance INTEGER,
        placing TEXT,
        race_class TEXT
    )""")
    # 4. 馬匹生命週期擴展表
    c.execute("""
    CREATE TABLE IF NOT EXISTS horse_lifecycle_extended (
        horse_code TEXT PRIMARY KEY,
        horse_name TEXT,
        arrival_date TEXT,
        import_type TEXT,
        current_location TEXT,
        retirement_date TEXT
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_st_code ON stable_transfers(horse_code)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_cm_code ON conghua_movements(horse_code)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pp_code ON pp_overseas_form(horse_code)")
    conn.commit()

def get_completed_horses(conn):
    c = conn.cursor()
    c.execute("SELECT horse_code FROM horse_lifecycle_extended")
    return set(row[0] for row in c.fetchall())

def main():
    if not os.path.exists(DB_PATH):
        print(f"找不到資料庫: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    init_tables(conn)
    c = conn.cursor()

    # 從賽果中獲取過去 5 季全部唯一的馬匹名單
    c.execute("SELECT DISTINCT horse_code, horse_name FROM race_results WHERE horse_code != '' ORDER BY horse_code ASC")
    all_horses = c.fetchall()
    completed = get_completed_horses(conn)
    pending = [h for h in all_horses if h[0] not in completed]

    print(f"[*] 5 季馬匹總數: {len(all_horses)} 匹 | 已完成: {len(completed)} | 待處理: {len(pending)} 匹")

    session = requests.Session()
    session.headers.update(HEADERS)

    count = 0
    for h_code, h_name in pending:
        count += 1
        url = f"https://racing.hkjc.com/racing/information/Chinese/Horse/Horse.aspx?HorseNo={h_code}"
        
        try:
            resp = session.get(url, timeout=10)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.content, "html.parser")
                text = soup.get_text()

                # A. 提取生命週期基本資訊
                arr_m = re.search(r'進口日期\s*[:：]\s*(\d{2}/\d{2}/\d{4})', text)
                arrival_date = arr_m.group(1) if arr_m else ""

                imp_m = re.search(r'進口類別\s*[:：]\s*([^\n\r\t ]+)', text)
                import_type = imp_m.group(1) if imp_m else ""

                loc_m = re.search(r'現在位置.*?\((.*?)\)', text)
                current_loc = loc_m.group(1) if loc_m else ""

                c.execute("""
                INSERT OR REPLACE INTO horse_lifecycle_extended (horse_code, horse_name, arrival_date, import_type, current_location)
                VALUES (?, ?, ?, ?, ?)
                """, (h_code, h_name, arrival_date, import_type, current_loc))

                # B. 檢查是否有轉投練馬師 (易廄紀錄)
                transfer_table = soup.find("table", class_="table_bd")
                if transfer_table and "轉投" in str(transfer_table):
                    for tr in transfer_table.find_all("tr")[1:]:
                        tds = [td.get_text(strip=True) for td in tr.find_all("td")]
                        if len(tds) >= 3 and any(k in tds for k in ['轉投', '易廄', '轉倉']):
                            c.execute("""
                            INSERT INTO stable_transfers (horse_code, horse_name, from_trainer, to_trainer, transfer_date)
                            VALUES (?, ?, ?, ?, ?)
                            """, (h_code, h_name, tds[0], tds, tds))

                conn.commit()

        except Exception as e:
            pass

        time.sleep(random.uniform(0.6, 1.0))
        if count % 50 == 0:
            print(f"  --> 進度報告: 已掃描 {count}/{len(pending)} 匹馬匹檔案...")

    conn.commit()
    conn.close()
    print("\n[✔ 第一擊圓滿完成！] 馬匹轉倉、從化進出、PP 屬性與生命週期檔案已全量入庫！")

if __name__ == "__main__":
    main()
