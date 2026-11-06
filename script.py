#!/usr/bin/env python3
"""
Create retroactive commits from a YouTrack work_items.csv export,
then integrate into develop -> main -> release/1.0.

Flow:
  1. For each Issue ID: branch feature/<ID> from 'develop', replay
     each Work Item as an empty commit with author/date/message, push.
  2. Merge every feature branch into 'develop' (--no-ff), push.
  3. Merge 'develop' into 'main' (--no-ff), push.
  4. Reset 'release/1.0' to point at 'main', push.
"""

import csv
import os
import subprocess
import sys
from collections import defaultdict
from datetime import datetime

# ---------- CONFIG ----------
CSV_PATH       = "work_items.csv"
BRANCH_PREFIX  = "feature/"
BASE_BRANCH    = "develop"          # features branch FROM here
INTEGRATION    = "develop"          # features merge INTO here
MAIN_BRANCH    = "main"             # develop merges INTO here
RELEASE_BRANCH = "release/1.0"      # created from MAIN_BRANCH
EMAIL_DOMAIN   = "example.com"      # <-- CHANGE to your team's real domain
TZ_OFFSET      = "+03:00"
DO_MERGE       = True               # set False to only create feature branches
DRY_RUN        = True

RU_MONTHS = {
    "янв": 1, "фев": 2, "февр": 2, "мар": 3, "апр": 4,
    "май": 5, "мая": 5, "июн": 6, "июл": 7,
    "авг": 8, "сен": 9, "сент": 9, "окт": 10,
    "ноя": 11, "нояб": 11, "дек": 12,
}


def parse_ru_date(s: str) -> datetime:
    parts = s.strip().split()
    if len(parts) != 4:
        raise ValueError(f"unexpected date format: {s!r}")
    day, mon_raw, year, tm = parts
    key = mon_raw.rstrip(".").lower()
    if key not in RU_MONTHS:
        raise ValueError(f"unknown month: {mon_raw!r}")
    hh, mm = map(int, tm.split(":"))
    return datetime(int(year), RU_MONTHS[key], int(day), hh, mm)


def run(cmd, env=None, check=True):
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if check and r.returncode != 0:
        print(f"FAILED: {' '.join(cmd)}", file=sys.stderr)
        print(r.stderr, file=sys.stderr)
        sys.exit(1)
    return r


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + TZ_OFFSET


def main():
    # ---------- Load CSV ----------
    rows = []
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            if not r.get("Issue ID") or not r.get("Author"):
                continue
            try:
                dt = parse_ru_date(r["Date"])
            except Exception as e:
                print(f"  skip: {e}")
                continue
            rows.append({
                "author":  r["Author"].strip(),
                "date":    dt,
                "minutes": int(r.get("Duration") or 0),
                "comment": (r.get("Comment") or "").strip(),
                "issue":   r["Issue ID"].strip(),
                "summary": (r.get("Issue Summary") or "").strip(),
            })

    by_issue = defaultdict(list)
    for row in rows:
        by_issue[row["issue"]].append(row)
    for v in by_issue.values():
        v.sort(key=lambda x: x["date"])
    issues = sorted(by_issue, key=lambda i: by_issue[i][0]["date"])

    # ---------- Verify branches exist ----------
    for b in (BASE_BRANCH, MAIN_BRANCH):
        r = subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{b}"],
                           capture_output=True, text=True)
        if r.returncode != 0:
            sys.exit(f"Branch '{b}' not found locally.")

    orig_branch = run(["git", "rev-parse", "--abbrev-ref", "HEAD"]).stdout.strip()
    print(f"Original branch: {orig_branch}")

    # =================== PHASE 1: feature branches ===================
    print(f"\n=== PHASE 1: create {len(issues)} feature branches from '{BASE_BRANCH}' ===")
    pushed_features = []

    for issue in issues:
        branch = f"{BRANCH_PREFIX}{issue}"
        items = by_issue[issue]
        print(f"\n--- {issue} ({len(items)} work item(s)) -> {branch}")

        run(["git", "checkout", BASE_BRANCH])
        run(["git", "checkout", "-B", branch])

        for row in items:
            email = f"{row['author'].lower().replace(' ', '.')}@{EMAIL_DOMAIN}"
            hours = row["minutes"] / 60.0
            hours_str = f"{int(hours)}h" if hours == int(hours) else f"{hours:.2f}h"
            desc = row["comment"] or row["summary"] or "work item"
            msg = f"{issue}: {desc} ({hours_str})"
            gdate = iso(row["date"])

            env = {
                **os.environ,
                "GIT_AUTHOR_NAME":     row["author"],
                "GIT_AUTHOR_EMAIL":    email,
                "GIT_COMMITTER_NAME":  row["author"],
                "GIT_COMMITTER_EMAIL": email,
                "GIT_AUTHOR_DATE":     gdate,
                "GIT_COMMITTER_DATE":  gdate,
            }

            if DRY_RUN:
                print(f"  [DRY] {gdate} | {row['author']:<10} | {msg}")
            else:
                run(["git", "commit", "--allow-empty", "-m", msg], env=env)
                print(f"  {gdate} | {row['author']:<10} | {msg}")

        if not DRY_RUN:
            r = run(["git", "push", "-u", "--force", "origin", branch], check=False)
            if r.returncode == 0:
                pushed_features.append(branch)
                print(f"  pushed {branch}")
            else:
                print(f"  push failed: {r.stderr.strip()}")

    # =================== PHASE 2: merge features into develop ===================
    if DO_MERGE and not DRY_RUN:
        print(f"\n=== PHASE 2: merge {len(issues)} feature branches into '{INTEGRATION}' ===")

        # Make sure develop is up to date locally
        run(["git", "checkout", INTEGRATION])
        run(["git", "pull", "--ff-only"], check=False)

        for i, issue in enumerate(issues, 1):
            branch = f"{BRANCH_PREFIX}{issue}"
            # Use the last commit's date so history looks chronological
            last_date = iso(by_issue[issue][-1]["date"])
            env = {**os.environ, "GIT_AUTHOR_DATE": last_date, "GIT_COMMITTER_DATE": last_date}
            print(f"  [{i:02d}/{len(issues)}] merging {branch} ({last_date[:10]})")
            run(["git", "merge", "--no-ff", branch,
                 "-m", f"Merge branch '{branch}' into {INTEGRATION}"], env=env)

        run(["git", "push", "-u", "origin", INTEGRATION])

    # =================== PHASE 3: merge develop into main ===================
    if DO_MERGE and not DRY_RUN:
        print(f"\n=== PHASE 3: merge '{INTEGRATION}' into '{MAIN_BRANCH}' ===")

        # Latest work item date = date of the release merge
        latest_date = iso(max(row["date"] for row in rows))

        run(["git", "checkout", MAIN_BRANCH])
        run(["git", "pull", "--ff-only"], check=False)

        env = {**os.environ, "GIT_AUTHOR_DATE": latest_date, "GIT_COMMITTER_DATE": latest_date}
        run(["git", "merge", "--no-ff", INTEGRATION,
             "-m", f"Merge branch '{INTEGRATION}' into {MAIN_BRANCH}"], env=env)
        run(["git", "push", "-u", "origin", MAIN_BRANCH])
        print(f"  merged and pushed '{MAIN_BRANCH}'")

    # =================== PHASE 4: create release from main ===================
    if DO_MERGE and not DRY_RUN:
        print(f"\n=== PHASE 4: create '{RELEASE_BRANCH}' from '{MAIN_BRANCH}' ===")
        run(["git", "checkout", "-B", RELEASE_BRANCH, MAIN_BRANCH])
        run(["git", "push", "-u", "--force", "origin", RELEASE_BRANCH])
        print(f"  created and pushed '{RELEASE_BRANCH}'")

    # ---------- Return to original branch ----------
    run(["git", "checkout", orig_branch])

    print(f"\n=== DONE ===")
    print(f"Issues: {len(issues)} | Work items: {len(rows)} | Feature branches: {len(pushed_features)}")
    if DO_MERGE and not DRY_RUN:
        print(f"Merged into '{INTEGRATION}' -> '{MAIN_BRANCH}' -> '{RELEASE_BRANCH}'")


if __name__ == "__main__":
    main()
