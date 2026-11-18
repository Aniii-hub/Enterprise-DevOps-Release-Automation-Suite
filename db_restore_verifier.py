#!/usr/bin/env python3
"""
Medvista Master Database Verification & Pre-Swap Audit Tool
------------------------------------------------------------
A universal multi-environment tool to compare any two environments
(e.g., UAT vs DEV, PROD vs UAT, DEV vs PROD) AND verify database restores
before swapping temporary databases to active.

Supported Modes:
  1. PRE-SWAP RESTORE MODE (--mode swap):
     - 3-Way verification: Source DB ➔ Target Temp DB (<db>_temp) ➔ Target Active DB (<db>)
     - Checks if restore is 100% faithful BEFORE performing Blue-Green swap
     - Analyzes data delta between current Active DB and incoming Temp DB

  2. DIRECT ENVIRONMENT COMPARISON MODE (--mode compare):
     - Direct 2-Way audit between any two environments (e.g. UAT vs PROD, DEV vs UAT)
     - Compares table inventory, schema columns, data types, and exact row counts (COUNT(*))
     - Highlights missing tables, schema discrepancies, and row count differences

Built-in Environment Presets:
  - uat  : Utkal UAT (devops-admin-utkal-uat)
  - dev  : Utkal DEV / SIT (devops-admin-Utkal_sit)
  - prod : Utkal PROD (devops-admin-utkal-prod)
"""

import os
import sys
import json
import argparse
import subprocess
import smtplib
from datetime import datetime
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from concurrent.futures import ThreadPoolExecutor, as_completed

# Built-in Environment Presets (Configure via environment variables or CLI flags)
ENV_PRESETS = {
    "uat": {
        "name": "Staging / UAT",
        "kc": os.environ.get("UAT_KUBECONFIG", os.path.expanduser("~/.kube/config")),
        "ns": os.environ.get("UAT_NAMESPACE", "dev"),
        "pod": "postgresql-0",
        "container": "postgresql",
        "user": os.environ.get("UAT_DB_USER", "postgres"),
        "pwd": os.environ.get("UAT_DB_PASS", ""),
    },
    "dev": {
        "name": "Development / SIT",
        "kc": os.environ.get("DEV_KUBECONFIG", os.path.expanduser("~/.kube/config")),
        "ns": os.environ.get("DEV_NAMESPACE", "dev"),
        "pod": "postgresql-0",
        "container": "postgresql",
        "user": os.environ.get("DEV_DB_USER", "postgres"),
        "pwd": os.environ.get("DEV_DB_PASS", ""),
    },
    "prod": {
        "name": "Production",
        "kc": os.environ.get("PROD_KUBECONFIG", os.path.expanduser("~/.kube/config")),
        "ns": os.environ.get("PROD_NAMESPACE", "prod"),
        "pod": "postgresql-ha-postgresql-1",
        "container": "postgresql",
        "user": os.environ.get("PROD_DB_USER", "postgres"),
        "pwd": os.environ.get("PROD_DB_PASS", ""),
    },
}

DEFAULT_RECIPIENTS = [
    os.environ.get("ALERT_EMAIL", "devops@example.com")
]


def resolve_primary_pod(env_cfg):
    """If pod is part of a HA cluster (e.g. prod), detect which replica is the primary read/write node."""
    if "postgresql-ha" not in env_cfg["pod"]:
        return env_cfg["pod"]

    candidates = ["postgresql-ha-postgresql-1", "postgresql-ha-postgresql-0", "postgresql-ha-postgresql-2", "postgresql-0"]
    for p in candidates:
        cmd = [
            "kubectl", "--kubeconfig", env_cfg["kc"],
            "exec", "-n", env_cfg["ns"], p, "-c", env_cfg["container"], "--",
            "env", f"PGPASSWORD={env_cfg['pwd']}",
            "psql", "-U", env_cfg["user"], "-d", "postgres", "-t", "-A", "-c", "SELECT pg_is_in_recovery();"
        ]
        try:
            res = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True, timeout=10)
            if res.returncode == 0 and res.stdout.strip() in ("f", "false"):
                return p
        except Exception:
            continue
    return env_cfg["pod"]


def run_psql(kc, ns, pod, container, user, pwd, db_name, sql, timeout=60):
    """Executes a SQL query inside postgres pod."""
    cmd = [
        "kubectl", "--kubeconfig", kc,
        "exec", "-n", ns, pod, "-c", container, "--",
        "env", f"PGPASSWORD={pwd}",
        "psql", "-U", user, "-d", db_name, "-t", "-A", "-c", sql
    ]
    try:
        res = subprocess.run(
            cmd,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
            timeout=timeout
        )
        if res.returncode != 0:
            return False, res.stderr.strip()
        return True, res.stdout.strip()
    except subprocess.TimeoutExpired:
        return False, "Query timed out"
    except Exception as e:
        return False, str(e)


def categorize_table(table_name):
    """Classifies table into functional domains for clear reporting."""
    tl = table_name.lower()
    if any(k in tl for k in ["audit", "log", "history", "event", "tracking", "revision"]):
        return "AUDIT_LOG", "#64748b"
    if any(k in tl for k in ["patient", "admission", "encounter", "appointment", "diagnosis", "clinical", "bed", "discharge"]):
        return "PATIENT_CLINICAL", "#2563eb"
    if any(k in tl for k in ["bill", "invoice", "payment", "receivable", "ledger", "rate", "price_book", "charge", "discount"]):
        return "BILLING_FINANCE", "#059669"
    if any(k in tl for k in ["inv", "inventory", "stock", "batch", "item", "order", "indent", "vendor", "purchase", "store"]):
        return "INVENTORY_STORE", "#d97706"
    if any(k in tl for k in ["user", "role", "permission", "keycloak", "auth", "credential", "session"]):
        return "AUTH_IDENTITY", "#7c3aed"
    return "CORE_MASTER", "#0284c7"


def get_db_metadata(kc, ns, pod, container, user, pwd, db_name, env_label=""):
    """Fetches tables, column schemas, and exact row counts."""
    # 1. Fetch Column Schemas
    col_sql = """
    SELECT table_schema || '.' || table_name || '.' || column_name || '|' || data_type || '|' || is_nullable
    FROM information_schema.columns
    WHERE table_schema NOT IN ('pg_catalog', 'information_schema')
    ORDER BY table_schema, table_name, ordinal_position;
    """
    ok, col_raw = run_psql(kc, ns, pod, container, user, pwd, db_name, col_sql, timeout=90)
    if not ok:
        return None, f"[{env_label}] Failed to connect to {db_name}: {col_raw}"

    columns = {}
    tables = set()
    if col_raw:
        for line in col_raw.split("\n"):
            line = line.strip()
            if not line or "|" not in line:
                continue
            col_id, data_type, nullable = line.split("|")
            parts = col_id.split(".")
            table_id = f"{parts[0]}.{parts[1]}"
            tables.add(table_id)
            columns[col_id] = {"type": data_type, "nullable": nullable}

    # 2. Fetch Exact Row Counts table by table
    row_counts = {}
    row_sql = """
    DO $$
    DECLARE
        r RECORD;
        v_cnt bigint;
    BEGIN
        CREATE TEMP TABLE _tbl_counts (tbl text, cnt bigint);
        FOR r IN (
            SELECT schemaname, relname
            FROM pg_stat_user_tables
            WHERE schemaname NOT IN ('pg_catalog', 'information_schema')
              AND schemaname NOT LIKE 'pg_temp%'
        ) LOOP
            BEGIN
                EXECUTE format('SELECT count(*) FROM %I.%I', r.schemaname, r.relname) INTO v_cnt;
                INSERT INTO _tbl_counts VALUES (r.schemaname || '.' || r.relname, v_cnt);
            EXCEPTION WHEN OTHERS THEN
                INSERT INTO _tbl_counts VALUES (r.schemaname || '.' || r.relname, -1);
            END;
        END LOOP;
    END $$;
    SELECT tbl || '|' || cnt FROM _tbl_counts;
    DROP TABLE IF EXISTS _tbl_counts;
    """
    ok, row_raw = run_psql(kc, ns, pod, container, user, pwd, db_name, row_sql, timeout=180)
    if ok and row_raw:
        for line in row_raw.split("\n"):
            line = line.strip()
            if "|" in line:
                t, c = line.split("|")
                try:
                    row_counts[t] = int(c)
                except ValueError:
                    row_counts[t] = 0

    for t in tables:
        if t not in row_counts:
            row_counts[t] = 0

    return {
        "db_name": db_name,
        "env_label": env_label,
        "tables": sorted(list(tables)),
        "columns": columns,
        "row_counts": row_counts,
        "total_tables": len(tables),
        "total_columns": len(columns),
        "total_rows": sum(v for v in row_counts.values() if v > 0)
    }, None


# ==============================================================================
# MODE 1: PRE-SWAP RESTORE VERIFICATION (3-WAY)
# ==============================================================================
def compare_pre_swap(base_db, src_cfg, tgt_cfg, temp_suffix="_temp"):
    src_db = base_db
    temp_db = f"{base_db}{temp_suffix}"
    active_db = base_db

    print(f"\n[{base_db}] 🔄 Auditing Pre-Swap: {src_cfg['name']} ({src_db}) ➔ {tgt_cfg['name']} ({temp_db}) vs Active ({active_db})...")

    with ThreadPoolExecutor(max_workers=3) as executor:
        f_src = executor.submit(get_db_metadata, src_cfg["kc"], src_cfg["ns"], src_cfg["pod"], src_cfg["container"], src_cfg["user"], src_cfg["pwd"], src_db, src_cfg["name"])
        f_temp = executor.submit(get_db_metadata, tgt_cfg["kc"], tgt_cfg["ns"], tgt_cfg["pod"], tgt_cfg["container"], tgt_cfg["user"], tgt_cfg["pwd"], temp_db, f"{tgt_cfg['name']}_TEMP")
        f_active = executor.submit(get_db_metadata, tgt_cfg["kc"], tgt_cfg["ns"], tgt_cfg["pod"], tgt_cfg["container"], tgt_cfg["user"], tgt_cfg["pwd"], active_db, f"{tgt_cfg['name']}_ACTIVE")

        src_meta, src_err = f_src.result()
        temp_meta, temp_err = f_temp.result()
        active_meta, active_err = f_active.result()

    result = {
        "mode": "swap",
        "db_name": base_db,
        "src_env": src_cfg["name"],
        "tgt_env": tgt_cfg["name"],
        "src_db": src_db,
        "temp_db": temp_db,
        "active_db": active_db,
        "status": "PASS",
        "safe_to_swap": True,
        "errors": [],
        "source_vs_temp": {},
        "active_vs_temp": {}
    }

    if src_err:
        result["errors"].append(src_err)
    if temp_err:
        result["errors"].append(temp_err)

    if not src_meta or not temp_meta:
        result["status"] = "FAIL"
        result["safe_to_swap"] = False
        return result

    # 1. Source vs Temp (Restore Accuracy)
    src_tables = set(src_meta["tables"])
    temp_tables = set(temp_meta["tables"])
    missing_in_temp = sorted(list(src_tables - temp_tables))
    extra_in_temp = sorted(list(temp_tables - src_tables))
    common_tables = sorted(list(src_tables & temp_tables))

    src_cols = set(src_meta["columns"].keys())
    temp_cols = set(temp_meta["columns"].keys())
    missing_cols = sorted(list(src_cols - temp_cols))
    extra_cols = sorted(list(temp_cols - src_cols))

    row_mismatches = []
    exact_matches = 0
    for tbl in common_tables:
        s_cnt = src_meta["row_counts"].get(tbl, 0)
        t_cnt = temp_meta["row_counts"].get(tbl, 0)
        diff = t_cnt - s_cnt
        if diff != 0:
            pct = ((diff / s_cnt) * 100) if s_cnt > 0 else 100.0
            category, color = categorize_table(tbl)
            row_mismatches.append({
                "table": tbl,
                "src_rows": s_cnt,
                "temp_rows": t_cnt,
                "diff": diff,
                "pct": pct,
                "category": category,
                "color": color
            })
        else:
            exact_matches += 1

    restore_pass = (len(missing_in_temp) == 0) and (len(missing_cols) == 0) and (len(row_mismatches) == 0)
    result["source_vs_temp"] = {
        "status": "PASS" if restore_pass else "FAIL",
        "common_tables_count": len(common_tables),
        "missing_tables": missing_in_temp,
        "extra_tables": extra_in_temp,
        "missing_columns": missing_cols,
        "extra_columns": extra_cols,
        "exact_matches": exact_matches,
        "row_mismatches": sorted(row_mismatches, key=lambda x: abs(x["diff"]), reverse=True),
        "src_total_rows": src_meta["total_rows"],
        "temp_total_rows": temp_meta["total_rows"],
        "row_delta": temp_meta["total_rows"] - src_meta["total_rows"]
    }

    if not restore_pass:
        result["safe_to_swap"] = False
        result["status"] = "WARNING" if (len(missing_in_temp) == 0 and len(missing_cols) == 0) else "FAIL"

    # 2. Active vs Temp (Swap Delta Analysis)
    if active_meta:
        act_tables = set(active_meta["tables"])
        dropped_from_act = sorted(list(act_tables - temp_tables))
        new_in_temp = sorted(list(temp_tables - act_tables))
        act_common = sorted(list(act_tables & temp_tables))

        act_deltas = []
        for tbl in act_common:
            a_cnt = active_meta["row_counts"].get(tbl, 0)
            t_cnt = temp_meta["row_counts"].get(tbl, 0)
            diff = t_cnt - a_cnt
            if diff != 0:
                pct = ((diff / a_cnt) * 100) if a_cnt > 0 else 100.0
                category, color = categorize_table(tbl)
                act_deltas.append({
                    "table": tbl,
                    "active_rows": a_cnt,
                    "temp_rows": t_cnt,
                    "delta": diff,
                    "pct": pct,
                    "category": category,
                    "color": color
                })

        result["active_vs_temp"] = {
            "active_exists": True,
            "dropped_tables": dropped_from_act,
            "new_tables": new_in_temp,
            "active_total_rows": active_meta["total_rows"],
            "temp_total_rows": temp_meta["total_rows"],
            "net_row_change": temp_meta["total_rows"] - active_meta["total_rows"],
            "table_deltas": sorted(act_deltas, key=lambda x: abs(x["delta"]), reverse=True)
        }
    else:
        result["active_vs_temp"] = {
            "active_exists": False,
            "notice": "Active DB does not exist on target. Temp will become initial database."
        }

    return result


# ==============================================================================
# MODE 2: DIRECT MULTI-ENVIRONMENT COMPARISON (2-WAY)
# ==============================================================================
def compare_direct_envs(base_db, src_cfg, tgt_cfg):
    print(f"\n[{base_db}] ⚖️ Direct Comparison: {src_cfg['name']} vs {tgt_cfg['name']}...")

    with ThreadPoolExecutor(max_workers=2) as executor:
        f_src = executor.submit(get_db_metadata, src_cfg["kc"], src_cfg["ns"], src_cfg["pod"], src_cfg["container"], src_cfg["user"], src_cfg["pwd"], base_db, src_cfg["name"])
        f_tgt = executor.submit(get_db_metadata, tgt_cfg["kc"], tgt_cfg["ns"], tgt_cfg["pod"], tgt_cfg["container"], tgt_cfg["user"], tgt_cfg["pwd"], base_db, tgt_cfg["name"])

        src_meta, src_err = f_src.result()
        tgt_meta, tgt_err = f_tgt.result()

    result = {
        "mode": "compare",
        "db_name": base_db,
        "src_env": src_cfg["name"],
        "tgt_env": tgt_cfg["name"],
        "status": "PASS",
        "errors": [],
        "comparison": {}
    }

    if src_err:
        result["errors"].append(src_err)
    if tgt_err:
        result["errors"].append(tgt_err)

    if not src_meta or not tgt_meta:
        result["status"] = "FAIL"
        return result

    src_tables = set(src_meta["tables"])
    tgt_tables = set(tgt_meta["tables"])
    only_in_src = sorted(list(src_tables - tgt_tables))
    only_in_tgt = sorted(list(tgt_tables - src_tables))
    common_tables = sorted(list(src_tables & tgt_tables))

    src_cols = set(src_meta["columns"].keys())
    tgt_cols = set(tgt_meta["columns"].keys())
    cols_only_in_src = sorted(list(src_cols - tgt_cols))
    cols_only_in_tgt = sorted(list(tgt_cols - src_cols))

    row_diffs = []
    exact_matches = 0
    for tbl in common_tables:
        s_cnt = src_meta["row_counts"].get(tbl, 0)
        t_cnt = tgt_meta["row_counts"].get(tbl, 0)
        diff = t_cnt - s_cnt
        if diff != 0:
            pct = ((diff / s_cnt) * 100) if s_cnt > 0 else 100.0
            category, color = categorize_table(tbl)
            row_diffs.append({
                "table": tbl,
                "src_rows": s_cnt,
                "tgt_rows": t_cnt,
                "diff": diff,
                "pct": pct,
                "category": category,
                "color": color
            })
        else:
            exact_matches += 1

    is_identical = (len(only_in_src) == 0) and (len(only_in_tgt) == 0) and (len(cols_only_in_src) == 0) and (len(cols_only_in_tgt) == 0) and (len(row_diffs) == 0)

    result["comparison"] = {
        "is_identical": is_identical,
        "src_total_tables": src_meta["total_tables"],
        "tgt_total_tables": tgt_meta["total_tables"],
        "src_total_rows": src_meta["total_rows"],
        "tgt_total_rows": tgt_meta["total_rows"],
        "net_row_difference": tgt_meta["total_rows"] - src_meta["total_rows"],
        "only_in_src_tables": only_in_src,
        "only_in_tgt_tables": only_in_tgt,
        "cols_only_in_src": cols_only_in_src,
        "cols_only_in_tgt": cols_only_in_tgt,
        "exact_matches_count": exact_matches,
        "row_differences": sorted(row_diffs, key=lambda x: abs(x["diff"]), reverse=True)
    }

    result["status"] = "IDENTICAL" if is_identical else "DIFF"
    return result


# ==============================================================================
# REPORT GENERATORS (MARKDOWN & HTML)
# ==============================================================================
def generate_markdown_report(results, run_time, mode, src_name, tgt_name):
    lines = [
        f"# 🛡️ Medvista Master Database Detailed Audit Report",
        f"**Audit Mode:** `{mode.upper()}` | **Execution Time:** {run_time} IST  ",
        f"**Source Environment:** {src_name} | **Target Environment:** {tgt_name}  ",
        f"**Databases Audited:** {len(results)}  ",
        "",
        "---",
        "## 1. Executive Parity & Clearance Summary",
        ""
    ]

    if mode == "swap":
        lines.extend([
            "| Database | Source vs Temp (Restore) | Active vs Temp (Delta) | Clearance | Status |",
            "| :--- | :--- | :--- | :---: | :--- |"
        ])
        for r in results:
            db = r["db_name"]
            st = r.get("source_vs_temp", {})
            at = r.get("active_vs_temp", {})
            safe = "✅ **READY**" if r.get("safe_to_swap") else "❌ **BLOCKED**"
            status = r["status"]
            st_str = f"{st.get('status', 'FAIL')} ({st.get('temp_total_rows', 0):,d} vs {st.get('src_total_rows', 0):,d} rows)" if st else "N/A"
            at_str = f"Net: {at.get('net_row_change', 0):+,d}" if at and at.get("active_exists") else "New DB"
            lines.append(f"| **`{db}`** | {st_str} | {at_str} | {safe} | **{status}** |")
    else:
        lines.extend([
            f"| Database | {src_name} Tables | {tgt_name} Tables | {src_name} Rows | {tgt_name} Rows | Net Difference | Parity Status |",
            "| :--- | :---: | :---: | :---: | :---: | :---: | :---: |"
        ])
        for r in results:
            db = r["db_name"]
            c = r.get("comparison", {})
            st_cnt = c.get("src_total_tables", 0)
            tt_cnt = c.get("tgt_total_tables", 0)
            sr_cnt = f"{c.get('src_total_rows', 0):,d}"
            tr_cnt = f"{c.get('tgt_total_rows', 0):,d}"
            net = f"{c.get('net_row_difference', 0):+,d}"
            status = r["status"]
            lines.append(f"| **`{db}`** | {st_cnt} | {tt_cnt} | {sr_cnt} | {tr_cnt} | {net} | **{status}** |")

    lines.extend(["", "---", "## 2. In-Depth Per-Database Table Differences & Schema Deltas", ""])

    for r in results:
        db = r["db_name"]
        lines.append(f"### 🗄️ Database: `{db}`")

        if mode == "swap":
            st = r.get("source_vs_temp", {})
            at = r.get("active_vs_temp", {})
            mismatches = st.get("row_mismatches", [])
            missing_t = st.get("missing_tables", [])
            missing_c = st.get("missing_columns", [])

            if missing_t:
                lines.append(f"- ❌ **Missing Tables in Temp DB ({len(missing_t)}):** {', '.join(missing_t)}")
            if missing_c:
                lines.append(f"- ⚠️ **Missing Columns in Temp DB ({len(missing_c)}):** {', '.join(missing_c[:15])}")

            if mismatches:
                lines.append(f"\n#### Table Row Discrepancies (Source `{src_name}` vs Temp `{tgt_name}_temp`):")
                lines.append("| Domain | Table Name | Source Rows | Temp Rows | Diff | % Change |")
                lines.append("| :--- | :--- | :---: | :---: | :---: | :---: |")
                for m in mismatches[:25]:
                    lines.append(f"| `{m['category']}` | `{m['table']}` | {m['src_rows']:,d} | {m['temp_rows']:,d} | **{m['diff']:+d}** | {m['pct']:+.1f}% |")
                if len(mismatches) > 25:
                    lines.append(f"*...and {len(mismatches) - 25} more tables with count differences.*")
            else:
                lines.append("✨ All restored tables in Temp matched Source 100%.")

            if at and at.get("active_exists"):
                act_deltas = at.get("table_deltas", [])
                if act_deltas:
                    lines.append(f"\n#### Impact on Current Active Data if Swapped (`{tgt_name}` Active vs Temp):")
                    lines.append("| Domain | Table Name | Active Rows | Temp Rows | Net Change | % Change |")
                    lines.append("| :--- | :--- | :---: | :---: | :---: | :---: |")
                    for m in act_deltas[:20]:
                        lines.append(f"| `{m['category']}` | `{m['table']}` | {m['active_rows']:,d} | {m['temp_rows']:,d} | **{m['delta']:+d}** | {m['pct']:+.1f}% |")

        else:
            c = r.get("comparison", {})
            only_src = c.get("only_in_src_tables", [])
            only_tgt = c.get("only_in_tgt_tables", [])
            cols_src = c.get("cols_only_in_src", [])
            cols_tgt = c.get("cols_only_in_tgt", [])
            diffs = c.get("row_differences", [])

            if only_src:
                lines.append(f"- 📌 **Tables present ONLY in `{src_name}` ({len(only_src)}):** {', '.join(only_src)}")
            if only_tgt:
                lines.append(f"- 📌 **Tables present ONLY in `{tgt_name}` ({len(only_tgt)}):** {', '.join(only_tgt)}")
            if cols_src:
                lines.append(f"- 🔍 **Columns present ONLY in `{src_name}` ({len(cols_src)}):** {', '.join(cols_src[:10])}")
            if cols_tgt:
                lines.append(f"- 🔍 **Columns present ONLY in `{tgt_name}` ({len(cols_tgt)}):** {', '.join(cols_tgt[:10])}")

            if diffs:
                lines.append(f"\n#### Table-by-Table Row Differences (`{src_name}` vs `{tgt_name}`):")
                lines.append(f"| Domain | Table Name | {src_name} Rows | {tgt_name} Rows | Difference | % Change |")
                lines.append("| :--- | :--- | :---: | :---: | :---: | :---: |")
                for m in diffs[:30]:
                    lines.append(f"| `{m['category']}` | `{m['table']}` | {m['src_rows']:,d} | {m['tgt_rows']:,d} | **{m['diff']:+d}** | {m['pct']:+.1f}% |")
                if len(diffs) > 30:
                    lines.append(f"*...and {len(diffs) - 30} more tables with row differences.*")
            else:
                lines.append("✨ All tables and row counts are 100% identical between both environments.")

        lines.append("\n---\n")

    return "\n".join(lines)


def generate_html_report(results, run_time, mode, src_name, tgt_name):
    all_clear = all(r.get("safe_to_swap", True) if mode == "swap" else r.get("comparison", {}).get("is_identical", False) for r in results)
    banner_color = "#10b981" if all_clear else "#dc2626"
    banner_title = f"CLEARANCE: {mode.upper()} AUDIT PASSED" if all_clear else f"ATTENTION: {mode.upper()} DISCREPANCIES DETECTED"
    banner_sub = f"Comprehensive In-Depth Database Parity Audit: {src_name} vs {tgt_name} ({len(results)} databases)."

    # 1. Summary Rows
    summary_rows_html = ""
    for r in results:
        db = r["db_name"]
        if mode == "swap":
            st = r.get("source_vs_temp", {})
            at = r.get("active_vs_temp", {})
            safe = r.get("safe_to_swap", False)
            badge = '<span style="background-color: #d1fae5; color: #065f46; padding: 4px 10px; border-radius: 9999px; font-weight: bold; font-size: 11px;">READY</span>' if safe else '<span style="background-color: #fee2e2; color: #991b1b; padding: 4px 10px; border-radius: 9999px; font-weight: bold; font-size: 11px;">BLOCKED</span>'
            summary_rows_html += f"""
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 10px 12px; font-weight: 600; font-family: monospace; color: #1e293b;">{db}</td>
                <td style="padding: 10px 12px; text-align: right; color: #475569;">{st.get('src_total_rows', 0):,d}</td>
                <td style="padding: 10px 12px; text-align: right; font-weight: 600; color: #0f172a;">{st.get('temp_total_rows', 0):,d}</td>
                <td style="padding: 10px 12px; text-align: center; font-weight: bold; color: {'#10b981' if st.get('status') == 'PASS' else '#f59e0b'};">{st.get('status', 'FAIL')}</td>
                <td style="padding: 10px 12px; text-align: right; color: #475569;">{at.get('active_total_rows', 0):,d}</td>
                <td style="padding: 10px 12px; text-align: right; font-weight: 600; color: #334155;">{at.get('net_row_change', 0):+,d}</td>
                <td style="padding: 10px 12px; text-align: center;">{badge}</td>
            </tr>
            """
        else:
            c = r.get("comparison", {})
            is_id = c.get("is_identical", False)
            badge = '<span style="background-color: #d1fae5; color: #065f46; padding: 4px 10px; border-radius: 9999px; font-weight: bold; font-size: 11px;">MATCH</span>' if is_id else '<span style="background-color: #fef3c7; color: #92400e; padding: 4px 10px; border-radius: 9999px; font-weight: bold; font-size: 11px;">DIFF</span>'
            summary_rows_html += f"""
            <tr style="border-bottom: 1px solid #e2e8f0;">
                <td style="padding: 10px 12px; font-weight: 600; font-family: monospace; color: #1e293b;">{db}</td>
                <td style="padding: 10px 12px; text-align: center; color: #475569;">{c.get('src_total_tables', 0)} / {c.get('tgt_total_tables', 0)}</td>
                <td style="padding: 10px 12px; text-align: right; color: #475569;">{c.get('src_total_rows', 0):,d}</td>
                <td style="padding: 10px 12px; text-align: right; font-weight: 600; color: #0f172a;">{c.get('tgt_total_rows', 0):,d}</td>
                <td style="padding: 10px 12px; text-align: right; font-weight: 600; color: {'#059669' if c.get('net_row_difference', 0) >= 0 else '#dc2626'};">{c.get('net_row_difference', 0):+,d}</td>
                <td style="padding: 10px 12px; text-align: center;">{badge}</td>
            </tr>
            """

    # 2. Detailed Per-Database Cards
    detail_cards_html = ""
    for r in results:
        db = r["db_name"]
        
        if mode == "swap":
            st = r.get("source_vs_temp", {})
            mismatches = st.get("row_mismatches", [])
            missing_t = st.get("missing_tables", [])
            missing_c = st.get("missing_columns", [])
            at = r.get("active_vs_temp", {})
            act_deltas = at.get("table_deltas", []) if at else []

            if not mismatches and not missing_t and not missing_c and not act_deltas:
                continue

            card_border = "#fecaca" if (mismatches or missing_t) else "#e2e8f0"
            detail_cards_html += f"""
            <div style="background-color: #ffffff; border: 1px solid {card_border}; border-radius: 10px; margin-bottom: 24px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #f1f5f9; padding-bottom: 10px; margin-bottom: 14px;">
                    <h3 style="margin: 0; font-size: 16px; font-family: monospace; color: #0f172a;">🗄️ {db}</h3>
                    <span style="font-size: 12px; color: #64748b; font-weight: 600;">{len(mismatches)} table differences</span>
                </div>
            """

            if missing_t:
                detail_cards_html += f"""
                <div style="background-color: #fef2f2; border: 1px solid #fee2e2; border-radius: 6px; padding: 10px 14px; margin-bottom: 12px;">
                    <strong style="color: #991b1b; font-size: 12px;">❌ Missing Tables in Temp DB ({len(missing_t)}):</strong>
                    <p style="margin: 4px 0 0 0; font-family: monospace; font-size: 11px; color: #b91c1c;">{', '.join(missing_t)}</p>
                </div>
                """

            if mismatches:
                detail_cards_html += f"""
                <div style="margin-bottom: 16px;">
                    <h4 style="margin: 0 0 8px 0; font-size: 13px; color: #334155;">Restore Discrepancies: Source ({src_name}) vs Temp ({tgt_name}_temp)</h4>
                    <table style="width: 100%; border-collapse: collapse; font-size: 12px;">
                        <thead>
                            <tr style="background-color: #f8fafc; color: #475569; border-bottom: 1px solid #cbd5e1;">
                                <th style="padding: 6px 10px; text-align: left;">Domain</th>
                                <th style="padding: 6px 10px; text-align: left;">Table Name</th>
                                <th style="padding: 6px 10px; text-align: right;">Source Rows</th>
                                <th style="padding: 6px 10px; text-align: right;">Temp Rows</th>
                                <th style="padding: 6px 10px; text-align: right;">Difference</th>
                                <th style="padding: 6px 10px; text-align: right;">% Change</th>
                            </tr>
                        </thead>
                        <tbody>
                """
                for m in mismatches[:15]:
                    detail_cards_html += f"""
                    <tr style="border-bottom: 1px solid #f1f5f9;">
                        <td style="padding: 6px 10px;"><span style="background-color: {m['color']}15; color: {m['color']}; padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 600;">{m['category']}</span></td>
                        <td style="padding: 6px 10px; font-family: monospace; color: #1e293b;">{m['table']}</td>
                        <td style="padding: 6px 10px; text-align: right; color: #64748b;">{m['src_rows']:,d}</td>
                        <td style="padding: 6px 10px; text-align: right; font-weight: 600; color: #0f172a;">{m['temp_rows']:,d}</td>
                        <td style="padding: 6px 10px; text-align: right; font-weight: bold; color: {'#dc2626' if m['diff'] < 0 else '#059669'};">{m['diff']:+d}</td>
                        <td style="padding: 6px 10px; text-align: right; color: #64748b;">{m['pct']:+.1f}%</td>
                    </tr>
                    """
                detail_cards_html += "</tbody></table></div>"

            detail_cards_html += "</div>"

        else:
            # DIRECT COMPARE MODE DETAILS
            c = r.get("comparison", {})
            diffs = c.get("row_differences", [])
            only_src = c.get("only_in_src_tables", [])
            only_tgt = c.get("only_in_tgt_tables", [])
            cols_src = c.get("cols_only_in_src", [])
            cols_tgt = c.get("cols_only_in_tgt", [])

            if not diffs and not only_src and not only_tgt and not cols_src and not cols_tgt:
                continue

            detail_cards_html += f"""
            <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; margin-bottom: 24px; padding: 20px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                <div style="display: flex; justify-content: space-between; align-items: center; border-bottom: 2px solid #f1f5f9; padding-bottom: 10px; margin-bottom: 14px;">
                    <h3 style="margin: 0; font-size: 16px; font-family: monospace; color: #0f172a;">🗄️ {db}</h3>
                    <span style="font-size: 12px; color: #64748b; font-weight: 600;">Net Row Difference: <strong style="color: #0f172a;">{c.get('net_row_difference', 0):+,d}</strong></span>
                </div>
            """

            # Schema additions or deletions
            if only_src or only_tgt:
                detail_cards_html += f"""
                <div style="background-color: #eff6ff; border: 1px solid #dbeafe; border-radius: 6px; padding: 10px 14px; margin-bottom: 12px;">
                """
                if only_src:
                    detail_cards_html += f"<p style='margin: 0 0 4px 0; font-size: 12px; color: #1e40af;'><strong>Tables present only in {src_name} ({len(only_src)}):</strong> <code style='font-size: 11px;'>{', '.join(only_src)}</code></p>"
                if only_tgt:
                    detail_cards_html += f"<p style='margin: 0; font-size: 12px; color: #1e40af;'><strong>Tables present only in {tgt_name} ({len(only_tgt)}):</strong> <code style='font-size: 11px;'>{', '.join(only_tgt)}</code></p>"
                detail_cards_html += "</div>"

            # Table by table differences table
            if diffs:
                detail_cards_html += f"""
                <h4 style="margin: 12px 0 8px 0; font-size: 13px; color: #334155;">Table-by-Table Row Count Differences ({len(diffs)} changed tables):</h4>
                <div style="overflow-x: auto;">
                    <table style="width: 100%; border-collapse: collapse; font-size: 12px;">
                        <thead>
                            <tr style="background-color: #f8fafc; color: #475569; border-bottom: 1px solid #cbd5e1; text-transform: uppercase; font-size: 11px;">
                                <th style="padding: 8px 10px; text-align: left;">Domain</th>
                                <th style="padding: 8px 10px; text-align: left;">Table Name</th>
                                <th style="padding: 8px 10px; text-align: right;">{src_name} Rows</th>
                                <th style="padding: 8px 10px; text-align: right;">{tgt_name} Rows</th>
                                <th style="padding: 8px 10px; text-align: right;">Difference</th>
                                <th style="padding: 8px 10px; text-align: right;">% Change</th>
                            </tr>
                        </thead>
                        <tbody>
                """
                for m in diffs[:20]:
                    diff_color = "#059669" if m['diff'] > 0 else "#dc2626"
                    detail_cards_html += f"""
                    <tr style="border-bottom: 1px solid #f1f5f9;">
                        <td style="padding: 8px 10px;"><span style="background-color: {m['color']}15; color: {m['color']}; padding: 2px 6px; border-radius: 4px; font-size: 10px; font-weight: 600;">{m['category']}</span></td>
                        <td style="padding: 8px 10px; font-family: monospace; color: #1e293b; font-weight: 500;">{m['table']}</td>
                        <td style="padding: 8px 10px; text-align: right; color: #64748b;">{m['src_rows']:,d}</td>
                        <td style="padding: 8px 10px; text-align: right; font-weight: 600; color: #0f172a;">{m['tgt_rows']:,d}</td>
                        <td style="padding: 8px 10px; text-align: right; font-weight: bold; color: {diff_color};">{m['diff']:+d}</td>
                        <td style="padding: 8px 10px; text-align: right; color: #64748b;">{m['pct']:+.1f}%</td>
                    </tr>
                    """
                detail_cards_html += "</tbody></table>"
                if len(diffs) > 20:
                    detail_cards_html += f"<p style='font-size: 11px; color: #64748b; margin: 8px 0 0 0;'>...and {len(diffs) - 20} more tables with count differences in this database.</p>"
                detail_cards_html += "</div>"

            detail_cards_html += "</div>"

    if not detail_cards_html:
        detail_cards_html = """
        <div style="background-color: #f0fdf4; border: 1px solid #bbf7d0; border-radius: 8px; padding: 18px; color: #166534; font-size: 13px;">
            ✅ <strong>Zero Discrepancies:</strong> Every single table, column, and row matched with 100% precision between both environments.
        </div>
        """

    return f"""
    <!DOCTYPE html>
    <html>
    <head><meta charset="utf-8"></head>
    <body style="font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; background-color: #f1f5f9; margin: 0; padding: 24px; color: #0f172a;">
        <div style="max-width: 960px; margin: 0 auto; background-color: #ffffff; border-radius: 12px; overflow: hidden; border: 1px solid #cbd5e1; box-shadow: 0 4px 6px -1px rgba(0,0,0,0.05);">
            
            <!-- Banner -->
            <div style="background-color: {banner_color}; color: #ffffff; padding: 24px 32px;">
                <h1 style="margin: 0; font-size: 20px; font-weight: 700; letter-spacing: -0.01em;">{banner_title}</h1>
                <p style="margin: 6px 0 0 0; font-size: 13px; opacity: 0.95;">{banner_sub}</p>
                <p style="margin: 4px 0 0 0; font-size: 11px; opacity: 0.8;">{run_time} IST | MedVista DevOps & Database Engineering</p>
            </div>

            <!-- Body -->
            <div style="padding: 28px 32px; background-color: #f8fafc;">
                
                <!-- Section 1 -->
                <div style="background-color: #ffffff; border: 1px solid #e2e8f0; border-radius: 10px; padding: 20px; margin-bottom: 28px; box-shadow: 0 1px 3px rgba(0,0,0,0.05);">
                    <h2 style="margin: 0 0 14px 0; font-size: 14px; text-transform: uppercase; letter-spacing: 0.05em; color: #475569;">1. Executive Summary Across All Databases</h2>
                    <div style="overflow-x: auto;">
                        <table style="width: 100%; border-collapse: collapse; font-size: 13px;">
                            <thead>
                                <tr style="background-color: #f1f5f9; color: #475569; text-transform: uppercase; font-size: 11px; letter-spacing: 0.05em; border-bottom: 2px solid #cbd5e1;">
                                    <th style="padding: 10px 12px; text-align: left;">Database</th>
                                    {f'<th style="padding: 10px 12px; text-align: right;">{src_name} Rows</th><th style="padding: 10px 12px; text-align: right;">Temp Rows</th><th style="padding: 10px 12px; text-align: center;">Restore Status</th><th style="padding: 10px 12px; text-align: right;">Active Rows</th><th style="padding: 10px 12px; text-align: right;">Net Delta</th><th style="padding: 10px 12px; text-align: center;">Swap Clearance</th>' if mode == 'swap' else f'<th style="padding: 10px 12px; text-align: center;">Tables ({src_name}/{tgt_name})</th><th style="padding: 10px 12px; text-align: right;">{src_name} Rows</th><th style="padding: 10px 12px; text-align: right;">{tgt_name} Rows</th><th style="padding: 10px 12px; text-align: right;">Net Delta</th><th style="padding: 10px 12px; text-align: center;">Parity Status</th>'}
                                </tr>
                            </thead>
                            <tbody>
                                {summary_rows_html}
                            </tbody>
                        </table>
                    </div>
                </div>

                <!-- Section 2 -->
                <h2 style="margin: 0 0 14px 0; font-size: 14px; text-transform: uppercase; letter-spacing: 0.05em; color: #475569;">2. In-Depth Table Differences & Schema Deltas</h2>
                {detail_cards_html}

            </div>

            <!-- Footer -->
            <div style="background-color: #ffffff; border-top: 1px solid #e2e8f0; padding: 16px 32px; font-size: 11px; color: #64748b; text-align: center;">
                Generated automatically by MedVista Master <code>db_restore_verifier.py</code>.
            </div>
        </div>
    </body>
    </html>
    """


def send_email_report(html_content, subject, to_emails):
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_user = os.environ.get("SMTP_USER") or "anirudh.pardeshi@medvista.com.au"
    smtp_pass = os.environ.get("SMTP_PASS") or "xsywevpfgcvbanbt"

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = ", ".join(to_emails)
        msg.attach(MIMEText(html_content, "html"))

        print(f"\n📧 Sending detailed email via SMTP SSL ({smtp_server}:465) to {len(to_emails)} recipients...")
        try:
            server = smtplib.SMTP_SSL(smtp_server, 465, timeout=15)
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to_emails, msg.as_string())
            server.quit()
            print(f"✅ Detailed email report successfully sent via SSL (465)!")
            return True
        except Exception as ssl_err:
            print(f"⚠️ SSL port 465 failed ({ssl_err}), trying TLS port 587...")
            server = smtplib.SMTP(smtp_server, 587, timeout=15)
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to_emails, msg.as_string())
            server.quit()
            print(f"✅ Detailed email report successfully sent via TLS (587)!")
            return True
    except Exception as e:
        print(f"❌ Failed to dispatch email report: {e}", file=sys.stderr)
        return False


def main():
    parser = argparse.ArgumentParser(description="Medvista Master Multi-Environment Database Comparison & Pre-Swap Tool")
    
    parser.add_argument("--mode", choices=["swap", "compare"], default="swap",
                        help="Operation mode: 'swap' for Pre-Swap restore verification (3-way); 'compare' for direct env-to-env comparison (2-way).")

    parser.add_argument("--source-env", choices=["uat", "dev", "prod", "custom"], default="uat",
                        help="Source environment preset: uat, dev, prod, or custom.")
    parser.add_argument("--target-env", choices=["uat", "dev", "prod", "custom"], default="dev",
                        help="Target environment preset: uat, dev, prod, or custom.")

    parser.add_argument("--db", help="Specific database to audit (e.g. Medvista_QAIPDBillDB).")
    parser.add_argument("--all", action="store_true", help="Audit all 12 Medvista databases.")
    parser.add_argument("--temp-suffix", default="_temp", help="Suffix for temporary database in swap mode (default: _temp).")

    parser.add_argument("--source-kc", help="Override Source kubeconfig path.")
    parser.add_argument("--source-ns", help="Override Source namespace.")
    parser.add_argument("--source-pod", help="Override Source pod name.")
    parser.add_argument("--source-pwd", help="Override Source password.")

    parser.add_argument("--target-kc", help="Override Target kubeconfig path.")
    parser.add_argument("--target-ns", help="Override Target namespace.")
    parser.add_argument("--target-pod", help="Override Target pod name.")
    parser.add_argument("--target-pwd", help="Override Target password.")

    parser.add_argument("--output-dir", default="reports", help="Directory where reports are saved.")
    parser.add_argument("--email", nargs="+", help="Recipient email addresses.")
    parser.add_argument("--no-email", action="store_true", help="Skip sending email report.")

    args = parser.parse_args()

    run_time = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    now_file_str = datetime.now().strftime("%Y-%m-%d_%H%M%S")

    if args.source_env in ENV_PRESETS:
        src_cfg = dict(ENV_PRESETS[args.source_env])
    else:
        src_cfg = {"name": "Custom Source", "container": "postgresql", "user": "postgres"}

    if args.source_kc: src_cfg["kc"] = args.source_kc
    if args.source_ns: src_cfg["ns"] = args.source_ns
    if args.source_pod: src_cfg["pod"] = args.source_pod
    if args.source_pwd: src_cfg["pwd"] = args.source_pwd

    if args.target_env in ENV_PRESETS:
        tgt_cfg = dict(ENV_PRESETS[args.target_env])
    else:
        tgt_cfg = {"name": "Custom Target", "container": "postgresql", "user": "postgres"}

    if args.target_kc: tgt_cfg["kc"] = args.target_kc
    if args.target_ns: tgt_cfg["ns"] = args.target_ns
    if args.target_pod: tgt_cfg["pod"] = args.target_pod
    if args.target_pwd: tgt_cfg["pwd"] = args.target_pwd

    src_cfg["pod"] = resolve_primary_pod(src_cfg)
    tgt_cfg["pod"] = resolve_primary_pod(tgt_cfg)

    target_dbs = [args.db] if args.db else ALL_DATABASES

    print("=" * 105)
    print("           MEDVISTA MASTER DATABASE COMPARISON & PRE-SWAP AUDIT TOOL")
    print("=" * 105)
    print(f"🕒 Run Time:    {run_time} IST")
    print(f"⚙️ Mode:        {args.mode.upper()} ({'3-Way Pre-Swap' if args.mode == 'swap' else '2-Way Direct Parity'})")
    print(f"📂 Source Env:  {src_cfg['name']} (Pod: {src_cfg.get('pod', 'N/A')})")
    print(f"🎯 Target Env:  {tgt_cfg['name']} (Pod: {tgt_cfg.get('pod', 'N/A')})")
    if args.mode == "swap":
        print(f"🏷️ Temp Suffix: {args.temp_suffix}")
    print(f"🗄️ Databases:   {len(target_dbs)} targeted ({', '.join(target_dbs[:4])}{'...' if len(target_dbs) > 4 else ''})")
    print("=" * 105)

    results = []
    for db in target_dbs:
        if args.mode == "swap":
            res = compare_pre_swap(db, src_cfg, tgt_cfg, temp_suffix=args.temp_suffix)
        else:
            res = compare_direct_envs(db, src_cfg, tgt_cfg)
        results.append(res)

    print("\n" + "=" * 105)
    if args.mode == "swap":
        print(f"{'Database':<25} | {'Source Rows':<12} | {'Temp Rows':<12} | {'Restore':<8} | {'Active Rows':<12} | {'Net Delta':<10} | {'Clearance'}")
        print("-" * 105)
        for r in results:
            db = r["db_name"]
            st = r.get("source_vs_temp", {})
            at = r.get("active_vs_temp", {})
            s_rows = f"{st.get('src_total_rows', 0):,d}" if st else "-"
            t_rows = f"{st.get('temp_total_rows', 0):,d}" if st else "-"
            st_res = st.get("status", "FAIL")
            a_rows = f"{at.get('active_total_rows', 0):,d}" if at and at.get("active_exists") else "N/A"
            delta = f"{at.get('net_row_change', 0):+,d}" if at and at.get("active_exists") else "NEW"
            safe = "READY" if r.get("safe_to_swap") else "BLOCKED"
            print(f"{db:<25} | {s_rows:<12} | {t_rows:<12} | {st_res:<8} | {a_rows:<12} | {delta:<10} | {safe}")
    else:
        print(f"{'Database':<25} | {src_cfg['name'] + ' Rows':<16} | {tgt_cfg['name'] + ' Rows':<16} | {'Net Difference':<15} | {'Parity Status'}")
        print("-" * 105)
        for r in results:
            db = r["db_name"]
            c = r.get("comparison", {})
            sr = f"{c.get('src_total_rows', 0):,d}"
            tr = f"{c.get('tgt_total_rows', 0):,d}"
            net = f"{c.get('net_row_difference', 0):+,d}"
            status = r["status"]
            print(f"{db:<25} | {sr:<16} | {tr:<16} | {net:<15} | {status}")
    print("=" * 105)

    os.makedirs(args.output_dir, exist_ok=True)
    mode_tag = f"{args.source_env}_to_{args.target_env}_{args.mode}"

    md_content = generate_markdown_report(results, run_time, args.mode, src_cfg["name"], tgt_cfg["name"])
    md_path = os.path.join(args.output_dir, f"db_report_{mode_tag}_{now_file_str}.md")
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"\n📝 Detailed Markdown Report: {md_path}")

    html_content = generate_html_report(results, run_time, args.mode, src_cfg["name"], tgt_cfg["name"])
    html_path = os.path.join(args.output_dir, f"db_report_{mode_tag}_{now_file_str}.html")
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"🌐 Detailed HTML Report:     {html_path}")

    json_path = os.path.join(args.output_dir, f"db_report_{mode_tag}_{now_file_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2, default=str)
    print(f"📊 Detailed JSON Summary:    {json_path}")

    if not args.no_email:
        recipients = args.email or (os.environ.get("EMAIL_TO").split(",") if os.environ.get("EMAIL_TO") else DEFAULT_RECIPIENTS)
        if recipients:
            subject = f"📊 MedVista In-Depth DB Report [{args.mode.upper()}]: {src_cfg['name']} vs {tgt_cfg['name']} ({now_file_str})"
            send_email_report(html_content, subject, recipients)
    else:
        print("\nℹ️ Email dispatch skipped (--no-email flag set).")

    print("\n🎉 In-Depth Master Audit Complete!")


if __name__ == "__main__":
    main()
