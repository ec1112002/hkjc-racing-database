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
    # 1. 自購馬來港前海外賽績表
    c.execute("""
    CREATE TABLE IF NOT EXISTS pp_overseas_form (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        horse_code TEXT,
        horse_name TEXT,
        former_name TEXT,
        country TEXT
    )""")
    # 2. 騎師停賽處罰與日程表
    c.execute("""
    CREATE TABLE IF NOT EXISTS jockey_suspensions (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        race_id TEXT,
        race_date TEXT,
        jockey_name TEXT,
        suspension_days TEXT,
        fine_amount TEXT,
        raw_verdict TEXT
    )""")
    # 3. 專項列管健康名單 (喘鳴症、喉部手術、流鼻血等)
    c.execute("""
    CREATE TABLE IF NOT EXISTS special_health_registers (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        horse_code TEXT,
        horse_name TEXT,
        record_date TEXT,
        health_type TEXT,
        condition_desc TEXT
    )""")
    # 4. 賽日更易與突發事項 (換騎師、超磅、閘前重裝蹄鐵)
    c.execute("""
    CREATE TABLE IF NOT EXISTS raceday_changes_incidents (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        race_id TEXT,
        race_date TEXT,
        race_no INTEGER,
        horse_name TEXT,
        change_type TEXT,
        details TEXT
    )""")
    c.execute("CREATE INDEX IF NOT EXISTS idx_pp_c ON pp_overseas_form(horse_code)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_js_j ON jockey_suspensions(jockey_name)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_shr_c ON special_health_registers(horse_code)")
    c.execute("CREATE INDEX IF NOT EXISTS idx_rci_r ON raceday_changes_incidents(race_id)")
    conn.commit()

def main():
    if not os.path.exists(DB_PATH):
        print(f"找不到資料庫: {DB_PATH}")
        return

    conn = sqlite3.connect(DB_PATH)
    init_tables(conn)
    c = conn.cursor()

    # ==========================================
    # 維度 1：爬取 5 季自購馬 (PP) 來港前海外原名與檔案
    # ==========================================
    print("[1/4] 正在抓取 5 季全量自購馬 (PP) 來港前海外名冊...")
    pp_urls = [
        "https://racing.hkjc.com/racing/chinese/racing-info/ppo_performance_current.asp",
        "https://racing.hkjc.com/racing/chinese/racing-info/24_ppo_performance.asp",
        "https://racing.hkjc.com/racing/chinese/racing-info/23_ppo_performance.asp",
        "https://racing.hkjc.com/racing/chinese/racing-info/22_ppo_performance.asp",
        "https://racing.hkjc.com/racing/chinese/racing-info/21_ppo_performance.asp"
    ]
    session = requests.Session()
    session.headers.update(HEADERS)
    pp_rows = []

    for url in pp_urls:
        try:
            resp = session.get(url, timeout=10)
            if resp.status_code == 200:
                soup = BeautifulSoup(resp.content, "html.parser")
                table = soup.find("table", class_="table_bd")
                if table:
                    for tr in table.find_all("tr")[1:]:
                        tds = tr.find_all("td")
                        if len(tds) >= 3:
                            code = tds[0].get_text(strip=True)
                            h_name = tds.get_text(strip=True)
                            former = tds.get_text(strip=True)
                            m_country = re.search(r'\(([A-Z]{2,3})\)', former)
                            country = m_country.group(1) if m_country else ""
                            if code:
                                pp_rows.append((code, h_name, former, country))
        except Exception:
            pass
        time.sleep(1.0)

    c.execute("DELETE FROM pp_overseas_form")
    c.executemany("INSERT INTO pp_overseas_form VALUES (NULL, ?, ?, ?, ?)", pp_rows)
    conn.commit()
    print(f"  [✔] 成功建立自購馬海外對照庫: {len(pp_rows)} 匹！")

    # ==========================================
    # 維度 2：從 5 年競賽事件報告中全量提煉 騎師停賽日程與處罰表
    # ==========================================
    print("[2/4] 正在從 5 年受薪董事報告中全量抽取「騎師停賽處罰表」...")
    c.execute("SELECT race_id, race_date, incident_report FROM stewards_incidents WHERE incident_report LIKE '%停賽%'")
    suspension_data = []
    for r_id, r_date, report in c.fetchall():
        m_name = re.search(r'騎師([^\s，,被由至]+)', report)
        m_days = re.search(r'停賽(\d+|[一二兩三四五六七八九十]+)個', report)
        m_fine = re.search(r'罰款([^\s，,。]+港?元)', report)
        j_name = m_name.group(1) if m_name else "未知騎師"
        s_days = m_days.group(1) if m_days else ""
        f_amt = m_fine.group(1) if m_fine else ""
        suspension_data.append((r_id, r_date, j_name, s_days, f_amt, report))

    c.execute("DELETE FROM jockey_suspensions")
    c.executemany("INSERT INTO jockey_suspensions VALUES (NULL, ?, ?, ?, ?, ?, ?)", suspension_data)
    conn.commit()
    print(f"  [✔] 成功建立騎師停賽處罰日程表: {len(suspension_data)} 筆！")

    # ==========================================
    # 維度 3：從獸醫與賽事報告全量抽取 專項列管健康名單 (喘鳴症/喉部手術/流鼻血)
    # ==========================================
    print("[3/4] 正在建立專項列管健康名單 (喉部手術、喘鳴症、流鼻血)...")
    c.execute("SELECT horse_code, horse_name, incident_date, condition_desc FROM veterinary_records")
    health_data = []
    for h_code, h_name, v_date, desc in c.fetchall():
        h_type = "常規傷患"
        if "喉部" in desc or "喘鳴" in desc: h_type = "喉部手術/喘鳴症"
        elif "血" in desc: h_type = "流鼻血/氣管多血"
        elif "心律" in desc: h_type = "心律不正常"
        elif "不良於行" in desc or "跛足" in desc: h_type = "不良於行/跛足"
        elif "懸韌帶" in desc or "筋腱" in desc: h_type = "筋腱/韌帶損傷"
        health_data.append((h_code, h_name, v_date, h_type, desc))

    c.execute("DELETE FROM special_health_registers")
    c.executemany("INSERT INTO special_health_registers VALUES (NULL, ?, ?, ?, ?, ?)", health_data)
    conn.commit()
    print(f"  [✔] 成功建立特殊健康列管庫: {len(health_data)} 筆！")

    # ==========================================
    # 維度 4：從賽事報告抽取 臨場突發事項 (換騎師/超磅/閘前重新裝蹄)
    # ==========================================
    print("[4/4] 正在建立賽日更易事項庫 (換騎師、超磅、閘前重釘蹄鐵)...")
    c.execute("SELECT race_id, race_date, race_no, horse_name, incident_report FROM stewards_incidents WHERE incident_report LIKE '%蹄鐵%' OR incident_report LIKE '%更換騎師%' OR incident_report LIKE '%超磅%'")
    changes_data = []
    for r_id, r_date, r_no, h_name, report in c.fetchall():
        c_type = "閘前裝蹄" if "蹄鐵" in report else ("更換騎師" if "更換騎師" in report else "超磅")
        changes_data.append((r_id, r_date, r_no, h_name, c_type, report))

    c.execute("DELETE FROM raceday_changes_incidents")
    c.executemany("INSERT INTO raceday_changes_incidents VALUES (NULL, ?, ?, ?, ?, ?, ?)", changes_data)
    conn.commit()
    print(f"  [✔] 成功建立臨場更易與突發庫: {len(changes_data)} 筆！")

    # 終極大盤點
    print("\n==================================================")
    print("💎 香港賽馬 19 大專項表【真正宇宙級全量】終極大盤點 💎")
    print("==================================================")
    tables = [
        "race_results", "sectionals", "dividends", "races_meta",
        "trackwork", "barrier_trials", "veterinary_records", "stewards_incidents",
        "horse_weights_allowance", "track_environment_detail",
        "stable_transfers", "conghua_movements", "horse_lifecycle_extended",
        "trump_cards_priority", "running_comments",
        "pp_overseas_form", "jockey_suspensions", "special_health_registers", "raceday_changes_incidents"
    ]
    for t in tables:
        try:
            c.execute(f"SELECT COUNT(*) FROM {t}")
            cnt = c.fetchone()[0]
            print(f"  ✔ {t.ljust(28)} : {cnt:,} 筆")
        except Exception:
            print(f"  ✖ {t.ljust(28)} : 資料表未建立")
    print("==================================================\n")

    conn.close()
    print("[🎉 宇宙級全量大圓滿！] 真正毫無保留的香港賽馬終極神庫誕生！")

if __name__ == "__main__":
    main()
