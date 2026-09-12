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
    is_slow = 1 if any(w in t for w in ['起步笨拙', '出閘緩慢', '出閘笨拙', '躍出時', '慢閘', 'slow to begin', 'jumped awkwardly']) else 0
    is_wide = 1 if any(w in t for w in ['走大外疊', '走外疊', '走第三疊', '多跑路程', '沒有遮擋', 'wide without cover', 'raced wide']) else 0
    is_lame = 1 if any(w in t for w in ['氣管多血', '跛足', '不良於行', '心律不正常', '流鼻血', 'blood in trachea', 'mucus', 'lame']) else 0
    is_inquiry = 1 if any(w in t for w in ['研訊', '抗議', '小組審查', 'inquiry', 'protest']) else 0
    return is_checked, is_held_up, is_slow, is_wide, is_lame, is_inquiry

def get_completed_dates(conn):
    c = conn.cursor()
    c.execute("SELECT DISTINCT race_date FROM stewards_incidents")
    return set(row[0] for row in c.fetchall())

def main():
    conn = sqlite3.connect(DB_PATH)
    init_tables(conn)
    completed_dates = get_completed_dates(conn)
    
    # 產生 2021 至 2026 年賽季的常規賽馬日（星期三、六、日）
    current_date = datetime.date(2021, 9, 1)
    end_date = datetime.date.today()
    target_weekdays = (2, 5, 6)
    
    candidate_dates = []
    while current_date <= end_date:
        if current_date.weekday() in target_weekdays:
            if not (current_date.month == 7 and current_date.day > 16) and current_date.month != 8:
                candidate_dates.append(current_date.strftime("%Y-%m-%d"))
        current_date += datetime.timedelta(days=1)
        
    pending_dates = [d for d in candidate_dates if d not in completed_dates]
    print(f"總待檢查賽日: {len(pending_dates)} 天 (已完成: {len(completed_dates)} 天)")
    
    session = requests.Session()
    session.headers.update(HEADERS)
    
    total_meetings = 0
    total_incidents = 0
    
    for idx, race_date in enumerate(pending_dates, 1):
        date_str = race_date.replace("-", "/")
        date_id = race_date.replace("-", "")
        
        # 1. 先探測第 1 場：檢查當天是否有賽事
        test_url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/LocalResults.aspx?RaceDate={date_str}&RaceNo=1"
        try:
            resp = session.get(test_url, timeout=10)
            if resp.status_code != 200 or "沒有相關賽事" in resp.text:
                continue
            
            soup = BeautifulSoup(resp.content, "html.parser")
            # 尋找當天最大場次（通常 8 至 11 場）
            max_race = 11
            race_nav = soup.find_all("a", href=lambda h: h and "RaceNo=" in h)
            if race_nav:
                nums = [int(a.text.strip()) for a in race_nav if a.text.strip().isdigit()]
                if nums:
                    max_race = max(nums)
                    
            total_meetings += 1
            print(f"[{idx}/{len(pending_dates)}] 發現賽事日: {race_date} (共 {max_race} 場)")
            
            # 2. 僅抓取實際存在的場次
            for race_no in range(1, max_race + 1):
                race_id = f"{date_id}{race_no:02d}"
                race_url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/LocalResults.aspx?RaceDate={date_str}&RaceNo={race_no}"
                
                try:
                    r_resp = session.get(race_url, timeout=10)
                    r_soup = BeautifulSoup(r_resp.content, "html.parser")
                    incident_div = r_soup.find("div", class_="racingIncidents") or r_soup.find("div", id="incidentReport")
                    
                    c = conn.cursor()
                    if incident_div:
                        for row in incident_div.find_all("tr"):
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
                                c.execute("""
                                INSERT INTO stewards_incidents (
                                    race_id, race_date, race_no, horse_no, horse_name, horse_code,
                                    incident_report, is_checked_hampered, is_held_up, is_slow_to_begin,
                                    is_wide_without_cover, is_trachea_mucus_or_lame, is_stewards_inquiry
                                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                """, (race_id, race_date, race_no, h_no, h_name, h_code, report_text, chk, held, slow, wide, lame, inq))
                                total_incidents += 1
                                
                    # 記錄該場次已處理
                    c.execute("INSERT INTO stewards_incidents (race_id, race_date, race_no, incident_report) VALUES (?, ?, ?, '已處理')", (race_id, race_date, race_no))
                    conn.commit()
                except Exception as ex:
                    print(f"  場次 {race_id} 略過: {ex}")
                
                time.sleep(random.uniform(0.6, 1.0))
                
        except Exception as e:
            print(f"賽日 {race_date} 探測失敗: {e}")
            
        time.sleep(random.uniform(0.8, 1.2))
        
    conn.close()
    print(f"補齊大功告成！累計處理 {total_meetings} 個賽馬日，擷取 {total_incidents} 筆受阻報告。")

if __name__ == "__main__":
    main()
