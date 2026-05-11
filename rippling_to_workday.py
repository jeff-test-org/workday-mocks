#!/usr/bin/env python3
"""
Parse a Rippling Org Chart PDF (list view) and output Workday-format JSON.

Usage:
    python rippling_to_workday.py <path-to-pdf>
    python rippling_to_workday.py <path-to-pdf> --push

The PDF should be printed from https://app.rippling.com/org-chart/chart
using the "Org Chart" (list) view with "Expand All" clicked.

Output is always written to cortex/index.json (relative to this script).
Use --push to also commit and push to git.
"""

import argparse
import json
import subprocess
import sys
from collections import defaultdict
from pathlib import Path

try:
    import pdfplumber
except ImportError:
    print("Error: pdfplumber is required. Install with: pip install pdfplumber", file=sys.stderr)
    sys.exit(1)


# Unicode ligature and special character normalization
LIGATURE_MAP = {
    "\ufb01": "fi",  # ﬁ ligature
    "\ufb02": "fl",  # ﬂ ligature
    "\ufb00": "ff",
    "\ufb03": "ffi",
    "\ufb04": "ffl",
}


def _normalize_text(text: str) -> str:
    """Normalize unicode ligatures and special characters."""
    for lig, replacement in LIGATURE_MAP.items():
        text = text.replace(lig, replacement)
    return text


# Known indentation x-positions mapped to hierarchy depth
# These come from the Rippling org chart PDF layout
INDENT_THRESHOLDS = [55, 105, 136, 166, 197, 228, 259, 290]


def x_to_depth(x: float) -> int:
    """Convert an x-position to a hierarchy depth level."""
    rounded = round(x)
    for i, threshold in enumerate(INDENT_THRESHOLDS):
        if abs(rounded - threshold) <= 5:
            return i
    # Fallback: estimate depth from x position
    return max(0, round((x - 55) / 31))


def extract_people_from_pdf(pdf_path: str) -> list[dict]:
    """Extract people (name, title, depth) from the Rippling org chart PDF."""
    people = []

    skip_keywords = [
        "First name", "Expand", "Collapse", "Direct", "Reports",
        "Org Chart", "Help docs", "Filters", "Last update",
        "Org Diagram", "Total Team", "Size",
    ]

    with pdfplumber.open(pdf_path) as pdf:
        for page in pdf.pages:
            # Group characters by y-position (row)
            rows = defaultdict(list)
            for c in page.chars:
                y_key = round(c["top"] / 2) * 2
                rows[y_key].append(c)

            for y_key in sorted(rows.keys()):
                row_chars = sorted(rows[y_key], key=lambda c: c["x0"])
                text_chars = [c for c in row_chars if c["text"].strip()]
                if not text_chars:
                    continue

                sizes = {round(c["size"], 1) for c in text_chars}
                text = "".join(c["text"] for c in row_chars).strip()

                # Normalize unicode ligatures and special chars
                text = _normalize_text(text)

                # Skip header/UI rows
                if any(kw in text for kw in skip_keywords):
                    continue

                # Skip number-only rows (direct reports / team size columns)
                if all(c in "0123456789 " for c in text):
                    continue

                # Skip rows that are just unicode icons (Rippling UI elements)
                if all(ord(c) > 0xE000 or c.isspace() for c in text):
                    continue

                # Name rows: size 12, no 10.5
                # Title rows: size 10.5
                if 12.0 in sizes and 10.5 not in sizes:
                    # Find the first printable ASCII char for x-position
                    ascii_chars = [c for c in text_chars
                                   if ord(c["text"]) < 0xE000 and c["text"].strip()]
                    if not ascii_chars:
                        continue
                    x = ascii_chars[0]["x0"]
                    depth = x_to_depth(x)
                    # Filter out the "-" expand/collapse indicators
                    name = text.lstrip("- ").strip()
                    # Remove any remaining unicode private-use chars
                    name = "".join(c for c in name if ord(c) < 0xE000 or ord(c) > 0xF8FF).strip()
                    if name and len(name) > 1:
                        # If the previous person has no title and this is on a new page,
                        # check if title might have been on the previous page
                        people.append({
                            "name": name,
                            "title": "",
                            "depth": depth,
                        })
                elif 10.5 in sizes and 12.0 in sizes and people:
                    # Mixed row: might have both name and title combined
                    # Try to attach as title to most recent person
                    if not people[-1]["title"]:
                        people[-1]["title"] = text
                elif 10.5 in sizes and people:
                    # Title row - attach to the most recent person
                    people[-1]["title"] = _normalize_text(text)

    return people


def infer_email(name: str, domain: str = "cortex.io") -> str:
    """Infer email from name as firstname.lastname@domain."""
    parts = name.strip().split()
    if len(parts) < 2:
        return f"{parts[0].lower()}@{domain}"
    first = parts[0].lower()
    last = parts[-1].lower()
    # Handle special characters
    for char in ["ł", "ę", "ó", "ą", "ś", "ź", "ż", "ń", "ć"]:
        first = first.replace(char, char)  # keep as-is for now
        last = last.replace(char, char)
    return f"{first}.{last}@{domain}"


# Known titles for people whose titles may be split across PDF pages
# or otherwise missing from extraction. Sourced from Cortex/Rippling data.
KNOWN_TITLES = {
    "Taylor Schmidt": "Director of Customer Education & Delivery",
    "Josh Somerville": "Director of Customer Success",
    "Bradley Sauln": "Director, Sales Engineering",
    "Matt McGonegle": "Director of Revenue Operations",
    "Stephanie Cantaley": "Manager, Customer Engineering",
    "Roshni Sondhi": "VP, Customer Experience",
    "Michael Connell": "VP, Sales",
    "Cristina Buenahora Bustamante": "VP, Strategic Initiatives",
    "Kenji Porter": "Head of People & Talent",
    "Evan Pincus": "Finance Advisor",
    "Kara Gillis": "VP of Product",
    "Ganesh Datta": "CTO",
    "Anish Dhar": "CEO",
}


def build_workday_report(people: list[dict], domain: str = "cortex.io") -> dict:
    """Convert flat people list with depth into Workday report format."""
    entries = []
    # Track the manager stack: depth -> person info
    manager_stack = {}
    # Track teams: manager_email -> team info
    teams = {}
    team_counter = 0

    # Fill in missing titles from known data
    for person in people:
        if not person["title"] and person["name"] in KNOWN_TITLES:
            person["title"] = KNOWN_TITLES[person["name"]]

    # Normalize depths: find the minimum depth and shift everything down
    if people:
        min_depth = min(p["depth"] for p in people)
        if min_depth > 0:
            for p in people:
                p["depth"] -= min_depth

    # Index people by email so we can look up manager titles
    people_by_email = {}

    for person in people:
        depth = person["depth"]
        name = person["name"]
        title = person["title"]
        parts = name.split()

        if len(parts) < 2:
            first_name = parts[0] if parts else name
            last_name = ""
        else:
            first_name = parts[0]
            last_name = " ".join(parts[1:])

        email = infer_email(name, domain)
        people_by_email[email] = {"name": name, "title": title}

        # Find manager: the most recent person at depth - 1
        manager_email = ""
        if depth > 0:
            for d in range(depth - 1, -1, -1):
                if d in manager_stack:
                    manager_email = manager_stack[d]["email"]
                    break
        else:
            # Top-level: reports to self (Workday convention)
            manager_email = email

        # Update manager stack
        manager_stack[depth] = {"email": email, "name": name, "title": title}
        # Clear deeper levels (they're no longer current ancestors)
        for d in list(manager_stack.keys()):
            if d > depth:
                del manager_stack[d]

        # Determine team: each manager gets a team, named by their title/role
        if manager_email == email:
            # Top-level: create their own team
            team_counter += 1
            team_id = f"WORKTEAM-1-{team_counter:03d}"
            team_name = f"CX: {title_to_dept(title, name)}"
            parent_team_id = "NONE"
            teams[email] = {
                "teamId": team_id,
                "teamName": team_name,
                "parentTeamId": parent_team_id,
            }
        elif manager_email not in teams:
            # Manager doesn't have a team yet, create one based on MANAGER's title
            team_counter += 1
            team_id = f"WORKTEAM-1-{team_counter:03d}"

            # Find manager's manager's team for parentTeamId
            parent_team_id = "NONE"
            if depth >= 2:
                for d in range(depth - 2, -1, -1):
                    if d in manager_stack and manager_stack[d]["email"] in teams:
                        parent_team_id = teams[manager_stack[d]["email"]]["teamId"]
                        break

            # Use the MANAGER's title to name the team
            mgr_info = people_by_email.get(manager_email, {})
            mgr_title = mgr_info.get("title", "")
            mgr_name = mgr_info.get("name", "")
            team_name = f"CX: {title_to_dept(mgr_title, mgr_name)}"
            teams[manager_email] = {
                "teamId": team_id,
                "teamName": team_name,
                "parentTeamId": parent_team_id,
            }

        # Get team info
        if manager_email in teams:
            team_info = teams[manager_email]
        else:
            team_info = {"teamId": "UNKNOWN", "teamName": "Unknown", "parentTeamId": "NONE"}

        employee_id = str(100000 + len(entries))

        entries.append({
            "email": email,
            "employeeId": employee_id,
            "firstName": first_name,
            "lastName": last_name,
            "managersEmail": manager_email,
            "teamId": team_info["teamId"],
            "teamName": team_info["teamName"],
            "parentTeamId": team_info["parentTeamId"],
        })

    return {"Report_Entry": entries}


def title_to_dept(title: str, manager_name: str) -> str:
    """Derive a department/team name from a person's title and their manager."""
    title_lower = title.lower()

    # Ordered list of mappings - more specific matches first
    mappings = [
        ("sales engineer", "Sales Engineering"),
        ("sales development", "Sales Development"),
        ("account executive", "Sales"),
        ("vp, sales", "Sales"),
        ("vp sales", "Sales"),
        ("product designer", "Design"),
        ("design engineer", "Design"),
        ("product marketing", "Product Marketing"),
        ("product manager", "Product"),
        ("vp of product", "Product"),
        ("vp, product", "Product"),
        ("customer success", "Customer Success"),
        ("customer engineer", "Customer Engineering"),
        ("customer education", "Customer Education"),
        ("customer experience", "Customer Experience"),
        ("knowledge strategist", "Customer Education"),
        ("solution architect", "Solutions"),
        ("engineering manager", "Engineering"),
        ("software engineer", "Engineering"),
        ("frontend engineer", "Engineering"),
        ("backend engineer", "Engineering"),
        ("platform engineer", "Engineering"),
        ("distinguished engineer", "Engineering"),
        ("chief architect", "Architecture"),
        ("cto", "Engineering"),
        ("ceo", "Executive"),
        ("vp, strategic", "Strategic Initiatives"),
        ("strategic initiatives", "Strategic Initiatives"),
        ("management associate", "Strategic Initiatives"),
        ("revenue operation", "Revenue Operations"),
        ("director of revenue", "Revenue Operations"),
        ("gtm operation", "Revenue Operations"),
        ("gtm engineer", "Revenue Operations"),
        ("business development", "Business Development"),
        ("bdr", "Business Development"),
        ("demand generation", "Demand Generation"),
        ("financial controller", "Finance"),
        ("finance", "Finance"),
        ("recruiter", "People & Talent"),
        ("people", "People & Talent"),
        ("talent", "People & Talent"),
        ("marketing", "Marketing"),
        ("content writer", "Content & Marketing"),
        ("data science", "Data Science"),
        ("information technology", "IT"),
        ("recruiting", "Recruiting"),
    ]

    import re
    for keyword, dept in mappings:
        # Use word boundary for short keywords to avoid false matches
        # (e.g., "cto" matching inside "director")
        if len(keyword) <= 4:
            if re.search(r'\b' + re.escape(keyword) + r'\b', title_lower):
                return dept
        elif keyword in title_lower:
            return dept

    # Fallback to manager name
    return f"Team {manager_name}"


def main():
    parser = argparse.ArgumentParser(
        description="Convert Rippling Org Chart PDF to Workday-format JSON"
    )
    parser.add_argument("pdf_path", help="Path to the Rippling Org Chart PDF")
    parser.add_argument(
        "--domain", "-d",
        help="Email domain (default: cortex.io)",
        default="cortex.io",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Commit and push cortex/index.json to git after generating",
    )
    args = parser.parse_args()

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        print(f"Error: PDF not found at {pdf_path}", file=sys.stderr)
        sys.exit(1)

    # Output always goes to cortex/index.json relative to this script
    script_dir = Path(__file__).resolve().parent
    output_path = script_dir / "cortex" / "index.json"
    output_path.parent.mkdir(parents=True, exist_ok=True)

    print(f"Parsing {pdf_path}...", file=sys.stderr)
    people = extract_people_from_pdf(str(pdf_path))
    print(f"Found {len(people)} employees", file=sys.stderr)

    report = build_workday_report(people, domain=args.domain)

    output = json.dumps(report, indent=2) + "\n"
    output_path.write_text(output)
    print(f"Written to {output_path}", file=sys.stderr)

    if args.push:
        _git_commit_and_push(script_dir, output_path)


def _git_commit_and_push(repo_dir: Path, file_path: Path):
    """Commit and push the updated index.json."""
    def run(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, cwd=repo_dir, capture_output=True, text=True
        )

    # Stage the file
    result = run(["git", "add", str(file_path.relative_to(repo_dir))])
    if result.returncode != 0:
        print(f"git add failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)

    # Check if there are staged changes
    result = run(["git", "diff", "--cached", "--quiet"])
    if result.returncode == 0:
        print("No changes to commit.", file=sys.stderr)
        return

    # Commit
    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    msg = f"Update Cortex employee report ({timestamp})"
    result = run(["git", "commit", "-m", msg])
    if result.returncode != 0:
        print(f"git commit failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    print(f"Committed: {msg}", file=sys.stderr)

    # Push
    result = run(["git", "push"])
    if result.returncode != 0:
        print(f"git push failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    print("Pushed to remote.", file=sys.stderr)


if __name__ == "__main__":
    main()
