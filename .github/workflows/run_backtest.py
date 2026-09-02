import os
import time
import sqlite3
import datetime
import requests
import pandas as pd
from bs4 import BeautifulSoup

DB_NAME = "hk_racing.db"
REPORT_NAME = "trainer_backtest_report.xlsx"

START_YEAR = 2021
END_YEAR = 2026

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"
}

def at(seq, index):
    """安全取得列表指定位置之元素"""
    return seq[index]

def init_db():
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
        CREATE TABLE IF NOT EXISTS race_results (
            race_id TEXT PRIMARY KEY,
            race_date TEXT,
            race_no INTEGER,
            placing INTEGER,
            horse_code TEXT,
            horse_name TEXT,
            jockey TEXT,
            trainer TEXT,
            actual_weight REAL,
            draw INTEGER,
            finish_time TEXT,
            win_odds REAL
        )
    """)
    conn.commit()
    conn.close()

def get_race_dates(start_year, end_year):
    race_dates = []
    current_date = datetime.date(start_year, 9, 1)
    end_date = datetime.date.today()
    target_days = (2, 5, 6) # 星期三、六、日
    while current_date <= end_date:
        if current_date.weekday() in target_days:
            if not (current_date.month == 7 and current_date.day > 16) and current_date.month != 8:
                race_dates.append(current_date.strftime("%Y/%m/%d"))
        current_date += datetime.timedelta(days=1)
    return race_dates

def crawl_results():
    init_db()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    race_dates = get_race_dates(START_YEAR, END_YEAR)
    print(f"[*] 預計檢查賽日: {len(race_dates)} 個")
    session = requests.Session()
    total_saved = 0

    for r_date in race_dates:
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
            
            print(f"[+] 正在抓取賽日 {r_date} (共 {max_race} 場)...")
            for race_no in range(1, max_race + 1):
                race_url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/LocalResults.aspx?RaceDate={r_date}&RaceNo={race_no}"
                r_resp = session.get(race_url, headers=HEADERS, timeout=10)
                r_soup = BeautifulSoup(r_resp.content, "lxml")
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
                            parts = horse_code_name.split("(")
                            horse_name = at(parts, 0).strip()
                            horse_code = at(parts, 1).replace(")", "").strip()
                        else:
                            horse_name = horse_code_name.strip()
                            horse_code = ""
                        jockey = at(cols, 3)
                        trainer = at(cols, 4)
                        weight_str = at(cols, 5)
                        actual_weight = float(weight_str) if weight_str.replace(".", "").isdigit() else 0.0
                        draw_str = at(cols, 7)
                        draw = int(draw_str) if draw_str.isdigit() else 0
                        finish_time = at(cols, 10)
                        odds_str = at(cols, 11)
                        win_odds = float(odds_str) if odds_str.replace(".", "").isdigit() else 0.0
                        race_id = f"{r_date.replace('/', '')}_{race_no}_{horse_code or horse_name}"
                        c.execute("""
                            INSERT OR IGNORE INTO race_results 
                            (race_id, race_date, race_no, placing, horse_code, horse_name, jockey, trainer, actual_weight, draw, finish_time, win_odds)
                            VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (race_id, r_date, race_no, placing, horse_code, horse_name, jockey, trainer, actual_weight, draw, finish_time, win_odds))
                        total_saved += 1
                    except Exception:
                        continue
                time.sleep(0.3)
            conn.commit()
        except Exception as e:
            print(f"[-] 抓取 {r_date} 異常: {e}")
            continue
            
    conn.commit()
    conn.close()
    print(f"[*] 歷史數據抓取完畢，共收錄 {total_saved} 筆出賽記錄。")

def run_trainer_backtest():
    print("[*] 正在進行練馬師訓練模式回測分析...")
    conn = sqlite3.connect(DB_NAME)
    df = pd.read_sql_query("SELECT * FROM race_results ORDER BY horse_code, race_date ASC", conn)
    conn.close()
    if df.empty:
        print("[-] 資料庫為空。")
        return
    df['race_date'] = pd.to_datetime(df['race_date'])
    df['prev_race_date'] = df.groupby('horse_code')['race_date'].shift(1)
    df['days_rest'] = (df['race_date'] - df['prev_race_date']).dt.days

    def categorize_rest(days):
        if pd.isna(days):
            return "初出/首戰"
        elif days <= 14:
            return "急促連戰 (≤14天)"
        elif days <= 35:
            return "正常週期 (15-35天)"
        elif days <= 60:
            return "稍長休息 (36-60天)"
        else:
            return "久休復出 (>60天)"
            
    df['rest_category'] = df['days_rest'].apply(categorize_rest)
    df['is_win'] = (df['placing'] == 1).astype(int)
    top_three = (1, 2, 3)
    df['is_place'] = (df['placing'].isin(top_three)).astype(int)
    df['win_payout'] = df['is_win'] * (df['win_odds'] * 10)
    
    trainer_rest_stats = df.groupby(['trainer', 'rest_category']).agg(
        出賽次數=('placing', 'count'),
        頭馬數=('is_win', 'sum'),
        上名數=('is_place', 'sum'),
        總投注額=('placing', lambda x: len(x) * 10),
        總派彩=('win_payout', 'sum')
    ).reset_index()
    
    trainer_rest_stats['勝率(%)'] = (trainer_rest_stats['頭馬數'] / trainer_rest_stats['出賽次數'] * 100).round(1)
    trainer_rest_stats['上名率(%)'] = (trainer_rest_stats['上名數'] / trainer_rest_stats['出賽次數'] * 100).round(1)
    trainer_rest_stats['獨贏ROI(%)'] = (trainer_rest_stats['總派彩'] / trainer_rest_stats['總投注額'] * 100).round(1)

    high_roi_patterns = trainer_rest_stats[
        (trainer_rest_stats['出賽次數'] >= 30) & 
        (trainer_rest_stats['獨贏ROI(%)'] > 100)
    ].sort_values(by='獨贏ROI(%)', ascending=False)
    
    trainer_overall = df.groupby('trainer').agg(
        總出賽=('placing', 'count'),
        總頭馬=('is_win', 'sum'),
        總上名=('is_place', 'sum'),
        平均賠率=('win_odds', 'mean'),
        總投注額=('placing', lambda x: len(x) * 10),
        總派彩=('win_payout', 'sum')
    ).reset_index()
    
    trainer_overall['勝率(%)'] = (trainer_overall['總頭馬'] / trainer_overall['總出賽'] * 100).round(1)
    trainer_overall['上名率(%)'] = (trainer_overall['總上名'] / trainer_overall['總出賽'] * 100).round(1)
    trainer_overall['獨贏ROI(%)'] = (trainer_overall['總派彩'] / trainer_overall['總投注額'] * 100).round(1)
    trainer_overall = trainer_overall.sort_values(by='總頭馬', ascending=False)
    
    print(f"[*] 正在匯出報表至 {REPORT_NAME}...")
    with pd.ExcelWriter(REPORT_NAME, engine='openpyxl') as writer:
        trainer_overall.to_excel(writer, sheet_name='5季練馬師總榜', index=False)
        high_roi_patterns.to_excel(writer, sheet_name='高勝算訓練模式(ROI超100%)', index=False)
        trainer_rest_stats.to_excel(writer, sheet_name='出賽間隔完整明細', index=False)
    print(f"[✓] 回測成功！檔案已輸出。")

if __name__ == "__main__":
    crawl_results()
    run_trainer_backtest()
