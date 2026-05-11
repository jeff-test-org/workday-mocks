#!/usr/bin/env bash
#
# Deletes all Workday-managed teams from Cortex.
# Uses the Cortex Catalog API to find teams with provider: WORKDAY and deletes them.
#
# Usage:
#   delete-workday-teams            # dry run (default) — lists Workday teams
#   delete-workday-teams --dry-run  # same as above, explicit dry run
#   delete-workday-teams --confirm  # actually delete all Workday teams
#
# Requires CORTEX_API_KEY environment variable to be set.
# Optionally set CORTEX_BASE_URL (defaults to https://api.getcortexapp.com).

set -euo pipefail

CORTEX_BASE_URL="${CORTEX_BASE_URL:-https://api.getcortexapp.com}"
PAGE_SIZE=250
DRY_RUN=true

case "${1:-}" in
  --confirm) DRY_RUN=false ;;
  --dry-run|"") DRY_RUN=true ;;
  *) echo "Usage: delete-workday-teams [--dry-run | --confirm]" >&2; exit 1 ;;
esac

if [[ -z "${CORTEX_API_KEY:-}" ]]; then
  echo "Error: CORTEX_API_KEY environment variable is not set." >&2
  exit 1
fi

auth_header="Authorization: Bearer ${CORTEX_API_KEY}"

# Collect all Workday team tags across paginated results
workday_tags=()
page=0

while true; do
  response=$(curl -s -X GET \
    "${CORTEX_BASE_URL}/api/v1/catalog/descriptors?types=team&yaml=true&pageSize=${PAGE_SIZE}&page=${page}" \
    -H "${auth_header}" \
    -H "Accept: application/json")

  # Extract total pages from first request
  total_pages=$(echo "$response" | jq -r '.totalPages // 0')

  # Parse each descriptor YAML string looking for provider: WORKDAY
  # The .descriptors[] array contains raw YAML strings (not objects)
  tags_on_page=$(echo "$response" | jq -r '
    .descriptors[]
    | select(test("provider:\\s*WORKDAY"))
    | capture("x-cortex-tag:\\s*(?<tag>\\S+)")
    | .tag
  ')

  while IFS= read -r tag; do
    [[ -n "$tag" ]] && workday_tags+=("$tag")
  done <<< "$tags_on_page"

  page=$((page + 1))
  if [[ $page -ge $total_pages ]]; then
    break
  fi
done

if [[ ${#workday_tags[@]} -eq 0 ]]; then
  echo "No Workday-managed teams found."
  exit 0
fi

echo "Found ${#workday_tags[@]} Workday-managed team(s):"
printf "  - %s\n" "${workday_tags[@]}"

if $DRY_RUN; then
  echo ""
  echo "Dry run — no teams deleted. Re-run with --confirm to delete."
  exit 0
fi

echo ""
echo "Deleting..."

deleted=0
failed=0
for tag in "${workday_tags[@]}"; do
  http_code=$(curl -s -o /dev/null -w "%{http_code}" -X DELETE \
    "${CORTEX_BASE_URL}/api/v1/catalog/${tag}" \
    -H "${auth_header}")

  if [[ "$http_code" == "200" || "$http_code" == "204" ]]; then
    echo "  Deleted: $tag"
    deleted=$((deleted + 1))
  else
    echo "  FAILED ($http_code): $tag" >&2
    failed=$((failed + 1))
  fi
done

echo ""
echo "Done. Deleted: $deleted, Failed: $failed"
