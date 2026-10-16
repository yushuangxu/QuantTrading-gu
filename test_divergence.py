import pandas as pd

from strategy import KdjMacdStrategy

CASES = [
    (r"runtime/cache/600487_SH_1MIN.csv", "2026-06-22 14:17:00", "600487 昨天14:17"),
    (r"runtime/cache/600487_SH_1MIN.csv", "2026-06-22 14:18:00", "600487 昨天14:18"),
    (r"runtime/cache/600487_SH_1MIN.csv", "2026-06-22 14:19:00", "600487 昨天14:19"),
    (r"runtime/cache/600487_SH_1MIN.csv", "2026-06-22 14:20:00", "600487 昨天14:20"),
    (r"runtime/cache/600487_SH_1MIN.csv", "2026-06-22 14:21:00", "600487 昨天14:21"),
    (r"runtime/cache/600487_SH_1MIN.csv", "2026-06-22 14:27:00", "600487 昨天14:27"),
    (r"runtime/cache/600487_SH_1MIN.csv", "2026-06-22 14:28:00", "600487 昨天14:28"),
    (r"runtime/cache/600487_SH_1MIN.csv", "2026-06-22 14:29:00", "600487 昨天14:29"),
    (r"runtime/cache/600487_SH_1MIN.csv", "2026-06-22 14:30:00", "600487 昨天14:30"),
    (r"runtime/cache/600487_SH_1MIN.csv", "2026-06-22 14:31:00", "600487 昨天14:31"),
    (r"runtime/cache/600487_SH_1MIN.csv", "2026-06-22 14:33:00", "600487 昨天14:33"),
    (r"runtime/cache/600487_SH_1MIN.csv", "2026-06-22 14:34:00", "600487 昨天14:34"),
    (r"runtime/cache/600487_SH_1MIN.csv", "2026-06-22 14:35:00", "600487 昨天14:35"),
    (r"runtime/cache/600487_SH_1MIN.csv", "2026-06-22 14:36:00", "600487 昨天14:36"),
    (r"runtime/cache/600487_SH_1MIN.csv", "2026-06-22 14:37:00", "600487 昨天14:37"),
]

FIELDS = [
    "BUY_B",
    "SELL_S",
    "BUY_JB",
    "SELL_JS",
    "BOTTOM_FISHING",
    "TOP_ESCAPE",
    "BOTTOM_DIV",
    "TOP_DIV",
    "STRONG_BOTTOM",
    "STRONG_TOP",
    "TWO_ROUND_BOTTOM_DIV",
    "INNER_BOTTOM_DIV",
    "TWO_ROUND_TOP_DIV",
    "INNER_TOP_DIV",
    "DIF",
    "DEA",
    "MACD",
    "CCI",
    "J",
    "BIG_RED",
    "BIG_GRE",
    "WAVE_PEAK",
    "WAVE_TROUGH",
]


def main() -> None:
    strategy = KdjMacdStrategy()

    for path, ts_str, label in CASES:
        print(f"=== {label} {ts_str} ===")
        df = pd.read_csv(path)
        df["time"] = pd.to_datetime(df["time"])
        df = df.sort_values("time").reset_index(drop=True)

        target_time = pd.Timestamp(ts_str)
        row = df[df["time"] == target_time]

        if row.empty:
            print("NO_ROW\n")
            continue

        enriched = strategy.compute(df)
        row = enriched[enriched["time"] == target_time]

        r = row.iloc[0]
        triggered = []
        for field in FIELDS:
            value = r[field]
            if isinstance(value, (bool, np.bool_)):
                value = bool(value)
            if value:
                triggered.append(f"{field}={value}")

        if triggered:
            print("TRIGGERED:")
            for item in triggered:
                print(item)
        else:
            print("NO_SIGNAL")

        sell_s_reasons = {
            "TOP_DIV": bool(r["TOP_DIV"]),
            "CCI>120": float(r["CCI"]) > 120,
            "J>80": float(r["J"]) > 80,
        }
        sell_js_reasons = {
            "BIG_RED": bool(r["BIG_RED"]),
            "WAVE_PEAK": bool(r["WAVE_PEAK"]),
            "DIF>0": float(r["DIF"]) > 0,
            "DEA>0": float(r["DEA"]) > 0,
            "CCI>120": float(r["CCI"]) > 120,
            "J>85": float(r["J"]) > 85,
        }

        if not bool(r["SELL_S"]):
            print("SELL_S missing conditions:")
            for k, v in sell_s_reasons.items():
                print(f"{k}={v}")

        if not bool(r["SELL_JS"]):
            print("SELL_JS missing conditions:")
            for k, v in sell_js_reasons.items():
                print(f"{k}={v}")

        print()


if __name__ == "__main__":
    import numpy as np

    main()
