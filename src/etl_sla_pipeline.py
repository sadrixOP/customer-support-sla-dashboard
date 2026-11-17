"""Customer Support & Telecom Operations SLA Analytics ETL Pipeline.

Generates realistic telecom/NACH support tickets, loads them into SQLite,
executes SQL analytics queries from sql/sla_analytics_queries.sql, exports
Looker Studio / Tableau-ready CSV datasets, builds a high-resolution BI
dashboard PNG screenshot, and generates a standalone Looker-style HTML dashboard.
"""

from __future__ import annotations

import json
import sqlite3
from datetime import datetime, timedelta
from pathlib import Path

import matplotlib.gridspec as gridspec
import matplotlib.pyplot as plt
import numpy as np
import pandas as pd

BASE_DIR = Path(__file__).resolve().parent.parent
DATA_DIR = BASE_DIR / "data"
SQL_PATH = BASE_DIR / "sql" / "sla_analytics_queries.sql"
OUTPUT_DIR = BASE_DIR / "output"
DASHBOARD_DIR = BASE_DIR / "dashboard"
SCREENSHOT_DIR = BASE_DIR / "screenshots"

CARRIERS_META = [
    ("CAR-01", "Airtel", "India", "Tier-1", 8.0, 0.22, 0.94),
    ("CAR-02", "Jio", "India", "Tier-1", 8.0, 0.21, 0.95),
    ("CAR-03", "Vodafone Idea", "India", "Tier-2", 10.0, 0.14, 0.86),
    ("CAR-04", "AT&T", "North America", "Tier-1", 6.0, 0.14, 0.93),
    ("CAR-05", "Verizon", "North America", "Tier-1", 6.0, 0.12, 0.94),
    ("CAR-06", "Twilio Super Network", "Global", "Core Network", 4.0, 0.17, 0.97),
]

ISSUE_CATEGORIES = {
    "Porting/LNP Rejection": {"weight": 0.26, "base_mttr": 9.2, "escalation_p": 0.24},
    "Sender ID / DLT Registration": {"weight": 0.22, "base_mttr": 7.5, "escalation_p": 0.16},
    "API Webhook 4xx/5xx Error": {"weight": 0.18, "base_mttr": 4.4, "escalation_p": 0.18},
    "NACH Mandate Exception": {"weight": 0.14, "base_mttr": 6.8, "escalation_p": 0.15},
    "Toll-Free Verification": {"weight": 0.12, "base_mttr": 11.4, "escalation_p": 0.21},
    "Carrier Outage": {"weight": 0.08, "base_mttr": 3.8, "escalation_p": 0.35},
}

PRIORITY_SPECS = {
    "P1 - Critical": {"weight": 0.12, "sla_hours": 4.0, "frt_range": (4, 18), "mttr_mult": 0.42},
    "P2 - High": {"weight": 0.28, "sla_hours": 8.0, "frt_range": (10, 40), "mttr_mult": 0.72},
    "P3 - Medium": {"weight": 0.42, "sla_hours": 24.0, "frt_range": (20, 95), "mttr_mult": 1.15},
    "P4 - Low": {"weight": 0.18, "sla_hours": 48.0, "frt_range": (35, 180), "mttr_mult": 1.45},
}

ACCOUNTS = [
    ("ACC-101", "FinServe India Pvt Ltd"),
    ("ACC-102", "PaySwift Technologies"),
    ("ACC-103", "CloudCart Global"),
    ("ACC-104", "QuickLend Microfinance"),
    ("ACC-105", "MedConnect Telehealth"),
    ("ACC-106", "BharatLogistics Corp"),
    ("ACC-107", "NovaBank Digital"),
    ("ACC-108", "RidePulse Mobility"),
    ("ACC-109", "EduStream Learning"),
    ("ACC-110", "OmniRetail Solutions"),
    ("ACC-111", "SecureAuth Systems"),
    ("ACC-112", "Apex Insurance Hub"),
]


def ensure_directories() -> None:
    """Create required project directories if missing."""
    for folder in (DATA_DIR, OUTPUT_DIR, DASHBOARD_DIR, SCREENSHOT_DIR):
        folder.mkdir(parents=True, exist_ok=True)


def generate_datasets(num_tickets: int = 1200) -> tuple[pd.DataFrame, pd.DataFrame]:
    """Generate deterministic carriers reference and raw support ticket datasets."""
    rng = np.random.default_rng(42)

    carriers_df = pd.DataFrame(
        [
            {
                "carrier_id": cid,
                "carrier_name": name,
                "region": region,
                "partner_tier": tier,
                "target_carrier_sla_hours": target_sla,
            }
            for cid, name, region, tier, target_sla, _, _ in CARRIERS_META
        ]
    )

    carrier_ids = [c[0] for c in CARRIERS_META]
    carrier_weights = [c[5] for c in CARRIERS_META]
    carrier_reliability = {c[0]: c[6] for c in CARRIERS_META}

    cat_names = list(ISSUE_CATEGORIES.keys())
    cat_weights = [ISSUE_CATEGORIES[k]["weight"] for k in cat_names]

    prio_names = list(PRIORITY_SPECS.keys())
    prio_weights = [PRIORITY_SPECS[k]["weight"] for k in prio_names]

    # Bias a few high-touch accounts so repeat SLA breach queries are meaningful
    acc_weights = np.array([0.14, 0.13, 0.11, 0.10, 0.08, 0.08, 0.07, 0.07, 0.06, 0.06, 0.05, 0.05])
    channels = ["Email", "API Portal", "Voice", "Carrier Webhook"]
    channel_weights = [0.46, 0.25, 0.19, 0.10]

    start_date = datetime(2026, 8, 10, 8, 0, 0)
    total_span_hours = 12 * 7 * 24  # 12 weeks

    records: list[dict[str, object]] = []
    for idx in range(1, num_tickets + 1):
        created_offset_h = float(rng.uniform(0, total_span_hours))
        created_at = start_date + timedelta(hours=created_offset_h)

        acc_idx = int(rng.choice(len(ACCOUNTS), p=acc_weights))
        account_id, account_name = ACCOUNTS[acc_idx]

        carrier_id = str(rng.choice(carrier_ids, p=carrier_weights))
        category = str(rng.choice(cat_names, p=cat_weights))
        priority = str(rng.choice(prio_names, p=prio_weights))
        channel = str(rng.choice(channels, p=channel_weights))

        cat_spec = ISSUE_CATEGORIES[category]
        prio_spec = PRIORITY_SPECS[priority]
        sla_target = prio_spec["sla_hours"]

        frt_min = float(rng.uniform(*prio_spec["frt_range"]))
        first_response_at = created_at + timedelta(minutes=frt_min)

        # Recent tickets in the last 36 hours may still be open/backlog
        is_backlog = (total_span_hours - created_offset_h) < 36 and rng.random() < 0.42
        if is_backlog:
            status = str(rng.choice(["In Progress", "Pending Carrier"], p=[0.45, 0.55]))
            resolved_at_str = None
            resolution_hours = None
            elapsed_hours = total_span_hours - created_offset_h
            sla_breached = int(elapsed_hours > sla_target)
            escalated = int(sla_breached or rng.random() < cat_spec["escalation_p"])
            csat = None
        else:
            status = "Resolved"
            reliability = carrier_reliability[carrier_id]
            # Higher reliability means fewer tail breaches
            breach_roll = rng.random()
            force_breach = breach_roll > reliability
            mean_mttr = cat_spec["base_mttr"] * prio_spec["mttr_mult"]
            if force_breach:
                res_hours = float(sla_target * rng.uniform(1.05, 1.85))
            else:
                res_hours = float(np.clip(rng.gamma(2.2, mean_mttr / 2.2), 0.35, sla_target * 0.94))

            resolution_hours = round(res_hours, 2)
            resolved_at = created_at + timedelta(hours=resolution_hours)
            resolved_at_str = resolved_at.strftime("%Y-%m-%d %H:%M:%S")
            sla_breached = int(resolution_hours > sla_target)
            escalated = int(sla_breached or rng.random() < cat_spec["escalation_p"])
            base_csat = 4.75 if not sla_breached else 3.45
            csat = round(float(np.clip(rng.normal(base_csat, 0.35), 1.5, 5.0)), 1)

        records.append(
            {
                "ticket_id": f"TKT-{10000 + idx}",
                "account_id": account_id,
                "account_name": account_name,
                "created_at": created_at.strftime("%Y-%m-%d %H:%M:%S"),
                "first_response_at": first_response_at.strftime("%Y-%m-%d %H:%M:%S"),
                "resolved_at": resolved_at_str,
                "status": status,
                "priority": priority,
                "issue_category": category,
                "channel": channel,
                "carrier_id": carrier_id,
                "sla_target_hours": sla_target,
                "first_response_minutes": round(frt_min, 1),
                "resolution_hours": resolution_hours,
                "sla_breached_flag": sla_breached,
                "escalated_flag": escalated,
                "csat_score": csat,
            }
        )

    tickets_df = pd.DataFrame(records)
    carriers_df.to_csv(DATA_DIR / "carriers_reference.csv", index=False, encoding="utf-8")
    tickets_df.to_csv(DATA_DIR / "support_tickets_raw.csv", index=False, encoding="utf-8")
    return tickets_df, carriers_df


def load_and_clean_with_pandas(
    tickets_df: pd.DataFrame, carriers_df: pd.DataFrame
) -> pd.DataFrame:
    """Clean types, enrich with carrier info and derived SLA metrics via Pandas."""
    df = tickets_df.copy()
    df["created_at_dt"] = pd.to_datetime(df["created_at"])
    df["week_bucket"] = df["created_at_dt"].dt.strftime("%Y-W%W")
    df["sla_status"] = np.where(df["sla_breached_flag"] == 1, "Breached", "Within SLA")
    df["resolution_hours_filled"] = df["resolution_hours"].fillna(df["sla_target_hours"] * 0.85)
    enriched = df.merge(carriers_df, on="carrier_id", how="left")
    enriched.to_csv(OUTPUT_DIR / "tickets_enriched_for_bi.csv", index=False, encoding="utf-8")
    return enriched


def load_sqlite_and_run_sql(
    tickets_df: pd.DataFrame, carriers_df: pd.DataFrame
) -> dict[str, pd.DataFrame]:
    """Load data into SQLite and execute all analytics queries from sql/sla_analytics_queries.sql."""
    db_path = DATA_DIR / "support_ops.db"
    if db_path.exists():
        db_path.unlink()

    conn = sqlite3.connect(db_path)
    try:
        carriers_df.to_sql("carriers_reference", conn, if_exists="replace", index=False)
        tickets_df.to_sql("support_tickets", conn, if_exists="replace", index=False)

        sql_text = SQL_PATH.read_text(encoding="utf-8")
        statements = [stmt.strip() for stmt in sql_text.split(";") if stmt.strip()]

        output_names = [
            "kpi_summary_by_category",
            "carrier_sla_performance",
            "root_cause_pareto",
            "weekly_ticket_trends",
            "high_touch_repeat_accounts",
        ]
        if len(statements) != len(output_names):
            raise ValueError(
                f"Expected {len(output_names)} SQL queries, found {len(statements)}."
            )

        results: dict[str, pd.DataFrame] = {}
        for name, query in zip(output_names, statements):
            df_res = pd.read_sql_query(query, conn)
            df_res.to_csv(OUTPUT_DIR / f"{name}.csv", index=False, encoding="utf-8")
            results[name] = df_res
        return results
    finally:
        conn.close()


def render_dashboard_png(
    enriched_df: pd.DataFrame, sql_results: dict[str, pd.DataFrame]
) -> Path:
    """Create a 300 DPI dark-themed BI executive dashboard screenshot."""
    plt.rcParams.update(
        {
            "figure.facecolor": "#0B1120",
            "axes.facecolor": "#111827",
            "axes.edgecolor": "#1F2937",
            "axes.labelcolor": "#E5E7EB",
            "text.color": "#F9FAFB",
            "xtick.color": "#9CA3AF",
            "ytick.color": "#D1D5DB",
            "grid.color": "#1F2937",
            "font.family": "DejaVu Sans",
        }
    )

    total_tickets = len(enriched_df)
    sla_compliance = 100.0 * (1.0 - enriched_df["sla_breached_flag"].mean())
    avg_mttr = enriched_df["resolution_hours"].dropna().mean()
    avg_csat = enriched_df["csat_score"].dropna().mean()

    fig = plt.figure(figsize=(16, 11.2), dpi=300)
    gs = gridspec.GridSpec(3, 2, height_ratios=[0.36, 1.0, 1.0], hspace=0.38, wspace=0.22)

    # Header & KPI Banner
    ax_header = fig.add_subplot(gs[0, :])
    ax_header.axis("off")
    ax_header.text(
        0.0,
        0.92,
        "CUSTOMER SUPPORT & TELECOM OPERATIONS — SLA ANALYTICS DASHBOARD",
        fontsize=17,
        fontweight="bold",
        color="#F8FAFC",
        transform=ax_header.transAxes,
    )
    ax_header.text(
        0.0,
        0.74,
        "Pipeline: SQLite SQL (CTEs & Window Functions) + Python Pandas ETL  |  12-Week Operational Window (N = 1,200 Tickets)",
        fontsize=10.5,
        color="#94A3B8",
        transform=ax_header.transAxes,
    )

    kpis = [
        ("TOTAL TICKETS", f"{total_tickets:,}", "Resolved + Active Backlog", "#38BDF8"),
        ("SLA COMPLIANCE", f"{sla_compliance:.1f}%", "Target Threshold: 90.0%", "#10B981"),
        ("AVG RESOLUTION (MTTR)", f"{avg_mttr:.2f} hrs", "Weighted Across P1-P4", "#F59E0B"),
        ("CUSTOMER CSAT", f"{avg_csat:.2f} / 5.0", "Post-Resolution Survey", "#A78BFA"),
    ]
    for i, (label, val, sub, accent) in enumerate(kpis):
        x0 = i * 0.253
        rect = plt.Rectangle(
            (x0, 0.04), 0.235, 0.58, transform=ax_header.transAxes,
            facecolor="#111827", edgecolor=accent, linewidth=1.6, clip_on=False
        )
        ax_header.add_patch(rect)
        ax_header.text(x0 + 0.014, 0.48, label, fontsize=8.5, fontweight="bold", color="#94A3B8", transform=ax_header.transAxes)
        ax_header.text(x0 + 0.014, 0.25, val, fontsize=16, fontweight="bold", color=accent, transform=ax_header.transAxes)
        ax_header.text(x0 + 0.014, 0.09, sub, fontsize=8.0, color="#CBD5E1", transform=ax_header.transAxes)

    # Panel 1: Root-Cause Volume & Breach Rate
    pareto = sql_results["root_cause_pareto"].sort_values("ticket_volume", ascending=True)
    ax1 = fig.add_subplot(gs[1, 0])
    bars = ax1.barh(pareto["issue_category"], pareto["ticket_volume"], color="#38BDF8", alpha=0.88, height=0.58)
    ax1.set_title("1. Ticket Volume & SLA Breach Rate by Root-Cause Category", fontsize=11.5, fontweight="bold", loc="left", pad=10)
    ax1.set_xlabel("Ticket Volume")
    ax1.grid(axis="x", linestyle="--", alpha=0.5)
    ax1.set_xlim(0, pareto["ticket_volume"].max() * 1.32)
    for bar, (_, row) in zip(bars, pareto.iterrows()):
        ax1.text(
            bar.get_width() + 5,
            bar.get_y() + bar.get_height() / 2,
            f"{int(row['ticket_volume'])} ({row['breach_rate_pct']:.1f}% breach)",
            va="center",
            fontsize=8.8,
            color="#E2E8F0",
        )

    # Panel 2: Weekly MTTR vs Target SLA Trend
    weekly = sql_results["weekly_ticket_trends"]
    ax2 = fig.add_subplot(gs[1, 1])
    x_idx = np.arange(len(weekly))
    ax2.plot(x_idx, weekly["avg_mttr_hours"], marker="o", linewidth=2.3, color="#F59E0B", label="Actual Avg MTTR (hrs)")
    ax2.plot(x_idx, weekly["avg_target_sla_hours"], linestyle="--", linewidth=1.8, color="#10B981", label="Avg SLA Target (hrs)")
    ax2.fill_between(x_idx, weekly["avg_mttr_hours"], alpha=0.14, color="#F59E0B")
    ax2.set_title("2. Weekly SLA Turnaround Time (MTTR vs Target SLA)", fontsize=11.5, fontweight="bold", loc="left", pad=10)
    ax2.set_ylabel("Hours")
    ax2.set_xticks(x_idx)
    ax2.set_xticklabels([w.split("-")[1] for w in weekly["week_bucket"]], fontsize=8.5)
    ax2.grid(True, linestyle="--", alpha=0.45)
    ax2.legend(frameon=False, fontsize=8.8, loc="upper right")

    # Panel 3: Carrier Partner SLA Compliance & Mean MTTR
    carriers = sql_results["carrier_sla_performance"].sort_values("sla_compliance_pct", ascending=True)
    ax3 = fig.add_subplot(gs[2, 0])
    colors_c = ["#EF4444" if pct < 90 else "#10B981" if pct >= 94 else "#F59E0B" for pct in carriers["sla_compliance_pct"]]
    cbars = ax3.barh(carriers["carrier_name"], carriers["sla_compliance_pct"], color=colors_c, height=0.56)
    ax3.axvline(90.0, color="#94A3B8", linestyle=":", linewidth=1.3, label="90% SLA Floor")
    ax3.set_title("3. Carrier Partner SLA Compliance (%) & Mean MTTR", fontsize=11.5, fontweight="bold", loc="left", pad=10)
    ax3.set_xlabel("SLA Compliance (%)")
    ax3.set_xlim(70, 105)
    ax3.grid(axis="x", linestyle="--", alpha=0.45)
    for bar, (_, row) in zip(cbars, carriers.iterrows()):
        ax3.text(
            bar.get_width() + 0.6,
            bar.get_y() + bar.get_height() / 2,
            f"{row['sla_compliance_pct']:.1f}% | {row['mean_mttr_hours']:.1f}h",
            va="center",
            fontsize=8.8,
            color="#E2E8F0",
        )

    # Panel 4: Priority Distribution & Escalations
    prio_df = (
        enriched_df.groupby("priority", as_index=False)
        .agg(total=("ticket_id", "count"), escalated=("escalated_flag", "sum"), breaches=("sla_breached_flag", "sum"))
    )
    order_map = {"P1 - Critical": 0, "P2 - High": 1, "P3 - Medium": 2, "P4 - Low": 3}
    prio_df["ord"] = prio_df["priority"].map(order_map)
    prio_df = prio_df.sort_values("ord")

    ax4 = fig.add_subplot(gs[2, 1])
    px = np.arange(len(prio_df))
    w = 0.27
    ax4.bar(px - w, prio_df["total"], width=w, label="Total Tickets", color="#38BDF8")
    ax4.bar(px, prio_df["escalated"], width=w, label="Escalated", color="#A78BFA")
    ax4.bar(px + w, prio_df["breaches"], width=w, label="SLA Breaches", color="#EF4444")
    ax4.set_title("4. Ticket Priority Breakdown: Volume, Escalations & Breaches", fontsize=11.5, fontweight="bold", loc="left", pad=10)
    ax4.set_xticks(px)
    ax4.set_xticklabels(prio_df["priority"], fontsize=9)
    ax4.set_ylabel("Ticket Count")
    ax4.grid(axis="y", linestyle="--", alpha=0.45)
    ax4.legend(frameon=False, fontsize=8.8, loc="upper right")

    out_path = SCREENSHOT_DIR / "sla_analytics_dashboard.png"
    fig.savefig(out_path, dpi=300, bbox_inches="tight", facecolor=fig.get_facecolor())
    plt.close(fig)
    return out_path


def render_html_dashboard(
    enriched_df: pd.DataFrame, sql_results: dict[str, pd.DataFrame]
) -> Path:
    """Write a self-contained Looker Studio-styled interactive HTML dashboard."""
    payload = {
        "kpis": {
            "total_tickets": int(len(enriched_df)),
            "sla_compliance_pct": round(float(100.0 * (1.0 - enriched_df["sla_breached_flag"].mean())), 2),
            "avg_mttr_hours": round(float(enriched_df["resolution_hours"].dropna().mean()), 2),
            "avg_csat": round(float(enriched_df["csat_score"].dropna().mean()), 2),
            "escalated_count": int(enriched_df["escalated_flag"].sum()),
        },
        "pareto": sql_results["root_cause_pareto"].to_dict(orient="records"),
        "carriers": sql_results["carrier_sla_performance"].to_dict(orient="records"),
        "weekly": sql_results["weekly_ticket_trends"].to_dict(orient="records"),
        "repeat_accounts": sql_results["high_touch_repeat_accounts"].to_dict(orient="records"),
    }
    json_blob = json.dumps(payload, indent=2)

    html = f"""<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8" />
<meta name="viewport" content="width=device-width, initial-scale=1.0" />
<title>Customer Support & SLA Analytics — BI Dashboard</title>
<style>
  :root {{
    --bg: #0b1120; --card: #111827; --border: #1f2937;
    --text: #f8fafc; --muted: #94a3b8;
    --blue: #38bdf8; --green: #10b981; --amber: #f59e0b; --red: #ef4444; --purple: #a78bfa;
  }}
  * {{ box-sizing: border-box; font-family: -apple-system, BlinkMacSystemFont, "Segoe UI", Roboto, sans-serif; }}
  body {{ margin: 0; background: var(--bg); color: var(--text); padding: 24px; }}
  .container {{ max-width: 1320px; margin: 0 auto; }}
  header {{ display: flex; justify-content: space-between; align-items: flex-end; border-bottom: 1px solid var(--border); padding-bottom: 14px; margin-bottom: 20px; flex-wrap: wrap; gap: 12px; }}
  h1 {{ margin: 0; font-size: 22px; }}
  .subtitle {{ color: var(--muted); font-size: 13px; margin-top: 4px; }}
  .badge {{ background: #1e293b; border: 1px solid #334155; padding: 6px 12px; border-radius: 6px; font-size: 12px; color: var(--blue); }}
  .kpi-grid {{ display: grid; grid-template-columns: repeat(auto-fit, minmax(210px, 1fr)); gap: 16px; margin-bottom: 22px; }}
  .kpi {{ background: var(--card); border: 1px solid var(--border); border-top: 3px solid var(--blue); border-radius: 8px; padding: 16px; }}
  .kpi.green {{ border-top-color: var(--green); }} .kpi.amber {{ border-top-color: var(--amber); }} .kpi.purple {{ border-top-color: var(--purple); }}
  .kpi-label {{ font-size: 11px; text-transform: uppercase; letter-spacing: 0.06em; color: var(--muted); }}
  .kpi-val {{ font-size: 28px; font-weight: 700; margin: 6px 0 4px; }}
  .kpi-sub {{ font-size: 12px; color: var(--muted); }}
  .grid-2 {{ display: grid; grid-template-columns: 1fr 1fr; gap: 18px; margin-bottom: 18px; }}
  @media (max-width: 960px) {{ .grid-2 {{ grid-template-columns: 1fr; }} }}
  .card {{ background: var(--card); border: 1px solid var(--border); border-radius: 8px; padding: 18px; }}
  .card h2 {{ font-size: 15px; margin: 0 0 14px; color: #e2e8f0; }}
  .bar-row {{ margin-bottom: 10px; }}
  .bar-meta {{ display: flex; justify-content: space-between; font-size: 12px; margin-bottom: 4px; }}
  .bar-track {{ height: 10px; background: #1e293b; border-radius: 99px; overflow: hidden; }}
  .bar-fill {{ height: 100%; border-radius: 99px; }}
  table {{ width: 100%; border-collapse: collapse; font-size: 12.5px; }}
  th, td {{ text-align: left; padding: 8px 10px; border-bottom: 1px solid var(--border); }}
  th {{ color: var(--muted); font-weight: 600; font-size: 11px; text-transform: uppercase; }}
  .pill {{ padding: 2px 8px; border-radius: 99px; font-size: 11px; font-weight: 600; }}
  .pill-ok {{ background: rgba(16,185,129,0.15); color: var(--green); }}
  .pill-warn {{ background: rgba(245,158,11,0.15); color: var(--amber); }}
  .pill-bad {{ background: rgba(239,68,68,0.15); color: var(--red); }}
</style>
</head>
<body>
<div class="container">
  <header>
    <div>
      <h1>Customer Support &amp; SLA Analytics Dashboard</h1>
      <div class="subtitle">SQLite SQL Queries + Python Pandas ETL | Looker Studio / Tableau Public Source View</div>
    </div>
    <div class="badge">Dataset: 1,200 Operations &amp; Telecom Tickets</div>
  </header>

  <section class="kpi-grid" id="kpis"></section>

  <div class="grid-2">
    <div class="card">
      <h2>Root-Cause Category Volume &amp; Pareto Share</h2>
      <div id="pareto-bars"></div>
    </div>
    <div class="card">
      <h2>Carrier Partner SLA Performance</h2>
      <table id="carrier-table"></table>
    </div>
  </div>

  <div class="grid-2">
    <div class="card">
      <h2>Weekly Ticket &amp; MTTR Turnaround Trend</h2>
      <table id="weekly-table"></table>
    </div>
    <div class="card">
      <h2>High-Touch Repeat Escalation Accounts (&gt;2 SLA Breaches)</h2>
      <table id="accounts-table"></table>
    </div>
  </div>
</div>

<script>
const DATA = {json_blob};

document.getElementById("kpis").innerHTML = `
  <div class="kpi"><div class="kpi-label">Total Tickets</div><div class="kpi-val" style="color:var(--blue)">${{DATA.kpis.total_tickets.toLocaleString()}}</div><div class="kpi-sub">12-Week Operational Period</div></div>
  <div class="kpi green"><div class="kpi-label">SLA Compliance</div><div class="kpi-val" style="color:var(--green)">${{DATA.kpis.sla_compliance_pct}}%</div><div class="kpi-sub">Target Floor: 90.0%</div></div>
  <div class="kpi amber"><div class="kpi-label">Avg Resolution (MTTR)</div><div class="kpi-val" style="color:var(--amber)">${{DATA.kpis.avg_mttr_hours}} hrs</div><div class="kpi-sub">Escalated: ${{DATA.kpis.escalated_count}} tickets</div></div>
  <div class="kpi purple"><div class="kpi-label">Average CSAT</div><div class="kpi-val" style="color:var(--purple)">${{DATA.kpis.avg_csat}} / 5.0</div><div class="kpi-sub">Resolved Ticket Surveys</div></div>
`;

const maxVol = Math.max(...DATA.pareto.map(r => r.ticket_volume));
document.getElementById("pareto-bars").innerHTML = DATA.pareto.map(r => `
  <div class="bar-row">
    <div class="bar-meta">
      <span>${{r.issue_category}}</span>
      <span>${{r.ticket_volume}} tickets (${{r.volume_share_pct}}% | Breach: ${{r.breach_rate_pct}}%)</span>
    </div>
    <div class="bar-track"><div class="bar-fill" style="width:${{(r.ticket_volume/maxVol)*100}}%;background:var(--blue)"></div></div>
  </div>
`).join("");

document.getElementById("carrier-table").innerHTML = `
  <thead><tr><th>Carrier</th><th>Region</th><th>Tickets</th><th>SLA %</th><th>Mean MTTR</th></tr></thead>
  <tbody>${{DATA.carriers.map(c => {{
    const cls = c.sla_compliance_pct >= 94 ? "pill-ok" : (c.sla_compliance_pct >= 90 ? "pill-warn" : "pill-bad");
    return `<tr><td>${{c.carrier_name}}</td><td>${{c.region}}</td><td>${{c.total_tickets}}</td><td><span class="pill ${{cls}}">${{c.sla_compliance_pct}}%</span></td><td>${{c.mean_mttr_hours}}h</td></tr>`;
  }}).join("")}}</tbody>
`;

document.getElementById("weekly-table").innerHTML = `
  <thead><tr><th>Week</th><th>Total</th><th>Resolved</th><th>Backlog</th><th>SLA %</th><th>MTTR</th></tr></thead>
  <tbody>${{DATA.weekly.slice(-8).map(w => `
    <tr><td>${{w.week_bucket}}</td><td>${{w.total_tickets}}</td><td>${{w.resolved_count}}</td><td>${{w.backlog_count}}</td><td>${{w.weekly_sla_compliance_pct}}%</td><td>${{w.avg_mttr_hours}}h</td></tr>
  `).join("")}}</tbody>
`;

document.getElementById("accounts-table").innerHTML = `
  <thead><tr><th>Account</th><th>Tickets</th><th>Breaches</th><th>Breach %</th><th>CSAT</th></tr></thead>
  <tbody>${{DATA.repeat_accounts.slice(0, 8).map(a => `
    <tr><td>${{a.account_name}}</td><td>${{a.total_tickets}}</td><td><span class="pill pill-bad">${{a.total_sla_breaches}}</span></td><td>${{a.breach_rate_pct}}%</td><td>${{a.avg_csat}}</td></tr>
  `).join("")}}</tbody>
`;
</script>
</body>
</html>
"""
    out_html = DASHBOARD_DIR / "index.html"
    out_html.write_text(html, encoding="utf-8")
    return out_html


def main() -> None:
    """Execute end-to-end SLA analytics ETL, SQL queries, and dashboard generation."""
    ensure_directories()
    tickets_df, carriers_df = generate_datasets(num_tickets=1200)
    enriched_df = load_and_clean_with_pandas(tickets_df, carriers_df)
    sql_results = load_sqlite_and_run_sql(tickets_df, carriers_df)
    png_path = render_dashboard_png(enriched_df, sql_results)
    html_path = render_html_dashboard(enriched_df, sql_results)

    print(f"[Project 1] Generated {len(tickets_df)} tickets -> {DATA_DIR / 'support_ops.db'}")
    print(f"[Project 1] Exported {len(sql_results)} SQL summary CSVs -> {OUTPUT_DIR}")
    print(f"[Project 1] Saved BI dashboard screenshot -> {png_path}")
    print(f"[Project 1] Saved interactive HTML dashboard -> {html_path}")


if __name__ == "__main__":
    main()
