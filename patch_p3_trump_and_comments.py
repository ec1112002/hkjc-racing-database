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
    # 1. 練馬師王牌與出賽優先次序表
    c.execute("""
    CREATE TABLE IF NOT EXISTS trump_cards_priority (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        race_id TEXT,
        race_date TEXT,
        race_no INTEGER,
        horse_code TEXT,
        horse_name TEXT,
        is_trump_card INTEGER DEFAULT 0,    -- 1=王牌馬 (+)
        has_protection INTEGER DEFAULT 0,   -- 1=獲得保護權 (*)
        priority_order TEXT                 -- 優先次序文字
    )""")
    # 2. 全體馬匹官方沿途走勢評述表
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
    # 3. 賽前退出馬匹與原因表
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

    # 獲取 5 季全部賽事清單
    c.execute("SELECT DISTINCT race_id, race_date, race_no FROM race_results ORDER BY race_date ASC, race_no ASC")
    all_races = c.fetchall()
    completed = get_completed_races(conn)
    pending = [r for r in all_races if r[0] not in completed]

    print(f"[*] 5 季賽事總數: {len(all_races)} 場 | 已處理: {len(completed)} | 待處理: {len(pending)} 場")

    session = requests.Session()
    session.headers.update(HEADERS)

    count = 0
    for race_id, r_date, r_no in pending:
        count += 1
        date_str = str(r_date).replace("-", "/")
        date_id = str(r_date).replace("-", "")

        # A. 抓取排位表 (獲取王牌馬 + 與保護權 *)
        card_url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/RaceCard.aspx?RaceDate={date_str}&RaceNo={r_no}"
        trump_data = []
        try:
            resp_c = session.get(card_url, timeout=10)
            if resp_c.status_code == 200:
                soup_c = BeautifulSoup(resp_c.content, "html.parser")
                card_table = soup_c.find("table", class_="table_bd") or soup_c.find("table", class_="f_tac")
                if card_table:
                    for tr in card_table.find_all("tr")[1:]:
                        tds = tr.find_all("td")
                        if len(tds) >= 10:
                            h_text = tds.get_text(strip=True) if len(tds) > 2 else ""
                            m_code = re.search(r'\(([A-Z0-9]+)\)', h_text)
                            h_code = m_code.group(1) if m_code else ""
                            h_name = re.sub(r'\(.*?\)', '', h_text).strip()
                            
                            p_text = tds[-2].get_text(strip=True) if len(tds) >= 2 else ""
                            is_trump = 1 if "+" in p_text else 0
                            has_prot = 1 if "*" in p_text else 0
                            
                            if h_code:
                                trump_data.append((race_id, str(r_date), r_no, h_code, h_name, is_trump, has_prot, p_text))
        except Exception:
            pass

        # B. 抓取賽果頁的沿途走勢評述 (Comments on Running)
        res_url = f"https://racing.hkjc.com/racing/information/Chinese/Reports/CORunning.aspx?Date={date_id}&RaceNo={r_no}"
        comments_data = []
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

        # 寫入資料庫
        c = conn.cursor()
        if trump_data:
            c.executemany("INSERT INTO trump_cards_priority VALUES (NULL,?,?,?,?,?,?,?,?)", trump_data)
        if comments_data:
            c.executemany("INSERT INTO running_comments VALUES (NULL,?,?,?,?,?,?)", comments_data)
        else:
            # 若該場無獨立評述，留一筆標記防止重複爬取
            c.execute("INSERT INTO running_comments (race_id, race_date, race_no, running_comment) VALUES (?, ?, ?, '已處理')", (race_id, str(r_date), r_no))
            
        conn.commit()
        time.sleep(random.uniform(0.6, 1.0))

        if count % 50 == 0:
            print(f"  --> 進度: 已處理 {count}/{len(pending)} 場賽事之王牌與走勢短評...")

    conn.commit()
    conn.close()
    print("\n[🎉 終極大圓滿！] 5 年全部王牌標記與沿途走勢短評已全量注入資料庫！")

if __name__ == "__main__":
    main()
