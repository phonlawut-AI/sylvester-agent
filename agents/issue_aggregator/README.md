# Issue Aggregator Agent

Reads all daily plan logs and produces a ranked technician repair priority list.

## Responsibility

This agent owns **one task**: turn raw issue flags in the JSON logs into an
actionable repair priority summary. It does not send messages, parse PDFs, or
write to the log — it only reads.

## Input

`data/logs/daily_plans/{YYYY-MM-DD}.json` — written by the orchestrator and
updated by the webhook when staff tap Issue buttons.

A machine is included if any of these are true:
- `status == "issue"`
- `issue == true`
- `issue_note` is non-empty

## Output

```
🔧 Technician Repair Priority
(last 30 day(s))

1. #210364 · 3 issues
Location: Cabrini Malvern ICU
Priority: High
Issues:
- Brew head needs replacing
- Canister P2 empty
- Calibrate coffee grinder

2. #210300 · 1 issue
Location: Southern Cross Station
Priority: Medium
Issues:
- Replace air filter
```

## Priority scoring

| Factor | Points |
|---|---|
| Each issue report | +2 |
| Issue within last 7 days | +1 per report |
| Keyword: `not dispensing`, `no coffee`, `leak`, `payment` | +3 |
| Keyword: `broken`, `blocked`, `brew head` | +2 |
| Keyword: `air filter`, `calibrate`, `canister` | +1 |

**Thresholds:** High ≥ 5 · Medium ≥ 3 · Low < 3

## Usage

```python
from agents.issue_aggregator.issue_aggregator import generate_repair_priority_summary

# Last 30 days (default)
summary = generate_repair_priority_summary()

# Custom window
summary = generate_repair_priority_summary(days=7)

# All time
summary = generate_repair_priority_summary(days=0)
```

CLI:
```bash
python -m agents.issue_aggregator.issue_aggregator        # last 30 days
python -m agents.issue_aggregator.issue_aggregator 7      # last 7 days
```

## Extending

- **Connect to Google Sheets**: replace `_collect_issues()` with a Sheets reader
  and keep all scoring/formatting logic unchanged.
- **Add new keywords**: append to `CRITICAL_KEYWORDS` in `issue_aggregator.py`.
- **Change thresholds**: edit `_priority_label()`.
- **Send via LINE**: call `generate_repair_priority_summary()` from the
  orchestrator and pass the result to `line_communicator.sender.send_text()`.
