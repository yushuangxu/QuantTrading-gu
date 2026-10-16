from __future__ import annotations

import argparse
from pathlib import Path
from typing import Sequence

import pandas as pd

from strategy import KdjMacdStrategy


SIGNAL_FIELDS: Sequence[str] = (
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
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Check one trading day signals for a minute-level CSV.")
    parser.add_argument("--csv", required=True, help="Path to the 1-minute CSV file.")
    parser.add_argument("--date", required=True, help="Trading date in YYYY-MM-DD format.")
    parser.add_argument(
        "--min-bars",
        type=int,
        default=150,
        help="Minimum bars required before computing latest signal logic.",
    )
    parser.add_argument(
        "--export",
        default="",
        help="Optional path to export the full filtered day signal table as CSV.",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    csv_path = Path(args.csv)
    if not csv_path.exists():
        raise FileNotFoundError(f"CSV not found: {csv_path}")

    trade_day = pd.Timestamp(args.date).date()

    df = pd.read_csv(csv_path)
    if "time" not in df.columns:
        raise ValueError("CSV must contain a 'time' column")

    df["time"] = pd.to_datetime(df["time"])
    df = df.sort_values("time").reset_index(drop=True)

    strategy = KdjMacdStrategy()
    enriched = strategy.compute(df)
    day_df = enriched[enriched["time"].dt.date == trade_day].copy()

    print(f"rows={len(day_df)}")

    signal_rows = []
    for _, r in day_df.iterrows():
        items = [field for field in SIGNAL_FIELDS if bool(r[field])]
        if not items:
            continue
        signal_rows.append(r)
        print(
            r["time"],
            items,
            "close", round(float(r["close"]), 3),
            "DIF", round(float(r["DIF"]), 4),
            "DEA", round(float(r["DEA"]), 4),
            "MACD", round(float(r["MACD"]), 4),
            "CCI", round(float(r["CCI"]), 2) if pd.notna(r["CCI"]) else r["CCI"],
            "J", round(float(r["J"]), 2),
        )

    print(f"signals={len(signal_rows)}")

    if args.export:
        export_path = Path(args.export)
        export_path.parent.mkdir(parents=True, exist_ok=True)
        day_df.to_csv(export_path, index=False, encoding="utf-8-sig")
        print(f"exported={export_path}")

    if len(day_df) >= args.min_bars:
        last_signal = strategy.latest_signal("UNKNOWN", day_df, min_bars_required=args.min_bars)
        if last_signal is not None:
            print(
                "latest_signal",
                {
                    "bar_time": last_signal.bar_time,
                    "buy_b": last_signal.buy_b,
                    "sell_s": last_signal.sell_s,
                    "bottom_fishing": last_signal.bottom_fishing,
                    "top_escape": last_signal.top_escape,
                    "buy_jb": last_signal.buy_jb,
                    "sell_js": last_signal.sell_js,
                },
            )


if __name__ == "__main__":
    main()
