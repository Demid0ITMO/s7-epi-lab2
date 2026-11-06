#!/usr/bin/env python3
"""
Event-driven simulation:
  - Process work items in chronological order.
  - First work item for an issue  -> create branch feature/<ID> from current develop.
  - Every work item               -> commit on that branch with correct author/date.
  - Last work item for an issue   -> merge branch into develop (with that date).
  - Finally: merge develop into main, then reset release/1.0 to main.

This makes each feature branch parent point = develop's state at the moment
work started on that issue — i.e. branches naturally appear in time order.
"""

import csv
import os
import subprocess
import sys
from datetime import datetime

# ---------- CONFIG ----------
CSV_PATH       = "work_items.csv"
BRANCH_PREFIX  = "feature/"
BASE_BRANCH    = "develop"
MAIN_BRANCH    = "main"
RELEASE_BRANCH = "release/1.0"
EMAIL_DOMAIN   = "example.com"      # <-- CHANGE to your real domain
TZ_OFFSET      = "+03:00"
DRY_RUN        = False
VERBOSE        = False              # set True to see each git invocation

RU_MONTHS = {
    "янв": 1, "фев": 2, "февр": 2, "мар": 3, "апр": 4,
    "май": 5, "мая": 5, "июн": 6, "июл": 7,
    "авг": 8, "сен": 9, "сент": 9, "окт": 10,
    "ноя": 11, "нояб": 11, "дек": 12,
}


def parse_ru_date(s: str) -> datetime:
    day, mon_raw, year, tm = s.strip().split()
    key = mon_raw.rstrip(".").lower()
    if key not in RU_MONTHS:
        raise ValueError(f"unknown month: {mon_raw!r}")
    hh, mm = map(int, tm.split(":"))
    return datetime(int(year), RU_MONTHS[key], int(day), hh, mm)


def run(cmd, env=None, check=True, show=None):
    if show is None:
        show = VERBOSE
    if show:
        print(f"    $ {' '.join(cmd)}")
    r = subprocess.run(cmd, capture_output=True, text=True, env=env)
    if show and r.stdout.strip():
        for line in r.stdout.strip().splitlines():
            print(f"      {line}")
    if check and r.returncode != 0:
        print(f"FAILED: {' '.join(cmd)}", file=sys.stderr)
        print(r.stderr, file=sys.stderr)
        sys.exit(1)
    return r


def rev_parse(ref: str) -> str:
    return subprocess.check_output(["git", "rev-parse", ref], text=True).strip()


def iso(dt: datetime) -> str:
    return dt.strftime("%Y-%m-%dT%H:%M:%S") + TZ_OFFSET


def force_merge_commit(branch: str, message: str, env: dict) -> None:
    """Merge `branch` into HEAD; force a merge commit if merge is a no-op."""
    before = rev_parse("HEAD")
    r = subprocess.run(
        ["git", "merge", "--no-ff", "--no-edit", "-m", message, branch],
        capture_output=True, text=True, env=env,
    )
    if rev_parse("HEAD") != before:
        return
    # Trees identical → merge was a no-op. Force commit-tree with 2 parents.
    tree   = rev_parse(f"{branch}^{{tree}}")
    target = rev_parse(branch)
    sha = subprocess.check_output(
        ["git", "commit-tree", tree, "-p", before, "-p", target, "-m", message],
        env=env, text=True,
    ).strip()
    run(["git", "reset", "--hard", sha])


def main():
    # ---------- Load ----------
    rows = []
    with open(CSV_PATH, encoding="utf-8-sig", newline="") as f:
        for idx, r in enumerate(csv.DictReader(f)):
            if not r.get("Issue ID") or not r.get("Author"):
                continue
            try:
                dt = parse_ru_date(r["Date"])
            except Exception as e:
                print(f"  skip row {idx}: {e}")
                continue
            rows.append({
                "author":  r["Author"].strip(),
                "date":    dt,
                "minutes": int(r.get("Duration") or 0),
                "comment": (r.get("Comment") or "").strip(),
                "issue":   r["Issue ID"].strip(),
                "summary": (r.get("Issue Summary") or "").strip(),
                "row":     idx,
            })

    # Chronological events. Tie-break by issue id, then CSV row order.
    events = sorted(rows, key=lambda e: (e["date"], e["issue"], e["row"]))

    first_idx, last_idx = {}, {}
    for i, ev in enumerate(events):
        first_idx.setdefault(ev["issue"], i)
        last_idx[ev["issue"]] = i

    n_issues = len(first_idx)
    print(f"Loaded {len(events)} work items across {n_issues} issues")
    print(f"Timeline: {events[0]['date'].date()} → {events[-1]['date'].date()}\n")

    # ---------- Verify branches ----------
    for b in (BASE_BRANCH, MAIN_BRANCH):
        if subprocess.run(["git", "rev-parse", "--verify", "--quiet", f"refs/heads/{b}"],
                          capture_output=True).returncode != 0:
            sys.exit(f"Branch '{b}' not found.")

    if subprocess.check_output(["git", "status", "--porcelain"], text=True).strip():
        sys.exit("Working directory is dirty. Commit/stash changes first.")

    orig = subprocess.check_output(["git", "rev-parse", "--abbrev-ref", "HEAD"],
                                   text=True).strip()

    branches_created = []

    # ---------- EVENT LOOP ----------
    for i, ev in enumerate(events):
        issue   = ev["issue"]
        branch  = f"{BRANCH_PREFIX}{issue}"
        is_first = first_idx[issue] == i
        is_last  = last_idx[issue]  == i
        date_s  = ev["date"].strftime("%Y-%m-%d")
        hours   = ev["minutes"] / 60.0
        h_str   = f"{int(hours)}h" if hours == int(hours) else f"{hours:.2f}h"

        flags = ("[create]" if is_first else "") + (" [close]" if is_last else "")
        print(f"[{i+1:02d}/{len(events)}] {date_s} | {issue:<7} | {ev['author']:<10} | {h_str:<6} {flags}")

        if is_first:
            if not DRY_RUN:
                run(["git", "checkout", BASE_BRANCH])
                run(["git", "checkout", "-B", branch])
                branches_created.append(branch)
        else:
            if not DRY_RUN:
                run(["git", "checkout", branch])

        email = f"{ev['author'].lower().replace(' ', '.')}@{EMAIL_DOMAIN}"
        desc  = ev["comment"] or ev["summary"] or "work item"
        msg   = f"{issue}: {desc} ({h_str})"
        gdate = iso(ev["date"])
        env = {
            **os.environ,
            "GIT_AUTHOR_NAME":     ev["author"],
            "GIT_AUTHOR_EMAIL":    email,
            "GIT_COMMITTER_NAME":  ev["author"],
            "GIT_COMMITTER_EMAIL": email,
            "GIT_AUTHOR_DATE":     gdate,
            "GIT_COMMITTER_DATE":  gdate,
        }

        if DRY_RUN:
            print(f"           → commit: {msg}")
        else:
            run(["git", "commit", "--allow-empty", "-m", msg], env=env)
            print(f"           ✓ {msg}")

        if is_last:
            merge_msg = f"Merge branch '{branch}' into {BASE_BRANCH}"
            if DRY_RUN:
                print(f"           → merge: {merge_msg}")
            else:
                run(["git", "checkout", BASE_BRANCH])
                force_merge_commit(branch, merge_msg, env)
                print(f"           ✓ merged into {BASE_BRANCH}")

    # ---------- FINAL ----------
    if not DRY_RUN:
        print(f"\n=== FINAL: {BASE_BRANCH} → {MAIN_BRANCH} → {RELEASE_BRANCH} ===")
        latest = iso(events[-1]["date"])
        merge_env = {**os.environ,
                     "GIT_AUTHOR_DATE": latest, "GIT_COMMITTER_DATE": latest}

        run(["git", "checkout", MAIN_BRANCH])
        force_merge_commit(
            BASE_BRANCH,
            f"Merge branch '{BASE_BRANCH}' into {MAIN_BRANCH}",
            merge_env,
        )
        print(f"    ✓ {BASE_BRANCH} merged into {MAIN_BRANCH}")

        run(["git", "checkout", "-B", RELEASE_BRANCH, MAIN_BRANCH])
        print(f"    ✓ {RELEASE_BRANCH} points at {MAIN_BRANCH}")

        print(f"\n=== PUSH ===")
        for b in branches_created + [BASE_BRANCH, MAIN_BRANCH, RELEASE_BRANCH]:
            run(["git", "push", "-u", "--force", "origin", b])
            print(f"    ✓ pushed {b}")

    run(["git", "checkout", orig])

    print(f"\n=== DONE ===")
    print(f"Issues: {n_issues} | Work items: {len(events)} | Feature branches: {len(branches_created)}\n")
    print("Verify:")
    print("  git log --graph --all --date-order --pretty=format:'%h %ad %an %s' --date=short | head -100")
    print(f"  git rev-parse {MAIN_BRANCH} {RELEASE_BRANCH}   # same SHA")


if __name__ == "__main__":
    main()