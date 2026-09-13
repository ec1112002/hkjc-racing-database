import os
import re
import time
import random
import sqlite3
import datetime
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
    c.execute("""
    CREATE TABLE IF NOT EXISTS gear_changes_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        horse_code TEXT,
        horse_name TEXT,
        race_id TEXT,
        race_date TEXT,
        current_gear TEXT,
        gear_changes TEXT,
        first_time_gear TEXT,
        removed_gear TEXT,
        reapplied_gear TEXT
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS horse_weights_allowance (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        race_id TEXT,
        race_date TEXT,
        race_no INTEGER,
        horse_code TEXT,
        horse_name TEXT,
        jockey TEXT,
        actual_weight REAL,
        declared_weight REAL,
        weight_change REAL,
        jockey_allowance INTEGER DEFAULT 0
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS horse_ratings_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        horse_code TEXT,
        horse_name TEXT,
        race_id TEXT,
        race_date TEXT,
        pre_race_rating INTEGER,
        rating_change INTEGER,
        post_race_rating INTEGER
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS track_environment_detail (
        race_id TEXT PRIMARY KEY,
        race_date TEXT,
        track_info TEXT,
        going TEXT,
        penetrometer_reading REAL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_gear_h ON gear_changes_history(horse_code)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_weights_r ON horse_weights_allowance(race_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_ratings_h ON horse_ratings_history(horse_code)")
    conn.commit()

def parse_gear_changes(gear_str):
    if not gear_str or gear_str in ('-', '--', 'N/A', 'None'):
        return "", "", "", ""
    items = [x.strip() for x in gear_str.replace('/', ' ').split() if x.strip()]
    first_time, removed, reapplied, current = [], [], [], []
    for item in items:
        if item.endswith('1'):
            first_time.append(item[:-1])
            current.append(item[:-1])
        elif item.endswith('2'):
            reapplied.append(item[:-1])
            current.append(item[:-1])
        elif item.endswith('-'):
            removed.append(item[:-1])
        else:
            current.append(item)
    return "/".join(current), "/".join(first_time), "/".join(removed), "/".join(reapplied)

def main():
    if not os.path.exists(DB_PATH):
        print(f"找不到資料庫: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    init_tables(conn)
    c = conn.cursor()

    # ==========================================
    # 第一部分：秒級提煉 配備、體重與評分 (約 3 秒)
    # ==========================================
    print("[1/2] 正在從現有賽果中提煉 5 年配備代碼、體重讓磅與評分...")
    c.execute("""
    SELECT race_id, race_date, race_no, horse_code, horse_name, jockey, actual_weight, declared_weight, gear, rating
    FROM race_results
    ORDER BY horse_code ASC, race_date ASC, race_no ASC
    """)
    rows = c.fetchall()

    weights_data = []
    gear_data = []
    ratings_data = []

    prev_horse = None
    prev_weight = None
    prev_rating = None

    for r in rows:
        race_id, r_date, r_no, h_code, h_name, jockey_str, act_wt, dec_wt, gear_str, rating = r
        
        if h_code != prev_horse:
            wt_change = 0.0
            rating_change = 0
        else:
            wt_change = round(dec_wt - prev_weight, 1) if (dec_wt and prev_weight) else 0.0
            rating_change = (rating - prev_rating) if (rating is not None and prev_rating is not None) else 0
            
        prev_horse = h_code
        prev_weight = dec_wt
        prev_rating = rating

        allow_m = re.search(r'\(-?(\d+)\)', str(jockey_str or ''))
        allowance = int(allow_m.group(1)) if allow_m else 0
        clean_jockey = re.sub(r'\(.*?\)', '', str(jockey_str or '')).strip()

        weights_data.append((race_id, r_date, r_no, h_code, h_name, clean_jockey, act_wt, dec_wt, wt_change, allowance))
        cur_g, f_g, rem_g, re_g = parse_gear_changes(str(gear_str or ''))
        gear_data.append((h_code, h_name, race_id, r_date, cur_g, str(gear_str or ''), f_g, rem_g, re_g))

        if rating is not None:
            ratings_data.append((h_code, h_name, race_id, r_date, rating, rating_change, rating))

    c.execute("DELETE FROM horse_weights_allowance")
    c.execute("DELETE FROM gear_changes_history")
    c.execute("DELETE FROM horse_ratings_history")

    c.executemany("INSERT INTO horse_weights_allowance VALUES (NULL,?,?,?,?,?,?,?,?,?,?)", weights_data)
    c.executemany("INSERT INTO gear_changes_history VALUES (NULL,?,?,?,?,?,?,?,?,?)", gear_data)
    c.executemany("INSERT INTO horse_ratings_history VALUES (NULL,?,?,?,?,?,?,?)", ratings_data)
    conn.commit()
    print(f"  [✔] 成功寫入體重表 {len(weights_data)} 筆、配備表 {len(gear_data)} 筆、評分表 {len(ratings_data)} 筆！")

    # ==========================================
    # 第二部分：抓取 5 年賽日移欄跑道與場地硬度 (約 12 分鐘)
    # ==========================================
    print("[2/2] 正在抓取 5 年賽馬日之移欄跑道 (A/B/C) 與壓地儀硬度讀數...")
    c.execute("SELECT DISTINCT race_date FROM race_results ORDER BY race_date ASC")
    distinct_dates = [row[0] for row in c.fetchall()]
    
    session = requests.Session()
    session.headers.update(HEADERS)
    
    track_count = 0
    for idx, r_date in enumerate(distinct_dates, 1):
        date_str = r_date.replace("-", "/")
        date_id = r_date.replace("-", "")
        race_id = f"{date_id}01"
        
        url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/LocalResults.aspx?RaceDate={date_str}&RaceNo=1"
        try:
            resp = session.get(url, timeout=10)
            if resp.status_code == 200 and "沒有相關賽事" not in resp.text:
                soup = BeautifulSoup(resp.content, "html.parser")
                page_text = soup.get_text()
                
                m_going = re.search(r'場地狀況\s*[:：]\s*([^\s<]+)', page_text)
                going = m_going.group(1) if m_going else ""
                
                m_track = re.search(r'賽道\s*[:：]\s*([^\n<]+)', page_text)
                track_info = m_track.group(1).strip() if m_track else ""
                
                m_pene = re.search(r'壓地儀讀數\s*[:：]\s*(\d+\.?\d*)', page_text)
                pene = float(m_pene.group(1)) if m_pene else None
                
                c.execute("""
                INSERT OR REPLACE INTO track_environment_detail (race_id, race_date, track_info, going, penetrometer_reading)
                VALUES (?, ?, ?, ?, ?)
                """, (race_id, r_date, track_info, going, pene))
                track_count += 1
                
        except Exception:
            pass
            
        time.sleep(random.uniform(1.0, 1.5))
        if idx % 50 == 0:
            conn.commit()
            print(f"  --> 進度: {idx}/{len(distinct_dates)} 賽日處理完成...")

    conn.commit()
    conn.close()
    print(f"\n[🎉 恭喜！全量終極數據庫補齊大圓滿！]")
    print(f"已記錄 {track_count} 個賽日之跑道環境，所有維度全部到位！")

if __name__ == "__main__":
    main()
