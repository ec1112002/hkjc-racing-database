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
        horse_no INTEGER,
        horse_code TEXT,
        horse_name TEXT,
        jockey TEXT,
        actual_weight REAL,
        declared_weight REAL,
        jockey_allowance INTEGER DEFAULT 0
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_gear_horse ON gear_changes_history(horse_code)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_weights_race ON horse_weights_allowance(race_id)")
    conn.commit()

def parse_gear_changes(gear_str):
    """解析配備變更代碼：如 B1/TT, CP-/H2 等"""
    if not gear_str or gear_str == '-' or gear_str == '--':
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

def get_completed_dates(conn):
    c = conn.cursor()
    c.execute("SELECT DISTINCT race_date FROM horse_weights_allowance")
    return set(row[0] for row in c.fetchall())

def main():
    conn = sqlite3.connect(DB_PATH)
    init_tables(conn)
    completed_dates = get_completed_dates(conn)
    
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
    print(f"總待處理賽日: {len(pending_dates)} 天 (已完成: {len(completed_dates)} 天)")
    
    session = requests.Session()
    session.headers.update(HEADERS)
    
    total_meetings = 0
    total_records = 0
    
    for idx, race_date in enumerate(pending_dates, 1):
        date_str = race_date.replace("-", "/")
        date_id = race_date.replace("-", "")
        
        # 1. 探測第 1 場是否為有效賽馬日
        test_url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/LocalResults.aspx?RaceDate={date_str}&RaceNo=1"
        try:
            resp = session.get(test_url, timeout=10)
            if resp.status_code != 200 or "沒有相關賽事" in resp.text:
                continue
            
            soup = BeautifulSoup(resp.content, "html.parser")
            max_race = 11
            race_nav = soup.find_all("a", href=lambda h: h and "RaceNo=" in h)
            if race_nav:
                nums = [int(a.text.strip()) for a in race_nav if a.text.strip().isdigit()]
                if nums:
                    max_race = max(nums)
                    
            total_meetings += 1
            print(f"[{idx}/{len(pending_dates)}] 處理賽日: {race_date} (共 {max_race} 場)")
            
            for race_no in range(1, max_race + 1):
                race_id = f"{date_id}{race_no:02d}"
                
                # 抓取賽果頁（取得實際負磅、排位體重、騎師讓磅）
                res_url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/LocalResults.aspx?RaceDate={date_str}&RaceNo={race_no}"
                # 抓取排位表（取得最完整官方配備異動代碼）
                card_url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/RaceCard.aspx?RaceDate={date_str}&RaceNo={race_no}"
                
                # A. 提取排位表的配備資訊
                gear_map = {}
                try:
                    c_resp = session.get(card_url, timeout=10)
                    c_soup = BeautifulSoup(c_resp.content, "html.parser")
                    card_table = c_soup.find("table", class_="table_bd") or c_soup.find("table", class_="f_tac")
                    if card_table:
                        for row in card_table.find_all("tr")[1:]:
                            tds = row.find_all("td")
                            if len(tds) >= 10:
                                h_name_code = tds.get_text(strip=True) if len(tds) > 2 else ""
                                m = re.search(r'\(([A-Z0-9]+)\)', h_name_code)
                                if m:
                                    h_code = m.group(1)
                                    raw_gear = tds[-1].get_text(strip=True)
                                    gear_map[h_code] = raw_gear
                except Exception:
                    pass
                
                # B. 提取賽果頁的體重與負磅資訊
                try:
                    r_resp = session.get(res_url, timeout=10)
                    r_soup = BeautifulSoup(r_resp.content, "html.parser")
                    res_table = r_soup.find("table", class_="table_bd") or r_soup.find("table", class_="f_tac")
                    
                    if res_table:
                        c = conn.cursor()
                        for row in res_table.find_all("tr")[1:]:
                            tds = row.find_all("td")
                            if len(tds) >= 8:
                                try:
                                    h_no_str = tds.get_text(strip=True)
                                    h_no = int(h_no_str) if h_no_str.isdigit() else 0
                                    
                                    h_name_code = tds.get_text(strip=True)
                                    m = re.search(r'\(([A-Z0-9]+)\)', h_name_code)
                                    h_code = m.group(1) if m else ""
                                    h_name = re.sub(r'\(.*?\)', '', h_name_code).strip()
                                    
                                    jockey_raw = tds.get_text(strip=True)
                                    allow_m = re.search(r'\(-?(\d+)\)', jockey_raw)
                                    allowance = int(allow_m.group(1)) if allow_m else 0
                                    jockey = re.sub(r'\(.*?\)', '', jockey_raw).strip()
                                    
                                    act_wt_str = tds.get_text(strip=True)
                                    act_wt = float(act_wt_str) if act_wt_str.replace('.', '', 1).isdigit() else 0.0
                                    
                                    dec_wt_str = tds.get_text(strip=True)
                                    dec_wt = float(dec_wt_str) if dec_wt_str.replace('.', '', 1).isdigit() else 0.0
                                    
                                    # 寫入體重與讓磅表
                                    c.execute("""
                                    INSERT INTO horse_weights_allowance (
                                        race_id, race_date, race_no, horse_no, horse_code, horse_name,
                                        jockey, actual_weight, declared_weight, jockey_allowance
                                    ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                                    """, (race_id, race_date, race_no, h_no, h_code, h_name, jockey, act_wt, dec_wt, allowance))
                                    
                                    # 寫入配備變更表
                                    raw_gear = gear_map.get(h_code, "")
                                    cur_g, f_g, rem_g, re_g = parse_gear_changes(raw_gear)
                                    if raw_gear or cur_g:
                                        c.execute("""
                                        INSERT INTO gear_changes_history (
                                            horse_code, horse_name, race_id, race_date, current_gear,
                                            gear_changes, first_time_gear, removed_gear, reapplied_gear
                                        ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                                        """, (h_code, h_name, race_id, race_date, cur_g, raw_gear, f_g, rem_g, re_g))
                                        
                                    total_records += 1
                                except Exception:
                                    continue
                        conn.commit()
                except Exception as ex:
                    print(f"  場次 {race_id} 處理異常: {ex}")
                    
                time.sleep(random.uniform(0.6, 1.0))
                
        except Exception as e:
            print(f"賽日 {race_date} 探測失敗: {e}")
            
        time.sleep(random.uniform(0.8, 1.2))
        
    conn.close()
    print(f"第二階段補齊圓滿完成！累計更新 {total_meetings} 個賽事日，新增 {total_records} 筆配備與體重紀錄。")

if __name__ == "__main__":
    main()
