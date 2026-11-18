#!/usr/bin/env python3
"""
MedVista Backend Release Merger & Conflict Watchdog
====================================================
Automates backend service release merges (e.g. utkal-dev ➔ utkal-uat)
using native GitHub Pull Requests.

Key Highlights:
1. Backend Only: Strictly processes 'Medvista-BE-*' services.
2. 100% Native GitHub PRs: Merges through GitHub PR API using your account.
   Shows in GitHub UI as: "Merged by anicorazonkare".
   Commit counts stay exact, standard, and never break GitHub UI compare trees.
3. Automated Email Parser: Extracts backend services from developer release emails.
4. Conflict Handler: If merge conflicts occur, leaves PR open and automatically
   emails the developer with the PR link, commit SHA, and conflicting files.
5. CI/CD Build Check: Verifies that GitHub Actions build workflow triggered after merge.
6. Mobile Friendly: Works locally or via GitHub Mobile App (workflow_dispatch).
"""

import os
import re
import sys
import json
import time
import smtplib
import argparse
import urllib.request
import urllib.error
from email.mime.text import MIMEText
from email.mime.multipart import MIMEMultipart
from datetime import datetime

# --- Configuration ---
ORG = os.environ.get("GITHUB_ORG", "MedVista-Global-Pvt-Ltd")
DEFAULT_SRC = os.environ.get("DEFAULT_SRC_BRANCH", "dev")
DEFAULT_TGT = os.environ.get("DEFAULT_TGT_BRANCH", "uat")

AUTHOR_NAME = os.environ.get("GIT_AUTHOR_NAME", "Aniii-hub")
AUTHOR_EMAIL = os.environ.get("GIT_AUTHOR_EMAIL", "anirudhpardeshi25@gmail.com")

TOKEN = os.environ.get("PAT_TOKEN") or os.environ.get("GITHUB_TOKEN", "")

SMTP_USER = os.environ.get("SMTP_USER", "")
SMTP_PASS = os.environ.get("SMTP_PASS", "")
DEVOPS_EMAILS = [os.environ.get("ALERT_EMAIL", "devops@example.com")]

HEADERS = {
    "Authorization": f"token {TOKEN}",
    "Accept": "application/vnd.github.v3+json",
    "User-Agent": "Backend-Release-Bot"
}

# ANSI Colors
BOLD = "\033[1m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
RED = "\033[31m"
MAGENTA = "\033[35m"
DIM = "\033[2m"
RESET = "\033[0m"


def api_request(url, method="GET", data=None):
    """Executes authenticated GitHub API request."""
    encoded_data = json.dumps(data).encode("utf-8") if data else None
    req = urllib.request.Request(url, data=encoded_data, headers=HEADERS, method=method)
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            content = resp.read().decode("utf-8")
            return resp.status, json.loads(content) if content else {}
    except urllib.error.HTTPError as e:
        err_body = e.read().decode("utf-8", errors="replace")
        try:
            parsed = json.loads(err_body)
        except Exception:
            parsed = {"message": err_body}
        return e.code, parsed
    except Exception as e:
        return 500, {"message": str(e)}


KNOWN_BE_SERVICES = [
    "Medvista-BE-Appointment-Service",
    "Medvista-BE-Audit-Service",
    "Medvista-BE-Auth-Service",
    "Medvista-BE-Blood-Bank-Service",
    "Medvista-BE-CDSS-Adapter",
    "Medvista-BE-CSSD-Service",
    "Medvista-BE-Clinical-Common-Service",
    "Medvista-BE-Clinical-Pharmacy-Service",
    "Medvista-BE-Clinical-Workflow-Service",
    "Medvista-BE-Core-Service",
    "Medvista-BE-Diet-Service",
    "Medvista-BE-File-Store",
    "Medvista-BE-Gateway-Service",
    "Medvista-BE-IPD-Clinical-Service",
    "Medvista-BE-IPD-Service",
    "Medvista-BE-Inventory-Service",
    "Medvista-BE-LIS-Service",
    "Medvista-BE-Mobility-Service",
    "Medvista-BE-Notification-Service",
    "Medvista-BE-Phlebotomy-Service",
    "Medvista-BE-Public-Gateway-Service",
    "Medvista-BE-QC-Service",
    "Medvista-BE-Queue-Management-Service",
    "Medvista-BE-RCM-Service",
    "Medvista-BE-Relay-Service",
    "Medvista-BE-Report-Service",
    "Medvista-BE-Roster-Mgmt-Service",
    "Medvista-BE-Supplementary-Service",
    "Medvista-BE-User-Management-Service",
    "Medvista-BE-User-Service",
    "Medvista-BE-Video-Consult-Service"
]


STOP_WORDS = {"from", "to", "dev", "uat", "prod", "sit", "staging", "please", "merge", "deploy", "service", "services", "and", "the", "branch", "branches", "done", "exit", "quit"}


def resolve_short_name_to_be_service(token):
    """Matches a short token (e.g. 'core', 'ipd clinical', 'inventory') to full BE repo."""
    token_clean = re.sub(r"[^a-zA-Z0-9]", "", token.lower())
    if not token_clean or token_clean in STOP_WORDS:
        return None
    for full_svc in KNOWN_BE_SERVICES:
        svc_core = full_svc.replace("Medvista-BE-", "").replace("-Service", "").replace("-Adapter", "")
        svc_clean = re.sub(r"[^a-zA-Z0-9]", "", svc_core.lower())
        # Exact match or word boundary match
        if token_clean == svc_clean:
            return full_svc
        # Substring match if token is distinct enough (>= 4 chars)
        if len(token_clean) >= 4 and token_clean in svc_clean:
            return full_svc
    return None


def detect_branches_from_text(text, default_src="utkal-dev", default_tgt="utkal-uat"):
    """Auto-detects source and target branches from text phrases like 'from dev to uat'."""
    text_lower = text.lower()
    src = default_src
    tgt = default_tgt
    if "uat to prod" in text_lower or "utkal uat to utkal prod" in text_lower:
        src = "utkal-uat"
        tgt = "utkal-prod"
    elif "dev to uat" in text_lower or "utkal dev to utkal uat" in text_lower or "from dev to uat" in text_lower:
        src = "utkal-dev"
        tgt = "utkal-uat"
    elif "sit to staging" in text_lower or "rmh-sit to utkal-staging" in text_lower:
        src = "RMH-SIT"
        tgt = "UTKAL-LT-Staging"
    return src, tgt


def parse_backend_services_from_text(text):
    """
    Extracts all unique Medvista-BE-* service names from email text, tables,
    OR natural sentences like 'core from dev to uat', 'merge ipd, report and inventory'.
    Preserves order of appearance while deduplicating.
    """
    # 1. Exact Medvista-BE-* matches
    matches = re.findall(r"Medvista-BE-[A-Za-z0-9_-]+", text, re.IGNORECASE)
    seen = set()
    services = []
    for m in matches:
        clean_name = m.strip().rstrip(".,;:|)")
        clean_lower = clean_name.lower()
        if clean_lower not in seen:
            seen.add(clean_lower)
            services.append(clean_name)

    # 2. If no full Medvista-BE-* names found, parse tokens/words
    if not services:
        # Check compound names first (e.g. 'ipd clinical', 'blood bank')
        compound_candidates = [
            ("ipd clinical", "Medvista-BE-IPD-Clinical-Service"),
            ("ipd-clinical", "Medvista-BE-IPD-Clinical-Service"),
            ("clinical common", "Medvista-BE-Clinical-Common-Service"),
            ("clinical pharmacy", "Medvista-BE-Clinical-Pharmacy-Service"),
            ("clinical workflow", "Medvista-BE-Clinical-Workflow-Service"),
            ("blood bank", "Medvista-BE-Blood-Bank-Service"),
            ("public gateway", "Medvista-BE-Public-Gateway-Service"),
            ("queue management", "Medvista-BE-Queue-Management-Service"),
            ("video consult", "Medvista-BE-Video-Consult-Service"),
            ("user management", "Medvista-BE-User-Management-Service"),
        ]
        text_lower = text.lower()
        for phrase, repo_name in compound_candidates:
            if phrase in text_lower and repo_name.lower() not in seen:
                seen.add(repo_name.lower())
                services.append(repo_name)

        # Then split remaining tokens
        tokens = [t.strip() for t in re.split(r"[,;\n\r\s]+", text) if t.strip()]
        for t in tokens:
            resolved = resolve_short_name_to_be_service(t)
            if resolved and resolved.lower() not in seen:
                seen.add(resolved.lower())
                services.append(resolved)

    return services


def get_commit_author_info(repo, commit_sha):
    """Retrieves author and committer details for a given commit."""
    url = f"https://api.github.com/repos/{ORG}/{repo}/commits/{commit_sha}"
    status, data = api_request(url)
    if status == 200 and data:
        commit_obj = data.get("commit", {})
        author = commit_obj.get("author", {})
        committer = commit_obj.get("committer", {})
        return {
            "name": author.get("name") or committer.get("name") or "Developer",
            "email": author.get("email") or committer.get("email") or "",
            "message": commit_obj.get("message", "").split("\n")[0],
            "sha": commit_sha[:8],
            "full_sha": commit_sha,
            "html_url": data.get("html_url")
        }
    return {
        "name": "Developer",
        "email": "",
        "message": "",
        "sha": commit_sha[:8] if commit_sha else "Unknown",
        "full_sha": commit_sha,
        "html_url": ""
    }


def send_conflict_notification(repo, pr_info, last_commit, src_branch, tgt_branch, sender_email=None):
    """Sends an automated email to developer and DevOps regarding merge conflict."""
    recipients = []
    if last_commit.get("email") and "@" in last_commit.get("email") and not last_commit.get("email").endswith("noreply.github.com"):
        recipients.append(last_commit["email"])
    if sender_email and "@" in sender_email and sender_email not in recipients:
        recipients.append(sender_email)

    # Always CC DevOps
    cc_list = [e for e in DEVOPS_EMAILS if e not in recipients]

    pr_url = pr_info.get("html_url", "")
    pr_number = pr_info.get("number", "")

    subject = f"🚨 Action Required: Merge Conflict in {repo} ({src_branch} ➔ {tgt_branch})"

    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
        <div style="background-color: #d93025; color: white; padding: 16px; border-radius: 6px;">
            <h2 style="margin: 0;">🚨 Action Required: Merge Conflict Detected</h2>
            <p style="margin: 4px 0 0 0;">Release deployment blocked for <strong>{repo}</strong></p>
        </div>
        <div style="padding: 16px; border: 1px solid #e0e0e0; border-top: none; border-radius: 0 0 6px 6px;">
            <p>Hi <strong>{last_commit.get('name', 'Team')}</strong>,</p>
            <p>During automated release deployment from <code>{src_branch}</code> to <code>{tgt_branch}</code>, a <strong>merge conflict</strong> occurred in repository <strong>{repo}</strong>.</p>
            
            <table style="width: 100%; border-collapse: collapse; margin: 16px 0;">
                <tr style="background: #f8f9fa;">
                    <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold; width: 30%;">Repository:</td>
                    <td style="padding: 8px; border: 1px solid #ddd;">{repo}</td>
                </tr>
                <tr>
                    <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Branches:</td>
                    <td style="padding: 8px; border: 1px solid #ddd;"><code>{src_branch}</code> ➔ <code>{tgt_branch}</code></td>
                </tr>
                <tr style="background: #f8f9fa;">
                    <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Pull Request:</td>
                    <td style="padding: 8px; border: 1px solid #ddd;"><a href="{pr_url}" style="color: #1a73e8; font-weight: bold;">PR #{pr_number}: View Conflicts on GitHub</a></td>
                </tr>
                <tr>
                    <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Last Commit on Source:</td>
                    <td style="padding: 8px; border: 1px solid #ddd;"><code>{last_commit.get('sha')}</code> - {last_commit.get('message')} (by {last_commit.get('name')})</td>
                </tr>
            </table>

            <div style="background: #fff3cd; border-left: 4px solid #ffc107; padding: 12px; margin: 16px 0;">
                <strong>Next Steps:</strong>
                <ol style="margin: 8px 0 0 20px; padding: 0;">
                    <li>Open the Pull Request: <a href="{pr_url}">{pr_url}</a></li>
                    <li>Resolve the conflicting files on branch <code>{src_branch}</code> (or directly in the PR).</li>
                    <li>Once resolved and pushed, notify DevOps to complete the UAT deployment.</li>
                </ol>
            </div>
            
            <p style="font-size: 12px; color: #777; margin-top: 24px;">Automated by MedVista Backend Release Bot on behalf of DevOps ({AUTHOR_EMAIL})</p>
        </div>
    </body>
    </html>
    """

    all_recipients = recipients + cc_list
    if not all_recipients:
        all_recipients = DEVOPS_EMAILS

    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"MedVista DevOps <{SMTP_USER}>"
    msg["To"] = ", ".join(recipients) if recipients else ", ".join(DEVOPS_EMAILS)
    if cc_list:
        msg["Cc"] = ", ".join(cc_list)

    msg.attach(MIMEText(html_content, "html"))

    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, all_recipients, msg.as_string())
        print(f"  📧 Conflict notification email sent to: {', '.join(all_recipients)}")
        return True
    except Exception as e:
        print(f"  ⚠️ Could not send conflict email: {e}")
        return False


def send_build_missing_notification(repo, commit_sha, tgt_branch, reason):
    """Sends an email alert to DevOps if CI/CD build did not trigger after merge."""
    subject = f"⚠️ Alert: CI/CD Build Not Triggered: {repo} (Merged to {tgt_branch})"
    html_content = f"""
    <html>
    <body style="font-family: Arial, sans-serif; color: #333; line-height: 1.6;">
        <div style="background-color: #f2994a; color: white; padding: 16px; border-radius: 6px;">
            <h2 style="margin: 0;">⚠️ CI/CD Build Not Triggered</h2>
            <p style="margin: 4px 0 0 0;">Service merged to <strong>{tgt_branch}</strong>, but Docker build did not start.</p>
        </div>
        <div style="padding: 16px; border: 1px solid #e0e0e0; border-top: none; border-radius: 0 0 6px 6px;">
            <table style="width: 100%; border-collapse: collapse; margin: 16px 0;">
                <tr style="background: #f8f9fa;">
                    <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold; width: 30%;">Repository:</td>
                    <td style="padding: 8px; border: 1px solid #ddd;"><strong>{repo}</strong></td>
                </tr>
                <tr>
                    <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Target Branch:</td>
                    <td style="padding: 8px; border: 1px solid #ddd;"><code>{tgt_branch}</code></td>
                </tr>
                <tr style="background: #f8f9fa;">
                    <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold;">Merge Commit SHA:</td>
                    <td style="padding: 8px; border: 1px solid #ddd;"><code>{commit_sha[:8]}</code></td>
                </tr>
                <tr>
                    <td style="padding: 8px; border: 1px solid #ddd; font-weight: bold; color: #d93025;">Failure Reason:</td>
                    <td style="padding: 8px; border: 1px solid #ddd; color: #d93025; font-weight: bold;">{reason}</td>
                </tr>
            </table>
            <p><strong>Action Needed:</strong> Check <code>.github/workflows/build.yaml</code> in <code>{repo}</code> on branch <code>{tgt_branch}</code> to ensure <code>{tgt_branch}</code> is listed under branch triggers.</p>
        </div>
    </body>
    </html>
    """
    msg = MIMEMultipart("alternative")
    msg["Subject"] = subject
    msg["From"] = f"MedVista Release Bot <{SMTP_USER}>"
    msg["To"] = ", ".join(DEVOPS_EMAILS)
    msg.attach(MIMEText(html_content, "html"))
    try:
        with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=15) as server:
            server.login(SMTP_USER, SMTP_PASS)
            server.sendmail(SMTP_USER, DEVOPS_EMAILS, msg.as_string())
        print(f"  📧 Build alert email sent to DevOps ({', '.join(DEVOPS_EMAILS)})")
        return True
    except Exception as e:
        print(f"  ⚠️ Could not send build alert email: {e}")
        return False


def check_build_triggered(repo, merge_commit_sha, tgt_branch, max_retries=4, wait_sec=5):
    """Verifies if GitHub Actions CI/CD build started for the new merge commit on target branch."""
    for attempt in range(max_retries):
        time.sleep(wait_sec)
        url = f"https://api.github.com/repos/{ORG}/{repo}/actions/runs?head_sha={merge_commit_sha}"
        status, data = api_request(url)
        if status == 200 and data and data.get("total_count", 0) > 0:
            run = data["workflow_runs"][0]
            return {
                "triggered": True,
                "run_id": run.get("id"),
                "run_number": run.get("run_number"),
                "status": run.get("status"),
                "html_url": run.get("html_url")
            }

    # Inspect workflow definition to determine reason
    wf_url = f"https://api.github.com/repos/{ORG}/{repo}/contents/.github/workflows/build.yaml?ref={tgt_branch}"
    status, wf_data = api_request(wf_url)
    if status == 404:
        wf_url = f"https://api.github.com/repos/{ORG}/{repo}/contents/.github/workflows/build.yml?ref={tgt_branch}"
        status, wf_data = api_request(wf_url)

    reason = "Build workflow has not queued yet or runners are delayed."
    missing_trigger = False
    wf_path = ".github/workflows/build.yaml"
    wf_sha = ""
    raw_content = ""

    if status == 200 and wf_data and "content" in wf_data:
        import base64
        try:
            raw_content = base64.b64decode(wf_data["content"]).decode("utf-8")
            wf_sha = wf_data.get("sha", "")
            wf_path = wf_data.get("path", wf_path)
            if tgt_branch not in raw_content:
                reason = f"Branch '{tgt_branch}' is MISSING in workflow branch triggers ({wf_path})!"
                missing_trigger = True
        except Exception:
            pass
    elif status == 404:
        reason = "No .github/workflows/build.yaml found on target branch."

    return {
        "triggered": False,
        "reason": reason,
        "missing_trigger": missing_trigger,
        "wf_path": wf_path,
        "wf_sha": wf_sha,
        "raw_content": raw_content
    }


def prompt_and_fix_missing_trigger(repo, tgt_branch, build_info, auto_fix=False):
    """
    Asks user for permission to fix missing branch trigger in build.yaml.
    If granted, commits the updated workflow directly via GitHub API and launches build.
    """
    if not build_info.get("missing_trigger"):
        return False

    wf_path = build_info.get("wf_path", ".github/workflows/build.yaml")
    raw_content = build_info.get("raw_content", "")
    wf_sha = build_info.get("wf_sha", "")

    if not raw_content or not wf_sha:
        return False

    print(f"\n{YELLOW}{BOLD}⚠️ MISSING TRIGGER DETECTED:{RESET} '{tgt_branch}' is not in {wf_path} for {BOLD}{repo}{RESET}.")
    
    do_fix = auto_fix
    if not do_fix:
        try:
            choice = input(f"   Would you like the bot to add '{tgt_branch}' to {wf_path} and trigger the build? [y/N]: ").strip().lower()
            do_fix = choice in ("y", "yes")
        except EOFError:
            do_fix = False

    if not do_fix:
        print(f"   Skipped modifying {wf_path}.")
        return False

    # Insert branch under branches list
    import base64
    lines = raw_content.splitlines()
    new_lines = []
    inserted = False
    for line in lines:
        new_lines.append(line)
        if not inserted and re.search(r"^\s*-\s*(utkal-dev|main|dev|uat)", line):
            indent = re.match(r"^\s*", line).group(0)
            new_lines.append(f"{indent}- {tgt_branch}")
            inserted = True

    if not inserted:
        # Fallback if no matching pattern
        new_content = raw_content.replace("branches:", f"branches:\n      - {tgt_branch}")
    else:
        new_content = "\n".join(new_lines) + "\n"

    encoded_content = base64.b64encode(new_content.encode("utf-8")).decode("utf-8")

    # Commit via GitHub API
    commit_payload = {
        "message": f"ci(build): add {tgt_branch} trigger for automated release [skip ci]",
        "content": encoded_content,
        "sha": wf_sha,
        "branch": tgt_branch,
        "committer": {
            "name": AUTHOR_NAME,
            "email": AUTHOR_EMAIL
        }
    }

    url = f"https://api.github.com/repos/{ORG}/{repo}/contents/{wf_path}"
    status, resp = api_request(url, method="PUT", data=commit_payload)

    if status in (200, 201):
        print(f"   {GREEN}✅ Successfully added '{tgt_branch}' to {wf_path}!{RESET}")
        # Dispatch workflow run
        wf_name = os.path.basename(wf_path)
        dispatch_url = f"https://api.github.com/repos/{ORG}/{repo}/actions/workflows/{wf_name}/dispatches"
        d_status, _ = api_request(dispatch_url, method="POST", data={"ref": tgt_branch})
        if d_status in (200, 204):
            print(f"   {GREEN}🚀 Triggered workflow build via GitHub Actions API!{RESET}")
        return True
    else:
        print(f"   ❌ Failed to update {wf_path}: {resp.get('message')}")
        return False


def check_k8s_rollout(repo, namespace="dev", timeout_sec=60):
    """
    Checks live Kubernetes rollout status using kubectl.
    Gracefully skips if kubectl or cluster context is not configured.
    """
    import subprocess
    import shutil

    if not shutil.which("kubectl"):
        return None  # kubectl not installed, skip gracefully

    # Map repository name to deployment name: Medvista-BE-Core-Service ➔ utkal-be-core-service
    dep_name = repo.lower().replace("medvista-", "utkal-")
    
    cmd = f"kubectl rollout status deployment/{dep_name} -n {namespace} --timeout={timeout_sec}s"
    try:
        res = subprocess.run(cmd, shell=True, capture_output=True, text=True, timeout=timeout_sec + 5)
        if res.returncode == 0:
            return {
                "success": True,
                "deployment": dep_name,
                "namespace": namespace,
                "msg": res.stdout.strip()
            }
        else:
            return {
                "success": False,
                "deployment": dep_name,
                "namespace": namespace,
                "error": res.stderr.strip() or res.stdout.strip()
            }
    except Exception as e:
        return {
            "success": False,
            "deployment": dep_name,
            "namespace": namespace,
            "error": str(e)
        }


def process_service_merge(repo, src_branch, tgt_branch, dry_run=False, sender_email=None, auto_fix_triggers=False, check_k8s=False, k8s_namespace="dev"):
    """
    Processes a single backend service:
    1. Compares branches
    2. Opens/checks PR
    3. Merges via API if clean
    4. Handles conflict & notifies developer if blocked
    5. Checks build trigger
    """
    print(f"\n{BOLD}📦 Processing: {CYAN}{repo}{RESET} ({src_branch} ➔ {tgt_branch})")

    # 1. Compare branches on GitHub
    compare_url = f"https://api.github.com/repos/{ORG}/{repo}/compare/{tgt_branch}...{src_branch}"
    status, comp = api_request(compare_url)

    if status == 404:
        print(f"  ❌ Repository or branch not found on GitHub ({status})")
        return {"repo": repo, "status": "NOT_FOUND", "detail": f"Repo or branch '{src_branch}' / '{tgt_branch}' does not exist"}

    if status != 200:
        print(f"  ❌ GitHub compare API returned error: {status} - {comp.get('message')}")
        return {"repo": repo, "status": "ERROR", "detail": comp.get("message")}

    ahead_by = comp.get("ahead_by", 0)
    commits = comp.get("commits", [])
    files = comp.get("files", [])

    if ahead_by == 0:
        print(f"  🟢 {GREEN}Already Up-to-Date{RESET} (0 commits ahead)")
        return {"repo": repo, "status": "UP_TO_DATE", "ahead": 0}

    print(f"  📊 Found {BOLD}{ahead_by}{RESET} commit(s) ahead ({len(files)} files changed)")
    last_commit_sha = commits[-1]["sha"] if commits else ""
    last_commit_info = get_commit_author_info(repo, last_commit_sha)
    print(f"     Last commit: [{last_commit_info['sha']}] {last_commit_info['message']} by {last_commit_info['name']}")

    # 2. Check for existing open PR or create a new PR
    pr_list_url = f"https://api.github.com/repos/{ORG}/{repo}/pulls?state=open&head={ORG}:{src_branch}&base={tgt_branch}"
    status, existing_prs = api_request(pr_list_url)
    pr = existing_prs[0] if (status == 200 and existing_prs) else None

    if not pr:
        # Human-like title matching exactly how you do it manually on GitHub (e.g. 'Utkal dev', 'Utkal uat')
        if "dev" in src_branch.lower():
            human_title = "Utkal dev"
        elif "uat" in src_branch.lower():
            human_title = "Utkal uat"
        elif "prod" in src_branch.lower():
            human_title = "Utkal prod"
        else:
            human_title = src_branch.replace("-", " ").capitalize()

        pr_payload = {
            "title": human_title,
            "head": src_branch,
            "base": tgt_branch,
            "body": ""
        }
        create_pr_url = f"https://api.github.com/repos/{ORG}/{repo}/pulls"
        status, pr_data = api_request(create_pr_url, method="POST", data=pr_payload)
        if status in (200, 201):
            pr = pr_data
            print(f"  📝 Created Pull Request: {CYAN}{pr['html_url']}{RESET}")
        else:
            msg = pr_data.get("message", "")
            print(f"  ❌ Could not create PR: {msg}")
            return {"repo": repo, "status": "PR_FAILED", "detail": msg}
    else:
        print(f"  📝 Found Existing Pull Request: {CYAN}{pr['html_url']}{RESET}")

    pr_number = pr["number"]
    pr_url = pr["html_url"]

    # 3. Poll PR for mergeability (GitHub computes this asynchronously)
    mergeable = pr.get("mergeable")
    retries = 0
    while mergeable is None and retries < 5:
        time.sleep(2)
        status, pr_refreshed = api_request(f"https://api.github.com/repos/{ORG}/{repo}/pulls/{pr_number}")
        if status == 200:
            pr = pr_refreshed
            mergeable = pr.get("mergeable")
        retries += 1

    # 4. Handle Conflict
    if mergeable is False:
        print(f"  🚨 {RED}{BOLD}MERGE CONFLICT DETECTED!{RESET}")
        print(f"     PR #{pr_number} remains OPEN on GitHub for manual resolution: {pr_url}")
        email_sent = send_conflict_notification(repo, pr, last_commit_info, src_branch, tgt_branch, sender_email)
        return {
            "repo": repo,
            "status": "CONFLICT",
            "pr_url": pr_url,
            "pr_number": pr_number,
            "author": last_commit_info,
            "notified": email_sent
        }

    # 5. Dry-Run Check
    if dry_run:
        print(f"  🔎 {YELLOW}[DRY-RUN]{RESET} Branch is clean and ready to merge via PR #{pr_number} (No merge performed)")
        return {
            "repo": repo,
            "status": "DRY_RUN_MERGEABLE",
            "pr_url": pr_url,
            "pr_number": pr_number,
            "ahead": ahead_by
        }

    # 6. Perform Native GitHub PR Merge
    print(f"  🚀 Merging PR #{pr_number} via GitHub API as {BOLD}{AUTHOR_NAME}{RESET}...")
    merge_payload = {
        "commit_title": f"Merge pull request #{pr_number} from {ORG}/{src_branch}",
        "commit_message": "",
        "merge_method": "merge"
    }
    merge_url = f"https://api.github.com/repos/{ORG}/{repo}/pulls/{pr_number}/merge"
    m_status, m_data = api_request(merge_url, method="PUT", data=merge_payload)

    if m_status != 200 or not m_data.get("merged"):
        err = m_data.get("message", "Merge failed")
        print(f"  ❌ Merge rejected by GitHub: {err}")
        return {"repo": repo, "status": "MERGE_FAILED", "detail": err, "pr_url": pr_url}

    merge_sha = m_data.get("sha", "")
    short_sha = merge_sha[:8] if merge_sha else "merged"
    print(f"  ✅ {GREEN}{BOLD}Successfully Merged!{RESET} Commit SHA: {short_sha}")

    # 7. Check if CI/CD Build Workflow Triggered
    print(f"  ⏳ Checking GitHub Actions build trigger (10-15s)...")
    build_info = check_build_triggered(repo, merge_sha, tgt_branch)
    if build_info.get("triggered"):
        print(f"  🎉 {GREEN}BUILD TRIGGERED:{RESET} Run #{build_info['run_number']} ({build_info['status']})")
        print(f"     URL: {build_info.get('html_url')}")
    else:
        print(f"  ⚠️  {YELLOW}{BOLD}BUILD NOT TRIGGERED:{RESET} {build_info.get('reason')}")
        send_build_missing_notification(repo, merge_sha, tgt_branch, build_info.get("reason"))
        if build_info.get("missing_trigger"):
            fixed = prompt_and_fix_missing_trigger(repo, tgt_branch, build_info, auto_fix=auto_fix_triggers)
            if fixed:
                build_info["triggered"] = True

    # 8. Check Kubernetes Rollout status (if requested)
    k8s_info = None
    if check_k8s:
        print(f"  ☸️ Checking Kubernetes pod rollout status in namespace '{k8s_namespace}'...")
        k8s_info = check_k8s_rollout(repo, namespace=k8s_namespace)
        if k8s_info:
            if k8s_info.get("success"):
                print(f"     {GREEN}🟢 Rollout Complete:{RESET} {k8s_info['deployment']} is active")
            else:
                print(f"     {YELLOW}⚠️ Rollout Stalled / Warning:{RESET} {k8s_info.get('error')}")

    return {
        "repo": repo,
        "status": "MERGED",
        "sha": short_sha,
        "pr_url": pr_url,
        "ahead": ahead_by,
        "build": build_info,
        "k8s": k8s_info
    }


def generate_execution_summary(results, src_branch, tgt_branch):
    """Generates an ANSI terminal summary and formatted markdown."""
    merged = [r for r in results if r["status"] == "MERGED"]
    dry_runs = [r for r in results if r["status"] == "DRY_RUN_MERGEABLE"]
    conflicts = [r for r in results if r["status"] == "CONFLICT"]
    up_to_date = [r for r in results if r["status"] == "UP_TO_DATE"]
    failed = [r for r in results if r["status"] not in ("MERGED", "DRY_RUN_MERGEABLE", "CONFLICT", "UP_TO_DATE")]

    no_build = [r for r in merged if r.get("build") and not r["build"].get("triggered")]

    print("\n" + "=" * 80)
    print(f"{BOLD}📊 BACKEND RELEASE MERGE SUMMARY ({src_branch} ➔ {tgt_branch}){RESET}")
    print("=" * 80)
    print(f"Total Backend Services: {len(results)}")
    if merged:
        print(f"  ✅ Successfully Merged: {GREEN}{len(merged)}{RESET}")
    if dry_runs:
        print(f"  🔎 Mergeable (Dry-Run): {YELLOW}{len(dry_runs)}{RESET}")
    if conflicts:
        print(f"  🚨 Conflicts Detected:  {RED}{BOLD}{len(conflicts)}{RESET}")
    print(f"  🟢 Already Up-to-Date:  {DIM}{len(up_to_date)}{RESET}")
    if failed:
        print(f"  ❌ Errors / Skipped:    {RED}{len(failed)}{RESET}")
    if no_build:
        print(f"  ⚠️  Build Not Triggered: {YELLOW}{BOLD}{len(no_build)}{RESET}")
    print("=" * 80)

    if merged:
        print(f"\n{BOLD}{GREEN}✅ Merged Repositories:{RESET}")
        for r in merged:
            b_str = "Build: Triggered ✅" if r.get("build", {}).get("triggered") else f"{YELLOW}Build: NOT TRIGGERED ⚠️{RESET}"
            print(f"  • {BOLD}{r['repo']}{RESET} (PR: {r['pr_url']}, SHA: {r['sha']}) | {b_str}")

    if conflicts:
        print(f"\n{BOLD}{RED}🚨 Repositories with Conflicts (PR left open, developer notified):{RESET}")
        for r in conflicts:
            dev_str = f"Dev: {r['author'].get('name')} <{r['author'].get('email')}>" if r.get('author') else ""
            print(f"  • {BOLD}{r['repo']}{RESET} | PR: {r['pr_url']} | {dev_str}")

    if no_build:
        print(f"\n{BOLD}{YELLOW}⚠️  Merged Repositories Missing Build Triggers:{RESET}")
        for r in no_build:
            print(f"  • {BOLD}{r['repo']}{RESET}: {r.get('build', {}).get('reason')}")

    # GitHub Actions Step Summary (Rendered visually in GitHub Mobile App)
    summary_path = os.environ.get("GITHUB_STEP_SUMMARY")
    if summary_path:
        try:
            with open(summary_path, "a", encoding="utf-8") as f:
                f.write(f"## 📊 MedVista Backend Release Summary (`{src_branch}` ➔ `{tgt_branch}`)\n\n")
                f.write(f"- **Total Backend Repos:** {len(results)}\n")
                f.write(f"- **Merged:** {len(merged)}\n")
                f.write(f"- **Conflicts:** {len(conflicts)}\n")
                f.write(f"- **Already Up-to-Date:** {len(up_to_date)}\n\n")
                if merged:
                    f.write("### ✅ Successfully Merged\n")
                    for r in merged:
                        b_badge = "✅ Build Triggered" if r.get("build", {}).get("triggered") else "⚠️ **BUILD NOT TRIGGERED**"
                        f.write(f"- [{r['repo']}]({r['pr_url']}) (Commit: `{r['sha']}`) — {b_badge}\n")
                    f.write("\n")
                if conflicts:
                    f.write("### 🚨 Conflicts (PR Kept Open)\n")
                    for r in conflicts:
                        f.write(f"- [{r['repo']}]({r['pr_url']}) — Developer: `{r['author'].get('name')}` (`{r['author'].get('email')}`)\n")
                    f.write("\n")
                if no_build:
                    f.write("### ⚠️ Missing CI/CD Build Alerts\n")
                    for r in no_build:
                        f.write(f"- **{r['repo']}**: {r.get('build', {}).get('reason')}\n")
                    f.write("\n")
        except Exception:
            pass


def main():
    parser = argparse.ArgumentParser(description="MedVista Backend Release Merger & Conflict Watchdog")
    parser.add_argument("--email-text", type=str, help="Raw text or paste of developer release email")
    parser.add_argument("--email-file", type=str, help="Path to file containing release email text")
    parser.add_argument("--services", type=str, help="Comma-separated backend services (e.g. Medvista-BE-Core-Service,Medvista-BE-IPD-Service)")
    parser.add_argument("--src", default=DEFAULT_SRC, help=f"Source branch (default: {DEFAULT_SRC})")
    parser.add_argument("--tgt", default=DEFAULT_TGT, help=f"Target branch (default: {DEFAULT_TGT})")
    parser.add_argument("--sender", type=str, help="Email address of developer who sent release email (e.g. jagdeesh@...)")
    parser.add_argument("--dry-run", action="store_true", help="Simulate check and detect conflicts without merging")
    parser.add_argument("--auto-fix-triggers", action="store_true", help="Automatically fix missing triggers in build.yaml without interactive prompt")
    parser.add_argument("--check-k8s", action="store_true", help="Verify Kubernetes pod rollout status after build")
    parser.add_argument("--k8s-namespace", default="dev", help="Kubernetes namespace to check rollout in (default: dev)")
    parser.add_argument("--yes", "-y", action="store_true", help="Skip confirmation prompt")

    args = parser.parse_args()

    # Collect raw text
    raw_text = ""
    if args.email_file and os.path.exists(args.email_file):
        with open(args.email_file, "r", encoding="utf-8") as f:
            raw_text = f.read()
    elif args.email_text:
        raw_text = args.email_text
    elif args.services:
        raw_text = args.services
    else:
        # Interactive paste mode
        print(f"{BOLD}{CYAN}╔═══════════════════════════════════════════════════════════════════╗{RESET}")
        print(f"{BOLD}{CYAN}║         🚀 MEDVISTA BACKEND RELEASE MERGER (BOT)                  ║{RESET}")
        print(f"{BOLD}{CYAN}╚═══════════════════════════════════════════════════════════════════╝{RESET}\n")
        print(f"Paste the developer release email text / table below.")
        print(f"{DIM}(Press Ctrl+D or type 'DONE' on a new line when finished):{RESET}\n")
        lines = []
        try:
            while True:
                line = input()
                clean_stripped = line.strip()
                if clean_stripped.upper() in ("DONE", "EXIT", "QUIT", "Q"):
                    break
                # If single-line quick command was given (e.g. 'core from dev to uat') and user hits Enter on empty line
                if not clean_stripped and lines:
                    break
                lines.append(line)
        except EOFError:
            pass
        raw_text = "\n".join(lines)

    services = parse_backend_services_from_text(raw_text)

    # Auto-detect branches from text if defaults were used
    src_branch = args.src
    tgt_branch = args.tgt
    if args.src == DEFAULT_SRC and args.tgt == DEFAULT_TGT:
        src_branch, tgt_branch = detect_branches_from_text(raw_text, default_src=DEFAULT_SRC, default_tgt=DEFAULT_TGT)

    if not services:
        print(f"\n{YELLOW}⚠️ No backend services ('Medvista-BE-*') detected in provided input.{RESET}")
        print("Example valid services: Medvista-BE-Inventory-Service, Medvista-BE-Core-Service (or short names like 'core, ipd')")
        sys.exit(1)

    print(f"\n{BOLD}🔍 Discovered {len(services)} Backend Services:{RESET}")
    for idx, s in enumerate(services, 1):
        print(f"  {idx}. {s}")

    print(f"\n{BOLD}Release Plan:{RESET}")
    print(f"  • Source Branch: {CYAN}{src_branch}{RESET}")
    print(f"  • Target Branch: {CYAN}{tgt_branch}{RESET}")
    print(f"  • Merge Author:  {GREEN}{AUTHOR_NAME} <{AUTHOR_EMAIL}>{RESET}")
    print(f"  • Mode:          {YELLOW}{'DRY-RUN (Simulate)' if args.dry_run else 'LIVE RELEASE MERGE'}{RESET}")

    if not args.yes and not args.dry_run:
        confirm = input(f"\nProceed with merging {len(services)} backend service(s)? [y/N]: ").strip().lower()
        if confirm not in ("y", "yes"):
            print("Aborted by user.")
            sys.exit(0)

    # Process all services
    results = []
    for s in services:
        res = process_service_merge(
            repo=s,
            src_branch=src_branch,
            tgt_branch=tgt_branch,
            dry_run=args.dry_run,
            sender_email=args.sender,
            auto_fix_triggers=args.auto_fix_triggers,
            check_k8s=args.check_k8s,
            k8s_namespace=args.k8s_namespace
        )
        results.append(res)

    generate_execution_summary(results, src_branch, tgt_branch)


if __name__ == "__main__":
    main()

