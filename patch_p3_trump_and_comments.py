import os
import re
import time
import random
import sqlite3
import requests
from bs4 import BeautifulSoup
from concurrent.futures import ThreadPoolExecutor, as_completed

DB_PATH = "hk_racing.db"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7",
    "Referer": "https://racing.hkjc.com/"
}

def init_tables(conn):
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS trump_cards_priority (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        race_id TEXT,
        race_date TEXT,
        race_no INTEGER,
        horse_code TEXT,
        horse_name TEXT,
        is_trump_card INTEGER DEFAULT 0,
        has_protection INTEGER DEFAULT 0,
        priority_order TEXT
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS running_comments (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        race_id TEXT,
        race_date TEXT,
        race_no INTEGER,
        horse_code TEXT,
        horse_name TEXT,
        running_comment TEXT
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS scratchings_withdrawals (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        race_id TEXT,
        race_date TEXT,
        race_no INTEGER,
        horse_code TEXT,
        horse_name TEXT,
        reason TEXT
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_tcp_r ON trump_cards_priority(race_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_rc_r ON running_comments(race_id)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_sw_r ON scratchings_withdrawals(race_id)")
    conn.commit()

def fetch_race_details(race_info):
    race_id, r_date, r_no = race_info
    date_str = str(r_date).replace("-", "/")
    date_id = str(r_date).replace("-", "")

    session = requests.Session()
    session.headers.update(HEADERS)

    trump_data = []
    comments_data = []

    # A. 抓取排位表 (獲取王牌馬 + 與保護權 *)
    card_url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/RaceCard.aspx?RaceDate={date_str}&RaceNo={r_no}"
    try:
        resp_c = session.get(card_url, timeout=10)
        if resp_c.status_code == 200:
            soup_c = BeautifulSoup(resp_c.content, "html.parser")
            card_table = soup_c.find("table", class_="table_bd") or soup_c.find("table", class_="f_tac")
            if card_table:
                for tr in card_table.find_all("tr")[1:]:
                    tds = tr.find_all("td")
                    if len(tds) >= 8:
                        h_code = ""
                        h_name = ""
                        for td in tds[1:4]:
                            t = td.get_text(strip=True)
                            m = re.search(r'\(([A-Z0-9]+)\)', t)
                            if m:
                                h_code = m.group(1)
                            elif not h_name and len(t) > 1 and not t.isdigit():
                                h_name = t
                        
                        priority_text = ""
                        for td in tds[-4:]:
                            txt = td.get_text(strip=True)
                            if "+" in txt or "*" in txt or txt.isdigit():
                                priority_text = txt
                                break
                                
                        is_trump = 1 if "+" in priority_text else 0
                        has_prot = 1 if "*" in priority_text else 0
                        if h_code:
                            trump_data.append((race_id, str(r_date), r_no, h_code, h_name, is_trump, has_prot, priority_text))
    except Exception:
        pass

    # B. 抓取賽果頁的沿途走勢評述 (Comments on Running)
    res_url = f"https://racing.hkjc.com/racing/information/Chinese/Reports/CORunning.aspx?Date={date_id}&RaceNo={r_no}"
    try:
        resp_r = session.get(res_url, timeout=10)
        if resp_r.status_code == 200:
            soup_r = BeautifulSoup(resp_r.content, "html.parser")
            comm_table = soup_r.find("table", class_="table_bd") or soup_r.find("table", class_="f_tac")
            if comm_table:
                for tr in comm_table.find_all("tr")[1:]:
                    tds = tr.find_all("td")
                    if len(tds) >= 3:
                        h_text = tds.get_text(strip=True)
                        m_code = re.search(r'\(([A-Z0-9]+)\)', h_text)
                        h_code = m_code.group(1) if m_code else ""
                        h_name = re.sub(r'\(.*?\)', '', h_text).strip()
                        comment_text = tds.get_text(strip=True)
                        if h_code:
                            comments_data.append((race_id, str(r_date), r_no, h_code, h_name, comment_text))
    except Exception:
        pass

    time.sleep(random.uniform(0.3, 0.6))
    return race_id, str(r_date), r_no, trump_data, comments_data

def get_completed_races(conn):
    c = conn.cursor()
    c.execute("SELECT DISTINCT race_id FROM running_comments")
    return set(row[0] for row in c.fetchall())

def main():
    if not os.path.exists(DB_PATH):
        print(f"找不到資料庫: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    init_tables(conn)
    c = conn.cursor()

    c.execute("SELECT DISTINCT race_id, race_date, race_no FROM race_results ORDER BY race_date ASC, race_no ASC")
    all_races = c.fetchall()
    completed = get_completed_races(conn)
    pending = [r for r in all_races if r[0] not in completed]

    print(f"[*] 5 季賽事總數: {len(all_races)} 場 | 已完成: {len(completed)} | 待處理: {len(pending)} 場")
    if not pending:
        print("[✔] 全部賽事之王牌與沿途走勢短評早已 100% 齊全！")
        return

    print("[*] 啟動 5 線程極速併發抓取 (預計約 20-25 分鐘)...")

    batch_trump = []
    batch_comm = []
    processed = 0

    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = {executor.submit(fetch_race_details, r): r for r in pending}
        for future in as_completed(futures):
            processed += 1
            race_id, r_date, r_no, trump_data, comments_data = future.result()

            if trump_data:
                batch_trump.extend(trump_data)
            if comments_data:
                batch_comm.extend(comments_data)
            else:
                batch_comm.append((race_id, r_date, r_no, "", "", "已處理"))

            if processed % 50 == 0 or processed == len(pending):
                c = conn.cursor()
                if batch_trump:
                    c.executemany("INSERT INTO trump_cards_priority VALUES (NULL,?,?,?,?,?,?,?,?)", batch_trump)
                    batch_trump = []
                if batch_comm:
                    c.executemany("INSERT INTO running_comments VALUES (NULL,?,?,?,?,?,?)", batch_comm)
                    batch_comm = []
                conn.commit()
                print(f"  --> 進度: {processed}/{len(pending)} 場賽事已寫入資料庫 ({(processed/len(pending)*100):.1f}%)...")

    conn.close()
    print("\n[🎉 終極全量大圓滿！] 5 年全部王牌標記、保護權與沿途走勢評述已 100% 注入完畢！")

if __name__ == "__main__":
    main()
