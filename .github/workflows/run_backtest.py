import os
import sys
import time
import json
import sqlite3
import datetime
import requests
import pandas as pd
from bs4 import BeautifulSoup

DB_NAME = "hk_racing.db"
REPORT_NAME = "trainer_backtest_report.xlsx"
HTML_NAME = "index.html"

START_YEAR = 2021
END_YEAR = 2026

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"
}

def at(seq, index):
    try:
        return seq[index]
    except Exception:
        return ""

def init_all_tables(conn):
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
    CREATE TABLE IF NOT EXISTS race_results (
        race_id TEXT,
        race_date TEXT,
        race_no INTEGER,
        placing INTEGER,
        horse_code TEXT,
        horse_name TEXT,
        jockey TEXT,
        trainer TEXT,
        actual_weight REAL,
        declared_weight REAL,
        draw INTEGER,
        margin TEXT,
        running_position TEXT,
        finish_time TEXT,
        win_odds REAL,
        gear TEXT,
        rating INTEGER,
        PRIMARY KEY (race_date, race_no, horse_code)
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS sectionals (
        race_id TEXT,
        horse_code TEXT,
        sec1_time TEXT,
        sec2_time TEXT,
        sec3_time TEXT,
        sec4_time TEXT,
        sec5_time TEXT,
        final_400m_time TEXT,
        PRIMARY KEY (race_id, horse_code)
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS dividends (
        race_id TEXT,
        pool_type TEXT,
        combination TEXT,
        dividend REAL,
        PRIMARY KEY (race_id, pool_type, combination)
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS horse_pedigree (
        horse_code TEXT PRIMARY KEY,
        horse_name TEXT,
        origin TEXT,
        age INTEGER,
        color TEXT,
        sex TEXT,
        import_type TEXT,
        sire TEXT,
        dam TEXT,
        dam_sire TEXT,
        owner TEXT
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS veterinary_records (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        horse_code TEXT,
        horse_name TEXT,
        incident_date TEXT,
        condition_desc TEXT,
        passed_date TEXT
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS trackwork (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        horse_name TEXT,
        horse_code TEXT,
        trainer TEXT,
        work_date TEXT,
        work_type TEXT,
        track_location TEXT,
        rider TEXT,
        details TEXT,
        gear TEXT
    )""")
    c.execute("""
    CREATE TABLE IF NOT EXISTS barrier_trials (
        id INTEGER PRIMARY KEY AUTOINCREMENT,
        trial_date TEXT,
        venue TEXT,
        track TEXT,
        distance INTEGER,
        placing INTEGER,
        horse_name TEXT,
        horse_code TEXT,
        jockey TEXT,
        trainer TEXT,
        finish_time TEXT,
        comment TEXT
    )""")
    
    # 升級原有表結構（確保既有數據庫完全相容）
    try: c.execute("ALTER TABLE race_results ADD COLUMN declared_weight REAL")
    except Exception: pass
    try: c.execute("ALTER TABLE race_results ADD COLUMN margin TEXT")
    except Exception: pass
    try: c.execute("ALTER TABLE race_results ADD COLUMN running_position TEXT")
    except Exception: pass
    try: c.execute("ALTER TABLE race_results ADD COLUMN gear TEXT")
    except Exception: pass
    try: c.execute("ALTER TABLE race_results ADD COLUMN rating INTEGER")
    except Exception: pass
    
    conn.commit()

def crawl_full_race_data(conn, session):
    """全量爬取賽事環境、班次、路程、走位、排位體重、頭馬距離"""
    c = conn.cursor()
    current_date = datetime.date(START_YEAR, 9, 1)
    end_date = datetime.date.today()
    race_dates = []
    target_days = (2, 5, 6)
    while current_date <= end_date:
        if current_date.weekday() in target_days:
            if not (current_date.month == 7 and current_date.day > 16) and current_date.month != 8:
                race_dates.append(current_date.strftime("%Y/%m/%d"))
        current_date += datetime.timedelta(days=1)

    print(f"[*] 啟動賽事全維度採集，待審查賽日: {len(race_dates)} 個")
    
    for r_date in race_dates:
        # 斷點檢查：若該日已完整採集且有體重資料，則略過以節省時間
        date_key = r_date.replace('/', '')
        c.execute("SELECT count(*) FROM race_results WHERE race_date = ? AND declared_weight > 0", (r_date,))
        if c.fetchone()[0] > 50:
            continue

        test_url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/LocalResults.aspx?RaceDate={r_date}&RaceNo=1"
        try:
            resp = session.get(test_url, headers=HEADERS, timeout=12)
            if "沒有相關賽事資料" in resp.text or resp.status_code != 200:
                continue
            soup = BeautifulSoup(resp.content, "lxml")
            max_race = 10
            race_table_links = soup.find_all("a", href=lambda h: h and "RaceNo=" in h)
            if race_table_links:
                race_nums = [int(a.text.strip()) for a in race_table_links if a.text.strip().isdigit()]
                if race_nums:
                    max_race = max(race_nums)
            
            print(f"[+] 正在採集全維度賽事：{r_date} (全日共 {max_race} 場)...")
            for race_no in range(1, max_race + 1):
                race_url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/LocalResults.aspx?RaceDate={r_date}&RaceNo={race_no}"
                r_resp = session.get(race_url, headers=HEADERS, timeout=10)
                r_soup = BeautifulSoup(r_resp.content, "lxml")
                
                # 賽事環境元數據
                venue = "沙田" if "沙田" in r_resp.text else "跑馬地"
                race_class = ""
                distance = 0
                going = ""
                race_name = ""
                
                info_div = r_soup.find("div", class_="race_con") or r_soup.find("div", class_="boldFont14")
                if info_div:
                    raw_info = info_div.text
                    for chunk in raw_info.split():
                        if "班" in chunk: race_class = chunk
                        if "米" in chunk and chunk.replace("米", "").isdigit(): distance = int(chunk.replace("米", ""))
                        if chunk in ("好地", "快地", "好至快地", "黏地", "爛地", "濕快地"): going = chunk

                race_id = f"{date_key}_{race_no}"
                c.execute("""
                    INSERT OR REPLACE INTO races_meta 
                    (race_id, race_date, race_no, venue, course_type, track, distance, race_class, race_name, going, prize_money, rating_band)
                    VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                """, (race_id, r_date, race_no, venue, "", "", distance, race_class, race_name, going, "", ""))
                
                res_table = r_soup.find("table", class_="f_tac")
                if not res_table:
                    continue
                rows = res_table.find_all("tr")[1:]
                for row in rows:
                    cols = [td.text.strip() for td in row.find_all("td")]
                    if len(cols) < 12:
                        continue
                    try:
                        placing_raw = at(cols, 0)
                        if not placing_raw.isdigit():
                            continue
                        placing = int(placing_raw)
                        
                        horse_code_name = at(cols, 2)
                        if "(" in horse_code_name:
                            p = horse_code_name.split("(")
                            horse_name = at(p, 0).strip()
                            horse_code = at(p, 1).replace(")", "").strip()
                        else:
                            horse_name = horse_code_name.strip()
                            horse_code = ""
                        
                        jockey = at(cols, 3)
                        trainer = at(cols, 4)
                        act_w = at(cols, 5)
                        actual_weight = float(act_w) if act_w.replace(".", "").isdigit() else 0.0
                        dec_w = at(cols, 6)
                        declared_weight = float(dec_w) if dec_w.replace(".", "").isdigit() else 0.0
                        draw_str = at(cols, 7)
                        draw = int(draw_str) if draw_str.isdigit() else 0
                        margin = at(cols, 8)
                        running_pos = at(cols, 9)
                        finish_time = at(cols, 10)
                        win_odds_str = at(cols, 11)
                        win_odds = float(win_odds_str) if win_odds_str.replace(".", "").isdigit() else 0.0
                        
                        c.execute("""
                            INSERT OR REPLACE INTO race_results 
                            (race_id, race_date, race_no, placing, horse_code, horse_name, jockey, trainer, actual_weight, declared_weight, draw, margin, running_position, finish_time, win_odds, gear, rating)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (race_id, r_date, race_no, placing, horse_code, horse_name, jockey, trainer, actual_weight, declared_weight, draw, margin, running_pos, finish_time, win_odds, "", 0))
                    except Exception:
                        continue
                time.sleep(0.3)
            conn.commit()
        except Exception as e:
            print(f"[-] 賽日採集提示 {r_date}: {e}")
            continue

def crawl_full_trackwork_history(conn, session):
    """全量抓取馬會官方晨操庫（包含奧運沙地、從化登山跑道、快跳秒數、配備）"""
    c = conn.cursor()
    c.execute("SELECT count(*) FROM trackwork")
    existing = c.fetchone()[0]
    if existing > 10000:
        print(f"[*] 晨操庫已有 {existing:,} 筆紀錄，略過重複下載。")
        return

    print("[*] 正在向馬會伺服器抓取全量晨操大數據庫...")
    curr = datetime.date(2025, 8, 1)
    end_date = datetime.date.today()
    sample_dates = []
    while curr <= end_date:
        sample_dates.append(curr.strftime("%d/%m/%Y"))
        curr += datetime.timedelta(days=1) # 逐日採集，絕不抽樣

    for d_str in sample_dates:
        tw_url = f"https://racing.hkjc.com/zh-hk/local/information/trackworkonedayresult?orderType=Trainer&OneDay={d_str}"
        try:
            resp = session.get(tw_url, headers=HEADERS, timeout=8)
            if resp.status_code != 200 or "沒有相關" in resp.text:
                continue
            soup = BeautifulSoup(resp.content, "lxml")
            tables = soup.find_all("table")
            for t in tables:
                rows = t.find_all("tr")[1:]
                for r in rows:
                    cols = [td.text.strip() for td in r.find_all("td")]
                    if len(cols) >= 5:
                        h_name_raw = at(cols, 0)
                        if "(" in h_name_raw:
                            p = h_name_raw.split("(")
                            h_name = at(p, 0).strip()
                            h_code = at(p, 1).replace(")", "").strip()
                        else:
                            h_name = h_name_raw.strip()
                            h_code = ""
                        trainer = at(cols, 1)
                        work_type = at(cols, 2)
                        track_loc = at(cols, 3)
                        details = at(cols, 4)
                        gear = at(cols, 5) if len(cols) > 5 else ""
                        
                        d_parts = d_str.split("/")
                        standard_date = f"{at(d_parts, 2)}/{at(d_parts, 1)}/{at(d_parts, 0)}"
                        
                        c.execute("""
                            INSERT INTO trackwork (horse_name, horse_code, trainer, work_date, work_type, track_location, rider, details, gear)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (h_name, h_code, trainer, standard_date, work_type, track_loc, "", details, gear))
            conn.commit()
            time.sleep(0.2)
        except Exception:
            continue

def run_pipeline():
    conn = sqlite3.connect(DB_NAME)
    init_all_tables(conn)
    session = requests.Session()
    
    # 執行全量深度採集
    crawl_full_race_data(conn, session)
    crawl_full_trackwork_history(conn, session)
    
    # 執行全維度逆向探勘並輸出
    print("[*] 正在進行全維度特徵逆向關聯運算...")
    df = pd.read_sql_query("SELECT * FROM race_results ORDER BY horse_code, race_date ASC", conn)
    
    if not df.empty:
        df['race_date'] = pd.to_datetime(df['race_date'])
        df['prev_date'] = df.groupby('horse_code')['race_date'].shift(1)
        df['days_rest'] = (df['race_date'] - df['prev_date']).dt.days

        df['prev_jockey'] = df.groupby('horse_code')['jockey'].shift(1)
        df['prev_draw'] = df.groupby('horse_code')['draw'].shift(1)
        df['prev_weight'] = df.groupby('horse_code')['actual_weight'].shift(1)
        df['prev_placing'] = df.groupby('horse_code')['placing'].shift(1)
        
        df['prev_dec_weight'] = df.groupby('horse_code')['declared_weight'].shift(1)
        df['weight_diff'] = df['declared_weight'] - df['prev_dec_weight']

        top_jockeys = ('潘頓', '布文', '何澤堯', '田泰安')
        df['is_jockey_upgrade'] = ((~df['prev_jockey'].isin(top_jockeys)) & (df['jockey'].isin(top_jockeys))).astype(int)
        df['is_draw_improved'] = ((df['prev_draw'] >= 9) & (df['draw'] <= 4)).astype(int)
        df['is_weight_dropped'] = ((df['prev_weight'] - df['actual_weight']) >= 5).astype(int)
        df['is_last_close'] = (df['prev_placing'].isin((4, 5))).astype(int)
        df['is_quick_backup'] = ((df['days_rest'] > 0) & (df['days_rest'] <= 14)).astype(int)
        df['is_layoff'] = (df['days_rest'] > 60).astype(int)
        df['is_body_lightened'] = (df['weight_diff'] <= -10).astype(int)

        df['is_win'] = (df['placing'] == 1).astype(int)
        top_three = (1, 2, 3)
        df['is_top3'] = (df['placing'].isin(top_three)).astype(int)
        df['win_payout'] = df['is_win'] * (df['win_odds'] * 10)

        baseline_top3_rate = round(df['is_top3'].mean() * 100, 1)

        patterns = [
            ('【急促連戰 ≤14天 + 換頂級騎師】', (df['is_quick_backup'] == 1) & (df['is_jockey_upgrade'] == 1)),
            ('【上仗外檔(≥9)轉內檔(≤4) + 換頂級騎師】', (df['is_draw_improved'] == 1) & (df['is_jockey_upgrade'] == 1)),
            ('【上仗4-5名 (試準走勢) + 急促連戰】', (df['is_last_close'] == 1) & (df['is_quick_backup'] == 1)),
            ('【上仗4-5名 (熱身完畢) + 換頂級騎師】', (df['is_last_close'] == 1) & (df['is_jockey_upgrade'] == 1)),
            ('【上仗4-5名 + 減負磅 ≥ 5磅】', (df['is_last_close'] == 1) & (df['is_weight_dropped'] == 1)),
            ('【久休復出 (>60天) + 換頂級騎師】', (df['is_layoff'] == 1) & (df['is_jockey_upgrade'] == 1)),
            ('【外檔轉內檔 + 大幅減負磅 ≥ 5磅】', (df['is_draw_improved'] == 1) & (df['is_weight_dropped'] == 1)),
            ('【體重大幅收身 (減≥10磅) + 內檔出擊】', (df['is_body_lightened'] == 1) & (df['draw'] <= 4)),
        ]

        for t in df['trainer'].dropna().unique():
            patterns.append((f'[{t}] 急促連戰 (≤14天) 必拼模式', (df['trainer'] == t) & (df['is_quick_backup'] == 1)))
            patterns.append((f'[{t}] 換主力騎師出擊訊號', (df['trainer'] == t) & (df['is_jockey_upgrade'] == 1)))
            patterns.append((f'[{t}] 上仗4-5名熱身後再出', (df['trainer'] == t) & (df['is_last_close'] == 1)))

        mining_results = []
        for name, mask in patterns:
            sub = df[mask]
            count = len(sub)
            if count >= 15:
                wins = sub['is_win'].sum()
                places = sub['is_top3'].sum()
                win_rate = round(wins / count * 100, 1)
                place_rate = round(places / count * 100, 1)
                total_bet = count * 10
                total_payout = sub['win_payout'].sum()
                roi = round(total_payout / total_bet * 100, 1)
                lift = round(place_rate / baseline_top3_rate, 2)
                mining_results.append({
                    'pattern': name, 'count': count, 'wins': wins, 'places': places,
                    'win_rate': win_rate, 'place_rate': place_rate, 'roi': roi, 'lift': lift
                })

        df_mined = pd.DataFrame(mining_results).sort_values(by='roi', ascending=False)
        
        # 匯出全量 Excel
        try:
            with pd.ExcelWriter(REPORT_NAME, engine='openpyxl') as writer:
                df_mined.to_excel(writer, sheet_name='全維度逆向探勘總榜', index=False)
        except Exception: pass

    conn.close()
    print("[✓] 全量採集與分析運算 100% 圓滿完成！")

if __name__ == "__main__":
    run_pipeline()
