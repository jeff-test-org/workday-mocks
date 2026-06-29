#!/usr/bin/env python3
"""
Parse a Rippling Org Chart PDF (list view) and output Workday-format JSON.

Uses direct Cortex CLI catalog commands (create/archive) instead of workflows.

Usage:
    python rippling_to_workday_cli.py <path-to-pdf>
    python rippling_to_workday_cli.py <path-to-pdf> --push
    python rippling_to_workday_cli.py --sync-employees [<path-to-pdf>] [--dryrun]

The PDF should be printed from https://app.rippling.com/org-chart/chart
using the "Org Chart" (list) view with "Expand All" clicked.

Output is always written to cortex/index.json (relative to this script).
Use --push to also commit and push to git.
Use --sync-employees to compare report against cortex-cx and onboard/archive.
"""

import argparse
import json
import subprocess
import sys
import textwrap
from collections import defaultdict
from datetime import date
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
                        people.append({
                            "name": name,
                            "title": "",
                            "depth": depth,
                        })
                elif 10.5 in sizes and 12.0 in sizes and people:
                    # Mixed row: might have both name and title combined
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
    """Convert flat people list with depth into Workday report format.

    Managers get their own team (named by their role/department).
    ICs share their manager's team.
    parentTeamId always points to the manager's manager's team.
    """
    entries = []
    manager_stack = {}
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

    # Pass 1: identify who is a manager (has anyone at depth+1 after them)
    managers = set()
    for i, person in enumerate(people):
        for j in range(i + 1, len(people)):
            if people[j]["depth"] <= person["depth"]:
                break
            if people[j]["depth"] == person["depth"] + 1:
                managers.add(person["name"])
                break

    # Pass 2: assign teams
    # email -> {teamId, teamName, parentTeamId}
    email_to_team = {}

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
        is_manager = name in managers

        # Find manager: the most recent person at depth - 1
        manager_email = ""
        if depth > 0:
            for d in range(depth - 1, -1, -1):
                if d in manager_stack:
                    manager_email = manager_stack[d]["email"]
                    break
        else:
            manager_email = email

        # Update manager stack
        manager_stack[depth] = {"email": email, "name": name, "title": title}
        for d in list(manager_stack.keys()):
            if d > depth:
                del manager_stack[d]

        if manager_email == email:
            # Top-level / root: create own team
            team_counter += 1
            team_id = f"WORKTEAM-1-{team_counter:03d}"
            dept = title_to_dept(title, name)
            team_name = dept
            parent_team_id = "NONE"
            email_to_team[email] = {
                "teamId": team_id,
                "teamName": team_name,
                "parentTeamId": parent_team_id,
            }
        elif is_manager:
            # This person manages others: they get their own team
            team_counter += 1
            team_id = f"WORKTEAM-1-{team_counter:03d}"
            dept = title_to_dept(title, name)
            # Disambiguate team name with manager's name
            team_name = f"{dept} ({first_name} {last_name})"
            # Parent = the manager's team
            mgr_team = email_to_team.get(manager_email, {})
            parent_team_id = mgr_team.get("teamId", "NONE")
            email_to_team[email] = {
                "teamId": team_id,
                "teamName": team_name,
                "parentTeamId": parent_team_id,
            }
        else:
            # IC: join their manager's team
            if manager_email in email_to_team:
                email_to_team[email] = email_to_team[manager_email]
            else:
                # Manager doesn't have a team yet (shouldn't happen), create one
                team_counter += 1
                team_id = f"WORKTEAM-1-{team_counter:03d}"
                team_name = f"Team {name}"
                parent_team_id = "NONE"
                email_to_team[email] = {
                    "teamId": team_id,
                    "teamName": team_name,
                    "parentTeamId": parent_team_id,
                }

        team_info = email_to_team[email]
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

    return {"Report_Entry": entries}, email_to_team


def build_customer_report(people: list[dict], domain: str = "cortex.io") -> dict:
    """Build the report in the customer's Workday format.

    Uses: Employee_ID, Email, First_Name, Last_Name, Managers_Email,
    and Workteam_Group array with teamName (ID), teamDisplayName, parentTeamId.

    Managers who lead a sub-team get two Workteam_Group entries:
    1. Their manager's (parent) team — as a member
    2. Their own team — with Team_Managed indicating they manage it
    """
    standard_report, email_to_team = build_workday_report(people, domain=domain)

    # Build a lookup from teamId to team info for parent team resolution
    team_id_to_info = {}
    for info in email_to_team.values():
        tid = info["teamId"]
        if tid not in team_id_to_info:
            team_id_to_info[tid] = info

    entries = []
    for entry in standard_report["Report_Entry"]:
        own_team = {
            "teamName": entry["teamId"],
            "teamDisplayName": entry["teamName"],
            "parentTeamId": entry["parentTeamId"],
        }

        # Check if this person manages their own team (has reports)
        parent_id = entry["parentTeamId"]
        mgr_email = entry["managersEmail"]
        mgr_team = email_to_team.get(mgr_email, {})

        # A manager has a different team than their manager, and a valid parent
        is_sub_manager = (
            parent_id != "NONE"
            and mgr_team.get("teamId") == parent_id
            and entry["teamId"] != parent_id
        )

        teams = []
        if is_sub_manager:
            # First entry: member of parent (manager's) team
            parent_info = team_id_to_info.get(parent_id, {})
            teams.append({
                "teamName": parent_id,
                "teamDisplayName": parent_info.get("teamName", parent_id),
                "parentTeamId": parent_info.get("parentTeamId", "NONE"),
            })
            # Second entry: manager of own team
            own_team["Team_Managed"] = entry["teamId"]
            teams.append(own_team)
        else:
            # Top-level manager: mark as managing their own team if they have reports
            if entry["email"] == mgr_email or entry["teamId"] != mgr_team.get("teamId"):
                own_team["Team_Managed"] = entry["teamId"]
            teams.append(own_team)

        customer_entry = {
            "Employee_ID": entry["employeeId"],
            "Email": entry["email"],
            "First_Name": entry["firstName"],
            "Last_Name": entry["lastName"],
            "Managers_Email": entry["managersEmail"],
            "Workteam_Group": teams,
        }
        entries.append(customer_entry)

    return {"Report_Entry": entries}


def title_to_dept(title: str, manager_name: str) -> str:
    """Derive a department/team name from a person's title and their manager."""
    title_lower = title.lower()

    import re
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

    for keyword, dept in mappings:
        if len(keyword) <= 4:
            if re.search(r'\b' + re.escape(keyword) + r'\b', title_lower):
                return dept
        elif keyword in title_lower:
            return dept

    return f"Team {manager_name}"


# Map accented/special characters to ASCII equivalents
_CHAR_MAP = str.maketrans({
    "ł": "l", "ę": "e", "ó": "o", "ą": "a", "ś": "s",
    "ź": "z", "ż": "z", "ń": "n", "ć": "c", "ö": "o",
    "ü": "u", "ä": "a", "é": "e", "è": "e", "ê": "e",
    "á": "a", "à": "a", "í": "i", "ñ": "n", "ø": "o",
})


def _name_to_tag(name: str) -> str:
    """Convert a name to the cortex entity tag format: firstname-lastname."""
    parts = name.strip().split()
    tag = "-".join(p.lower() for p in parts)
    return tag.translate(_CHAR_MAP)


def _get_cortex_employee_tags() -> tuple[set[str], set[str]]:
    """Fetch all employee entity tags from cortex-cx.

    Returns (all_tags, archived_tags) so callers can distinguish active
    from archived entities.
    """
    # Active entities
    r_active = subprocess.run(
        ["cortex", "-t", "cortex-cx", "catalog", "list", "-t", "employee", "-p", "0"],
        capture_output=True, text=True,
    )
    if r_active.returncode != 0:
        print(f"Failed to list employees: {r_active.stderr}", file=sys.stderr)
        sys.exit(1)
    active_tags = {e["tag"].lower() for e in json.loads(r_active.stdout)["entities"]}

    # All entities (including archived)
    r_all = subprocess.run(
        ["cortex", "-t", "cortex-cx", "catalog", "list", "-t", "employee", "-a", "-p", "0"],
        capture_output=True, text=True,
    )
    if r_all.returncode != 0:
        print(f"Failed to list employees: {r_all.stderr}", file=sys.stderr)
        sys.exit(1)
    all_tags = {e["tag"].lower() for e in json.loads(r_all.stdout)["entities"]}

    archived_tags = all_tags - active_tags
    return all_tags, archived_tags


def _get_report_people(pdf_path: str | None, script_dir: Path, domain: str) -> list[dict]:
    """Get people from either a PDF or the existing cortex/index.json in git.

    Returns list of dicts with 'name', 'email', and 'teamId' keys.
    """
    if pdf_path:
        people_raw = extract_people_from_pdf(pdf_path)
        valid_people = [p for p in people_raw if len(p["name"].strip().split()) >= 2]
        # Build the workday report to get team assignments
        report, _ = build_workday_report(valid_people, domain=domain)
        email_to_team = {}
        for entry in report["Report_Entry"]:
            email_to_team[entry["email"]] = entry["teamId"]
        return [
            {
                "name": p["name"],
                "email": infer_email(p["name"], domain),
                "teamId": email_to_team.get(infer_email(p["name"], domain), ""),
            }
            for p in valid_people
        ]
    # Fall back to existing report in git
    report_path = script_dir / "cortex" / "index.json"
    if not report_path.exists():
        print(f"Error: No PDF provided and {report_path} not found.", file=sys.stderr)
        sys.exit(1)
    data = json.loads(report_path.read_text())
    return [
        {
            "name": f"{e['firstName']} {e['lastName']}",
            "email": e["email"],
            "teamId": e.get("teamId", ""),
        }
        for e in data["Report_Entry"]
        if e.get("firstName") and e.get("lastName")  # skip garbage entries
    ]


def sync_employees(pdf_path: str | None, script_dir: Path, domain: str, dryrun: bool, limit: int = 0):
    """Compare Rippling report against cortex-cx employee entities.

    - New employees (in report, not in cortex): create entity via CLI
    - Departed employees (in cortex, not in report): archive via CLI + set offboarded-date
    """
    report_people = _get_report_people(pdf_path, script_dir, domain)
    all_cortex_tags, archived_tags = _get_cortex_employee_tags()

    report_tags = {}
    for person in report_people:
        tag = _name_to_tag(person["name"])
        report_tags[tag] = person

    # New employees: in report but not in cortex (including archived)
    new_tags = set(report_tags.keys()) - all_cortex_tags
    # Departed employees: in cortex and active (not archived), but not in report
    active_tags = all_cortex_tags - archived_tags
    departed_tags = active_tags - set(report_tags.keys())

    # Detect likely tag mismatches: a new tag and departed tag share a last name
    mismatches = []
    unmatched_new = set(new_tags)
    unmatched_departed = set(departed_tags)

    for nt in sorted(new_tags):
        nt_parts = nt.split("-")
        nt_last = nt_parts[-1] if len(nt_parts) > 1 else None
        if not nt_last:
            continue
        for dt in sorted(departed_tags):
            if dt not in unmatched_departed:
                continue
            dt_parts = dt.split("-")
            dt_last = dt_parts[-1] if len(dt_parts) > 1 else None
            if nt_last == dt_last:
                mismatches.append((nt, report_tags[nt]["name"], dt))
                unmatched_new.discard(nt)
                unmatched_departed.discard(dt)
                break

    print(f"\n{'=' * 60}", file=sys.stderr)
    print(f"Employee Sync {'(DRY RUN)' if dryrun else ''}", file=sys.stderr)
    print(f"  Report: {len(report_tags)} employees", file=sys.stderr)
    print(f"  Cortex: {len(active_tags)} active, {len(archived_tags)} archived", file=sys.stderr)
    print(f"{'=' * 60}", file=sys.stderr)

    if mismatches:
        print(f"\nTag mismatches - fix in cortex-cx ({len(mismatches)}):", file=sys.stderr)
        for new_tag, name, old_tag in mismatches:
            print(f"  ! {name}: cortex has '{old_tag}', report generates '{new_tag}'", file=sys.stderr)

    # --- New employees ---
    processed = 0
    truly_new = sorted(unmatched_new)
    if truly_new:
        print(f"\nNew employees ({len(truly_new)}):", file=sys.stderr)
        for tag in truly_new:
            person = report_tags[tag]
            print(f"  + {person['name']} ({person['email']})", file=sys.stderr)
            if not dryrun:
                _create_employee(person["name"], person["email"], person.get("teamId", ""))
                processed += 1
                if limit and processed >= limit:
                    print(f"\n  Limit reached ({limit}). Stopping.", file=sys.stderr)
                    break
    else:
        print("\nNo new employees.", file=sys.stderr)

    # --- Departed employees ---
    truly_departed = sorted(unmatched_departed)
    if truly_departed and not (limit and processed >= limit):
        print(f"\nDeparted employees ({len(truly_departed)}):", file=sys.stderr)
        for tag in truly_departed:
            print(f"  - {tag}", file=sys.stderr)
            if not dryrun:
                _archive_employee(tag)
                processed += 1
                if limit and processed >= limit:
                    print(f"\n  Limit reached ({limit}). Stopping.", file=sys.stderr)
                    break
    elif not truly_departed:
        print("\nNo departed employees.", file=sys.stderr)

    if mismatches and not dryrun:
        print(f"\nSkipped {len(mismatches)} mismatched employees. Fix tags in cortex-cx and re-run.", file=sys.stderr)

    print(file=sys.stderr)


def _create_employee(name: str, email: str, team_id: str):
    """Create a new employee entity via cortex catalog create."""
    tag = _name_to_tag(name)
    today = date.today().isoformat()
    descriptor = textwrap.dedent(f"""\
        openapi: 3.0.1
        info:
          title: {name}
          x-cortex-tag: {tag}
          x-cortex-type: employee
          x-cortex-owners:
          - name: {team_id.lower()}
            type: GROUP
            provider: CORTEX
          - type: EMAIL
            email: {email}
          x-cortex-definition:
            home: ""
            school: ""
            birthplace: ""
          x-cortex-custom-metadata:
            onboarded-date: "{today}"
            home:
              place: ""
            school:
              place: ""
            birthplace:
              place: ""
    """)
    result = subprocess.run(
        ["cortex", "-t", "cortex-cx", "catalog", "create", "-f-"],
        input=descriptor, capture_output=True, text=True,
    )
    if result.returncode != 0:
        error = (result.stderr.strip() or result.stdout.strip())
        print(f"    FAILED to create {name}: {error}", file=sys.stderr)
    else:
        print(f"    Created {name} ({tag})", file=sys.stderr)


def _archive_employee(tag: str):
    """Archive an employee entity and set offboarded-date custom metadata."""
    today = date.today().isoformat()

    # Set offboarded-date custom metadata before archiving
    result = subprocess.run(
        ["cortex", "-t", "cortex-cx", "catalog", "patch", "-f-"],
        input=textwrap.dedent(f"""\
            openapi: 3.0.1
            info:
              title: {tag}
              x-cortex-tag: {tag}
              x-cortex-custom-metadata:
                offboarded-date: "{today}"
        """),
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        error = (result.stderr.strip() or result.stdout.strip())
        print(f"    FAILED to set offboarded-date for {tag}: {error}", file=sys.stderr)

    # Archive the entity
    result = subprocess.run(
        ["cortex", "-t", "cortex-cx", "catalog", "archive", "-t", tag],
        capture_output=True, text=True,
    )
    if result.returncode != 0:
        error = (result.stderr.strip() or result.stdout.strip())
        print(f"    FAILED to archive {tag}: {error}", file=sys.stderr)
    else:
        print(f"    Archived {tag} (offboarded: {today})", file=sys.stderr)


def main():
    parser = argparse.ArgumentParser(
        description="Convert Rippling Org Chart PDF to Workday-format JSON"
    )
    parser.add_argument("pdf_path", nargs="?", help="Path to the Rippling Org Chart PDF")
    parser.add_argument(
        "--domain", "-d",
        help="Email domain (default: cortex.io)",
        default="cortex.io",
    )
    parser.add_argument(
        "--format", "-f",
        choices=["all", "cortex", "customer"],
        default="all",
        help="Output format: 'cortex' (flat fields), 'customer' (Workteam_Group), or 'all' (default: both)",
    )
    parser.add_argument(
        "--push",
        action="store_true",
        help="Commit and push generated files to git",
    )
    parser.add_argument(
        "--sync-employees",
        action="store_true",
        help="Sync employees: create new, archive departed",
    )
    parser.add_argument(
        "--force",
        action="store_true",
        help="Override the 25%% change threshold safety check",
    )
    parser.add_argument(
        "--dryrun",
        action="store_true",
        help="List changes without executing (use with --sync-employees)",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Stop after processing N employees (use with --sync-employees)",
    )
    args = parser.parse_args()

    script_dir = Path(__file__).resolve().parent

    if args.sync_employees:
        pdf_path = str(Path(args.pdf_path)) if args.pdf_path else None
        if pdf_path and not Path(pdf_path).exists():
            print(f"Error: PDF not found at {pdf_path}", file=sys.stderr)
            sys.exit(1)
        sync_employees(pdf_path, script_dir, args.domain, args.dryrun, args.limit)
        # After a real sync, push the JSON files so the workday config
        # picks up team hierarchy/member changes in cortex-cx
        if not args.dryrun:
            output_paths = [
                script_dir / "cortex" / "index.json",
                script_dir / "cortex-team-list" / "index.json",
            ]
            output_paths = [p for p in output_paths if p.exists()]
            if output_paths:
                _git_commit_and_push(script_dir, output_paths)
        return

    if not args.pdf_path:
        parser.error("pdf_path is required unless --sync-employees is used")

    pdf_path = Path(args.pdf_path)
    if not pdf_path.exists():
        print(f"Error: PDF not found at {pdf_path}", file=sys.stderr)
        sys.exit(1)

    print(f"Parsing {pdf_path}...", file=sys.stderr)
    people = extract_people_from_pdf(str(pdf_path))
    print(f"Found {len(people)} employees", file=sys.stderr)

    # Safety check: compare against existing report
    existing_report = script_dir / "cortex" / "index.json"
    if existing_report.exists() and not args.force:
        existing = json.loads(existing_report.read_text())
        prev_count = len(existing.get("Report_Entry", []))
        if prev_count > 0:
            change_pct = abs(len(people) - prev_count) / prev_count
            if change_pct >= 0.25:
                print(
                    f"Error: Employee count changed by {change_pct:.0%} "
                    f"({prev_count} → {len(people)}). "
                    f"Use --force to override.",
                    file=sys.stderr,
                )
                sys.exit(1)

    output_paths = []

    if args.format in ("all", "cortex"):
        report, _ = build_workday_report(people, domain=args.domain)
        out = script_dir / "cortex" / "index.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n")
        output_paths.append(out)
        print(f"Written cortex format to {out}", file=sys.stderr)

    if args.format in ("all", "customer"):
        report = build_customer_report(people, domain=args.domain)
        out = script_dir / "cortex-team-list" / "index.json"
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(report, indent=2) + "\n")
        output_paths.append(out)
        print(f"Written customer format to {out}", file=sys.stderr)

    if args.push and output_paths:
        _git_commit_and_push(script_dir, output_paths)


def _git_commit_and_push(repo_dir: Path, file_paths: list[Path]):
    """Commit and push the updated files."""
    def run(cmd: list[str]) -> subprocess.CompletedProcess:
        return subprocess.run(
            cmd, cwd=repo_dir, capture_output=True, text=True
        )

    for fp in file_paths:
        result = run(["git", "add", str(fp.relative_to(repo_dir))])
        if result.returncode != 0:
            print(f"git add failed: {result.stderr}", file=sys.stderr)
            sys.exit(1)

    result = run(["git", "diff", "--cached", "--quiet"])
    if result.returncode == 0:
        print("No changes to commit.", file=sys.stderr)
        return

    from datetime import datetime
    timestamp = datetime.now().strftime("%Y-%m-%d %H:%M")
    msg = f"Update Cortex employee report ({timestamp})"
    result = run(["git", "commit", "-m", msg])
    if result.returncode != 0:
        print(f"git commit failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    print(f"Committed: {msg}", file=sys.stderr)

    result = run(["git", "push"])
    if result.returncode != 0:
        print(f"git push failed: {result.stderr}", file=sys.stderr)
        sys.exit(1)
    print("Pushed to remote.", file=sys.stderr)


if __name__ == "__main__":
    main()
