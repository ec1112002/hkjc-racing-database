import os
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
    c.execute("""
        CREATE TABLE IF NOT EXISTS trackwork (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            horse_name TEXT,
            horse_code TEXT,
            trainer TEXT,
            work_date TEXT,
            work_type TEXT,
            track_location TEXT,
            details TEXT
        )
    """)
    conn.commit()
    conn.close()

def crawl_results_if_needed():
    if os.path.exists(DB_NAME) and os.path.getsize(DB_NAME) > 100000:
        print(f"[*] 偵測到已有完整 5 季歷史數據庫 {DB_NAME}，直接載入進行分析！")
        return

    print("[*] 資料庫不存在，開始抓取歷史賽果...")
    init_db()
    conn = sqlite3.connect(DB_NAME)
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

    session = requests.Session()
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
                    except Exception:
                        continue
                time.sleep(0.3)
            conn.commit()
        except Exception:
            continue
    conn.close()

def crawl_trackwork_signals():
    init_db()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    c.execute("SELECT count(*) FROM trackwork")
    existing_count = c.fetchone()[0]
    
    if existing_count > 300:
        print(f"[*] 晨操資料庫已有 {existing_count} 筆紀錄，直接進行交叉回測！")
        conn.close()
        return

    print("[*] 正在抓取馬會官方每日晨操紀錄（包含沙田奧運馬房沙地、從化登山跑道）...")
    session = requests.Session()
    
    sample_dates = []
    base_date = datetime.date(2026, 5, 1)
    end_date = datetime.date(2026, 8, 30)
    curr = base_date
    while curr <= end_date:
        sample_dates.append(curr.strftime("%d/%m/%Y"))
        curr += datetime.timedelta(days=2)

    saved_works = 0
    for d_str in sample_dates:
        tw_url = f"https://racing.hkjc.com/zh-hk/local/information/trackworkonedayresult?orderType=Trainer&OneDay={d_str}"
        try:
            resp = session.get(tw_url, headers=HEADERS, timeout=10)
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
                        
                        d_parts = d_str.split("/")
                        standard_date = f"{at(d_parts, 2)}/{at(d_parts, 1)}/{at(d_parts, 0)}"
                        
                        c.execute("""
                            INSERT INTO trackwork (horse_name, horse_code, trainer, work_date, work_type, track_location, details)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (h_name, h_code, trainer, standard_date, work_type, track_loc, details))
                        saved_works += 1
            time.sleep(0.2)
        except Exception:
            continue
            
    conn.commit()
    conn.close()
    print(f"[*] 晨操紀錄收錄完畢，共新增 {saved_works} 筆操練紀錄。")

def generate_interactive_dashboard():
    print("[*] 正在從資料庫運算練馬師特徵模型並生成網頁...")
    conn = sqlite3.connect(DB_NAME)
    df = pd.read_sql_query("SELECT * FROM race_results ORDER BY horse_code, race_date ASC", conn)

    if df.empty:
        print("[-] 賽果資料庫為空！")
        conn.close()
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
        count=('placing', 'count'),
        wins=('is_win', 'sum'),
        places=('is_place', 'sum'),
        total_bet=('placing', lambda x: len(x) * 10),
        total_payout=('win_payout', 'sum')
    ).reset_index()
    
    trainer_rest_stats['win_rate'] = (trainer_rest_stats['wins'] / trainer_rest_stats['count'] * 100).round(1)
    trainer_rest_stats['place_rate'] = (trainer_rest_stats['places'] / trainer_rest_stats['count'] * 100).round(1)
    trainer_rest_stats['roi'] = (trainer_rest_stats['total_payout'] / trainer_rest_stats['total_bet'] * 100).round(1)

    high_roi_patterns = trainer_rest_stats[
        (trainer_rest_stats['count'] >= 30) & 
        (trainer_rest_stats['roi'] > 100)
    ].sort_values(by='roi', ascending=False)

    olympic_query = """
    WITH matched AS (
        SELECT DISTINCT
            r.race_id, r.race_date, r.trainer, r.placing, r.win_odds
        FROM race_results r
        JOIN trackwork t ON (r.horse_name = t.horse_name OR (r.horse_code != '' AND r.horse_code = t.horse_code))
        WHERE t.track_location LIKE '%奧運%沙地%'
          AND julianday(replace(r.race_date, '/', '-')) - julianday(replace(t.work_date, '/', '-')) BETWEEN 1 AND 14
    )
    SELECT 
        trainer,
        COUNT(*) as total_runs,
        SUM(CASE WHEN placing = 1 THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN placing IN (1, 2, 3) THEN 1 ELSE 0 END) as places,
        ROUND(SUM(CASE WHEN placing = 1 THEN win_odds * 10 ELSE 0 END), 1) as total_payout,
        COUNT(*) * 10 as total_bet
    FROM matched
    GROUP BY trainer
    ORDER BY places DESC
    """
    df_olympic = pd.read_sql_query(olympic_query, conn)
    if not df_olympic.empty:
        df_olympic['win_rate'] = (df_olympic['wins'] / df_olympic['total_runs'] * 100).round(1)
        df_olympic['place_rate'] = (df_olympic['places'] / df_olympic['total_runs'] * 100).round(1)
        df_olympic['roi'] = (df_olympic['total_payout'] / df_olympic['total_bet'] * 100).round(1)

    conghua_query = """
    WITH matched AS (
        SELECT DISTINCT
            r.race_id, r.race_date, r.trainer, r.placing, r.win_odds
        FROM race_results r
        JOIN trackwork t ON (r.horse_name = t.horse_name OR (r.horse_code != '' AND r.horse_code = t.horse_code))
        WHERE (t.track_location LIKE '%從化%登山%' OR t.track_location LIKE '%從化%')
          AND julianday(replace(r.race_date, '/', '-')) - julianday(replace(t.work_date, '/', '-')) BETWEEN 1 AND 14
    )
    SELECT 
        trainer,
        COUNT(*) as total_runs,
        SUM(CASE WHEN placing = 1 THEN 1 ELSE 0 END) as wins,
        SUM(CASE WHEN placing IN (1, 2, 3) THEN 1 ELSE 0 END) as places,
        ROUND(SUM(CASE WHEN placing = 1 THEN win_odds * 10 ELSE 0 END), 1) as total_payout,
        COUNT(*) * 10 as total_bet
    FROM matched
    GROUP BY trainer
    ORDER BY places DESC
    """
    df_conghua = pd.read_sql_query(conghua_query, conn)
    if not df_conghua.empty:
        df_conghua['win_rate'] = (df_conghua['wins'] / df_conghua['total_runs'] * 100).round(1)
        df_conghua['place_rate'] = (df_conghua['places'] / df_conghua['total_runs'] * 100).round(1)
        df_conghua['roi'] = (df_conghua['total_payout'] / df_conghua['total_bet'] * 100).round(1)

    horse_trigger_query = """
    SELECT DISTINCT
        r.horse_name,
        r.trainer,
        CASE 
            WHEN t.track_location LIKE '%奧運%沙地%' THEN '奧運沙地踱步'
            ELSE '從化特訓'
        END as signal_type,
        r.race_date,
        r.placing,
        r.win_odds
    FROM race_results r
    JOIN trackwork t ON (r.horse_name = t.horse_name OR (r.horse_code != '' AND r.horse_code = t.horse_code))
    WHERE (t.track_location LIKE '%奧運%沙地%' OR t.track_location LIKE '%從化%登山%')
      AND julianday(replace(r.race_date, '/', '-')) - julianday(replace(t.work_date, '/', '-')) BETWEEN 1 AND 14
    ORDER BY r.race_date DESC, r.placing ASC
    LIMIT 30
    """
    df_triggered_horses = pd.read_sql_query(horse_trigger_query, conn)
    conn.close()

    with pd.ExcelWriter(REPORT_NAME, engine='openpyxl') as writer:
        high_roi_patterns.to_excel(writer, sheet_name='高勝算模式(ROI超100%)', index=False)
        trainer_rest_stats.to_excel(writer, sheet_name='出賽間隔完整明細', index=False)
        if not df_olympic.empty:
            df_olympic.to_excel(writer, sheet_name='奧運沙地戰績榜', index=False)
        if not df_conghua.empty:
            df_conghua.to_excel(writer, sheet_name='從化登山戰績榜', index=False)

    high_roi_rows = ""
    for _, r in high_roi_patterns.iterrows():
        high_roi_rows += f"""
        <tr>
            <td><strong>{r['trainer']}</strong></td>
            <td><span class="badge badge-pill">{r['rest_category']}</span></td>
            <td>{int(r['count'])}</td>
            <td>{int(r['wins'])}</td>
            <td>{r['win_rate']}%</td>
            <td>{r['place_rate']}%</td>
            <td>${int(r['total_payout']):,}</td>
            <td class="roi-positive">{r['roi']}%</td>
        </tr>
        """

    olympic_rows = ""
    if not df_olympic.empty:
        for _, r in df_olympic.iterrows():
            roi_class = "roi-positive" if r['roi'] > 100 else ""
            place_badge = "badge-success" if r['place_rate'] >= 50 else "badge-pill"
            olympic_rows += f"""
            <tr>
                <td><strong>{r['trainer']}</strong></td>
                <td>{int(r['total_runs'])}</td>
                <td>{int(r['wins'])}</td>
                <td><span class="badge {place_badge}">{int(r['places'])}</span></td>
                <td>{r['win_rate']}%</td>
                <td><strong>{r['place_rate']}%</strong></td>
                <td class="{roi_class}">{r['roi']}%</td>
            </tr>
            """
    else:
        olympic_rows = "<tr><td colspan='7' style='text-align:center; color: var(--text-muted);'>正在載入中，請重新整理</td></tr>"

    conghua_rows = ""
    if not df_conghua.empty:
        for _, r in df_conghua.iterrows():
            roi_class = "roi-positive" if r['roi'] > 100 else ""
            place_badge = "badge-success" if r['place_rate'] >= 50 else "badge-pill"
            conghua_rows += f"""
            <tr>
                <td><strong>{r['trainer']}</strong></td>
                <td>{int(r['total_runs'])}</td>
                <td>{int(r['wins'])}</td>
                <td><span class="badge {place_badge}">{int(r['places'])}</span></td>
                <td>{r['win_rate']}%</td>
                <td><strong>{r['place_rate']}%</strong></td>
                <td class="{roi_class}">{r['roi']}%</td>
            </tr>
            """
    else:
        conghua_rows = "<tr><td colspan='7' style='text-align:center; color: var(--text-muted);'>正在載入中，請重新整理</td></tr>"

    horse_rows = ""
    if not df_triggered_horses.empty:
        for _, r in df_triggered_horses.iterrows():
            placing_str = f"第 {int(r['placing'])} 名" if r['placing'] > 0 else "待開跑"
            badge_class = "badge-success" if r['placing'] in (1, 2, 3) else "badge-pill"
            horse_rows += f"""
            <tr>
                <td><strong>{r['horse_name']}</strong></td>
                <td>{r['trainer']}</td>
                <td><span class="badge badge-gold">{r['signal_type']}</span></td>
                <td>{r['race_date']}</td>
                <td><span class="badge {badge_class}">{placing_str}</span></td>
                <td>{r['win_odds']} 倍</td>
            </tr>
            """
    else:
        horse_rows = "<tr><td colspan='6' style='text-align:center; color: var(--text-muted);'>暫無賽前觸發紀錄</td></tr>"

    trainers_json = trainer_rest_stats[['trainer', 'rest_category', 'count', 'wins', 'places', 'win_rate', 'place_rate', 'roi']].to_dict('records')
    total_runs_formatted = f"{len(df):,}"
    trainers_count = len(df['trainer'].unique())
    high_roi_count = len(high_roi_patterns)

    html_content = f"""<!DOCTYPE html>
<html lang="zh-HK">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>香港賽馬・練馬師出擊特徵與訓練模型儀表板</title>
    <style>
        :root {{
            --primary: #0f3460;
            --primary-dark: #16213e;
            --accent: #e94560;
            --bg: #f8f9fa;
            --card-
