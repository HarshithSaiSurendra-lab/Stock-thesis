from __future__ import annotations


SECTOR_BENCHMARKS_BY_SYMBOL: dict[str, str] = {
    # Technology
    "AAPL": "XLK", "MSFT": "XLK", "NVDA": "XLK", "AVGO": "XLK",
    "AMD": "XLK", "INTC": "XLK", "CSCO": "XLK", "ORCL": "XLK",
    "CRM": "XLK", "ADBE": "XLK", "QCOM": "XLK", "TXN": "XLK",
    "AMAT": "XLK", "MU": "XLK", "NOW": "XLK", "SNOW": "XLK",
    "PANW": "XLK", "CRWD": "XLK", "IBM": "XLK",
    # Communication services
    "GOOGL": "XLC", "META": "XLC", "NFLX": "XLC", "DIS": "XLC",
    "CMCSA": "XLC", "TMUS": "XLC", "VZ": "XLC", "T": "XLC",
    # Consumer discretionary
    "AMZN": "XLY", "TSLA": "XLY", "HD": "XLY", "MCD": "XLY",
    "NKE": "XLY", "SBUX": "XLY", "LOW": "XLY", "BKNG": "XLY",
    "TJX": "XLY", "CMG": "XLY", "MAR": "XLY", "GM": "XLY",
    "F": "XLY", "ORLY": "XLY",
    # Consumer staples
    "COST": "XLP", "PG": "XLP", "KO": "XLP", "PEP": "XLP",
    "WMT": "XLP", "PM": "XLP", "MO": "XLP", "MDLZ": "XLP",
    "CL": "XLP", "KMB": "XLP",
    # Health care
    "UNH": "XLV", "LLY": "XLV", "JNJ": "XLV", "MRK": "XLV",
    "PFE": "XLV", "ABBV": "XLV", "TMO": "XLV", "DHR": "XLV",
    "ABT": "XLV", "AMGN": "XLV", "ISRG": "XLV", "BMY": "XLV",
    "GILD": "XLV", "VRTX": "XLV", "REGN": "XLV", "MDT": "XLV",
    "SYK": "XLV",
    # Financials
    "JPM": "XLF", "BAC": "XLF", "MA": "XLF", "V": "XLF",
    "GS": "XLF", "MS": "XLF", "WFC": "XLF", "C": "XLF",
    "AXP": "XLF", "BLK": "XLF", "SCHW": "XLF", "PNC": "XLF",
    "USB": "XLF", "COF": "XLF", "SPGI": "XLF", "CME": "XLF",
    "ICE": "XLF", "CB": "XLF",
    # Energy
    "XOM": "XLE", "CVX": "XLE", "COP": "XLE", "SLB": "XLE",
    "EOG": "XLE", "MPC": "XLE", "PSX": "XLE", "VLO": "XLE",
    "OXY": "XLE", "HAL": "XLE",
    # Industrials
    "GE": "XLI", "CAT": "XLI", "HON": "XLI", "BA": "XLI",
    "UPS": "XLI", "RTX": "XLI", "LMT": "XLI", "DE": "XLI",
    "UNP": "XLI", "ETN": "XLI", "EMR": "XLI", "MMM": "XLI",
    # Utilities
    "NEE": "XLU", "DUK": "XLU", "SO": "XLU", "EXC": "XLU",
    "AEP": "XLU", "SRE": "XLU", "XEL": "XLU", "D": "XLU",
    "PEG": "XLU",
    # Materials
    "LIN": "XLB", "APD": "XLB", "SHW": "XLB", "ECL": "XLB",
    "FCX": "XLB", "NUE": "XLB", "DOW": "XLB", "DD": "XLB",
    # Real estate
    "PLD": "XLRE", "AMT": "XLRE", "EQIX": "XLRE", "O": "XLRE",
    "SPG": "XLRE", "WELL": "XLRE", "PSA": "XLRE", "CCI": "XLRE",
}


def sector_benchmark_for(symbol: str) -> str | None:
    return SECTOR_BENCHMARKS_BY_SYMBOL.get(symbol.upper())


def sector_benchmarks_for(symbols: list[str] | tuple[str, ...]) -> tuple[str, ...]:
    benchmarks = []
    for symbol in symbols:
        benchmark = sector_benchmark_for(symbol)
        if benchmark and benchmark not in benchmarks:
            benchmarks.append(benchmark)
    return tuple(benchmarks)
