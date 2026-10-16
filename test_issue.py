import unittest
from datetime import datetime, timedelta
import pandas as pd
from main import TushareDataSource, KdjMacdStrategy
from dotenv import load_dotenv
import os

class TestQuantTradingIssue(unittest.TestCase):
    def setUp(self):
        # 加载环境变量
        load_dotenv(".env")
        token = os.getenv("TUSHARE_TOKEN", "").strip()
        if not token:
            self.skipTest("TUSHARE_TOKEN not set")
        self.data_source = TushareDataSource(token)
        self.strategy = KdjMacdStrategy()

    def test_fetch_rt_min_data_freshness(self):
        """测试 Tushare rt_min 接口是否返回最新数据"""
        ts_code = "601872.SH"  # 使用日志中的标的
        freq = "1MIN"

        df = self.data_source.fetch_rt_min(ts_code=ts_code, freq=freq)
        print(f"测试标的: {ts_code}, 频率: {freq}")
        print(f"返回数据行数: {len(df)}")
        if df.empty:
            self.fail("接口返回空数据")

        # 检查最新BAR的时间
        last_time_str = str(df.iloc[-1]['time'])
        print(f"最新BAR时间: {last_time_str}")

        # 尝试解析时间
        try:
            # 假设时间格式为 'YYYY-MM-DD HH:MM:SS'
            last_time = datetime.strptime(last_time_str, '%Y-%m-%d %H:%M:%S')
            now = datetime.now()
            time_diff = now - last_time
            print(f"时间差: {time_diff}")

            # 如果时间差超过1小时，认为数据不新鲜
            if time_diff > timedelta(hours=1):
                print("警告: 数据可能不新鲜")
            else:
                print("数据看起来新鲜")

        except ValueError as e:
            print(f"时间解析失败: {e}")

        # 打印前几行数据
        print("前5行数据:")
        print(df.head())

    def test_strategy_with_sample_data(self):
        """测试策略计算是否正常"""
        # 创建模拟数据
        sample_data = pd.DataFrame({
            'high': [100.1, 100.2, 100.3, 100.4, 100.5] * 10,  # 50行
            'low': [99.9, 99.8, 99.7, 99.6, 99.5] * 10,
            'close': [100.0, 100.1, 100.2, 100.3, 100.4] * 10
        })

        result = self.strategy.compute(sample_data)
        print(f"策略计算结果列: {list(result.columns)}")
        print(f"BUY信号数量: {result['BUY'].sum()}")
        print(f"SELL信号数量: {result['SELL'].sum()}")

        # 检查是否有指标列
        expected_columns = ['K', 'D', 'J', 'DIF', 'DEA', 'MACD', 'BUY', 'SELL']
        for col in expected_columns:
            self.assertIn(col, result.columns, f"缺少列: {col}")

    def test_latest_signal_with_insufficient_data(self):
        """测试数据不足时的信号返回"""
        # 少于35行的数据
        small_data = pd.DataFrame({
            'high': [100.1, 100.2],
            'low': [99.9, 99.8],
            'close': [100.0, 100.1]
        })

        signal = self.strategy.latest_signal("TEST", small_data, min_bars_required=35)
        self.assertIsNone(signal, "数据不足时应返回None")

if __name__ == '__main__':
    unittest.main(verbosity=2)