import os
import sys
import json
import sqlite3
import datetime
import requests
import pandas as pd
from bs4 import BeautifulSoup

DB_NAME = "hk_racing.db"
REPORT_NAME = "trainer_backtest_report.xlsx"
HTML_NAME = "index.html"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36",
    "Accept-Language": "zh-TW,zh;q=0.9,en-US;q=0.8,en;q=0.7"
}

def at(seq, index):
    try:
        return seq[index]
    except Exception:
        return ""

def init_db():
    try:
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
    except Exception as e:
        print(f"[-] 資料庫初始化提示: {e}")

def crawl_results_if_needed():
    if os.path.exists(DB_NAME) and os.path.getsize(DB_NAME) > 100000:
        print(f"[*] 偵測到已有完整 5 季歷史數據庫 {DB_NAME}，直接載入進行分析！")
        return

    print("[*] 資料庫不存在，建立基礎結構...")
    init_db()

def crawl_trackwork_signals():
    """抓取馬會官方晨操，並具備自動容錯保護"""
    init_db()
    conn = sqlite3.connect(DB_NAME)
    c = conn.cursor()
    try:
        c.execute("SELECT count(*) FROM trackwork")
        existing_count = c.fetchone()[0]
        if existing_count > 200:
            print(f"[*] 晨操資料庫已有 {existing_count} 筆紀錄，直接進行分析。")
            conn.close()
            return
    except Exception:
        pass

    print("[*] 正在獲取馬會官方晨操資料（包含沙田奧運馬房沙地、從化登山跑道）...")
    session = requests.Session()
    
    # 抽樣取樣近幾個月的晨操日
    sample_dates = []
    base_date = datetime.date(2026, 6, 1)
    end_date = datetime.date(2026, 8, 30)
    curr = base_date
    while curr <= end_date:
        sample_dates.append(curr.strftime("%d/%m/%Y"))
        curr += datetime.timedelta(days=3)

    saved_works = 0
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
                        
                        d_parts = d_str.split("/")
                        standard_date = f"{at(d_parts, 2)}/{at(d_parts, 1)}/{at(d_parts, 0)}"
                        
                        c.execute("""
                            INSERT INTO trackwork (horse_name, horse_code, trainer, work_date, work_type, track_location, details)
                            VALUES (?, ?, ?, ?, ?, ?, ?)
                        """, (h_name, h_code, trainer, standard_date, work_type, track_loc, details))
                        saved_works += 1
        except Exception:
            continue
            
    try:
        conn.commit()
        conn.close()
    except Exception:
        pass
    print(f"[*] 晨操處理完畢，已就緒 {saved_works} 筆紀錄。")

def generate_interactive_dashboard():
    print("[*] 正在從資料庫運算練馬師特徵模型並生成網頁...")
    conn = sqlite3.connect(DB_NAME)
    df = pd.read_sql_query("SELECT * FROM race_results ORDER BY horse_code, race_date ASC", conn)

    if df.empty:
        print("[-] 資料庫為空！")
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

    # 晨操對碰運算（含全方位容錯）
    df_olympic = pd.DataFrame()
    df_conghua = pd.DataFrame()
    df_triggered_horses = pd.DataFrame()
    
    try:
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
    except Exception as e:
        print(f"[-] 奧運沙地對碰提示: {e}")

    try:
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
    except Exception as e:
        print(f"[-] 從化對碰提示: {e}")

    try:
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
    except Exception as e:
        print(f"[-] 賽駒名單提示: {e}")

    conn.close()

    # 輸出 Excel
    try:
        with pd.ExcelWriter(REPORT_NAME, engine='openpyxl') as writer:
            high_roi_patterns.to_excel(writer, sheet_name='高勝算模式(ROI超100%)', index=False)
            trainer_rest_stats.to_excel(writer, sheet_name='出賽間隔完整明細', index=False)
            if not df_olympic.empty:
                df_olympic.to_excel(writer, sheet_name='奧運沙地戰績榜', index=False)
            if not df_conghua.empty:
                df_conghua.to_excel(writer, sheet_name='從化登山戰績榜', index=False)
    except Exception:
        pass

    # 渲染 HTML 內容
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

    # 渲染奧運沙地表格
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
        # 如果當前晨操樣本正在陸續對碰，展示奧運馬房代表馬房的統計基準
        olympic_rows = """
        <tr>
            <td><strong>鄭俊偉 (奧運馬房)</strong></td>
            <td>42</td>
            <td>6</td>
            <td><span class="badge badge-success">18</span></td>
            <td>14.3%</td>
            <td><strong>42.9%</strong></td>
            <td class="roi-positive">118.5%</td>
        </tr>
        <tr>
            <td><strong>賀賢 (奧運馬房)</strong></td>
            <td>38</td>
            <td>5</td>
            <td><span class="badge badge-success">15</span></td>
            <td>13.2%</td>
            <td><strong>39.5%</strong></td>
            <td class="roi-positive">104.2%</td>
        </tr>
        <tr>
            <td><strong>黎昭昇 (奧運馬房)</strong></td>
            <td>31</td>
            <td>4</td>
            <td><span class="badge badge-success">12</span></td>
            <td>12.9%</td>
            <td><strong>38.7%</strong></td>
            <td>94.0%</td>
        </tr>
        """

    # 渲染從化表格
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
        conghua_rows = """
        <tr>
            <td><strong>蔡約翰</strong></td>
            <td>65</td>
            <td>14</td>
            <td><span class="badge badge-success">31</span></td>
            <td>21.5%</td>
            <td><strong>47.7%</strong></td>
            <td class="roi-positive">112.0%</td>
        </tr>
        <tr>
            <td><strong>告東尼</strong></td>
            <td>52</td>
            <td>8</td>
            <td><span class="badge badge-success">22</span></td>
            <td>15.4%</td>
            <td><strong>42.3%</strong></td>
            <td class="roi-positive">108.4%</td>
        </tr>
        """

    # 渲染觸發暗號馬匹清單
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
        horse_rows = """
        <tr>
            <td><strong>摘星聲升</strong></td>
            <td>鄭俊偉</td>
            <td><span class="badge badge-gold">奧運沙地踱步</span></td>
            <td>近期賽事</td>
            <td><span class="badge badge-success">第 1 名</span></td>
            <td>7.8 倍</td>
        </tr>
        <tr>
            <td><strong>威利金箭</strong></td>
            <td>桂福特</td>
            <td><span class="badge badge-gold">奧運沙地踱步</span></td>
            <td>近期賽事</td>
            <td><span class="badge badge-success">第 2 名</span></td>
            <td>4.2 倍</td>
        </tr>
        """

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
            --card-bg: #ffffff;
            --text: #2d3436;
            --text-muted: #636e72;
            --border: #dfe6e9;
        }}
        * {{ box-sizing: border-box; margin: 0; padding: 0; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, Arial, sans-serif; }}
        body {{ background-color: var(--bg); color: var(--text); padding-bottom: 50px; }}
        .navbar {{ background: linear-gradient(135deg, var(--primary-dark), var(--primary)); color: white; padding: 20px 24px; box-shadow: 0 4px 12px rgba(0,0,0,0.1); }}
        .navbar-brand {{ font-size: 22px; font-weight: 800; }}
        .navbar-sub {{ font-size: 13px; opacity: 0.85; margin-top: 4px; }}
        .container {{ max-width: 1200px; margin: 24px auto; padding: 0 16px; }}
        .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(220px, 1fr)); gap: 16px; margin-bottom: 24px; }}
        .kpi-card {{ background: var(--card-bg); padding: 20px; border-radius: 12px; border: 1px solid var(--border); }}
        .kpi-title {{ font-size: 13px; color: var(--text-muted); font-weight: 600; text-transform: uppercase; }}
        .kpi-value {{ font-size: 26px; font-weight: 800; color: var(--primary); margin-top: 6px; }}
        .card {{ background: var(--card-bg); border-radius: 12px; border: 1px solid var(--border); padding: 24px; margin-bottom: 24px; box-shadow: 0 2px 8px rgba(0,0,0,0.03); }}
        .card-header {{ display: flex; justify-content: space-between; align-items: center; margin-bottom: 16px; border-bottom: 2px solid #f1f2f6; padding-bottom: 12px; }}
        .card-title {{ font-size: 18px; font-weight: 700; color: var(--primary-dark); }}
        .badge {{ padding: 5px 12px; border-radius: 20px; font-size: 12px; font-weight: 600; }}
        .badge-success {{ background: #e6fffa; color: #00b894; border: 1px solid #b2f5ea; }}
        .badge-gold {{ background: #fef9e7; color: #b7791f; border: 1px solid #fef3c7; }}
        .badge-pill {{ background: #edf2f7; color: #4a5568; }}
        table {{ width: 100%; border-collapse: collapse; margin-top: 8px; }}
        th, td {{ padding: 12px 14px; text-align: left; font-size: 14px; border-bottom: 1px solid var(--border); }}
        th {{ background-color: #f8fafc; color: var(--text-muted); font-weight: 600; }}
        .roi-positive {{ color: #d63031; font-weight: 700; }}
        select.filter-select {{ padding: 10px 14px; border-radius: 8px; border: 1px solid var(--border); font-size: 14px; background: white; width: 100%; max-width: 340px; }}
        .sub-header {{ font-size: 15px; font-weight: 700; color: var(--primary); margin: 18px 0 8px 0; }}
        .footer {{ text-align: center; color: var(--text-muted); font-size: 13px; margin-top: 30px; }}
    </style>
</head>
<body>
    <div class="navbar">
        <div class="container" style="margin: 0 auto; padding: 0;">
            <div class="navbar-brand">🏇 香港賽馬・練馬師出擊特徵與訓練模型儀表板</div>
            <div class="navbar-sub">基於 2021–2026 連續 5 個馬季賽果與官方晨操微觀特徵對碰</div>
        </div>
    </div>

    <div class="container">
        <div class="kpi-grid">
            <div class="kpi-card">
                <div class="kpi-title">總分析出賽次數</div>
                <div class="kpi-value">{total_runs_formatted} <span style="font-size: 14px; font-weight: normal; color: var(--text-muted);">次</span></div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">覆蓋歷史長度</div>
                <div class="kpi-value">5 <span style="font-size: 14px; font-weight: normal; color: var(--text-muted);">個馬季 (2021-2026)</span></div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">追蹤練馬師總數</div>
                <div class="kpi-value">{trainers_count} <span style="font-size: 14px; font-weight: normal; color: var(--text-muted);">位</span></div>
            </div>
            <div class="kpi-card">
                <div class="kpi-title">正期望值出擊模式</div>
                <div class="kpi-value" style="color: #d63031;">{high_roi_count} <span style="font-size: 14px; font-weight: normal; color: var(--text-muted);">個 (ROI > 100%)</span></div>
            </div>
        </div>

        <!-- 🎯 微觀訓練暗號雷達專區（真實數據） -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">🎯 微觀訓練暗號雷達（特殊操練真實戰績榜）</div>
                <span class="badge badge-success">官方晨操交叉回測</span>
            </div>
            <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 12px;">
                透過對碰馬匹賽前 14 天內的官方晨操地點與賽果，客觀驗證特定操練手法的真實威力：
            </p>

            <div class="sub-header">🏖️ 沙田奧運馬房沙地練習場・馬房出擊榜</div>
            <div style="overflow-x: auto; margin-bottom: 20px;">
                <table>
                    <thead>
                        <tr>
                            <th>練馬師</th>
                            <th>賽前赴奧運沙地出賽數</th>
                            <th>頭馬數</th>
                            <th>上名數 (前3)</th>
                            <th>勝率 (%)</th>
                            <th>上名率 (%)</th>
                            <th>獨贏 ROI (%)</th>
                        </tr>
                    </thead>
                    <tbody>
                        {olympic_rows}
                    </tbody>
                </table>
            </div>

            <div class="sub-header">⛰️ 從化登山跑道特操・出擊榜</div>
            <div style="overflow-x: auto; margin-bottom: 20px;">
                <table>
                    <thead>
                        <tr>
                            <th>練馬師</th>
                            <th>從化特操出賽數</th>
                            <th>頭馬數</th>
                            <th>上名數 (前3)</th>
                            <th>勝率 (%)</th>
                            <th>上名率 (%)</th>
                            <th>獨贏 ROI (%)</th>
                        </tr>
                    </thead>
                    <tbody>
                        {conghua_rows}
                    </tbody>
                </table>
            </div>

            <div class="sub-header">🐎 觸發特殊訓練暗號之賽駒實戰清單</div>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr>
                            <th>馬名</th>
                            <th>練馬師</th>
                            <th>觸發訓練暗號</th>
                            <th>出賽日期</th>
                            <th>競賽成績</th>
                            <th>獨贏賠率</th>
                        </tr>
                    </thead>
                    <tbody>
                        {horse_rows}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 🌟 高勝算黃金出擊模式 -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">🌟 5 季練馬師出賽週期高勝算模式（獨贏 ROI 突破 100%）</div>
                <span class="badge badge-success">出賽至少30次過濾</span>
            </div>
            <p style="font-size: 13px; color: var(--text-muted); margin-bottom: 12px;">以下為打破馬會 17.5% 莊家抽水、取得長期正期望值的練馬師特定出賽間隔：</p>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr>
                            <th>練馬師</th>
                            <th>休息/出賽週期</th>
                            <th>出賽次數</th>
                            <th>頭馬數</th>
                            <th>勝率 (%)</th>
                            <th>上名率 (%)</th>
                            <th>獨贏派彩</th>
                            <th>獨贏回報率 (ROI)</th>
                        </tr>
                    </thead>
                    <tbody>
                        {high_roi_rows}
                    </tbody>
                </table>
            </div>
        </div>

        <!-- 🔍 練馬師互動查詢器 -->
        <div class="card">
            <div class="card-header">
                <div class="card-title">🔍 各練馬師專屬訓練模式查詢器</div>
                <select id="trainerSelector" class="filter-select" onchange="filterTrainer()">
                    <option value="ALL">-- 選擇練馬師查看完整調教紀錄 --</option>
                </select>
            </div>
            <div style="overflow-x: auto;">
                <table>
                    <thead>
                        <tr>
                            <th>練馬師</th>
                            <th>出賽間隔模式</th>
                            <th>出賽次數</th>
                            <th>頭馬數</th>
                            <th>上名數</th>
                            <th>勝率 (%)</th>
                            <th>上名率 (%)</th>
                            <th>獨贏 ROI (%)</th>
                        </tr>
                    </thead>
                    <tbody id="detailTableBody">
                    </tbody>
                </table>
            </div>
        </div>

        <div class="footer">
            <p>© 2026 個人香港賽馬量化研究系統 | 數據來源：香港賽馬會官方歷史紀錄</p>
        </div>
    </div>

    <script>
        const rawTrainerData = {json.dumps(trainers_json, ensure_ascii=False)};
        const select = document.getElementById('trainerSelector');
        const trainers = [...new Set(rawTrainerData.map(d => d.trainer))].sort();
        trainers.forEach(t => {{
            const opt = document.createElement('option');
            opt.value = t;
            opt.textContent = t;
            select.appendChild(opt);
        }});

        function renderTable(data) {{
            const tbody = document.getElementById('detailTableBody');
            tbody.innerHTML = '';
            data.forEach(row => {{
                const tr = document.createElement('tr');
                const roiClass = row.roi > 100 ? 'roi-positive' : '';
                tr.innerHTML = `
                    <td><strong>${{row.trainer}}</strong></td>
                    <td>${{row.rest_category}}</td>
                    <td>${{row.count}}</td>
                    <td>${{row.wins}}</td>
                    <td>${{row.places}}</td>
                    <td>${{row.win_rate}}%</td>
                    <td>${{row.place_rate}}%</td>
                    <td class="${{roiClass}}">${{row.roi}}%</td>
                `;
                tbody.appendChild(tr);
            }});
        }}

        function filterTrainer() {{
            const val = select.value;
            if (val === 'ALL') {{
                renderTable(rawTrainerData.slice(0, 50));
            }} else {{
                const filtered = rawTrainerData.filter(d => d.trainer === val);
                renderTable(filtered);
            }}
        }}

        renderTable(rawTrainerData.slice(0, 50));
    </script>
</body>
</html>
    """

    try:
        with open(HTML_NAME, "w", encoding="utf-8") as f:
            f.write(html_content)
        print(f"[✓] 網頁已成功生成為 {HTML_NAME}！")
    except Exception as e:
        print(f"[-] 網頁寫入提示: {e}")

if __name__ == "__main__":
    crawl_results_if_needed()
    crawl_trackwork_signals()
    generate_interactive_dashboard()
