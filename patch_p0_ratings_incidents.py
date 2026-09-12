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
    CREATE TABLE IF NOT EXISTS races_meta (
        race_id TEXT PRIMARY KEY,
        race_date TEXT,
        race_no INTEGER,
        venue TEXT,
        course_type TEXT,
        track TEXT,
        distance INTEGER,
        race_class TEXT,
        race_name TEXT,
        going TEXT,
        prize_money TEXT,
        rating_band TEXT
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

def get_race_list(conn):
    """智慧獲取賽事清單：無論資料庫是否有現成表格，均保證能順利獲取賽事"""
    c = conn.cursor()
    
    # 1. 優先檢查 races_meta
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='races_meta'")
    if c.fetchone():
        c.execute("SELECT DISTINCT race_id, race_date, race_no FROM races_meta ORDER BY race_date ASC, race_no ASC")
        rows = c.fetchall()
        if rows:
            return rows

    # 2. 次選檢查 race_results
    c.execute("SELECT name FROM sqlite_master WHERE type='table' AND name='race_results'")
    if c.fetchone():
        c.execute("SELECT DISTINCT race_id, race_date, race_no FROM race_results ORDER BY race_date ASC, race_no ASC")
        rows = c.fetchall()
        if rows:
            return rows

    # 3. 若為新庫/空庫，自動生成 2021-2026 的標準賽事日排程
    print("[*] 正在自動生成 2021 至 2026 賽事日期排程...")
    current_date = datetime.date(2021, 9, 1)
    end_date = datetime.date.today()
    target_weekdays = (2, 5, 6) # 香港常規賽日：星期三、六、日
    
    generated_races = []
    while current_date <= end_date:
        if current_date.weekday() in target_weekdays:
            # 避開 7 月中旬至 8 月的馬季歇暑期
            if not (current_date.month == 7 and current_date.day > 16) and current_date.month != 8:
                date_str = current_date.strftime("%Y-%m-%d")
                date_id = current_date.strftime("%Y%m%d")
                for rno in range(1, 12): # 每賽日常規最多 11 場
                    generated_races.append((f"{date_id}{rno:02d}", date_str, rno))
        current_date += datetime.timedelta(days=1)
        
    return generated_races

def fetch_race_details(race_id, race_date, race_no, conn):
    date_str = race_date.replace("-", "/")
    url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/LocalResults.aspx?RaceDate={date_str}&RaceNo={race_no}"
    
    try:
        resp = requests.get(url, headers=HEADERS, timeout=12)
        if resp.status_code != 200 or "沒有相關賽事" in resp.text:
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
        
        # 標記該場次已完成，避免重複檢查
        c = conn.cursor()
        c.execute("""
        INSERT INTO stewards_incidents (race_id, race_date, race_no, incident_report)
        VALUES (?, ?, ?, ?)
        """, (race_id, race_date, race_no, '處理完成' if incidents_found else '本場無特別報告'))
            
        conn.commit()
        return incidents_found
    except Exception as e:
        print(f"處理場次 {race_id} 發生錯誤: {e}")
        return False

def main():
    conn = sqlite3.connect(DB_PATH)
    init_tables(conn)
    
    all_races = get_race_list(conn)
    completed = get_completed_races(conn)
    pending = [r for r in all_races if r[0] not in completed]
    
    print(f"賽事規劃總數: {len(all_races)} | 已處理: {len(completed)} | 待處理: {len(pending)}")
    
    count = 0
    success_count = 0
    for race_id, race_date, race_no in pending:
        success = fetch_race_details(race_id, race_date, race_no, conn)
        count += 1
        if success:
            success_count += 1
            print(f"[{count}/{len(pending)}] 成功採集競賽報告: {race_id} ({race_date} 第 {race_no} 場)")
        
        time.sleep(random.uniform(1.2, 2.0))
        
        # 每處理 100 場自動輸出一次狀態
        if count % 100 == 0:
            print(f"--> 進度報告: 已檢索 {count} 場，成功擷取 {success_count} 筆事件報告")
            
    conn.close()
    print("本批次補齊任務順利完成！")

if __name__ == "__main__":
    main()
