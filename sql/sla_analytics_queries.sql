-- =============================================================================
-- Customer Support & Telecom Operations SLA Analytics Queries (SQLite / ANSI SQL)
-- Author: Sanjay V G
-- Description: Production SQL queries powering Looker Studio / Tableau KPI views
-- =============================================================================

-- Query 1: SLA Compliance Rate & Breach Count by Issue Category and Priority
WITH ticket_metrics AS (
    SELECT
        issue_category,
        priority,
        COUNT(*) AS total_tickets,
        SUM(CASE WHEN status = 'Resolved' THEN 1 ELSE 0 END) AS resolved_tickets,
        SUM(sla_breached_flag) AS sla_breaches,
        AVG(first_response_minutes) AS avg_frt_minutes,
        AVG(CASE WHEN resolution_hours IS NOT NULL THEN resolution_hours END) AS avg_mttr_hours,
        AVG(csat_score) AS avg_csat
    FROM support_tickets
    GROUP BY issue_category, priority
)
SELECT
    issue_category,
    priority,
    total_tickets,
    resolved_tickets,
    sla_breaches,
    ROUND(100.0 * (total_tickets - sla_breaches) / NULLIF(total_tickets, 0), 2) AS sla_compliance_pct,
    ROUND(avg_frt_minutes, 1) AS avg_first_response_min,
    ROUND(avg_mttr_hours, 2) AS avg_mttr_hours,
    ROUND(avg_csat, 2) AS avg_csat_score,
    RANK() OVER (
        PARTITION BY priority
        ORDER BY (100.0 * sla_breaches / NULLIF(total_tickets, 0)) DESC
    ) AS breach_severity_rank
FROM ticket_metrics
ORDER BY
    CASE priority
        WHEN 'P1 - Critical' THEN 1
        WHEN 'P2 - High' THEN 2
        WHEN 'P3 - Medium' THEN 3
        ELSE 4
    END,
    sla_breaches DESC;


-- Query 2: Carrier Partner Performance (FRT, MTTR, Escalation & SLA Compliance)
SELECT
    c.carrier_id,
    c.carrier_name,
    c.region,
    c.partner_tier,
    c.target_carrier_sla_hours,
    COUNT(t.ticket_id) AS total_tickets,
    SUM(t.escalated_flag) AS escalated_tickets,
    SUM(t.sla_breached_flag) AS sla_breaches,
    ROUND(100.0 * (COUNT(t.ticket_id) - SUM(t.sla_breached_flag)) / NULLIF(COUNT(t.ticket_id), 0), 2) AS sla_compliance_pct,
    ROUND(AVG(t.first_response_minutes), 1) AS mean_frt_minutes,
    ROUND(AVG(t.resolution_hours), 2) AS mean_mttr_hours,
    ROUND(AVG(t.csat_score), 2) AS avg_csat,
    RANK() OVER (
        ORDER BY (100.0 * (COUNT(t.ticket_id) - SUM(t.sla_breached_flag)) / NULLIF(COUNT(t.ticket_id), 0)) DESC
    ) AS carrier_sla_rank
FROM support_tickets t
INNER JOIN carriers_reference c
    ON t.carrier_id = c.carrier_id
GROUP BY
    c.carrier_id,
    c.carrier_name,
    c.region,
    c.partner_tier,
    c.target_carrier_sla_hours
ORDER BY sla_compliance_pct DESC, mean_mttr_hours ASC;


-- Query 3: Root-Cause Pareto Analysis of Ticket Volume & Escalations
WITH category_totals AS (
    SELECT
        issue_category,
        COUNT(*) AS ticket_volume,
        SUM(escalated_flag) AS escalation_count,
        SUM(sla_breached_flag) AS breach_count,
        ROUND(AVG(resolution_hours), 2) AS avg_resolution_hours
    FROM support_tickets
    GROUP BY issue_category
),
grand_total AS (
    SELECT SUM(ticket_volume) AS all_tickets FROM category_totals
)
SELECT
    ct.issue_category,
    ct.ticket_volume,
    ROUND(100.0 * ct.ticket_volume / gt.all_tickets, 2) AS volume_share_pct,
    ROUND(
        SUM(100.0 * ct.ticket_volume / gt.all_tickets) OVER (
            ORDER BY ct.ticket_volume DESC
            ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW
        ),
        2
    ) AS cumulative_pareto_pct,
    ct.escalation_count,
    ROUND(100.0 * ct.escalation_count / NULLIF(ct.ticket_volume, 0), 2) AS escalation_rate_pct,
    ct.breach_count,
    ROUND(100.0 * ct.breach_count / NULLIF(ct.ticket_volume, 0), 2) AS breach_rate_pct,
    ct.avg_resolution_hours
FROM category_totals ct
CROSS JOIN grand_total gt
ORDER BY ct.ticket_volume DESC;


-- Query 4: Weekly Ticket Volume, Resolved vs. Backlog & SLA Turnaround Trend
SELECT
    strftime('%Y-W%W', created_at) AS week_bucket,
    MIN(DATE(created_at)) AS week_start_date,
    COUNT(*) AS total_tickets,
    SUM(CASE WHEN status = 'Resolved' THEN 1 ELSE 0 END) AS resolved_count,
    SUM(CASE WHEN status != 'Resolved' THEN 1 ELSE 0 END) AS backlog_count,
    SUM(sla_breached_flag) AS sla_breaches,
    ROUND(100.0 * (COUNT(*) - SUM(sla_breached_flag)) / NULLIF(COUNT(*), 0), 2) AS weekly_sla_compliance_pct,
    ROUND(AVG(resolution_hours), 2) AS avg_mttr_hours,
    ROUND(AVG(sla_target_hours), 2) AS avg_target_sla_hours
FROM support_tickets
GROUP BY strftime('%Y-W%W', created_at)
ORDER BY week_bucket ASC;


-- Query 5: High-Touch / Repeat Escalation Accounts (>2 SLA Breaches)
WITH account_rollup AS (
    SELECT
        t.account_id,
        t.account_name,
        COUNT(*) AS total_tickets,
        SUM(t.escalated_flag) AS total_escalations,
        SUM(t.sla_breached_flag) AS total_sla_breaches,
        ROUND(AVG(t.resolution_hours), 2) AS avg_mttr_hours,
        ROUND(AVG(t.csat_score), 2) AS avg_csat,
        GROUP_CONCAT(DISTINCT t.issue_category) AS recurring_issue_areas
    FROM support_tickets t
    GROUP BY t.account_id, t.account_name
)
SELECT
    account_id,
    account_name,
    total_tickets,
    total_escalations,
    total_sla_breaches,
    ROUND(100.0 * total_sla_breaches / NULLIF(total_tickets, 0), 1) AS breach_rate_pct,
    avg_mttr_hours,
    avg_csat,
    recurring_issue_areas,
    RANK() OVER (ORDER BY total_sla_breaches DESC, total_escalations DESC) AS risk_rank
FROM account_rollup
WHERE total_sla_breaches > 2
ORDER BY total_sla_breaches DESC, total_tickets DESC;
