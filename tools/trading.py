"""Market data. Current: yfinance (delayed ~15min, free).

FUTURE REAL-TIME PATH: implement RealtimeProvider below against Databento /
Tradovate / IBKR websocket, then set PROVIDER = RealtimeProvider(). Nothing
else in the system changes - agents only call get_quote / get_history.

Futures symbols on yfinance: ES=F (S&P), NQ=F (Nasdaq), CL=F (Crude), GC=F (Gold).
Trade EXECUTION is intentionally absent. Recommendations only.
"""
import yfinance as yf


class DataProvider:
    def quote(self, symbol):
        raise NotImplementedError

    def history(self, symbol, period="5d", interval="15m"):
        raise NotImplementedError


class YFinanceProvider(DataProvider):
    def quote(self, symbol):
        t = yf.Ticker(symbol)
        fi = t.fast_info
        def g(k):
            try:
                return round(float(fi[k]), 4)
            except Exception:
                return None
        return {
            "symbol": symbol,
            "last": g("last_price"),
            "prev_close": g("previous_close"),
            "day_high": g("day_high"),
            "day_low": g("day_low"),
            "note": "yfinance data, may be ~15min delayed",
        }

    def history(self, symbol, period="5d", interval="15m"):
        df = yf.Ticker(symbol).history(period=period, interval=interval)
        if df.empty:
            return f"No data for {symbol}. Futures use '=F' suffix, e.g. ES=F."
        df = df[["Open", "High", "Low", "Close", "Volume"]].round(2).tail(60)
        return df.to_csv()


class RealtimeProvider(DataProvider):
    """Stub. Wire Databento/Tradovate/IBKR here later. Same interface."""
    def quote(self, symbol):
        raise NotImplementedError("Real-time feed not configured yet.")

    def history(self, symbol, period="5d", interval="15m"):
        raise NotImplementedError("Real-time feed not configured yet.")


PROVIDER = YFinanceProvider()


def get_quote(symbol):
    try:
        return str(PROVIDER.quote(symbol))
    except Exception as e:
        return f"Quote failed for {symbol}: {e}"


def get_history(symbol, period="5d", interval="15m"):
    try:
        return PROVIDER.history(symbol, period, interval)
    except Exception as e:
        return f"History failed for {symbol}: {e}"
