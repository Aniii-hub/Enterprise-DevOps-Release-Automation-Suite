#!/usr/bin/env python3
"""
MedVista Daily Standup Bot
==========================
Generates a concise, 1 to 2 minute daily standup briefing by analyzing:
  - Git commits across 120+ microservices in the workspace
  - PR merges & branch syncs (e.g. utkal-dev, utkal-uat, utkal-prod)
  - Build pipeline updates (build.yaml)
  - Uncommitted in-flight work & active branches
  - Recently modified workspace scripts, configs, and reports

Features:
  - Smart Date Detection: Automatically handles weekends (Mondays summarize Friday-Sunday)
  - 1-2 Minute Spoken Script: Conversational first-person update for verbal standup
  - Slack / Teams Format: Ready to copy-paste directly into chat channels
  - Voice Synthesis: Speaks the update out loud using macOS `say` (--speak / -s)
  - Clipboard Copy: Copies markdown to clipboard using `pbcopy` (--copy / -c)
  - Detailed Audit: Full commit and repo breakdown (--verbose / -v)
"""

import os
import sys
import re
import time
import shutil
import argparse
import subprocess
from datetime import datetime, timedelta
from collections import defaultdict

# ANSI Colors
BOLD = "\033[1m"
GREEN = "\033[32m"
CYAN = "\033[36m"
YELLOW = "\033[33m"
BLUE = "\033[34m"
MAGENTA = "\033[35m"
DIM = "\033[2m"
RESET = "\033[0m"

DEFAULT_AUTHORS = [
    os.environ.get("GIT_AUTHOR_NAME", "Aniii-hub"),
    os.environ.get("GIT_AUTHOR_EMAIL", "anirudhpardeshi25@gmail.com")
]


def get_git_config_user():
    """Detect local git user name and email."""
    users = []
    try:
        res = subprocess.run(["git", "config", "user.name"], capture_output=True, text=True)
        if res.stdout.strip():
            users.append(res.stdout.strip())
        res = subprocess.run(["git", "config", "user.email"], capture_output=True, text=True)
        if res.stdout.strip():
            users.append(res.stdout.strip())
    except Exception:
        pass
    for default_author in DEFAULT_AUTHORS:
        if default_author not in users:
            users.append(default_author)
    return users


def get_standup_timeframe(mode="smart", custom_days=None, custom_since=None, custom_until=None):
    """
    Computes (since_dt, until_dt, label).
    Smart mode:
      - If today is Monday (weekday == 0): spans Friday 00:00:00 to current time.
      - If today is Tue-Fri: spans yesterday 00:00:00 to yesterday 23:59:59 (or up to today 08:00).
      - If today is Sat/Sun: spans Friday 00:00:00 to now.
    """
    now = datetime.now()
    today_start = now.replace(hour=0, minute=0, second=0, microsecond=0)

    if custom_since:
        since_dt = datetime.strptime(custom_since, "%Y-%m-%d")
        until_dt = datetime.strptime(custom_until, "%Y-%m-%d").replace(hour=23, minute=59, second=59) if custom_until else now
        return since_dt, until_dt, f"Since {custom_since}"

    if custom_days is not None:
        since_dt = today_start - timedelta(days=custom_days)
        until_dt = now
        return since_dt, until_dt, f"Last {custom_days} days"

    if mode == "today":
        since_dt = today_start
        until_dt = now
        return since_dt, until_dt, f"Today ({now.strftime('%b %d, %Y')})"

    if mode == "yesterday":
        yesterday_start = today_start - timedelta(days=1)
        yesterday_end = yesterday_start.replace(hour=23, minute=59, second=59)
        return yesterday_start, yesterday_end, f"Yesterday ({yesterday_start.strftime('%b %d, %Y')})"

    # Smart Standup Mode
    weekday = now.weekday()  # Monday = 0, Tuesday = 1, Sunday = 6
    if weekday == 0:  # Monday standup covers Friday through Sunday
        since_dt = today_start - timedelta(days=3)  # Friday 00:00
        until_dt = now
        label = f"Friday to Weekend ({since_dt.strftime('%b %d')} - {(today_start - timedelta(days=1)).strftime('%b %d')})"
    elif weekday in (5, 6):  # Weekend
        since_dt = today_start - timedelta(days=(weekday - 4))
        until_dt = now
        label = f"Since Friday ({since_dt.strftime('%b %d')})"
    else:  # Tuesday through Friday: yesterday
        since_dt = today_start - timedelta(days=1)
        until_dt = today_start.replace(hour=6, minute=0, second=0) if now.hour < 12 else (today_start - timedelta(days=1)).replace(hour=23, minute=59, second=59)
        label = f"Yesterday ({since_dt.strftime('%b %d, %Y')})"

    return since_dt, until_dt, label


def find_git_repos(workspace_root):
    """Finds all git repositories in workspace up to depth 2."""
    repos = []
    try:
        entries = sorted(os.listdir(workspace_root))
    except Exception:
        return []

    for entry in entries:
        full_path = os.path.join(workspace_root, entry)
        if not os.path.isdir(full_path) or entry.startswith("."):
            continue
        if os.path.isdir(os.path.join(full_path, ".git")):
            repos.append((entry, full_path))
        else:
            # Check depth 2 subdirectories
            try:
                sub_entries = sorted(os.listdir(full_path))
                for sub in sub_entries:
                    sub_path = os.path.join(full_path, sub)
                    if os.path.isdir(os.path.join(sub_path, ".git")):
                        repos.append((f"{entry}/{sub}", sub_path))
            except Exception:
                pass
    return repos


def collect_git_activity(repos, authors, since_dt, until_dt):
    """Gathers commits and dirty files for each repository."""
    since_str = since_dt.strftime("%Y-%m-%d %H:%M:%S")
    until_str = until_dt.strftime("%Y-%m-%d %H:%M:%S")

    activity = {
        "commits": [],
        "commits_by_repo": defaultdict(list),
        "dirty_repos": {},
        "active_branches": {}
    }

    author_args = []
    for a in authors:
        author_args.extend(["--author", a])

    for repo_name, repo_path in repos:
        # 1. Fetch commits
        cmd = [
            "git", "-C", repo_path, "log", "--all",
            f"--since={since_str}", f"--until={until_str}"
        ] + author_args + ["--format=%H|%ad|%an|%s", "--date=iso"]

        res = subprocess.run(cmd, capture_output=True, text=True)
        if res.returncode == 0 and res.stdout.strip():
            for line in res.stdout.strip().splitlines():
                parts = line.split("|", 3)
                if len(parts) == 4:
                    commit_obj = {
                        "repo": repo_name,
                        "hash": parts[0][:8],
                        "date": parts[1],
                        "author": parts[2],
                        "subject": parts[3]
                    }
                    activity["commits"].append(commit_obj)
                    activity["commits_by_repo"][repo_name].append(commit_obj)

        # 2. Check current branch & uncommitted status
        branch_res = subprocess.run(["git", "-C", repo_path, "branch", "--show-current"], capture_output=True, text=True)
        current_branch = branch_res.stdout.strip() if branch_res.returncode == 0 else ""
        if current_branch:
            activity["active_branches"][repo_name] = current_branch

        status_res = subprocess.run(["git", "-C", repo_path, "status", "--porcelain"], capture_output=True, text=True)
        if status_res.returncode == 0 and status_res.stdout.strip():
            modified_files = [line.strip() for line in status_res.stdout.strip().splitlines()]
            activity["dirty_repos"][repo_name] = {
                "branch": current_branch,
                "files": modified_files
            }

    return activity


def collect_workspace_files(workspace_root, since_dt, until_dt):
    """Finds recently modified standalone scripts, XML, configs, or reports."""
    since_ts = since_dt.timestamp()
    until_ts = until_dt.timestamp()
    modified = []

    ignore_dirs = {".git", "node_modules", ".gemini", ".agent", "__pycache__", ".idea", ".vscode"}
    ignore_exts = {".pyc", ".DS_Store", ".swp"}

    for dirpath, dirnames, filenames in os.walk(workspace_root):
        dirnames[:] = [d for d in dirnames if d not in ignore_dirs and not d.startswith(".")]
        for f in filenames:
            if f.startswith(".") or any(f.endswith(ext) for ext in ignore_exts):
                continue
            fp = os.path.join(dirpath, f)
            try:
                mtime = os.path.getmtime(fp)
                if since_ts <= mtime <= until_ts:
                    rel_path = os.path.relpath(fp, workspace_root)
                    modified.append((rel_path, mtime))
            except OSError:
                pass

    modified.sort(key=lambda x: x[1], reverse=True)
    return modified


def clean_service_name(repo_name):
    """Shortens 'Medvista-BE-Inventory-Service' to 'Inventory'."""
    name = repo_name
    for prefix in ["Medvista-BE-", "Medvista-UI-Lib-", "Medvista-UI-", "medvista-", "VM-LT-BE-", "VM-LT-UI-"]:
        if name.startswith(prefix):
            name = name[len(prefix):]
    if name.endswith("-Service"):
        name = name[:-8]
    return name.replace("-", " ")


def analyze_standup_data(activity, workspace_files):
    """
    Synthesizes commits, dirty branches, and files into thematic standup sections.
    """
    pr_merges = defaultdict(lambda: {"repos": set(), "prs": []})
    build_yaml_updates = set()
    other_commits = []
    
    # 1. Classify commits
    for c in activity["commits"]:
        subj = c["subject"]
        repo = c["repo"]
        svc = clean_service_name(repo)

        # Match PR merges
        m_pr = re.search(r'Merge pull request #(\d+) from MedVista-Global-Pvt-Ltd/([a-zA-Z0-9_\-]+)', subj)
        m_branch = re.search(r'Merge branch \'([^\']+)\'', subj)
        m_build = re.search(r'Update build\.yaml', subj, re.IGNORECASE)

        if m_pr:
            pr_num = m_pr.group(1)
            src_branch = m_pr.group(2)
            pr_merges[src_branch]["repos"].add(svc)
            pr_merges[src_branch]["prs"].append((svc, pr_num))
        elif m_branch:
            src_branch = m_branch.group(1)
            pr_merges[src_branch]["repos"].add(svc)
        elif m_build:
            build_yaml_updates.add(svc)
        else:
            other_commits.append((svc, subj, c["hash"]))

    # 2. Extract in-flight / dirty work
    active_in_flight = []
    for repo, info in activity["dirty_repos"].items():
        svc = clean_service_name(repo)
        branch = info["branch"]
        num_files = len(info["files"])
        sample_file = os.path.basename(info["files"][0].split()[-1]) if info["files"] else ""
        active_in_flight.append({
            "service": svc,
            "branch": branch,
            "file_count": num_files,
            "sample": sample_file
        })

    # 3. Extract key workspace files modified
    key_tools_touched = []
    reports_generated = []
    xml_interfaces_touched = []

    for rel_path, mtime in workspace_files:
        basename = os.path.basename(rel_path)
        if basename.endswith(".py") and not "/." in rel_path:
            key_tools_touched.append(basename)
        elif rel_path.startswith("reports/") and basename.endswith((".md", ".html", ".json")):
            reports_generated.append(basename)
        elif basename.endswith(".xml"):
            xml_interfaces_touched.append(basename)

    return {
        "pr_merges": pr_merges,
        "build_yaml_updates": build_yaml_updates,
        "other_commits": other_commits,
        "active_in_flight": active_in_flight,
        "key_tools": list(dict.fromkeys(key_tools_touched))[:5],
        "reports": list(dict.fromkeys(reports_generated))[:4],
        "xml_interfaces": list(dict.fromkeys(xml_interfaces_touched))[:4]
    }


def generate_speech_script(analysis, label):
    """
    Generates a natural, spoken 60-90 second standup update (~130-170 words).
    """
    sentences = []
    
    # Opening
    sentences.append(f"In my last working session covering {label}, I focused primarily on release synchronization, build automation, and environment verification.")

    # PR Merges & Releases
    if analysis["pr_merges"]:
        branches_merged = list(analysis["pr_merges"].keys())
        all_repos = set()
        for b in branches_merged:
            all_repos.update(analysis["pr_merges"][b]["repos"])
        
        repo_count = len(all_repos)
        top_repos = list(all_repos)[:4]
        repos_str = ", ".join(top_repos)
        if repo_count > 4:
            repos_str += f", and {repo_count - 4} other services"

        branch_names = " and ".join(branches_merged[:3])
        sentences.append(f"First, I synchronized and merged {branch_names} branches across {repo_count} microservices, including {repos_str}.")

    # Build Pipelines
    if analysis["build_yaml_updates"]:
        build_count = len(analysis["build_yaml_updates"])
        sentences.append(f"I refreshed the automated build pipeline configurations across {build_count} services to trigger continuous integration.")

    # Tooling & Scripts
    if analysis["key_tools"]:
        tools_str = ", ".join(analysis["key_tools"][:3])
        sentences.append(f"On the infrastructure and tooling side, I updated and executed key automation scripts including {tools_str}.")

    # XML / Integration Specs
    if analysis["xml_interfaces"]:
        sentences.append(f"I also updated the integration interface specifications for LIS and Medsynapse.")

    # Other Specific Commits
    if analysis["other_commits"]:
        sample_msg = analysis["other_commits"][0][1]
        sample_svc = analysis["other_commits"][0][0]
        # Clean merge noise if any
        if not sample_msg.startswith("Merge"):
            sentences.append(f"In addition, I implemented updates for {sample_svc}, specifically {sample_msg}.")

    # Today's Plan
    today_items = []
    if analysis["active_in_flight"]:
        top_dirty = [item["service"] for item in analysis["active_in_flight"][:3]]
        today_items.append(f"verifying in-progress updates in {', '.join(top_dirty)}")
    today_items.append("monitoring deployment status and branch sync pipelines across UAT and production")
    
    sentences.append(f"Today, I'm focusing on {' and '.join(today_items)}.")
    sentences.append("I have no blockers at this time.")

    speech_text = " ".join(sentences)
    word_count = len(speech_text.split())
    # Normal spoken pace: ~140 words per minute
    est_seconds = int((word_count / 140.0) * 60)
    mins = est_seconds // 60
    secs = est_seconds % 60
    duration_str = f"{mins}m {secs:02d}s" if mins > 0 else f"{secs}s"

    return speech_text, word_count, duration_str


def generate_markdown_standup(analysis, label):
    """
    Generates Slack / Teams copy-pasteable Markdown standup format.
    """
    lines = []
    lines.append(f"📋 *Daily Standup Update* ({label})")
    lines.append("")
    lines.append("🎯 *Yesterday / Last Session Accomplishments:*")

    # 1. PR Merges
    if analysis["pr_merges"]:
        all_repos = set()
        branches = []
        for b, data in analysis["pr_merges"].items():
            branches.append(f"`{b}`")
            all_repos.update(data["repos"])
        repos_sample = ", ".join(sorted(list(all_repos))[:6])
        extra = f" +{len(all_repos) - 6} more" if len(all_repos) > 6 else ""
        lines.append(f"• *Branch & PR Merges:* Synchronized {', '.join(branches)} across {len(all_repos)} microservices ({repos_sample}{extra})")

    # 2. Build Pipeline
    if analysis["build_yaml_updates"]:
        lines.append(f"• *CI/CD Pipelines:* Refreshed `build.yaml` triggers across {len(analysis['build_yaml_updates'])} backend services")

    # 3. Tooling & Automation
    if analysis["key_tools"]:
        tools_str = ", ".join([f"`{t}`" for t in analysis["key_tools"]])
        lines.append(f"• *Automation & Verification:* Updated and ran {tools_str}")

    # 4. XML / Interfaces
    if analysis["xml_interfaces"]:
        xml_str = ", ".join([f"`{x}`" for x in analysis["xml_interfaces"]])
        lines.append(f"• *Interface Configurations:* Updated inbound/outbound XML specs ({xml_str})")

    # 5. Other Notable Commits
    if analysis["other_commits"]:
        for svc, subj, chash in analysis["other_commits"][:3]:
            if not subj.startswith("Merge"):
                lines.append(f"• *{svc}:* {subj} (`{chash}`)")

    if not analysis["pr_merges"] and not analysis["build_yaml_updates"] and not analysis["other_commits"]:
        lines.append("• Continued ongoing development and environment configuration tasks")

    lines.append("")
    lines.append("🚀 *Today's Focus:*")
    if analysis["active_in_flight"]:
        for item in analysis["active_in_flight"][:3]:
            branch_info = f" on `{item['branch']}`" if item['branch'] else ""
            lines.append(f"• Complete pending changes in *{item['service']}*{branch_info} ({item['sample']})")
    lines.append("• Monitor branch analyzer sync & UAT/DEV restore verifications")
    lines.append("• Helm release checks & deployment stability review")

    lines.append("")
    lines.append("🚧 *Blockers / Impediments:*")
    lines.append("• None currently")

    return "\n".join(lines)


def speak_text(text, voice=None, rate=170):
    """Speaks the text aloud on macOS using the native `say` command."""
    if not shutil.which("say"):
        print(f"{YELLOW}⚠️  `say` command is not available on this system.{RESET}")
        return False

    cmd = ["say", "-r", str(rate)]
    if voice:
        cmd.extend(["-v", voice])
    cmd.append(text)

    try:
        print(f"{CYAN}🔊 Speaking your 1-minute standup update... (Press Ctrl+C to stop){RESET}")
        subprocess.run(cmd, check=True)
        print(f"{GREEN}✅ Speech finished.{RESET}")
        return True
    except KeyboardInterrupt:
        print(f"\n{YELLOW}⏹️ Speech stopped by user.{RESET}")
        return True
    except Exception as e:
        print(f"{YELLOW}⚠️ Error playing speech: {e}{RESET}")
        return False


def copy_to_clipboard(text):
    """Copies text to macOS clipboard using pbcopy."""
    if shutil.which("pbcopy"):
        try:
            p = subprocess.Popen(["pbcopy"], stdin=subprocess.PIPE)
            p.communicate(text.encode("utf-8"))
            return True
        except Exception:
            return False
    return False


def print_banner():
    banner = f"""
{BOLD}{CYAN}╔═══════════════════════════════════════════════════════════════════╗
║                🎙️  MEDVISTA DAILY STANDUP BOT                      ║
║         Your 1-to-2 Minute Personal Work Summary & Speech         ║
╚═══════════════════════════════════════════════════════════════════╝{RESET}
"""
    print(banner)


def main():
    parser = argparse.ArgumentParser(
        description="MedVista Daily Standup Bot: Generates a 1-2 minute verbal & text standup summary."
    )
    parser.add_argument("--path", default=".", help="Workspace path to scan (defaults to current dir)")
    parser.add_argument("--author", action="append", help="Filter commits by author (can specify multiple)")
    parser.add_argument("--today", action="store_true", help="Summarize today's work so far")
    parser.add_argument("--yesterday", action="store_true", help="Strictly summarize yesterday")
    parser.add_argument("--days", type=int, help="Look back N days (e.g. --days 3)")
    parser.add_argument("--since", help="Start date (YYYY-MM-DD)")
    parser.add_argument("--until", help="End date (YYYY-MM-DD)")
    parser.add_argument("-s", "--speak", action="store_true", help="Speak the standup out loud using macOS voice")
    parser.add_argument("--voice", help="Specify macOS voice name (e.g. Samantha, Alex, Victoria)")
    parser.add_argument("-c", "--copy", action="store_true", help="Copy Slack/Teams markdown to clipboard")
    parser.add_argument("-v", "--verbose", action="store_true", help="Show all commits and raw activity details")
    parser.add_argument("--save", action="store_true", help="Save the summary into reports/standup_<date>.md")

    args = parser.parse_args()

    workspace_root = os.path.abspath(args.path)
    authors = args.author if args.author else get_git_config_user()

    # Determine timeframe
    if args.today:
        mode = "today"
    elif args.yesterday:
        mode = "yesterday"
    else:
        mode = "smart"

    since_dt, until_dt, timeframe_label = get_standup_timeframe(
        mode=mode,
        custom_days=args.days,
        custom_since=args.since,
        custom_until=args.until
    )

    print_banner()
    print(f"{DIM}🔍 Workspace:{RESET} {workspace_root}")
    print(f"{DIM}👤 Authors:{RESET}   {', '.join(authors[:3])}")
    print(f"{DIM}⏱️  Window:{RESET}    {since_dt.strftime('%Y-%m-%d %H:%M')} ➔ {until_dt.strftime('%Y-%m-%d %H:%M')} ({timeframe_label})")
    print(f"{DIM}⏳ Scanning microservices and workspace activity...{RESET}\n")

    # Discover and collect
    repos = find_git_repos(workspace_root)
    activity = collect_git_activity(repos, authors, since_dt, until_dt)
    workspace_files = collect_workspace_files(workspace_root, since_dt, until_dt)

    # Analyze
    analysis = analyze_standup_data(activity, workspace_files)

    # Generate Outputs
    speech_text, word_count, duration_str = generate_speech_script(analysis, timeframe_label)
    slack_markdown = generate_markdown_standup(analysis, timeframe_label)

    # Section 1: Spoken Standup Script
    print(f"{BOLD}{MAGENTA}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    print(f"{BOLD}{MAGENTA}🗣️  WHAT TO SAY IN STANDUP (1-2 Min Spoken Script){RESET}")
    print(f"{DIM}   ⏱️ Estimated spoken duration: {BOLD}{duration_str}{RESET}{DIM} ({word_count} words at 140 wpm){RESET}")
    print(f"{BOLD}{MAGENTA}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    print(f"\n{speech_text}\n")

    # Section 2: Slack / Teams Markdown
    print(f"{BOLD}{CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
    print(f"{BOLD}{CYAN}💬 SLACK / TEAMS COPY-PASTE FORMAT{RESET}")
    print(f"{BOLD}{CYAN}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}\n")
    print(slack_markdown)
    print()

    # Section 3: Verbose Breakdown (if requested)
    if args.verbose:
        print(f"{BOLD}{YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}")
        print(f"{BOLD}{YELLOW}🔍 DETAILED ACTIVITY AUDIT{RESET}")
        print(f"{BOLD}{YELLOW}━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━━{RESET}\n")
        print(f"Total Commits Found: {len(activity['commits'])} across {len(activity['commits_by_repo'])} repositories\n")
        for repo, comms in activity["commits_by_repo"].items():
            print(f"{BOLD}📦 {repo}:{RESET}")
            for c in comms:
                print(f"   • [{c['hash']}] {c['subject']} ({c['date'][:10]})")
        
        if activity["dirty_repos"]:
            print(f"\n{BOLD}📝 Working Tree In-Flight Repos:{RESET}")
            for r, d in activity["dirty_repos"].items():
                print(f"   • {r} (branch: {d['branch']}) - {len(d['files'])} uncommitted file(s)")

        if workspace_files:
            print(f"\n{BOLD}📄 Workspace Files Touched:{RESET}")
            for p, _ in workspace_files[:10]:
                print(f"   • {p}")
        print()

    # Clipboard copy
    if args.copy:
        if copy_to_clipboard(slack_markdown):
            print(f"{GREEN}📋 Successfully copied standup text to your clipboard! Just press Cmd+V in Slack/Teams.{RESET}")
        else:
            print(f"{YELLOW}⚠️ Could not copy to clipboard (pbcopy not available).{RESET}")

    # Save to reports
    if args.save:
        reports_dir = os.path.join(workspace_root, "reports")
        os.makedirs(reports_dir, exist_ok=True)
        report_file = os.path.join(reports_dir, f"standup_{datetime.now().strftime('%Y-%m-%d')}.md")
        try:
            with open(report_file, "w", encoding="utf-8") as f:
                f.write(f"# MedVista Daily Standup - {timeframe_label}\n\n")
                f.write("## 🗣️ Spoken Standup Script\n\n")
                f.write(f"> Estimated Duration: **{duration_str}** ({word_count} words)\n\n")
                f.write(f"{speech_text}\n\n")
                f.write("## 💬 Slack / Teams Update\n\n")
                f.write(f"```markdown\n{slack_markdown}\n```\n")
            print(f"{GREEN}💾 Saved standup report to: {report_file}{RESET}")
        except Exception as e:
            print(f"{YELLOW}⚠️ Could not save report: {e}{RESET}")

    # Voice Output
    if args.speak:
        print()
        speak_text(speech_text, voice=args.voice)


if __name__ == "__main__":
    main()

