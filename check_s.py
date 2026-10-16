import pandas as pd
from strategy import KdjMacdStrategy

cases = [
    (r"runtime/cache/000636_SZ_1MIN.csv", pd.Timestamp("2026-06-23 09:48:00"), "000636 09:48"),
    (r"runtime/cache/000636_SZ_1MIN.csv", pd.Timestamp("2026-06-23 10:57:00"), "000636 10:57"),
]

s = KdjMacdStrategy()

for path, ts, label in cases:
    df = pd.read_csv(path)
    df["time"] = pd.to_datetime(df["time"])
    df = df.sort_values("time").reset_index(drop=True)

    e = s.compute(df)
    r = e[e["time"] == ts]

    print(f"=== {label} ===")
    if r.empty:
        print("NO_ROW")
        continue

    row = r.iloc[0]
    print("BUY_JB", bool(row["BUY_JB"]))
    print("SELL_JS", bool(row["SELL_JS"]))
    print("BUY_B", bool(row["BUY_B"]))
    print("SELL_S", bool(row["SELL_S"]))
    print("BOTTOM_FISHING", bool(row["BOTTOM_FISHING"]))
    print("TOP_ESCAPE", bool(row["TOP_ESCAPE"]))
    print("DIF", row["DIF"])
    print("DEA", row["DEA"])
    print("MACD", row["MACD"])
    print("CCI", row["CCI"])
    print("J", row["J"])
    print("BIG_RED", bool(row["BIG_RED"]))
    print("WAVE_PEAK", bool(row["WAVE_PEAK"]))
    print("BIG_GRE", bool(row["BIG_GRE"]))
    print("WAVE_TROUGH", bool(row["WAVE_TROUGH"]))