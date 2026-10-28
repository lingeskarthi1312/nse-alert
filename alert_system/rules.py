"""Alert-rule semantics shared by the end-of-day and live jobs.

  Daily    - % change vs the previous close; "Below or equal 5" fires at <= -5%,
             "Above or equal 5" at >= +5% (threshold is a magnitude).
  Price    - price level in rupees; "Above or equal 900" fires at price >= 900,
             "Below or equal 800" at price <= 800.
  SinceSet - % change vs base_price (live price when saved, or one the user typed)
             (magnitude, like Daily). One-shot: the rule is disabled once it fires.
Weekly/Monthly rules are shown in the dashboard only (not emailed).
"""
EVALUATED_METRICS = ("Daily", "Price", "SinceSet")
ONE_SHOT_METRICS = ("SinceSet",)


def since_set_change(price, base_price):
    return (price / float(base_price) - 1) * 100


def triggered(metric, condition, threshold, change_pct, price, base_price=None):
    if metric == "Price":
        level = float(threshold)
        return price >= level if condition == "Above or equal" else price <= level
    if metric == "SinceSet":
        change_pct = since_set_change(price, base_price)
    limit = abs(float(threshold))
    return change_pct >= limit if condition == "Above or equal" else change_pct <= -limit


def describe(metric, condition, threshold, base_price=None):
    if metric == "Price":
        return f"price {'≥' if condition == 'Above or equal' else '≤'} ₹{float(threshold):,.2f}"
    if metric == "SinceSet":
        sign = "+" if condition == "Above or equal" else "-"
        base = f" from ₹{float(base_price):,.2f}" if base_price else " from the base price"
        return f"{sign}{abs(float(threshold)):g}%{base} (once)"
    return f"{condition} {threshold}%"
