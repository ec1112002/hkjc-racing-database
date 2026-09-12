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
    c.execute("""
    CREATE TABLE IF NOT EXISTS horse_ratings_history (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        horse_code TEXT,
        horse_name TEXT,
        race_id TEXT,
        race_date TEXT,
        pre_race_rating INTEGER,
        rating_change INTEGER,
        post_race_rating INTEGER,
        remarks TEXT
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS stewards_incidents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        race_id TEXT,
        race_date TEXT,
        race_no INTEGER,
        horse_no INTEGER,
        horse_name TEXT,
        horse_code TEXT,
        incident_report TEXT,
        is_checked_hampered INTEGER DEFAULT 0,
        is_held_up INTEGER DEFAULT 0,
        is_slow_to_begin INTEGER DEFAULT 0,
        is_wide_without_cover INTEGER DEFAULT 0,
        is_trachea_mucus_or_lame INTEGER DEFAULT 0,
        is_stewards_inquiry INTEGER DEFAULT 0
    )""")
    conn.commit()

def extract_incident_tags(text):
    if not text:
        return 0, 0, 0, 0, 0, 0
    t = text.lower()
    is_checked = 1 if any(w in t for w in ['受擠迫', '受阻', '勒避', '碰撞', '被帶出', '失位', 'crowded', 'hampered', 'checked']) else 0
    is_held_up = 1 if any(w in t for w in ['無路可上', '未能望空', '受困', '難以望空', 'held up', 'unable to obtain clear running']) else 0
    is_slow = 1 if any(w in t for w in ['起步笨拙', '出閘緩慢', '出閘笨拙', '躍出時', '慢閘', 'slow to begin', 'jumped awkwardly', 'slow to muster']) else 0
    is_wide = 1 if any(w in t for w in ['走大外疊', '走外疊', '走第三疊', '多跑路程', '沒有遮擋', 'wide without cover', 'raced wide']) else 0
    is_lame = 1 if any(w in t for w in ['氣管多血', '跛足', '不良於行', '心律不正常', '流鼻血', 'blood in trachea', 'mucus', 'lame', 'irregular heart']) else 0
    is_inquiry = 1 if any(w in t for w in ['研訊', '抗議', '小組審查', 'inquiry', 'protest']) else 0
    return is_checked, is_held_up, is_slow, is_wide, is_lame, is_inquiry

def get_completed_races(conn):
    c = conn.cursor()
    c.execute("SELECT DISTINCT race_id FROM stewards_incidents")
    return set(row[0] for row in c.fetchall())

def fetch_race_details(race_id, race_date, race_no, conn):
    date_str = race_date.replace("-", "/")
    url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/LocalResults.aspx?RaceDate={date_str}&RaceNo={race_no}"
    
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code != 200:
            return False
        
        soup = BeautifulSoup(resp.content, "html.parser")
        incident_div = soup.find("div", class_="racingIncidents") or soup.find("div", id="incidentReport")
        incidents_found = False
        
        if incident_div:
            rows = incident_div.find_all("tr")
            for row in rows:
                cols = row.find_all("td")
                if len(cols) >= 3:
                    h_no_str = cols[0].get_text(strip=True)
                    h_name_code = cols.get_text(strip=True)
                    report_text = cols.get_text(strip=True)
                    
                    code_match = re.search(r'\(([A-Z0-9]+)\)', h_name_code)
                    h_code = code_match.group(1) if code_match else ""
                    h_name = re.sub(r'\(.*?\)', '', h_name_code).strip()
                    
                    try:
                        h_no = int(h_no_str)
                    except ValueError:
                        continue
                    
                    chk, held, slow, wide, lame, inq = extract_incident_tags(report_text)
                    
                    c = conn.cursor()
                    c.execute("""
                    INSERT INTO stewards_incidents (
                        race_id, race_date, race_no, horse_no, horse_name, horse_code,
                        incident_report, is_checked_hampered, is_held_up, is_slow_to_begin,
                        is_wide_without_cover, is_trachea_mucus_or_lame, is_stewards_inquiry
                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                    """, (race_id, race_date, race_no, h_no, h_name, h_code, report_text, chk, held, slow, wide, lame, inq))
                    incidents_found = True
        
        if not incidents_found:
            c = conn.cursor()
            c.execute("""
            INSERT INTO stewards_incidents (race_id, race_date, race_no, incident_report)
            VALUES (?, ?, ?, '本場無特別競賽事件報告')
            """, (race_id, race_date, race_no))
            
        conn.commit()
        return True
    except Exception as e:
        print(f"處理場次 {race_id} 發生錯誤: {e}")
        return False

def main():
    if not os.path.exists(DB_PATH):
        print(f"錯誤: 找不到資料庫檔案 {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    init_tables(conn)
    
    c = conn.cursor()
    c.execute("SELECT race_id, race_date, race_no FROM races_meta ORDER BY race_date ASC, race_no ASC")
    all_races = c.fetchall()
    
    completed = get_completed_races(conn)
    pending = [r for r in all_races if r[0] not in completed]
    
    print(f"總場次: {len(all_races)} | 已完成: {len(completed)} | 待補齊: {len(pending)}")
    
    count = 0
    for race_id, race_date, race_no in pending:
        success = fetch_race_details(race_id, race_date, race_no, conn)
        count += 1
        if success:
            print(f"[{count}/{len(pending)}] 成功補齊場次: {race_id} ({race_date} 第 {race_no} 場)")
        
        sleep_sec = round(random.uniform(1.5, 2.5), 2)
        time.sleep(sleep_sec)
        
    conn.close()
    print("本批次補齊任務順利完成！")

if __name__ == "__main__":
    main()
