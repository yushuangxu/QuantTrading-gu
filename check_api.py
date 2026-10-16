#!/usr/bin/env python3
"""
检查Tushare rt_min接口返回的数据
读取tickers.txt中的股票代码，打印每个代码的实时分钟数据
"""

from datetime import datetime, timedelta
import pandas as pd
from dotenv import load_dotenv
import os
from main import TushareDataSource

def main():
    # 加载环境变量
    load_dotenv(".env")
    token = os.getenv("TUSHARE_TOKEN", "").strip()
    if not token:
        print("错误: TUSHARE_TOKEN 未设置")
        return

    min_freq = os.getenv("MIN_FREQ", "1MIN").strip().upper()
    print(f"当前配置: MIN_FREQ={min_freq}")

    # 读取tickers.txt
    tickers = []
    try:
        with open("tickers.txt", "r", encoding="utf-8") as f:
            for line in f:
                raw = line.strip()
                if raw and not raw.startswith("#"):
                    tickers.append(raw)
    except FileNotFoundError:
        print("错误: tickers.txt 文件不存在")
        return

    if not tickers:
        print("错误: tickers.txt 中无有效股票代码")
        return

    print(f"读取到 {len(tickers)} 个股票代码: {tickers}")

    # 创建数据源
    data_source = TushareDataSource(token)

    # 加载历史数据
    for ts_code in tickers:
        data_source.load_history(ts_code, min_freq)

    now = datetime.now()
    print(f"\n当前时间: {now.strftime('%Y-%m-%d %H:%M:%S')}")
    print("=" * 80)

    for ts_code in tickers:
        print(f"\n检查股票: {ts_code}")
        try:
            df = data_source.fetch_rt_min(ts_code=ts_code, freq=min_freq)
            if df.empty:
                print("  返回数据: 空")
                continue

            print(f"  返回数据行数: {len(df)}")
            last_row = df.iloc[-1]
            bar_time_str = str(last_row['time'])
            print(f"  最新BAR时间: {bar_time_str}")

            # 解析时间并计算时间差
            try:
                bar_time = datetime.strptime(bar_time_str, '%Y-%m-%d %H:%M:%S')
                time_diff = now - bar_time
                print(f"  时间差: {time_diff}")

                if time_diff > timedelta(minutes=5):
                    print("  ⚠️  警告: 数据可能过时 (>5分钟)")
                else:
                    print("  ✅ 数据看起来新鲜")
            except ValueError:
                print("  ⚠️  时间格式解析失败")

            # 打印最新一行数据
            print("  最新数据详情:")
            print(f"    时间: {last_row['time']}")
            print(f"    开盘: {last_row['open']:.3f}")
            print(f"    最高: {last_row['high']:.3f}")
            print(f"    最低: {last_row['low']:.3f}")
            print(f"    收盘: {last_row['close']:.3f}")
            print(f"    成交量: {last_row['vol']}")
            print(f"    成交额: {last_row['amount']}")

        except Exception as e:
            print(f"  错误: {e}")

    print("\n" + "=" * 80)
    print("检查完成")

if __name__ == "__main__":
    main()