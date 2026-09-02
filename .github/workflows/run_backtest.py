import os
import time
import sqlite3
import datetime
import requests
import pandas as pd
from bs4 import BeautifulSoup

DB_NAME = "hk_racing.db"
REPORT_NAME = "trainer_backtest_report.xlsx"

# 設定爬取目標：對上 5 個馬季 (以過去 5 個年度為範圍)
START_YEAR = 2021
END_YEAR = 2026

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"
}

def init_db():
    """初始化 SQLite 本地賽馬資料庫"""
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("""
    CREATE TABLE IF NOT EXISTS race_results (
        race_id TEXT PRIMARY KEY,
        race_date TEXT,
        race_no INTEGER,
        course TEXT,
        distance INTEGER,
        track_condition TEXT,
        race_class TEXT,
        placing INTEGER,
        horse_code TEXT,
        horse_name TEXT,
        jockey TEXT,
        trainer TEXT,
        actual_weight REAL,
        draw INTEGER,
        finish_time TEXT,
        win_odds REAL,
        place_odds REAL
    )
    """)
    conn.commit()
    conn.close()

def get_race_dates(start_year, end_year):
    """獲取過去 5 季已完成的賽日列表"""
    print(f"[*] 正在準備 {start_year} 至 {end_year} 賽事清單...")
    race_dates = []
    # 建立日期輪廓，涵蓋 5 季主要賽期（每年 9 月至翌年 7 月）
    current_date = datetime.date(start_year, 9, 1)
    end_date = datetime.date.today()
    
    while current_date <= end_date:
        # 香港賽馬主要在星期三 (Wednesday=2) 及星期日 (Sunday=6)，偶有星期六 (Saturday=5)
        if current_date.weekday() in:
            # 避開 7 月中至 8 月底歇暑期間
            if not (current_date.month == 7 and current_date.day > 16) and current_date.month != 8:
                race_dates.append(current_date.strftime("%Y/%m/%d"))
        current_date += datetime.timedelta(days=1)
    return race_dates

def crawl_results():
    """從馬會爬取賽事數據並寫入資料庫"""
    init_db()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    
    race_dates = get_race_dates(START_YEAR, END_YEAR)
    print(f"[*] 預計檢查賽日數量: {len(race_dates)} 個")
    
    session = requests.Session()
    total_saved = 0

    for r_date in race_dates:
        # 先以第 1 場測試該日是否有開賽
        test_url = f"https://racing.hkjc.com/racing/information/Chinese/Racing/LocalResults.aspx?RaceDate={r_date}&RaceNo=1"
        try:
            resp = session.get(test_url, headers=HEADERS, timeout=12)
            if "沒有相關賽事資料" in resp.text or resp.status_code != 200:
                continue
            
            # 若有賽事，遍歷該日 1 至 11 場
            soup = BeautifulSoup(resp.content, "lxml")
            
            # 取得該日總場數
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
                
                # 尋找賽果主表格
                res_table = r_soup.find("table", class_="f_tac")
                if not res_table:
                    continue
                
                rows = res_table.find_all("tr")[1:]
                for row in rows:
                    cols = [td.text.strip() for td in row.find_all("td")]
                    if len(cols) < 12:
                        continue
                    
                    try:
                        placing_raw = cols[0]
                        if not placing_raw.isdigit():
                            continue
                        placing = int(placing_raw)
                        
                        horse_code_name = cols
                        # 拆分馬名與烙號，例如 "金鑽貴人 (G180)"
                        horse_name = horse_code_name.split("(")[0].strip()
                        horse_code = horse_code_name.split("(").replace(")", "").strip() if "(" in horse_code_name else ""
                        
                        jockey = cols
                        trainer = cols[4]
                        actual_weight = float(cols[5]) if cols[5].replace(".", "").isdigit() else 0.0
                        draw = int(cols[7]) if cols[7].isdigit() else 0
                        finish_time = cols[10]
                        win_odds = float(cols[11]) if cols[11].replace(".", "").isdigit() else 0.0
                        
                        race_id = f"{r_date.replace('/', '')}_{race_no}_{horse_code or horse_name}"
                        
                        c.execute("""
                        INSERT OR IGNORE INTO race_results 
                        (race_id, race_date, race_no, course, distance, track_condition, race_class, placing, horse_code, horse_name, jockey, trainer, actual_weight, draw, finish_time, win_odds, place_odds)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
                        """, (race_id, r_date, race_no, "", 0, "", "", placing, horse_code, horse_name, jockey, trainer, actual_weight, draw, finish_time, win_odds, 0.0))
                        total_saved += 1
                    except Exception:
                        continue
                
                time.sleep(0.3) # 避免對馬會伺服器造成壓力
            conn.commit()
            
        except Exception as e:
            print(f"[-] 抓取 {r_date} 發生異常: {e}")
            continue
            
    conn.commit()
    conn.close()
    print(f"[*] 歷史數據抓取完畢，共收錄 {total_saved} 筆出賽記錄。")

def run_trainer_backtest():
    """針對 5 季大數據進行練馬師訓練特徵回測"""
    print("[*] 正在進行練馬師訓練模式回測分析...")
    conn = sqlite3.connect(DB_NAME)
    
    df = pd.read_sql_query("SELECT * FROM race_results ORDER BY horse_code, race_date ASC", conn)
    conn.close()
    
    if df.empty:
        print("[-] 數據庫為空，無法執行回測。")
        return
    
    # 轉換日期格式
    df['race_date'] = pd.to_datetime(df['race_date'])
    
    # 1. 計算「出賽間隔（Rest Days）」
    df['prev_race_date'] = df.groupby('horse_code')['race_date'].shift(1)
    df['days_rest'] = (df['race_date'] - df['prev_race_date']).dt.days
    
    # 定義出賽間隔類型
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
    df['is_place'] = (df['placing'].isin()).astype(int)
    
    # 計算獨贏派彩與回報
    df['win_payout'] = df['is_win'] * (df['win_odds'] * 10)
    
    # 2. 總體練馬師出賽間隔統計
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
    
    # 3. 找出練馬師「高回報特定訓練模式」（出賽至少 30 次，ROI > 100%）
    high_roi_patterns = trainer_rest_stats[
        (trainer_rest_stats['出賽次數'] >= 30) & 
        (trainer_rest_stats['獨贏ROI(%)'] > 100)
    ].sort_values(by='獨贏ROI(%)', ascending=False)
    
    # 4. 全體練馬師 5 季總排行榜
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
    
    # 5. 匯出 Excel 報表
    print(f"[*] 正在匯出回測報表至 {REPORT_NAME}...")
    with pd.ExcelWriter(REPORT_NAME, engine='openpyxl') as writer:
        trainer_overall.to_excel(writer, sheet_name='5季練馬師總榜', index=False)
        high_roi_patterns.to_excel(writer, sheet_name='高勝算訓練模式(ROI超100%)', index=False)
        trainer_rest_stats.to_excel(writer, sheet_name='出賽間隔完整明細', index=False)
        
    print(f"[✓] 回測完成！檔案已保存為 {REPORT_NAME}")

if __name__ == "__main__":
    crawl_results()
    run_trainer_backtest()
