"""Signal chart: last 24h of M15 candles, EMA20/50, labelled entry / SL / TP lines."""
from __future__ import annotations

import io
import logging

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
import mplfinance as mpf  # noqa: E402
import pandas as pd  # noqa: E402

log = logging.getLogger(__name__)


def signal_chart(m: pd.DataFrame, signal: dict, title: str, bars: int = 96, decimals: int = 2) -> bytes | None:
    try:
        d = m.iloc[-bars:][["open", "high", "low", "close", "ema20", "ema50"]].copy()
        d.index = d.index.tz_convert("UTC").tz_localize(None)
        levels = [("Entry", signal["entry"], "#2563eb"), ("SL", signal["sl"], "#dc2626"),
                  ("TP1", signal["tp1"], "#16a34a"), ("TP2", signal["tp2"], "#15803d")]
        lo = min(d["low"].min(), *(v for _, v, _ in levels))
        hi = max(d["high"].max(), *(v for _, v, _ in levels))
        pad = (hi - lo) * 0.04
        style = mpf.make_mpf_style(base_mpf_style="yahoo", gridstyle=":", facecolor="#ffffff", rc={"font.size": 9})
        ap = [mpf.make_addplot(d["ema20"], color="#f59e0b", width=1.0),
              mpf.make_addplot(d["ema50"], color="#7c3aed", width=1.0)]
        fig, axes = mpf.plot(d[["open", "high", "low", "close"]], type="candle", style=style, addplot=ap,
                             hlines=dict(hlines=[v for _, v, _ in levels], colors=[c for _, _, c in levels],
                                         linestyle="--", linewidths=1.1),
                             ylim=(lo - pad, hi + pad), title=title, ylabel="", figsize=(9, 5),
                             returnfig=True, tight_layout=True)
        ax = axes[0]
        for name, v, color in levels:
            ax.text(0.01, v, f" {name} {v:.{decimals}f} ", transform=ax.get_yaxis_transform(), color="white",
                    fontsize=8, va="center", ha="left", bbox=dict(boxstyle="round,pad=0.2", fc=color, ec="none"))
        ax.text(0.99, 0.02, "EMA20 orange · EMA50 purple · UTC", transform=ax.transAxes, fontsize=7,
                ha="right", va="bottom", color="#555")
        buf = io.BytesIO()
        fig.savefig(buf, dpi=110, format="png", bbox_inches="tight", pad_inches=0.15)
        plt.close(fig)
        return buf.getvalue()
    except Exception as e:  # noqa: BLE001 - a chart failure must never block a signal
        log.warning("chart failed: %s", e)
        return None
