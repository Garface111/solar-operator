"""Keyword-based transaction categorizer. Deterministic and cheap; the chat agent
layers smarter analysis on top of these buckets."""
from __future__ import annotations

_RULES: list[tuple[str, tuple[str, ...]]] = [
    ("income", ("PAYROLL", "DIRECT DEP", "SALARY", "IRS TREAS", "TAX REF")),
    ("transfer", ("TRANSFER", "ZELLE", "VENMO", "XFER", "ONLINE PMT FROM")),
    ("mortgage_rent", ("MORTGAGE", "RENT", "ESCROW")),
    ("utilities", ("ELECTRIC", "GAS CO", "WATER", "SEWER", "GMP", "GREEN MOUNTAIN POWER",
                   "COMCAST", "XFINITY", "VERIZON", "T-MOBILE", "AT&T", "INTERNET")),
    ("groceries", ("GROCERY", "MARKET", "HANNAFORD", "SHAW", "PRICE CHOPPER", "COSTCO",
                   "TRADER JOE", "WHOLE FOODS", "CO-OP", "COOP")),
    ("dining", ("RESTAURANT", "CAFE", "COFFEE", "PIZZA", "GRILL", "BAKERY", "DINER",
                "DOORDASH", "GRUBHUB", "UBER EATS", "BREWERY", "TAVERN")),
    ("transport", ("SHELL", "SUNOCO", "EXXON", "MOBIL", "IRVING", "CUMBERLAND", "FUEL",
                   "UBER", "LYFT", "PARKING", "TOLL", "EZPASS", "AMTRAK")),
    ("subscriptions", ("NETFLIX", "SPOTIFY", "HULU", "DISNEY", "APPLE.COM", "APPLE COM",
                       "GOOGLE", "AMAZON PRIME", "YOUTUBE", "SUBSTACK", "PATREON",
                       "GITHUB", "OPENAI", "ANTHROPIC", "DROPBOX", "ICLOUD")),
    ("insurance", ("INSURANCE", "GEICO", "ALLSTATE", "STATE FARM", "PROGRESSIVE", "AETNA",
                   "BCBS", "BLUE CROSS")),
    ("health", ("PHARMACY", "CVS", "WALGREEN", "MEDICAL", "DENTAL", "CLINIC", "HOSPITAL")),
    ("shopping", ("AMAZON", "AMZN", "TARGET", "WALMART", "EBAY", "ETSY", "HOME DEPOT",
                  "LOWES", "IKEA")),
    ("travel", ("AIRLINE", "AIRBNB", "HOTEL", "MARRIOTT", "HILTON", "DELTA AIR",
                "UNITED", "JETBLUE", "EXPEDIA")),
    ("fees", ("FEE", "INTEREST CHARGE", "OVERDRAFT", "SERVICE CHARGE")),
]


def categorize(description: str, amount: float) -> str:
    d = description.upper()
    for category, keywords in _RULES:
        if any(k in d for k in keywords):
            return category
    if amount > 0:
        return "income"
    return "uncategorized"
