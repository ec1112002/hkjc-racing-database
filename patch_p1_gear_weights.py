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
    CREATE TABLE IF NOT EXISTS track_environment_detail (
        race_id TEXT PRIMARY KEY,
        race_date TEXT,
        track_info TEXT,
        going TEXT,
        penetrometer_reading REAL
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_weights_r ON horse_weights_allowance(race_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_weights_h ON horse_weights_allowance(horse_code)")
    conn.commit()

def main():
    if not os.path.exists(DB_PATH):
        print(f"找不到資料庫: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    init_tables(conn)
    c = conn.cursor()

    # 1. 智慧探測 race_results 實際存在的欄位，避免報錯
    c.execute("PRAGMA table_info(race_results)")
    cols = {row for row in c.fetchall()}
    has_dec_wt = 'declared_weight' in cols

    print("[1/2] 正在提煉 5 年賽日負磅與見習騎師讓磅利益...")
    query = "SELECT race_id, race_date, race_no, horse_code, horse_name, jockey, actual_weight"
    if has_dec_wt:
        query += ", declared_weight"
    query += " FROM race_results ORDER BY horse_code ASC, race_date ASC, race_no ASC"

    c.execute(query)
    rows = c.fetchall()
    print(f"[*] 成功讀取 {len(rows)} 筆賽果，開始計算...")

    weights_data = []
    prev_horse = None
    prev_weight = None

    for r in rows:
        if has_dec_wt:
            race_id, r_date, r_no, h_code, h_name, jockey_str, act_wt, dec_wt = r
        else:
            race_id, r_date, r_no, h_code, h_name, jockey_str, act_wt = r
            dec_wt = 0.0
        
        if h_code != prev_horse:
            wt_change = 0.0
        else:
            wt_change = round(dec_wt - prev_weight, 1) if (dec_wt and prev_weight) else 0.0
            
        prev_horse = h_code
        prev_weight = dec_wt

        # 提取見習騎師受讓磅數 (Allowance)
        allow_m = re.search(r'\(-?(\d+)\)', str(jockey_str or ''))
        allowance = int(allow_m.group(1)) if allow_m else 0
        clean_jockey = re.sub(r'\(.*?\)', '', str(jockey_str or '')).strip()

        weights_data.append((race_id, r_date, r_no, h_code, h_name, clean_jockey, act_wt, dec_wt, wt_change, allowance))

    c.execute("DELETE FROM horse_weights_allowance")
    c.executemany("INSERT INTO horse_weights_allowance VALUES (NULL,?,?,?,?,?,?,?,?,?,?)", weights_data)
    conn.commit()
    print(f"  [✔] 成功寫入體重與讓磅表: {len(weights_data)} 筆！")

    # 2. 檢索 5 年賽日移欄跑道與壓地儀硬度讀數
    print("[2/2] 正在檢索 5 年賽馬日之移欄跑道與壓地儀讀數 (約 10-12 分鐘)...")
    c.execute("SELECT DISTINCT race_date FROM race_results ORDER BY race_date ASC")
    distinct_dates = [row[0] for row in c.fetchall()]
    
    session = requests.Session()
    session.headers.update(HEADERS)
    
    track_count = 0
    for idx, r_date in enumerate(distinct_dates, 1):
        date_str = str(r_date).replace("-", "/")
        date_id = str(r_date).replace("-", "")
        race_id = f"{date_id}01"
        
        url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/LocalResults.aspx?RaceDate={date_str}&RaceNo=1"
        try:
            resp = session.get(url, timeout=10)
            if resp.status_code == 200 and "沒有相關賽事" not in resp.text:
                soup = BeautifulSoup(resp.content, "html.parser")
                page_text = soup.get_text()
                
                m_going = re.search(r'場地狀況\s*[:：]\s*([^\s<]+)', page_text)
                going = m_going.group(1) if m_going else ""
                
                m_track = re.search(r'賽道\s*[:：]\s*([^<>\r\n]+)', page_text)
                track_info = m_track.group(1).strip() if m_track else ""
                
                m_pene = re.search(r'壓地儀讀數\s*[:：]\s*(\d+\.?\d*)', page_text)
                pene = float(m_pene.group(1)) if m_pene else None
                
                c.execute("""
                INSERT OR REPLACE INTO track_environment_detail (race_id, race_date, track_info, going, penetrometer_reading)
                VALUES (?, ?, ?, ?, ?)
                """, (race_id, str(r_date), track_info, going, pene))
                track_count += 1
        except Exception:
            pass
            
        time.sleep(random.uniform(1.0, 1.4))
        if idx % 50 == 0:
            conn.commit()
            print(f"  --> 進度: {idx}/{len(distinct_dates)} 賽日處理完成...")

    conn.commit()
    conn.close()
    print("\n[🎉 任務圓滿完成！]")
    print(f"成功記錄 {track_count} 個賽馬日之跑道環境！")

if __name__ == "__main__":
    main()
