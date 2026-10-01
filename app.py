"""
Denave x Canon CPP - Region-wise Performance Mail Generator
-----------------------------------------------------------
Upload one or more "Daily Performance Cockpit" dashboard HTML files
(e.g. the North file, the South file ...). The app reads the data embedded in
each file and writes a ready-to-send mail draft for every region found,
plus a combined "All regions" mail when more than one region is uploaded.

Run:  streamlit run app.py
"""

import calendar
import json
import re
import urllib.parse
from collections import Counter
from datetime import date
from email.message import EmailMessage

import streamlit as st

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
    }


# --------------------------------------------------------------------------
# Mail writers
# --------------------------------------------------------------------------
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
    L += ["", "We would value 30 minutes to walk through the dashboards and align on the next-month plan." if client
          else f"Let's keep the momentum going into {cfg['next_month']}.", "", signature(cfg)]
    return "\n".join(L)


def build_mail(s, cfg):
    return client_mail(s, cfg) if cfg["audience"] == "Client / leadership" else internal_mail(s, cfg)


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------
def make_eml(subject, body):
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["X-Unsent"] = "1"  # opens as an editable draft in Outlook
    msg.set_content(body)
    return msg.as_bytes()


def render_mail_block(key, subject, body):
    subj = st.text_input("Subject", subject, key=f"subj_{key}")
    text = st.text_area("Mail body (editable)", body, height=520, key=f"body_{key}")
    c1, c2, c3 = st.columns(3)
    safe = re.sub(r"[^A-Za-z0-9_-]+", "_", key)
    c1.download_button("⬇️ Download .eml (opens in Outlook)", make_eml(subj, text), f"{safe}.eml", "message/rfc822", use_container_width=True)
    c2.download_button("⬇️ Download .txt", f"Subject: {subj}\n\n{text}", f"{safe}.txt", use_container_width=True)
    mailto = "mailto:?subject=" + urllib.parse.quote(subj) + "&body=" + urllib.parse.quote(text)
    if len(mailto) < 1900:
        c3.link_button("✉️ Open in mail app", mailto, use_container_width=True)
    else:
        c3.caption("Mail is too long for a mailto link – use the .eml download.")
    with st.expander("Copy-friendly view"):
        st.code(f"Subject: {subj}\n\n{text}", language=None, wrap_lines=True)


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
        next_month = st.text_input("Next month label", "October")
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

    cfg = dict(audience=audience, company=company, program=program, recipient=recipient, next_month=next_month,
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
            render_mail_block(f"{s['region']}_{audience[:3]}", subject_for(s, cfg), build_mail(s, cfg))

    if len(ordered) > 1:
        with tabs[-1]:
            tot_t = sum(s["target"] for s in ordered)
            tot_a = sum(s["achieved"] for s in ordered)
            kpi_row({"target": tot_t, "achieved": tot_a, "ach_pct": tot_a / tot_t * 100 if tot_t else 0,
                     "above_n": sum(s["above_n"] for s in ordered), "reps_n": sum(s["reps_n"] for s in ordered)})
            period = ordered[0]["period"]
            subj = (f"{short(cfg)} × {cfg['program']} | All Regions – {period} Performance: {pct(tot_a / tot_t * 100)} Target Achievement"
                    if audience.startswith("Client") else f"{period} Results – All Regions at {pct(tot_a / tot_t * 100)} of Target")
            render_mail_block(f"All_{audience[:3]}", subj, combined_mail(ordered, cfg))

    st.caption("Numbers come straight from the uploaded dashboards. Please review the mail and add your own context before sending.")


if __name__ == "__main__":
    main()
