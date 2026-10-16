import pandas as pd
from strategy import KdjMacdStrategy

path = r"runtime/cache/000636_SZ_1MIN.csv"
target_time = pd.Timestamp("2026-06-23 10:57:00")

df = pd.read_csv(path)
df["time"] = pd.to_datetime(df["time"])
df = df.sort_values("time").reset_index(drop=True)

s = KdjMacdStrategy()
e = s.compute(df)

row = e[e["time"] == target_time]

if row.empty:
    print("NO_ROW")
else:
    r = row.iloc[0]
    cols = [
        "time",
        "SELL_S",
        "TOP_ESCAPE",
        "BUY_B",
        "BUY_JB",
        "SELL_JS",
        "BOTTOM_DIV",
        "TOP_DIV",
        "STRONG_BOTTOM",
        "STRONG_TOP",
        "TWO_ROUND_TOP_DIV",
        "INNER_TOP_DIV",
        "DIF",
        "DEA",
        "MACD",
        "CCI",
        "J",
        "BIG_RED",
        "WAVE_PEAK",
    ]
    print(r[cols].to_string())

    print("\n--- top breakdown details ---")
    if "TOP_DIV" in e.columns:
        print("TOP_DIV:", bool(r["TOP_DIV"]))
    if "TWO_ROUND_TOP_DIV" in e.columns:
        print("TWO_ROUND_TOP_DIV:", bool(r["TWO_ROUND_TOP_DIV"]))
    if "INNER_TOP_DIV" in e.columns:
        print("INNER_TOP_DIV:", bool(r["INNER_TOP_DIV"]))
    if "STRONG_TOP" in e.columns:
        print("STRONG_TOP:", bool(r["STRONG_TOP"]))