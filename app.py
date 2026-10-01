"""
Denave x Canon CPP - Region-wise Performance Mail Generator
-----------------------------------------------------------
Upload one or more "Daily Performance Cockpit" dashboard HTML files
(e.g. the North file, the South file ...). The app reads the data embedded in
each file and writes a ready-to-send mail draft for every region found,
plus a combined "All regions" mail when more than one region is uploaded.

Run:  streamlit run app.py
"""

import base64
import calendar
import hashlib
import io
import html as htmllib
import json
import re
import urllib.parse
from collections import Counter
from datetime import date
from email.message import EmailMessage

import matplotlib

matplotlib.use("Agg")
import matplotlib.pyplot as plt  # noqa: E402
from matplotlib.patches import Patch  # noqa: E402
import streamlit as st  # noqa: E402
import streamlit.components.v1 as components

# --------------------------------------------------------------------------
# Parsing
# --------------------------------------------------------------------------
MARKER = "const ALL_DATA = "


def load_dashboard(raw: bytes) -> dict:
    """Extract the ALL_DATA JSON object embedded in a dashboard HTML file."""
    text = raw.decode("utf-8", errors="ignore")
    idx = text.find(MARKER)
    if idx == -1:
        raise ValueError("This does not look like a Performance Cockpit dashboard (ALL_DATA not found).")
    data, _ = json.JSONDecoder().raw_decode(text[idx + len(MARKER):])
    return data


# --------------------------------------------------------------------------
# Formatting helpers
# --------------------------------------------------------------------------
def money(n: float) -> str:
    n = float(n)
    if abs(n) >= 1e7:
        return f"₹{n / 1e7:.2f} Cr"
    if abs(n) >= 1e5:
        return f"₹{n / 1e5:.1f} L"
    return f"₹{n:,.0f}"


def pct(n: float) -> str:
    return f"{n:.0f}%"


def join_list(items):
    items = list(items)
    if len(items) <= 1:
        return "".join(items)
    return ", ".join(items[:-1]) + " and " + items[-1]


# --------------------------------------------------------------------------
# Summary builder (one region)
# --------------------------------------------------------------------------
def weekday_stats(daily):
    """Average revenue per calendar day for each weekday (Sunday excluded from ranking)."""
    if not daily:
        return None
    dates = [date.fromisoformat(d["DateStr"]) for d in daily]
    y, m = dates[0].year, dates[0].month
    n_days = calendar.monthrange(y, m)[1]
    occurrences = Counter(date(y, m, d).weekday() for d in range(1, n_days + 1))
    revenue = Counter()
    for d, dt in zip(daily, dates):
        revenue[dt.weekday()] += d["Revenue"]
    avg = {w: revenue[w] / occurrences[w] for w in occurrences if w != 6 and occurrences[w]}
    if len(avg) < 2:
        return None
    soft = min(avg, key=avg.get)
    best = max(avg, key=avg.get)
    return {
        "soft_name": calendar.day_name[soft],
        "soft_avg": avg[soft],
        "best_name": calendar.day_name[best],
        "best_avg": avg[best],
        "gap_pct": (avg[best] - avg[soft]) / avg[best] * 100 if avg[best] else 0,
    }


def build_summary(d: dict, region: str) -> dict:
    rd = d.get("perRegion", {}).get(region, d)
    kpi = rd["kpi"]
    reg_row = next((r for r in rd.get("region", []) if r["Region"] == region), None)
    target = reg_row["Target"] if reg_row else kpi["totalTarget"]
    achieved = reg_row["Achieved"] if reg_row else kpi["totalAchieved"]
    reps_n = reg_row["Reps"] if reg_row else kpi["totalReps"]
    days = kpi.get("daysInMonth") or 30

    reps = [r for r in d.get("counterReps", []) if r["Region"] == region]
    reps_sorted = sorted(reps, key=lambda r: r["AchPct"], reverse=True)
    below = sorted([r for r in reps if r["AchPct"] < 100], key=lambda r: r["AchPct"])
    above_n = sum(1 for r in reps if r["AchPct"] >= 100) if reps else kpi["repsAbove100"]

    daily = rd.get("daily", [])
    top_days = sorted(daily, key=lambda x: x["Revenue"], reverse=True)[:3]

    cats = [c for c in rd.get("category", []) if c["Revenue"] > 0]
    cats = sorted(cats, key=lambda c: c["Revenue"], reverse=True)

    ab = rd.get("alphaBooster", {})
    ab_kpi = ab.get("kpi", {})
    prog = {p["Program"]: p for p in ab.get("programSummary", [])}
    type_rev = {t["Type"]: t["Revenue"] for t in ab.get("typeSummary", [])}
    prog_by_type = Counter()
    for c in ab.get("combo", []):
        prog_by_type[c["Type"]] += c["Revenue"]
    weak_type = None
    if type_rev and prog_by_type:
        shares = {t: prog_by_type[t] / type_rev[t] for t in type_rev if type_rev[t] > 0}
        if len(shares) > 1:
            weak_type = min(shares, key=shares.get)

    cov = [c for c in d.get("counterCoverage", []) if c["Region"] == region]
    partners = sum(c["PartnerCount"] for c in cov)
    reported = sum(c["PartnersReported"] for c in cov)
    untapped = sorted(cov, key=lambda c: c["PartnerCount"] - c["PartnersReported"], reverse=True)[:4]

    return {
        "region": region,
        "period": d.get("periodLabel", ""),
        "target": target,
        "achieved": achieved,
        "ach_pct": achieved / target * 100 if target else 0,
        "reps_n": reps_n,
        "above_n": above_n,
        "units": kpi.get("totalUnits", 0),
        "run_rate": achieved / days,
        "target_rate": target / days,
        "top_days": top_days,
        "cats": cats,
        "total_rev": sum(c["Revenue"] for c in cats) or achieved,
        "type_rev": type_rev,
        "ab_rev": ab_kpi.get("totalRevenue", 0),
        "ab_pct": ab_kpi.get("pctOfRevenue", 0),
        "xf": prog.get("X-Factor"),
        "alpha": prog.get("Alpha"),
        "weak_type": weak_type,
        "tiers": rd.get("tier", []),
        "top_reps": reps_sorted[:5],
        "below": below,
        "partners": partners,
        "reported": reported,
        "untapped": untapped,
        "weekday": weekday_stats(daily),
        "daily": daily,
        "target_line": rd.get("dailyTargetLine", 0),
        "all_reps": reps_sorted,
        "cats_all": cats,
    }


# --------------------------------------------------------------------------
# Mail writers
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# Snapshots (charts embedded in the mail)
# --------------------------------------------------------------------------
GREEN_C, RED_C, NAVY_C = "#1b7f3b", "#c0392b", "#0b3d91"
REGION_COLORS = ["#0b3d91", "#e07b00", "#1b7f3b", "#7b3fa0", "#c0392b", "#0e8f9c"]
SNAP_CAPTIONS = {
    "daily": "Daily revenue vs the pace needed to hit target",
    "reps": "Target achievement by rep",
    "mix": "Product mix and tier attainment",
    "regions": "Target vs achieved by region",
    "daily_regions": "Daily revenue by region vs daily pace needed",
}
SNAP_LABELS = {
    "daily": "Daily revenue vs target pace",
    "reps": "Rep-wise achievement %",
    "mix": "Product mix & tier attainment",
    "regions": "Region comparison (All-regions mail)",
    "daily_regions": "Daily revenue by region (All-regions mail)",
}
REGION_SNAPS = ("daily", "reps", "mix")
COMBINED_SNAPS = ("regions", "daily_regions")
SNAP_RE = re.compile(r"^\[\[SNAPSHOT:(\w+)\]\]$")


def _png(fig):
    buf = io.BytesIO()
    fig.savefig(buf, format="png", dpi=170, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def _style(ax):
    for sp in ("top", "right"):
        ax.spines[sp].set_visible(False)
    for sp in ("left", "bottom"):
        ax.spines[sp].set_color("#cfd6e0")
    ax.tick_params(colors="#374151", labelsize=8)
    ax.set_axisbelow(True)


def _title(ax, text):
    ax.set_title(text, fontsize=10, fontweight="bold", loc="left", color=NAVY_C)


def chart_daily(s):
    days = [date.fromisoformat(d["DateStr"]).day for d in s["daily"]]
    rev = [d["Revenue"] / 1e5 for d in s["daily"]]
    tl = s["target_line"] / 1e5
    fig, ax = plt.subplots(figsize=(7.2, 3.2))
    _style(ax)
    ax.bar(days, rev, color=[GREEN_C if r >= tl else RED_C for r in rev], width=0.75)
    ax.axhline(tl, ls="--", color=NAVY_C, lw=1.2)
    ax.set_xticks(days)
    ax.tick_params(axis="x", labelsize=7)
    ax.set_ylabel("Revenue (₹ Lakh)", fontsize=8)
    ax.grid(axis="y", color="#e5e7eb", lw=0.6)
    _title(ax, f"{s['region']} – daily revenue vs required pace (₹{tl:.1f} L/day)")
    ax.legend(handles=[Patch(color=GREEN_C, label="Above daily pace"), Patch(color=RED_C, label="Below daily pace"),
                       plt.Line2D([0], [0], ls="--", color=NAVY_C, label="Daily pace needed")],
              fontsize=7, frameon=False, loc="upper center", bbox_to_anchor=(0.5, -0.1), ncol=3)
    return _png(fig)


def chart_reps(s):
    reps = s["all_reps"]
    if not reps:
        return None
    if len(reps) > 24:
        reps = reps[:12] + reps[-12:]
    reps = reps[::-1]  # best performer ends up on top
    names = [f"{r['Name'].title()} ({r['City']})" for r in reps]
    vals = [r["AchPct"] for r in reps]
    mx = max(vals)
    cap = 300 if mx > 300 else mx * 1.15
    fig, ax = plt.subplots(figsize=(7.2, max(2.6, 0.27 * len(reps) + 1)))
    _style(ax)
    ax.barh(names, [min(v, cap) for v in vals], color=[GREEN_C if v >= 100 else RED_C for v in vals], height=0.7)
    ax.axvline(100, ls="--", color=NAVY_C, lw=1.1)
    ax.set_xlim(0, cap * 1.12)
    for i, v in enumerate(vals):
        if v > cap:
            ax.text(cap - 2, i, f"{v:.0f}%", ha="right", va="center", color="white", fontsize=7, fontweight="bold")
        else:
            ax.text(v + cap * 0.01, i, f"{v:.0f}%", va="center", fontsize=7)
    ax.tick_params(axis="y", labelsize=7)
    ax.set_xlabel("Achievement % (dashed line = 100% of target)", fontsize=8)
    _title(ax, f"{s['region']} – target achievement by rep")
    return _png(fig)


def chart_mix(s):
    fig, (a1, a2) = plt.subplots(1, 2, figsize=(7.2, 3.1), gridspec_kw={"width_ratios": [1.5, 1]})
    cats = s["cats_all"][:6][::-1]
    for a in (a1, a2):
        _style(a)
    if cats:
        vals = [c["Revenue"] / 1e7 for c in cats]
        a1.barh([c["Product Category"] for c in cats], vals, color=NAVY_C, height=0.65)
        for i, (c, v) in enumerate(zip(cats, vals)):
            a1.text(v * 1.02, i, f"₹{v:.2f} Cr ({c['Revenue'] / s['total_rev'] * 100:.0f}%)", va="center", fontsize=7)
        a1.set_xlim(0, max(vals) * 1.6)
    _title(a1, "Revenue by product (₹ Cr)")
    a1.tick_params(axis="y", labelsize=8)
    tiers = s["tiers"]
    if tiers:
        t_vals = [t["AchPct"] for t in tiers]
        a2.bar([t["Tier"] for t in tiers], t_vals, color=[GREEN_C if v >= 100 else RED_C for v in t_vals], width=0.6)
        a2.axhline(100, ls="--", color=NAVY_C, lw=1.1)
        for i, v in enumerate(t_vals):
            a2.text(i, v + max(t_vals) * 0.02, f"{v:.0f}%", ha="center", fontsize=8, fontweight="bold")
        a2.set_ylim(0, max(t_vals) * 1.18)
    else:
        a2.axis("off")
    _title(a2, "Attainment by tier")
    fig.tight_layout()
    return _png(fig)


def chart_regions(sums):
    fig, ax = plt.subplots(figsize=(7.2, 3.1))
    _style(ax)
    x = range(len(sums))
    w = 0.36
    ax.bar([i - w / 2 for i in x], [r["target"] / 1e7 for r in sums], w, color="#a9bde0", label="Target")
    ax.bar([i + w / 2 for i in x], [r["achieved"] / 1e7 for r in sums], w, color=NAVY_C, label="Achieved")
    top = max(r["achieved"] / 1e7 for r in sums)
    for i, r in enumerate(sums):
        ax.text(i + w / 2, r["achieved"] / 1e7 + top * 0.02, f"{r['ach_pct']:.0f}%", ha="center", fontsize=9,
                fontweight="bold", color=GREEN_C if r["ach_pct"] >= 100 else RED_C)
    ax.set_xticks(list(x))
    ax.set_xticklabels([r["region"] for r in sums])
    ax.set_ylabel("₹ Crore", fontsize=8)
    ax.set_ylim(0, top * 1.18)
    ax.grid(axis="y", color="#e5e7eb", lw=0.6)
    ax.legend(fontsize=8, frameon=False)
    _title(ax, "Target vs achieved by region")
    return _png(fig)


def chart_daily_regions(sums):
    fig, ax = plt.subplots(figsize=(7.2, 3.2))
    _style(ax)
    for i, r in enumerate(sums):
        col = REGION_COLORS[i % len(REGION_COLORS)]
        days = [date.fromisoformat(d["DateStr"]).day for d in r["daily"]]
        ax.plot(days, [d["Revenue"] / 1e5 for d in r["daily"]], color=col, lw=1.6, marker="o", ms=2.5, label=r["region"])
        ax.axhline(r["target_line"] / 1e5, color=col, ls=":", lw=1)
    ax.set_ylabel("Revenue (₹ Lakh)", fontsize=8)
    ax.set_xlabel("Day of month  (dotted line = daily pace needed)", fontsize=8)
    ax.grid(axis="y", color="#e5e7eb", lw=0.6)
    ax.legend(fontsize=8, frameon=False, ncol=len(sums))
    _title(ax, "Daily revenue by region")
    return _png(fig)


REGION_CHARTS = {"daily": chart_daily, "reps": chart_reps, "mix": chart_mix}
COMBINED_CHARTS = {"regions": chart_regions, "daily_regions": chart_daily_regions}


@st.cache_data(show_spinner=False)
def charts_for_region(summary_json, keys):
    s = json.loads(summary_json)
    return {k: img for k in keys if (img := REGION_CHARTS[k](s))}


@st.cache_data(show_spinner=False)
def charts_for_combined(summaries_json, keys):
    sums = json.loads(summaries_json)
    return {k: img for k in keys if (img := COMBINED_CHARTS[k](sums))}


def snap_lines(cfg, combined=False):
    allowed = COMBINED_SNAPS if combined else REGION_SNAPS
    keys = [k for k in cfg.get("snapshots", []) if k in allowed]
    if not keys:
        return []
    return ["", "SNAPSHOTS"] + [f"[[SNAPSHOT:{k}]]" for k in keys]


def manual_lines(cfg, internal=False):
    if not cfg.get("manual"):
        return []
    if internal:
        return ["", "HOW TO READ THE DASHBOARD",
                "1. Open the attached HTML file in Chrome or Edge – no login is needed and all data is inside the file.",
                "2. Use the Region buttons and the BM, State Head and City drop-downs at the top to see your own numbers; every chart and table follows these filters.",
                "3. In Day Explorer, click any day's bar for a full breakdown. Bars are colour-coded as above or below the daily pace needed to hit target.",
                "4. Check Top 10 Performers and Needs Attention (Bottom 10) for your ranking, and Rep Counter & Revenue Coverage for TPS partners reporting under each rep.",
                "5. Use the Model-wise table for units and revenue by model, and the search box in Revenue Pivot to find any partner quickly."]
    return ["", "HOW TO USE THE DASHBOARD",
            "1. Save the attached HTML file and open it in Chrome or Edge (double-click). No login or installation is needed – all data is inside the file.",
            "2. Use the Region buttons at the top and the BM, State Head and City drop-downs to filter; every KPI, chart and table updates with them.",
            "3. Start with the KPI cards and the Executive Summary for the month at a glance – the summary is auto-generated and follows the region filter.",
            "4. In Day Explorer, click any day's bar to see that day's breakdown. Bars are colour-coded as above or below the daily pace needed to hit target.",
            "5. Top 10 Performers and Needs Attention (Bottom 10) show who is leading and who needs support; Achievement Distribution groups reps by performance slab.",
            "6. The Alpha / X Factor section shows program revenue and the Ink vs Laser split; the Model-wise table gives units and revenue by model.",
            "7. Rep Counter & Revenue Coverage shows TPS partners reporting under each rep, and Revenue Pivot has a search box to find any partner or rep."]


def signature(cfg):
    lines = ["Warm regards," if cfg["audience"] == "Client / leadership" else "Regards,", cfg["sender"] or "[Your Name]"]
    desig = f"{cfg['designation']} | {cfg['company']}" if cfg["designation"] else cfg["company"]
    lines.append(desig)
    contact = " | ".join(x for x in [cfg["phone"], cfg["email"]] if x)
    if contact:
        lines.append(contact)
    return "\n".join(lines)


def below_text(s, with_names):
    items = []
    for r in s["below"][:5]:
        label = f"{r['Name']} ({r['City']})" if with_names else r["City"]
        items.append(f"{label} {pct(r['AchPct'])}")
    return ", ".join(items)


def weekday_line(s, internal):
    w = s["weekday"]
    if not w or w["gap_pct"] < 5:
        return None
    if internal:
        return (f"Keep momentum steady through the month and lift {w['soft_name']}s "
                f"(~{money(w['soft_avg'])}/day average, our softest working day) "
                f"closer to {w['best_name']} levels (~{money(w['best_avg'])}/day).")
    return (f"{w['soft_name']} was our softest working day at ~{money(w['soft_avg'])}/day on average, "
            f"versus ~{money(w['best_avg'])}/day on {w['best_name']}s – there is room for a more even run-rate across the week.")


def short(cfg):
    """Company name without a trailing 'India' for subject lines."""
    return re.sub(r"\s+India$", "", cfg["company"]).strip() or cfg["company"]


def subject_for(s, cfg):
    if s["ach_pct"] >= 100:
        tail = f"{pct(s['ach_pct'])} Target Achievement"
    else:
        tail = f"{pct(s['ach_pct'])} of Target Achieved"
    if cfg["audience"] == "Client / leadership":
        return f"{short(cfg)} × {cfg['program']} | {s['region']} Region – {s['period']} Performance: {tail}"
    return f"{s['period']} Results: {s['region']} at {pct(s['ach_pct'])} of Target – Thank You & Next-Month Focus"


def client_mail(s, cfg):
    ahead = s["achieved"] >= s["target"]
    gap = abs(s["achieved"] - s["target"])
    L = [f"Dear {cfg['recipient'] or '[Name]'},", ""]
    L.append(f"I'm pleased to share the {s['period']} performance update for the {cfg['program']} program "
             f"({s['region']} region), along with the attached Daily Performance Cockpit for a detailed drill-down.")
    L += ["", "HEADLINE",
          f"• Target: {money(s['target'])} | Achieved: {money(s['achieved'])} | Attainment: {pct(s['ach_pct'])}",
          f"• {'Ahead of' if ahead else 'Short of'} target by {money(gap)}",
          f"• {s['above_n']} of {s['reps_n']} field reps crossed 100% of their target",
          f"• {s['units']:,} units sold; average run-rate of ~{money(s['run_rate'])}/day against "
          f"~{money(s['target_rate'])}/day needed to meet target"]
    if s["top_days"]:
        days = ", ".join(f"{date.fromisoformat(x['DateStr']).day} {calendar.month_abbr[date.fromisoformat(x['DateStr']).month]} ({money(x['Revenue'])})"
                         for x in s["top_days"])
        L.append(f"• Strongest days: {days}")

    L += ["", "WHAT DROVE THE RESULT"]
    if s["cats"]:
        top = s["cats"][0]
        rest = join_list(f"{c['Product Category']} ({money(c['Revenue'])})" for c in s["cats"][1:3])
        line = (f"• Product mix: {top['Product Category']} led with {money(top['Revenue'])} "
                f"(~{top['Revenue'] / s['total_rev'] * 100:.0f}% of revenue)")
        if rest:
            line += f", followed by {rest}"
        L.append(line + ".")
    if s["type_rev"]:
        L.append("• " + " and ".join(f"{k} contributed {money(v)}" for k, v in s["type_rev"].items()) + ".")
    if s["ab_rev"]:
        parts = []
        if s["xf"]:
            parts.append(f"X-Factor {money(s['xf']['Revenue'])} ({s['xf']['Units']:,} units)")
        if s["alpha"]:
            parts.append(f"Alpha {money(s['alpha']['Revenue'])} ({s['alpha']['Units']:,} units)")
        L.append(f"• Alpha / X-Factor programs: {money(s['ab_rev'])} (~{s['ab_pct']:.0f}% of total revenue)"
                 + (" – " + " and ".join(parts) if parts else "") + ".")
    if s["tiers"]:
        L.append("• Tier performance: " + ", ".join(f"{t['Tier']} {pct(t['AchPct'])}" for t in s["tiers"]) + " of target.")
    if s["top_reps"]:
        L.append("• Top performers: " + join_list(f"{r['Name']} ({r['City']}, {pct(r['AchPct'])})" for r in s["top_reps"][:3]) + ".")

    L += snap_lines(cfg)
    L += ["", f"AREAS OF FOCUS FOR {cfg['next_month'].upper()}"]
    if s["below"]:
        L.append(f"• {len(s['below'])} rep{'s' if len(s['below']) > 1 else ''} remain below target "
                 f"({below_text(s, False)}) – we have started targeted coaching and joint-working plans with the respective Branch Managers.")
    if s["partners"]:
        cities = join_list(c["City"] for c in s["untapped"])
        L.append(f"• Counter coverage: {s['reported']:,} of {s['partners']:,} TPS partners reported sales "
                 f"(~{s['reported'] / s['partners'] * 100:.0f}%), leaving a significant untapped base. "
                 f"We will prioritise reactivation of non-reporting counters, especially in {cities}.")
    wl = weekday_line(s, internal=False)
    if wl:
        L.append("• " + wl)
    if s["ab_rev"]:
        extra = f", particularly in {s['weak_type']}" if s["weak_type"] else ""
        L.append(f"• Scale Alpha / X-Factor tagging further{extra}.")

    L += manual_lines(cfg)
    L += ["", f"We would value 30 minutes to walk you through the dashboard and align on the {cfg['next_month']} plan "
              "and any support needed from the Canon side.", "", "Thank you for your continued partnership.", "",
          signature(cfg), "",
          f"Attachment: {short(cfg)} × {cfg['program']} – Daily Performance Cockpit ({s['period']}, {s['region']})"]
    return "\n".join(L)


def internal_mail(s, cfg):
    L = ["Hi Team,", ""]
    if s["ach_pct"] >= 100:
        L.append(f"Great work, {s['region']}! We closed {s['period']} at {money(s['achieved'])} against a target of "
                 f"{money(s['target'])} – {pct(s['ach_pct'])} attainment, with {s['above_n']} of {s['reps_n']} reps above 100%. "
                 "This is a team result, and I want to thank every rep, Branch Manager and State Head behind it.")
    else:
        L.append(f"Thank you, {s['region']}, for the effort this month. We closed {s['period']} at {money(s['achieved'])} against a "
                 f"target of {money(s['target'])} – {pct(s['ach_pct'])} attainment, with {s['above_n']} of {s['reps_n']} reps above 100%. "
                 f"We are {money(s['target'] - s['achieved'])} short of target, and I want us to close that gap together next month.")
    if s["top_reps"]:
        L += ["", "SHOUT-OUTS"]
        for r in s["top_reps"]:
            L.append(f"• {r['Name']} ({r['City']}) – {pct(r['AchPct'])} | {money(r['RevenueAchieved'])}")
    L += ["", "HIGHLIGHTS", f"• {s['units']:,} units sold"]
    if s["top_days"]:
        b = s["top_days"][0]
        dt = date.fromisoformat(b["DateStr"])
        L[-1] += f"; best day was {dt.day} {calendar.month_abbr[dt.month]} at {money(b['Revenue'])}"
    if s["ab_rev"]:
        L.append(f"• Alpha / X-Factor contributed {money(s['ab_rev'])} (~{s['ab_pct']:.0f}% of revenue)")
    if s["tiers"]:
        best = max(s["tiers"], key=lambda t: t["AchPct"])
        L.append(f"• {best['Tier']} locations delivered the best attainment at {pct(best['AchPct'])}")

    L += snap_lines(cfg)
    L += ["", "WHERE WE CAN DO BETTER"]
    if s["below"]:
        L.append("• A few reps are still below target: " + below_text(s, True).replace("(", "– ").replace(")", "")
                 + ". BMs, please schedule a 1:1 and joint market visit this week.")
    if s["partners"]:
        L.append(f"• Only ~{s['reported'] / s['partners'] * 100:.0f}% of TPS partners ({s['reported']:,} of {s['partners']:,}) reported sales. "
                 "Let's push counter reactivation and new-counter onboarding.")
    wl = weekday_line(s, internal=True)
    if wl:
        L.append("• " + wl)

    L += ["", "NEXT STEPS"]
    n = 1
    if s["below"]:
        L.append(f"{n}. BMs to share a recovery plan for reps below 100% by {cfg['deadline'] or '[date]'}.")
        n += 1
    L.append(f"{n}. Each rep to list 10 non-reporting counters to activate in the first week of {cfg['next_month']}.")
    n += 1
    L.append(f"{n}. Review call on {cfg['review_call'] or '[date/time]'} using the attached dashboard.")
    L += manual_lines(cfg, internal=True)
    L += ["", "The dashboard is attached – please check your own numbers by city and model.", "",
          f"Let's make {cfg['next_month']} even bigger!", "", signature(cfg)]
    return "\n".join(L)


def combined_mail(summaries, cfg):
    period = summaries[0]["period"]
    target = sum(s["target"] for s in summaries)
    achieved = sum(s["achieved"] for s in summaries)
    reps_n = sum(s["reps_n"] for s in summaries)
    above = sum(s["above_n"] for s in summaries)
    units = sum(s["units"] for s in summaries)
    ach = achieved / target * 100 if target else 0
    ranked = sorted(summaries, key=lambda s: s["ach_pct"], reverse=True)
    all_top = sorted([r for s in summaries for r in s["top_reps"]], key=lambda r: r["AchPct"], reverse=True)[:5]
    all_below = sorted([r for s in summaries for r in s["below"]], key=lambda r: r["AchPct"])[:6]
    partners = sum(s["partners"] for s in summaries)
    reported = sum(s["reported"] for s in summaries)
    client = cfg["audience"] == "Client / leadership"

    L = [f"Dear {cfg['recipient'] or '[Name]'}," if client else "Hi Team,", ""]
    L.append(f"Please find below the consolidated {period} performance update for the {cfg['program']} program across "
             f"{join_list(s['region'] for s in summaries)}. The detailed regional dashboards are attached.")
    L += ["", "OVERALL",
          f"• Target: {money(target)} | Achieved: {money(achieved)} | Attainment: {pct(ach)}",
          f"• {above} of {reps_n} field reps crossed 100% of their target",
          f"• {units:,} units sold"]
    L += ["", "REGION SNAPSHOT"]
    for s in ranked:
        L.append(f"• {s['region']}: {money(s['achieved'])} vs {money(s['target'])} target – {pct(s['ach_pct'])} "
                 f"({s['above_n']}/{s['reps_n']} reps above 100%)")
    if all_top:
        L += ["", "TOP PERFORMERS"]
        for r in all_top:
            L.append(f"• {r['Name']} ({r['City']}, {r['Region']}) – {pct(r['AchPct'])} | {money(r['RevenueAchieved'])}")
    L += snap_lines(cfg, combined=True)
    L += ["", f"AREAS OF FOCUS FOR {cfg['next_month'].upper()}"]
    if all_below:
        L.append("• Reps below target: " + ", ".join(f"{r['Name'] if not client else r['City']} ({r['Region']}) {pct(r['AchPct'])}" for r in all_below)
                 + (" – coaching and joint-working plans are being put in place with the Branch Managers." if client
                    else ". BMs, please schedule 1:1s and joint market visits this week."))
    if partners:
        L.append(f"• Counter coverage: {reported:,} of {partners:,} TPS partners reported sales (~{reported / partners * 100:.0f}%); "
                 "reactivation of non-reporting counters is the biggest lever.")
    for s in ranked:
        if s["ach_pct"] < 100:
            L.append(f"• {s['region']} is {money(s['target'] - s['achieved'])} short of target and will get extra review attention.")
    L += manual_lines(cfg)
    L += ["", "We would value 30 minutes to walk through the dashboards and align on the next-month plan." if client
          else f"Let's keep the momentum going into {cfg['next_month']}.", "", signature(cfg)]
    return "\n".join(L)


def build_mail(s, cfg):
    return client_mail(s, cfg) if cfg["audience"] == "Client / leadership" else internal_mail(s, cfg)


# --------------------------------------------------------------------------
# Formatted (HTML) mail
# --------------------------------------------------------------------------
NAVY, GREEN, AMBER, RED = "#0b3d91", "#1b7f3b", "#b7791f", "#c0392b"
FONT = "Calibri,'Segoe UI',Arial,sans-serif"
HEADING_RE = re.compile(r"^[A-Z][A-Z0-9 /&'’\-]{3,}$")
EMPH_RE = re.compile(r"(₹[\d,.]+(?: (?:Cr|L))?|\d+(?:\.\d+)?%)")


def _inline(text):
    return EMPH_RE.sub(r"<b>\1</b>", htmllib.escape(text))


def _bullet(text):
    if " | " in text and text.count(": ") >= 2:
        return " &nbsp;|&nbsp; ".join(_bullet(part) for part in text.split(" | "))
    head, sep, tail = text.partition(": ")
    if sep and len(head) <= 34:
        return f"<b>{htmllib.escape(head)}:</b> {_inline(tail)}"
    return _inline(text)


TH = f"background:{NAVY};color:#ffffff;padding:6px 10px;border:1px solid {NAVY};font-weight:bold;text-align:center;"
TD = "padding:6px 10px;border:1px solid #bfc7d5;text-align:center;"


def _kpi_table(k):
    ach = k["ach_pct"]
    colour = GREEN if ach >= 100 else AMBER if ach >= 80 else RED
    heads = ["Target", "Achieved", "Attainment", "Reps ≥ 100%"]
    vals = [money(k["target"]), money(k["achieved"]), f'<b style="color:{colour};">{pct(ach)}</b>',
            f"{k['above_n']} of {k['reps_n']}"]
    return ('<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin:8px 0 6px;min-width:520px;"><tr>'
            + "".join(f'<th style="{TH}">{h}</th>' for h in heads) + "</tr><tr>"
            + "".join(f'<td style="{TD}">{v}</td>' for v in vals) + "</tr></table>")


def _snapshot_table(rows):
    heads = ["Region", "Target", "Achieved", "Attainment", "Reps ≥ 100%"]
    body = ""
    for s in rows:
        colour = GREEN if s["ach_pct"] >= 100 else AMBER if s["ach_pct"] >= 80 else RED
        body += ("<tr>" + f'<td style="{TD}text-align:left;"><b>{htmllib.escape(s["region"])}</b></td>'
                 f'<td style="{TD}">{money(s["target"])}</td><td style="{TD}">{money(s["achieved"])}</td>'
                 f'<td style="{TD}"><b style="color:{colour};">{pct(s["ach_pct"])}</b></td>'
                 f'<td style="{TD}">{s["above_n"]} of {s["reps_n"]}</td></tr>')
    return ('<table cellpadding="0" cellspacing="0" style="border-collapse:collapse;margin:8px 0 6px;min-width:520px;"><tr>'
            + "".join(f'<th style="{TH}">{h}</th>' for h in heads) + f"</tr>{body}</table>")


def to_html(body, kpi=None, snapshot=None, images=None, embed="data"):
    """Convert the plain-text mail into a clean, email-safe HTML body (Calibri, simple tables)."""
    lines = body.split("\n")
    out, mode, cards_done, skip_bullets, i = [], None, kpi is None, False, 0

    def close():
        nonlocal mode
        if mode:
            out.append(f"</{mode}>")
            mode = None

    def open_list(tag):
        nonlocal mode
        if mode != tag:
            close()
            out.append(f'<{tag} style="margin:4px 0 8px 0;padding-left:24px;">')
            mode = tag

    while i < len(lines):
        line = lines[i].rstrip()
        i += 1
        if not line.strip():
            close()
            skip_bullets = False
            continue
        msnap = SNAP_RE.match(line.strip())
        if msnap:
            close()
            key = msnap.group(1)
            if images and key in images:
                src = (f"cid:snap_{key}" if embed == "cid"
                       else "data:image/png;base64," + base64.b64encode(images[key]).decode())
                cap = htmllib.escape(SNAP_CAPTIONS.get(key, key))
                out.append(f'<p style="margin:10px 0 4px;"><img src="{src}" alt="{cap}" width="640" '
                           f'style="max-width:100%;height:auto;border:1px solid #d5dbe5;"><br>'
                           f'<span style="font-size:9pt;color:#555555;">Snapshot: {cap}</span></p>')
            continue
        if line.startswith(("Warm regards,", "Regards,")):
            close()
            block = [line]
            while i < len(lines) and lines[i].strip():
                block.append(lines[i].strip())
                i += 1
            rest = "<br>".join(htmllib.escape(b) for b in block[2:])
            out.append(f'<p style="margin:16px 0 0;">{htmllib.escape(block[0])}<br><br>'
                       f'<b>{htmllib.escape(block[1]) if len(block) > 1 else ""}</b><br>{rest}</p>')
        elif line.startswith("Attachment:"):
            close()
            out.append(f'<p style="margin:14px 0 0;font-size:10pt;color:#555555;">📎 {htmllib.escape(line)}</p>')
        elif HEADING_RE.match(line):
            close()
            if not cards_done:
                out.append(_kpi_table(kpi))
                cards_done = True
            out.append(f'<p style="margin:16px 0 4px;padding-bottom:2px;border-bottom:1px solid {NAVY};'
                       f'color:{NAVY};font-weight:bold;font-size:10.5pt;">{htmllib.escape(line)}</p>')
            if line == "REGION SNAPSHOT" and snapshot:
                out.append(_snapshot_table(snapshot))
                skip_bullets = True
        elif line.startswith("• "):
            if skip_bullets or (kpi is not None and line.startswith("• Target: ")):
                continue  # already shown in the summary table
            open_list("ul")
            out.append(f'<li style="margin-bottom:3px;">{_bullet(line[2:])}</li>')
        elif re.match(r"^\d+\. ", line):
            open_list("ol")
            out.append(f'<li style="margin-bottom:3px;">{_inline(re.sub(r"^\d+\. ", "", line))}</li>')
        else:
            close()
            out.append(f'<p style="margin:8px 0;">{_inline(line)}</p>')
    close()
    return (f'<div style="font-family:Calibri,Arial,sans-serif;font-size:11pt;color:#000000;line-height:1.5;">'
            f'{"".join(out)}</div>')


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------
def make_eml(subject, plain, html_body, to="", cc="", sender="", images=None):
    msg = EmailMessage()
    if sender:
        msg["From"] = sender
    if to:
        msg["To"] = to
    if cc:
        msg["Cc"] = cc
    msg["Subject"] = subject
    msg["X-Unsent"] = "1"  # opens as an editable draft in Outlook
    msg.set_content(plain)
    msg.add_alternative(f'<html><head><meta charset="utf-8"></head><body>{html_body}</body></html>', subtype="html")
    if images:
        html_part = msg.get_payload()[1]
        for key, data in images.items():
            html_part.add_related(data, "image", "png", cid=f"<snap_{key}>", filename=f"{key}.png")
    return msg.as_bytes()


def preview_component(html_body, subject, to, cc, sender, attach_note):
    """Mail-window style preview (From / To / Cc / Subject) with a one-click 'copy formatted body' button."""
    def row(label, value):
        return (f'<tr><td style="color:#6b7280;padding:3px 10px 3px 0;white-space:nowrap;vertical-align:top;">{label}</td>'
                f'<td style="padding:3px 0;">{htmllib.escape(value) if value else "<span style=\'color:#9ca3af\'>—</span>"}</td></tr>')
    header = ('<table style="font:14px Calibri,Arial,sans-serif;width:100%;border-collapse:collapse;">'
              + row("From", sender) + row("To", to) + row("Cc", cc)
              + f'<tr><td style="color:#6b7280;padding:3px 10px 3px 0;">Subject</td><td style="padding:3px 0;"><b>{htmllib.escape(subject)}</b></td></tr>'
              + row("Attachment", attach_note) + "</table>")
    page = """
    <div style="font-family:Arial;margin-bottom:8px;">
      <button id="cp" style="font:600 14px Arial;padding:8px 16px;border:0;border-radius:6px;background:#0b3d91;color:#fff;cursor:pointer;">
        📋 Copy mail body (formatted)</button>
      <span id="msg" style="font:13px Arial;margin-left:10px;color:#1b7f3b;"></span>
    </div>
    <div style="border:1px solid #cfd6e0;border-radius:6px;background:#fff;">
      <div style="background:#f3f4f6;padding:10px 16px;border-bottom:1px solid #cfd6e0;border-radius:6px 6px 0 0;">__HEADER__</div>
      <div id="mail" style="padding:14px 18px;">__HTML__</div>
    </div>
    <script>
    const msg = document.getElementById('msg');
    document.getElementById('cp').onclick = async () => {
      const el = document.getElementById('mail');
      try {
        await navigator.clipboard.write([new ClipboardItem({
          'text/html': new Blob([el.innerHTML], {type: 'text/html'}),
          'text/plain': new Blob([el.innerText], {type: 'text/plain'})})]);
        msg.textContent = 'Copied! Paste into the body of your mail (Ctrl+V).';
      } catch (e) {
        const r = document.createRange(); r.selectNodeContents(el);
        const s = window.getSelection(); s.removeAllRanges(); s.addRange(r);
        msg.textContent = document.execCommand('copy') ? 'Copied! Paste into the body of your mail (Ctrl+V).'
                          : 'Press Ctrl+C to copy the selected text.';
      }
    };
    </script>""".replace("__HEADER__", header).replace("__HTML__", html_body)
    components.html(page, height=1050, scrolling=True)


def plain_text(text):
    """Plain-text version of the mail: chart markers become a short note."""
    return re.sub(r"^\[\[SNAPSHOT:(\w+)\]\]$", lambda m: f"[Chart: {SNAP_CAPTIONS.get(m.group(1), m.group(1))}]",
                  text, flags=re.M)


def render_mail_block(key, subject, body, cfg, kpi=None, snapshot=None, images=None):
    ident = hashlib.md5((subject + body).encode()).hexdigest()[:8]  # reset widgets when settings change
    tab_fmt, tab_edit = st.tabs(["📧 Mail preview", "✏️ Edit text"])
    with tab_edit:
        subj = st.text_input("Subject", subject, key=f"subj_{key}_{ident}")
        text = st.text_area("Mail text (edits update the preview)", body, height=560, key=f"body_{key}_{ident}")
        st.caption("Lines like [[SNAPSHOT:daily]] insert a chart image – delete the line to remove that chart.")
    html_body = to_html(text, kpi, snapshot, images, "data")
    html_cid = to_html(text, kpi, snapshot, images, "cid")
    sender = f"{cfg['sender']} <{cfg['email']}>" if cfg["sender"] and cfg["email"] else (cfg["email"] or cfg["sender"])
    attach = re.search(r"^Attachment: (.*)$", text, re.M)
    with tab_fmt:
        preview_component(html_body, subj, cfg["to"], cfg["cc"], sender, attach.group(1) if attach else "")
        c1, c2, c3 = st.columns(3)
        safe = re.sub(r"[^A-Za-z0-9_-]+", "_", key)
        used = {k: v for k, v in (images or {}).items() if f"[[SNAPSHOT:{k}]]" in text}
        c1.download_button("⬇️ Download .eml (best – charts embedded)",
                           make_eml(subj, plain_text(text), html_cid, cfg["to"], cfg["cc"], sender, used),
                           f"{safe}.eml", "message/rfc822", use_container_width=True, key=f"eml_{key}_{ident}")
        c2.download_button("⬇️ Download .html", f'<html><head><meta charset="utf-8"></head><body>{html_body}</body></html>',
                           f"{safe}.html", "text/html", use_container_width=True, key=f"html_{key}_{ident}")
        c3.download_button("⬇️ Download plain .txt", f"Subject: {subj}\n\n{plain_text(text)}", f"{safe}.txt",
                           use_container_width=True, key=f"txt_{key}_{ident}")


def kpi_row(s):
    a, b, c, d_ = st.columns(4)
    a.metric("Target", money(s["target"]))
    b.metric("Achieved", money(s["achieved"]))
    c.metric("Attainment", pct(s["ach_pct"]))
    d_.metric("Reps ≥ 100%", f"{s['above_n']} / {s['reps_n']}")


# --------------------------------------------------------------------------
# UI
# --------------------------------------------------------------------------
def main():
    st.set_page_config(page_title="Region Mail Generator", page_icon="✉️", layout="wide")
    st.title("✉️ Region-wise Performance Mail Generator")
    st.caption("Upload the Performance Cockpit dashboard HTML file(s) – one per region, or one file with several regions – "
               "and get a ready-to-send mail for each region.")

    with st.sidebar:
        st.header("Mail settings")
        audience = st.radio("Audience", ["Client / leadership", "Internal team (BMs & reps)"])
        company = st.text_input("Your company", "Denave India")
        program = st.text_input("Program name", "Canon CPP")
        recipient = st.text_input("Recipient name (client mail)", "")
        to = st.text_input("To (email addresses)", "")
        cc = st.text_input("Cc (email addresses)", "")
        next_month = st.text_input("Next month label", "October")
        st.subheader("Mail content")
        snapshots = st.multiselect("Snapshots (charts) to include", list(SNAP_LABELS), default=list(SNAP_LABELS),
                                   format_func=lambda k: SNAP_LABELS[k])
        manual = st.checkbox("Include 'How to use the dashboard' guide", value=True)
        st.subheader("Signature")
        sender = st.text_input("Your name", "")
        designation = st.text_input("Designation", "")
        phone = st.text_input("Phone", "")
        email = st.text_input("Email", "")
        deadline = review_call = ""
        if audience.startswith("Internal"):
            st.subheader("Internal mail details")
            deadline = st.text_input("Recovery-plan deadline", "")
            review_call = st.text_input("Review call date/time", "")

    cfg = dict(snapshots=snapshots, manual=manual, to=to, cc=cc, audience=audience, company=company, program=program, recipient=recipient, next_month=next_month,
               sender=sender, designation=designation, phone=phone, email=email, deadline=deadline, review_call=review_call)

    files = st.file_uploader("Upload dashboard HTML file(s)", type=["html", "htm"], accept_multiple_files=True)
    if not files:
        st.info("👆 Upload a dashboard file to begin. You can upload North, South, East, West … files together.")
        return

    summaries = {}
    for f in files:
        try:
            data = load_dashboard(f.getvalue())
        except Exception as exc:  # noqa: BLE001
            st.error(f"**{f.name}**: {exc}")
            continue
        regions = list(data.get("perRegion", {}).keys()) or [r["Region"] for r in data.get("region", [])]
        for region in regions:
            try:
                summaries[region] = build_summary(data, region)
            except Exception as exc:  # noqa: BLE001
                st.error(f"**{f.name} / {region}**: could not read data ({exc})")

    if not summaries:
        return

    ordered = sorted(summaries.values(), key=lambda s: s["region"])
    labels = [s["region"] for s in ordered]
    if len(ordered) > 1:
        labels.append("All regions")
    tabs = st.tabs(labels)

    for tab, s in zip(tabs, ordered):
        with tab:
            kpi_row(s)
            imgs = charts_for_region(json.dumps(s, default=str), tuple(k for k in snapshots if k in REGION_SNAPS))
            render_mail_block(f"{s['region']}_{audience[:3]}", subject_for(s, cfg), build_mail(s, cfg), cfg, kpi=s, images=imgs)

    if len(ordered) > 1:
        with tabs[-1]:
            tot_t = sum(s["target"] for s in ordered)
            tot_a = sum(s["achieved"] for s in ordered)
            tot = {"target": tot_t, "achieved": tot_a, "ach_pct": tot_a / tot_t * 100 if tot_t else 0,
                   "above_n": sum(s["above_n"] for s in ordered), "reps_n": sum(s["reps_n"] for s in ordered)}
            kpi_row(tot)
            period = ordered[0]["period"]
            subj = (f"{short(cfg)} × {cfg['program']} | All Regions – {period} Performance: {pct(tot_a / tot_t * 100)} Target Achievement"
                    if audience.startswith("Client") else f"{period} Results – All Regions at {pct(tot_a / tot_t * 100)} of Target")
            imgs = charts_for_combined(json.dumps(ordered, default=str), tuple(k for k in snapshots if k in COMBINED_SNAPS))
            render_mail_block(f"All_{audience[:3]}", subj, combined_mail(ordered, cfg), cfg, kpi=tot, snapshot=sorted(ordered, key=lambda x: x['ach_pct'], reverse=True), images=imgs)

    st.caption("Numbers come straight from the uploaded dashboards. Please review the mail and add your own context before sending.")


if __name__ == "__main__":
    main()
