# Workday Mocks

## Project Overview

Mock Workday employee reports generated from Rippling org chart PDFs. Hosted as static JSON and served via GitHub Pages for Cortex integrations.

## Key Script: rippling_to_workday.py

### Usage

```bash
# Generate Workday JSON from Rippling org chart PDF
python rippling_to_workday.py <path-to-pdf>

# Generate + commit + push
python rippling_to_workday.py <path-to-pdf> --push

# Sync employees: compare report against cortex-cx entities (dry run)
python rippling_to_workday.py --sync-employees --dryrun

# Sync with a new PDF
python rippling_to_workday.py --sync-employees <path-to-pdf> --dryrun

# Sync for real (onboards new, archives departed)
python rippling_to_workday.py --sync-employees

# Limit processing (useful for testing)
python rippling_to_workday.py --sync-employees --limit 5
```

### How it works

1. Parses Rippling org chart PDF (list view, "Expand All") using pdfplumber
2. Extracts names, titles, and hierarchy depth from character positions
3. Builds Workday-format JSON with team assignments and manager relationships
4. Outputs to `cortex/index.json` (flat format) and `cortex-team-list/index.json` (Workteam_Group format)
5. `--sync-employees` compares report against cortex-cx catalog, runs onboarding/offboarding workflows

### Safety checks

- 25% employee count change threshold (override with `--force`)
- `--dryrun` mode for sync operations
- Tag mismatch detection (name changes between Rippling and Cortex)

## Future: Automating Rippling PDF Download with Playwright

### Context

The Rippling org chart PDF is currently downloaded manually. There's an existing Playwright setup in `~/git/cli/internal` that could be extended.

### Existing infrastructure (~/git/cli/internal)

- Playwright sync API with headed Chromium, anti-detection flags, WebAuthn/passkey suppression
- Google OAuth + TOTP MFA login flow (Auth0 -> Google -> pyotp)
- Session state caching (`ui/state.json`) for cookie persistence across runs
- Page Object Model pattern in `ui/pages/`
- Workday integration automation already built

### Proposed automation flow

1. Login to Rippling (SSO via Google or Rippling-native credentials)
2. Navigate to `https://app.rippling.com/org-chart/chart`
3. Switch to "List" view if not default
4. Click "Expand All"
5. Wait for full render
6. Save as PDF

### Implementation notes

- **Rippling login**: Medium effort. If SSO via Google, can reuse existing Google OAuth flow. MFA adds complexity.
- **Navigation + expand**: Easy-medium. Rippling is React-based; need stable selectors and `wait_for_selector`.
- **PDF generation**: `page.pdf()` only works in headless Chromium. For headed mode, use CDP directly:
  ```python
  client = page.context.new_cdp_session(page)
  result = client.send("Page.printToPDF", {...})
  ```
- **Estimated effort**: ~30-45 min of Claude time. Google OAuth/TOTP flow already exists in cli/internal. Main unknown is Rippling's DOM selectors (list view toggle, expand all button) which require live inspection.

### Dependencies

- playwright
- pyotp (for TOTP MFA)
- pdfplumber (for parsing the downloaded PDF)
