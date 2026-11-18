#!/usr/bin/env python3
"""
Daily Cross-Org Branch Analyzer, Auto-Refresher & Change Report Generator
Organizations:
  - Source Org: MedVista-Pvt-Ltd (branches: RMH-SIT, VM-QA)
  - Target Org: MedVista-Global-Pvt-Ltd (branches: RMH-SIT, Dev-k8s)

Rules & Automations:
  1. RMH-SIT ➔ RMH-SIT:
     - Scans for latest commits on MedVista-Pvt-Ltd.
     - IF changes detected: Automatically copies and merges latest RMH-SIT code into MedVista-Global-Pvt-Ltd.
  2. VM-QA ➔ Dev-k8s:
     - Scans and detects pending changes.
     - NEVER auto-merges VM-QA (Report-only notification).

Recipients:
  - devops@medvista.com.au
"""

import os
import sys
import json
import re
import smtplib
import tempfile
import shutil
import subprocess
import urllib.request
import urllib.error
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from datetime import datetime
import argparse

SRC_ORG = "MedVista-Pvt-Ltd"
TGT_ORG = "MedVista-Global-Pvt-Ltd"
TOKEN = os.environ.get("PAT_TOKEN") or os.environ.get("GITHUB_TOKEN", "")

DEFAULT_RECIPIENTS = [
    os.environ.get("ALERT_EMAIL", "devops@example.com")
]

BRANCH_PAIRS = [
    {"source_branch": "RMH-SIT", "target_branch": "RMH-SIT", "label": "RMH-SIT (Pvt-Ltd) ➔ RMH-SIT (Global)"},
    {"source_branch": "VM-QA", "target_branch": "Dev-k8s", "label": "VM-QA (Pvt-Ltd) ➔ Dev-k8s (Global)"}
]

HEADERS = {
    "Authorization": f"token {TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "MedVista-Branch-Analyzer"
}


def make_api_request(url):
    req = urllib.request.Request(url, headers=HEADERS)
    try:
        with urllib.request.urlopen(req) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as e:
        if e.code in (404, 422):
            return None
        print(f"⚠️ HTTP Error {e.code} for URL {url}: {e.reason}", file=sys.stderr)
        return None
    except Exception as e:
        print(f"⚠️ Request failed for {url}: {e}", file=sys.stderr)
        return None


def get_org_repos(org_name):
    repos = {}
    page = 1
    while True:
        url = f"https://api.github.com/orgs/{org_name}/repos?per_page=100&page={page}"
        data = make_api_request(url)
        if not data:
            break
        for repo in data:
            if not repo.get("archived", False):
                repos[repo["name"]] = repo
        page += 1
    return repos


def map_source_to_target_repo(src_name, tgt_repos):
    if src_name in tgt_repos:
        return src_name

    if src_name == "VM-LT-UI-Lifetrenz-Core":
        return "Medvista-UI-Core" if "Medvista-UI-Core" in tgt_repos else None
    if src_name == "VM-LT-UI-Lib-Iis-Configuration":
        return "Medvista-UI-Lib-IIis-Configuration" if "Medvista-UI-Lib-IIis-Configuration" in tgt_repos else None
    if src_name == "VM-LT-BE-User-Management-Service":
        if "Medvista-BE-User-Service" in tgt_repos:
            return "Medvista-BE-User-Service"
        if "Medvista-BE-User-Management-Service" in tgt_repos:
            return "Medvista-BE-User-Management-Service"


    cand1 = re.sub(r"^(VM-)?LT-", "Medvista-", src_name, flags=re.IGNORECASE)
    cand2 = re.sub(r"^VM-", "Medvista-", cand1, flags=re.IGNORECASE)

    for cand in [cand1, cand2]:
        if cand in tgt_repos:
            return cand
        for t_name in tgt_repos:
            if cand.lower() == t_name.lower():
                return t_name

    return None


def get_latest_commit(org_name, repo_name, branch):
    url = f"https://api.github.com/repos/{org_name}/{repo_name}/commits/{branch}"
    data = make_api_request(url)
    if not data:
        return None
    commit_info = data.get("commit", {})
    author_info = commit_info.get("author", {})
    committer_info = commit_info.get("committer", {})
    author_date = author_info.get("date", "")
    committer_date = committer_info.get("date", "")
    effective_date = max(author_date, committer_date) if (author_date and committer_date) else (committer_date or author_date)
    return {
        "sha": data.get("sha", ""),
        "short_sha": data.get("sha", "")[:7],
        "author": author_info.get("name", "Unknown"),
        "email": author_info.get("email", ""),
        "date": effective_date,
        "author_date": author_date,
        "committer_date": committer_date,
        "message": commit_info.get("message", "").split("\n")[0],
        "html_url": data.get("html_url", "")
    }


def get_recent_commits(org_name, repo_name, branch, limit=5):
    url = f"https://api.github.com/repos/{org_name}/{repo_name}/commits?sha={branch}&per_page={limit}"
    data = make_api_request(url)
    if not data:
        return []
    commits = []
    for c in data:
        commit_info = c.get("commit", {})
        author_info = commit_info.get("author", {})
        commits.append({
            "sha": c.get("sha", "")[:7],
            "author": author_info.get("name", "Unknown"),
            "date": author_info.get("date", ""),
            "message": commit_info.get("message", "").split("\n")[0]
        })
    return commits


def sync_rmh_sit_repo(src_name, tgt_name, token):
    """Auto-merges RMH-SIT code from MedVista-Pvt-Ltd into MedVista-Global-Pvt-Ltd in an isolated sandbox without touching local .git folders."""
    upstream_url = f"https://x-access-token:{token}@github.com/{SRC_ORG}/{src_name}.git"
    origin_url = f"https://x-access-token:{token}@github.com/{TGT_ORG}/{tgt_name}.git"

    # Always use an isolated temporary sandbox - never touch or copy any local .git folders
    temp_dir = tempfile.mkdtemp(prefix=f"auto_sync_{tgt_name}_")
    try:
        subprocess.run("git init --bare", shell=True, cwd=temp_dir, check=True, capture_output=True)
        subprocess.run("git config http.postBuffer 524288000", shell=True, cwd=temp_dir, capture_output=True)
        subprocess.run(f'git remote add upstream "{upstream_url}"', shell=True, cwd=temp_dir, check=True, capture_output=True)
        subprocess.run(f'git remote add origin "{origin_url}"', cwd=temp_dir, shell=True, check=True, capture_output=True)
        f = subprocess.run('git fetch upstream RMH-SIT', shell=True, cwd=temp_dir, timeout=180, capture_output=True, text=True)
        if f.returncode != 0:
            return False
        p = subprocess.run('git push origin FETCH_HEAD:refs/heads/RMH-SIT --force', shell=True, cwd=temp_dir, timeout=180, capture_output=True, text=True)
        return p.returncode == 0
    except Exception:
        return False
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)



def get_sort_timestamp(r):
    timestamps = []
    if r.get("sync_time"):
        timestamps.append(r["sync_time"])
    if r.get("tgt_commit") and r["tgt_commit"].get("date"):
        timestamps.append(r["tgt_commit"]["date"])
    if r.get("src_commit") and r["src_commit"].get("date"):
        timestamps.append(r["src_commit"]["date"])
    return max(timestamps) if timestamps else ""



def generate_markdown_report(results, report_date):
    auto_synced = sorted([r for r in results if r["status"] == "AUTO_SYNCED"], key=get_sort_timestamp, reverse=True)
    rmh_pending = sorted([r for r in results if r["status"] in ("PENDING", "AUTO_SYNC_FAILED") and r["source_branch"] == "RMH-SIT"], key=get_sort_timestamp, reverse=True)
    vmqa_pending = sorted([r for r in results if r["status"] in ("PENDING", "AUTO_SYNC_FAILED") and r["source_branch"] == "VM-QA"], key=get_sort_timestamp, reverse=True)
    synced = sorted([r for r in results if r["status"] == "UP_TO_DATE"], key=get_sort_timestamp, reverse=True)

    md = []
    md.append(f"# 📊 MedVista Daily Branch Difference & Auto-Sync Report - {report_date}")
    md.append(f"**Generated At:** {datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')}")
    md.append(f"**Source Org:** `https://github.com/orgs/{SRC_ORG}` | **Target Org:** `https://github.com/orgs/{TGT_ORG}`\n")

    md.append("## 📈 Summary Overview")
    md.append(f"- **Total Tracked Branch Pairs:** `{len(results)}`")
    md.append(f"- 🔄 **RMH-SIT Services Auto-Merged Today:** `{len(auto_synced)}`")
    if rmh_pending:
        md.append(f"- ⚠️ **RMH-SIT Services Pending Sync:** `{len(rmh_pending)}`")
    md.append(f"- ⚠️ **VM-QA Services Pending (Report Only — No Auto-Merge):** `{len(vmqa_pending)}`")
    md.append(f"- ✅ **Services In Sync / Up to Date:** `{len(synced)}`")

    if auto_synced:
        md.append("\n### 🔄 RMH-SIT Services Auto-Merged Today\n")
        md.append("| Service / Repositories | Comparison | Merged Commit | Author | Date | Status |")
        md.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for r in auto_synced:
            repo_pair = f"**{r['src_repo']}** ➔ `{r['tgt_repo']}`"
            branch_pair = f"`{r['source_branch']}` ➔ `{r['target_branch']}`"
            msg = r['src_commit']['message'][:45] + ("..." if len(r['src_commit']['message']) > 45 else "")
            author = r['src_commit']['author']
            date_str = r['src_commit']['date'].replace("T", " ").replace("Z", "")
            md.append(f"| {repo_pair} | {branch_pair} | `{r['src_commit']['short_sha']}`: {msg} | {author} | `{date_str}` | ✅ Auto-Merged |")

    if rmh_pending:
        md.append("\n### ⚠️ RMH-SIT Services Pending Sync\n")
        md.append("| Service / Repositories | Comparison | Latest Source Commit | Author | Date | Actions Needed |")
        md.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for r in rmh_pending:
            repo_pair = f"**{r['src_repo']}** ➔ `{r['tgt_repo']}`"
            branch_pair = f"`{r['source_branch']}` ➔ `{r['target_branch']}`"
            msg = r['src_commit']['message'][:45] + ("..." if len(r['src_commit']['message']) > 45 else "")
            author = r['src_commit']['author']
            date_str = r['src_commit']['date'].replace("T", " ").replace("Z", "")
            link = f"[View Source Repo](https://github.com/{SRC_ORG}/{r['src_repo']}/tree/{r['source_branch']})"
            md.append(f"| {repo_pair} | {branch_pair} | `{r['src_commit']['short_sha']}`: {msg} | {author} | `{date_str}` | {link} |")

    if vmqa_pending:
        md.append("\n### ⚠️ VM-QA Services Pending (Report Only — Not Merged)\n")
        md.append("| Service / Repositories | Comparison | Latest Source Commit | Author | Date | Actions Needed |")
        md.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for r in vmqa_pending:
            repo_pair = f"**{r['src_repo']}** ➔ `{r['tgt_repo']}`"
            branch_pair = f"`{r['source_branch']}` ➔ `{r['target_branch']}`"
            msg = r['src_commit']['message'][:45] + ("..." if len(r['src_commit']['message']) > 45 else "")
            author = r['src_commit']['author']
            date_str = r['src_commit']['date'].replace("T", " ").replace("Z", "")
            link = f"[View Source Repo](https://github.com/{SRC_ORG}/{r['src_repo']}/tree/{r['source_branch']})"
            md.append(f"| {repo_pair} | {branch_pair} | `{r['src_commit']['short_sha']}`: {msg} | {author} | `{date_str}` | {link} |")

    if synced:
        md.append("\n### ✅ Services Up to Date / In Sync\n")
        md.append("| Service / Repositories | Comparison | Latest Source Commit | Author | Date | Status |")
        md.append("| :--- | :--- | :--- | :--- | :--- | :--- |")
        for r in synced:
            repo_pair = f"**{r['src_repo']}** ➔ `{r['tgt_repo']}`"
            branch_pair = f"`{r['source_branch']}` ➔ `{r['target_branch']}`"
            msg = r['src_commit']['message'][:45] + ("..." if len(r['src_commit']['message']) > 45 else "")
            author = r['src_commit']['author']
            date_str = r['src_commit']['date'].replace("T", " ").replace("Z", "")
            md.append(f"| {repo_pair} | {branch_pair} | `{r['src_commit']['short_sha']}`: {msg} | {author} | `{date_str}` | ✅ In Sync |")

    return "\n".join(md)


def generate_html_report(results, report_date):
    now_ist = datetime.now().strftime('%Y-%m-%d %H:%M:%S IST')
    auto_synced = sorted([r for r in results if r["status"] == "AUTO_SYNCED"], key=get_sort_timestamp, reverse=True)
    rmh_pending = sorted([r for r in results if r["status"] in ("PENDING", "AUTO_SYNC_FAILED") and r["source_branch"] == "RMH-SIT"], key=get_sort_timestamp, reverse=True)
    vmqa_pending = sorted([r for r in results if r["status"] in ("PENDING", "AUTO_SYNC_FAILED") and r["source_branch"] == "VM-QA"], key=get_sort_timestamp, reverse=True)
    synced = sorted([r for r in results if r["status"] == "UP_TO_DATE"], key=get_sort_timestamp, reverse=True)

    auto_synced_rows = ""
    for r in auto_synced:
        refreshed_date = r.get("sync_time") or (r.get("tgt_commit", {}).get("date", "").replace("T", " ").replace("Z", "")[:16] if r.get("tgt_commit") else r['src_commit']['date'].replace("T", " ").replace("Z", "")[:16])
        auto_synced_rows += f"""
        <tr>
            <td style="padding: 10px; border-bottom: 1px solid #ddd; font-weight: bold;">
                {r['src_repo']}<br/>
                <span style="font-size: 11px; color: #57606a;">➔ {r['tgt_repo']}</span>
            </td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd;">
                <code style="background: #ddf4ff; color: #0969da; padding: 2px 6px; border-radius: 4px;">{r['source_branch']} ➔ {r['target_branch']}</code><br/>
                <span style="font-size: 11px; color: #0969da; font-weight: 500;">⏰ Refreshed: {refreshed_date}</span>
            </td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd;">
                <code style="color: #0969da;">{r['src_commit']['short_sha']}</code>: {r['src_commit']['message']}
            </td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd;">{r['src_commit']['author']}</td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd; font-size: 12px;">{r['src_commit']['date'].replace('T', ' ').replace('Z', '')}</td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd; color: #1a7f37; font-weight: bold;">
                ✅ Auto-Merged
            </td>
        </tr>
        """

    rmh_pending_rows = ""
    for r in rmh_pending:
        rmh_pending_rows += f"""
        <tr>
            <td style="padding: 10px; border-bottom: 1px solid #ddd; font-weight: bold;">
                {r['src_repo']}<br/>
                <span style="font-size: 11px; color: #57606a;">➔ {r['tgt_repo']}</span>
            </td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd;">
                <code style="background: #fff8c5; color: #573600; padding: 2px 6px; border-radius: 4px;">{r['source_branch']} ➔ {r['target_branch']}</code>
            </td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd;">
                <code style="color: #0969da;">{r['src_commit']['short_sha']}</code>: {r['src_commit']['message']}
            </td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd;">{r['src_commit']['author']}</td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd; font-size: 12px;">{r['src_commit']['date'].replace('T', ' ').replace('Z', '')}</td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd;">
                <a href="https://github.com/{SRC_ORG}/{r['src_repo']}/tree/{r['source_branch']}" style="color: #0969da; text-decoration: none; font-weight: bold;">View Source</a>
            </td>
        </tr>
        """

    vmqa_pending_rows = ""
    for r in vmqa_pending:
        vmqa_pending_rows += f"""
        <tr>
            <td style="padding: 10px; border-bottom: 1px solid #ddd; font-weight: bold;">
                {r['src_repo']}<br/>
                <span style="font-size: 11px; color: #57606a;">➔ {r['tgt_repo']}</span>
            </td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd;">
                <code style="background: #fff8c5; color: #573600; padding: 2px 6px; border-radius: 4px;">{r['source_branch']} ➔ {r['target_branch']}</code>
            </td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd;">
                <code style="color: #0969da;">{r['src_commit']['short_sha']}</code>: {r['src_commit']['message']}
            </td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd;">{r['src_commit']['author']}</td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd; font-size: 12px;">{r['src_commit']['date'].replace('T', ' ').replace('Z', '')}</td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd;">
                <a href="https://github.com/{SRC_ORG}/{r['src_repo']}/tree/{r['source_branch']}" style="color: #0969da; text-decoration: none; font-weight: bold;">View Source</a>
            </td>
        </tr>
        """

    synced_rows = ""
    for r in synced:
        branch_badge = f'<code style="background: #dafbe1; color: #1a7f37; padding: 2px 6px; border-radius: 4px;">{r["source_branch"]} ➔ {r["target_branch"]}</code>'
        if r["source_branch"] == "RMH-SIT" and r.get("tgt_commit"):
            refreshed_date = r.get("sync_time") or r["tgt_commit"]["date"].replace("T", " ").replace("Z", "")[:16]
            branch_badge += f'<br/><span style="font-size: 11px; color: #1a7f37; font-weight: 500;">⏰ Refreshed: {refreshed_date}</span>'

        synced_rows += f"""
        <tr>
            <td style="padding: 10px; border-bottom: 1px solid #ddd; font-weight: bold;">
                {r['src_repo']}<br/>
                <span style="font-size: 11px; color: #57606a;">➔ {r['tgt_repo']}</span>
            </td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd;">
                {branch_badge}
            </td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd;">
                <code style="color: #0969da;">{r['src_commit']['short_sha']}</code>: {r['src_commit']['message']}
            </td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd;">{r['src_commit']['author']}</td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd; font-size: 12px;">{r['src_commit']['date'].replace('T', ' ').replace('Z', '')}</td>
            <td style="padding: 10px; border-bottom: 1px solid #ddd; font-size: 12px; color: #1a7f37; font-weight: bold;">
                ✅ In Sync
            </td>
        </tr>
        """

    rmh_pending_summary = f"• ⚠️ <strong>{len(rmh_pending)}</strong> RMH-SIT services pending sync.<br/>" if rmh_pending else ""

    html = f"""
    <!DOCTYPE html>
    <html>
    <head>
        <style>
            body {{ font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Helvetica, Arial, sans-serif; color: #24292f; margin: 0; padding: 20px; background-color: #f6f8fa; }}
            .container {{ max-width: 950px; margin: 0 auto; background: #ffffff; border: 1px solid #d0d7de; border-radius: 8px; padding: 24px; }}
            .header {{ border-bottom: 2px solid #0969da; padding-bottom: 12px; margin-bottom: 20px; }}
            .header h1 {{ margin: 0; color: #0969da; font-size: 22px; }}
            .summary {{ background: #f6f8fa; border: 1px solid #d0d7de; border-radius: 6px; padding: 12px 16px; margin-bottom: 20px; line-height: 1.6; }}
            table {{ width: 100%; border-collapse: collapse; margin-top: 10px; margin-bottom: 25px; }}
            th {{ background: #f6f8fa; padding: 10px; text-align: left; border-bottom: 2px solid #d0d7de; font-size: 13px; color: #57606a; }}
            .section-title {{ font-size: 16px; font-weight: bold; margin-top: 20px; margin-bottom: 10px; }}
            .footer {{ margin-top: 30px; font-size: 12px; color: #57606a; text-align: center; border-top: 1px solid #e1e4e8; padding-top: 12px; }}
        </style>
    </head>
    <body>
        <div class="container">
            <div class="header">
                <h1>📊 MedVista Daily Branch Difference & Auto-Sync Report</h1>
                <p style="margin: 4px 0 0 0; color: #57606a; font-size: 13px;">Generated at {now_ist} | {SRC_ORG} ➔ {TGT_ORG}</p>
            </div>
            
            <div class="summary">
                <strong>📈 Summary Status:</strong> Tracked <strong>{len(results)}</strong> repository branch pairs.<br/>
                • 🔄 <strong>{len(auto_synced)}</strong> RMH-SIT services auto-merged and refreshed today.<br/>
                {rmh_pending_summary}
                • ⚠️ <strong>{len(vmqa_pending)}</strong> VM-QA services pending changes (Report only — Not merged).<br/>
                • ✅ <strong>{len(synced)}</strong> services are in sync.
            </div>

            {"<div class='section-title' style='color: #0969da;'>🔄 RMH-SIT Services Auto-Merged Today</div>" if auto_synced else ""}
            {"<table><thead><tr><th>Service / Repositories</th><th>Branches</th><th>Merged Commit</th><th>Author</th><th>Commit Date</th><th>Status</th></tr></thead><tbody>" + auto_synced_rows + "</tbody></table>" if auto_synced else ""}

            {"<div class='section-title' style='color: #d97706;'>⚠️ RMH-SIT Services Pending Sync</div>" if rmh_pending else ""}
            {"<table><thead><tr><th>Service / Repositories</th><th>Branches</th><th>Latest Source Commit</th><th>Author</th><th>Commit Date</th><th>Action</th></tr></thead><tbody>" + rmh_pending_rows + "</tbody></table>" if rmh_pending else ""}

            {"<div class='section-title' style='color: #9a6700;'>⚠️ VM-QA Services Pending (Report Only — Not Merged)</div>" if vmqa_pending else ""}
            {"<table><thead><tr><th>Service / Repositories</th><th>Branches</th><th>Latest Source Commit</th><th>Author</th><th>Commit Date</th><th>Action</th></tr></thead><tbody>" + vmqa_pending_rows + "</tbody></table>" if vmqa_pending else ""}

            {"<div class='section-title' style='color: #1a7f37;'>✅ Services Up to Date / In Sync</div>" if synced else ""}
            {"<table><thead><tr><th>Service / Repositories</th><th>Branches</th><th>Latest Source Commit</th><th>Author</th><th>Commit Date</th><th>Status</th></tr></thead><tbody>" + synced_rows + "</tbody></table>" if synced else ""}

            <div class="footer">
                Automated daily report generated by MedVista Branch Analyzer Bot.
            </div>
        </div>
    </body>
    </html>
    """
    return html


def send_email_report(html_content, subject, to_emails):
    smtp_server = os.environ.get("SMTP_SERVER", "smtp.gmail.com")
    smtp_user = os.environ.get("SMTP_USER", "")
    smtp_pass = os.environ.get("SMTP_PASS", "")

    if not smtp_user or not smtp_pass:
        print("ℹ️ SMTP credentials not configured. Skipping email dispatch.")
        return False

    try:
        msg = MIMEMultipart("alternative")
        msg["Subject"] = subject
        msg["From"] = smtp_user
        msg["To"] = ", ".join(to_emails)
        msg.attach(MIMEText(html_content, "html"))

        print(f"📧 Sending email via SMTP SSL ({smtp_server}:465) to {len(to_emails)} recipients...")
        try:
            server = smtplib.SMTP_SSL(smtp_server, 465, timeout=15)
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to_emails, msg.as_string())
            server.quit()
            print(f"✅ Email report successfully sent to: {', '.join(to_emails)}")
            return True
        except Exception as ssl_err:
            print(f"⚠️ SSL port 465 failed ({ssl_err}), trying TLS port 587...")
            server = smtplib.SMTP(smtp_server, 587, timeout=15)
            server.starttls()
            server.login(smtp_user, smtp_pass)
            server.sendmail(smtp_user, to_emails, msg.as_string())
            server.quit()
            print(f"✅ Email report successfully sent to: {', '.join(to_emails)}")
            return True
    except Exception as e:
        print(f"❌ Failed to send email: {e}", file=sys.stderr)
        return False


def check_single_repo(src_name, src_data, tgt_repos):
    tgt_name = map_source_to_target_repo(src_name, tgt_repos)
    if not tgt_name:
        return []

    repo_results = []
    for pair in BRANCH_PAIRS:
        src_branch = pair["source_branch"]
        tgt_branch = pair["target_branch"]

        src_commit = get_latest_commit(SRC_ORG, src_name, src_branch)
        tgt_commit = get_latest_commit(TGT_ORG, tgt_name, tgt_branch)

        if not src_commit or not tgt_commit:
            continue

        # A branch pair is PENDING sync only if source SHA is different AND source commit date is newer than target commit date
        is_pending = (src_commit["sha"] != tgt_commit["sha"] and src_commit["date"] > tgt_commit["date"])
        status = "PENDING" if is_pending else "UP_TO_DATE"

        recent_commits = get_recent_commits(SRC_ORG, src_name, src_branch, limit=5) if is_pending else []

        repo_results.append({
            "src_repo": src_name,
            "tgt_repo": tgt_name,
            "source_branch": src_branch,
            "target_branch": tgt_branch,
            "src_commit": src_commit,
            "tgt_commit": tgt_commit,
            "status": status,
            "recent_src_commits": recent_commits
        })
    return repo_results


def main():
    parser = argparse.ArgumentParser(description="MedVista Daily Cross-Org Branch Analyzer & Auto-Refresher")
    parser.add_argument("--output-dir", default="reports", help="Directory to save report markdown/HTML")
    parser.add_argument("--email", nargs="+", help="Recipient email addresses")
    parser.add_argument("--auto-sync-rmh-sit", action="store_true", default=True, help="Auto-merge new RMH-SIT commits into target RMH-SIT")
    parser.add_argument("--no-auto-sync-rmh-sit", dest="auto_sync_rmh_sit", action="store_false", help="Disable auto-merging of RMH-SIT")
    args = parser.parse_args()

    now_str = datetime.now().strftime("%Y-%m-%d")
    print(f"🚀 Starting MedVista Daily Branch Analyzer & Auto-Refresher for {now_str}...")
    print(f"   Auto-Sync RMH-SIT: {args.auto_sync_rmh_sit} (VM-QA is never auto-merged)")

    print(f"Fetching source repos from '{SRC_ORG}'...")
    src_repos = get_org_repos(SRC_ORG)
    print(f"Found {len(src_repos)} repositories in {SRC_ORG}.")

    print(f"Fetching target repos from '{TGT_ORG}'...")
    tgt_repos = get_org_repos(TGT_ORG)
    print(f"Found {len(tgt_repos)} repositories in {TGT_ORG}.")

    results = []
    from concurrent.futures import ThreadPoolExecutor, as_completed

    print("⚡ Analyzing repository branch differences concurrently...")
    with ThreadPoolExecutor(max_workers=15) as executor:
        futures = [executor.submit(check_single_repo, src_name, src_data, tgt_repos) for src_name, src_data in src_repos.items()]
        for future in as_completed(futures):
            res = future.result()
            if res:
                results.extend(res)

    print(f"\nAnalysis complete! Processed {len(results)} repository pairs.")

    # Auto-merge RMH-SIT if enabled
    if args.auto_sync_rmh_sit:
        rmh_pending = [r for r in results if r["source_branch"] == "RMH-SIT" and r["status"] == "PENDING"]
        if rmh_pending:
            print(f"\n🔄 Auto-Sync Enabled: Refreshing {len(rmh_pending)} RMH-SIT repositories from {SRC_ORG}...")
            for r in rmh_pending:
                print(f"  Syncing {r['src_repo']} ➔ {r['tgt_repo']} [RMH-SIT]...")
                success = sync_rmh_sit_repo(r["src_repo"], r["tgt_repo"], TOKEN)
                if success:
                    r["status"] = "AUTO_SYNCED"
                    r["sync_time"] = datetime.now().strftime("%Y-%m-%d %H:%M IST")
                    print(f"  ✅ Auto-merged latest RMH-SIT code into {r['tgt_repo']}")
                else:
                    r["status"] = "AUTO_SYNC_FAILED"
                    print(f"  ❌ Auto-sync failed for {r['tgt_repo']}")
        else:
            print("✨ All RMH-SIT repositories are already up to date. No auto-merge needed.")

    os.makedirs(args.output_dir, exist_ok=True)

    # Save Markdown
    md_path = os.path.join(args.output_dir, f"daily_branch_report_{now_str}.md")
    md_content = generate_markdown_report(results, now_str)
    with open(md_path, "w", encoding="utf-8") as f:
        f.write(md_content)
    print(f"✅ Markdown report saved to: {md_path}")

    # Save HTML
    html_path = os.path.join(args.output_dir, f"daily_branch_report_{now_str}.html")
    html_content = generate_html_report(results, now_str)
    with open(html_path, "w", encoding="utf-8") as f:
        f.write(html_content)
    print(f"✅ HTML report saved to: {html_path}")

    # Save JSON
    json_path = os.path.join(args.output_dir, f"daily_branch_report_{now_str}.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(results, f, indent=2)
    print(f"✅ JSON summary saved to: {json_path}")

    # Dispatch Email
    raw_emails = args.email or (os.environ.get("EMAIL_TO").split(",") if os.environ.get("EMAIL_TO") else DEFAULT_RECIPIENTS)
    to_emails = [e.strip() for e in raw_emails if e.strip() and e.strip() != "devops@example.com"]
    auto_synced_count = sum(1 for r in results if r.get("status") == "AUTO_SYNCED")
    pending_count = sum(1 for r in results if r.get("status") in ("PENDING", "AUTO_SYNC_FAILED"))

    if to_emails:
        subject = f"📊 MedVista Daily Branch Report - {now_str} ({auto_synced_count} RMH-SIT refreshed, {pending_count} VM-QA pending)"
        send_email_report(html_content, subject, to_emails)


if __name__ == "__main__":
    main()
