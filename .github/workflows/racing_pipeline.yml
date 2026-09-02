name: HKJC 5-Season Crawl & Trainer Backtest

on:
  workflow_dispatch:
  schedule:
    - cron: '0 16 * * 3,7'

jobs:
  run-pipeline:
    runs-on: ubuntu-latest
    permissions:
      contents: write

    steps:
      - name: 下載專案檔案
        uses: actions/checkout@v4

      - name: 設定 Python 環境
        uses: actions/setup-python@v5
        with:
          python-version: '3.11'

      - name: 安裝所需套件
        run: |
          pip install requests beautifulsoup4 pandas openpyxl lxml

      - name: 執行 5 季歷史抓取與練馬師回測
        run: |
          python .github/workflows/run_backtest.py

      - name: 上傳回測報表與資料庫
        uses: actions/upload-artifact@v4
        with:
          name: 賽馬5季練馬師訓練回測結果
          path: |
            trainer_backtest_report.xlsx
            hk_racing.db
          retention-days: 30
