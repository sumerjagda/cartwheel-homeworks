# Cartwheel Post-Deployment Monitoring (Homework 7)

## Monitored Configuration

- **Failure mode:** `raw_schema_or_planning_leak`
- **Frozen Homework 5 judge:** `raw_schema_or_planning_leak-v1` on `gemini/gemini-3.1-flash-lite` (held-out test split `n = 47`: failure sensitivity `1.0000` [15/15], pass specificity `1.0000` [32/32])
- **Cartwheel agent model:** `gemini/gemini-3.5-flash-lite`
- **Shared scenario set:** `scenarios/monitoring_scenarios.jsonl` (50 hold-out scenarios from Homework 3; 61 completed traces per period)
- **Sampling plan (`seed = 7`):** `20%` uniform random sample (`n = 10` conversations per period) kept strictly separate from pre-registered risk group `write_action` (`issue_refund` / `cancel_order`, `17` conversations per period; `3` overlapping, giving `24` unique judge calls per period)
- **Pre-registered alert threshold:** `0.15` (15% corrected failure prevalence)
- **Periods compared:**
  - `before` (`2026-11-22T18:05:00Z` to `2026-11-22T18:07:00Z`): original Homework 3 baseline traces for the 50 monitoring scenarios imported into Langfuse Cloud with a uniform +28-day offset (from `2026-10-25`, which was 31 days old and dropped by Langfuse Cloud Hobby ingestion), preserving exact original trace IDs, span payloads, and relative microsecond ordering.
  - `after` (`2026-11-25T18:48:00Z` to `2026-11-25T19:03:00Z`): fresh run of the same 50 scenarios against a reset database (`scenarios/hw7-after-results.jsonl`).

| Period | Langfuse Traces | Conversations | Random Flagged | `write_action` Flagged | Raw Rate | Corrected Rate | 95% Bootstrap CI |
| :--- | ---: | ---: | ---: | ---: | ---: | ---: | :--- |
| `before` | 61 | 50 | `0/10` | `0/17` | `0.0000` | `0.0000` | `0.0000 - 0.0000` |
| `after` | 61 | 50 | `0/10` | `0/17` | `0.0000` | `0.0000` | `0.0000 - 0.0000` |

## Interpretation (Part E)

### 1. Did the corrected failure estimate move between the two periods?

No observed movement. Both `before` and `after` recorded `0/10` flagged conversations in the 20% random sample (`raw = 0.0000`, `corrected = 0.0000`) using the frozen Homework 5 judge `raw_schema_or_planning_leak-v1` (`failure_sensitivity = 1.0000`, `pass_specificity = 1.0000`). That aligns with the fact that the Cartwheel agent system prompt and model (`gemini/gemini-3.5-flash-lite`) did not change between the two periods.

### 2. Do the intervals support a conclusion, or is the result uncertain?

**Uncertain.** Although the 20,000-draw seeded percentile bootstrap interval collapsed to `0.0000 - 0.0000`, observing `0/10` with `[0, 1]` clamping collapses a percentile bootstrap interval to zero rather than proving zero failures. With only `n = 10` random conversations per period, statistical power is low: by the Rule of Three (`3 / n`), observing `0/10` cannot rule out a true failure rate up to roughly `30%`. Cross-checking our Homework 4 and Homework 5 human labels confirms that at least **4** genuine `raw_schema_or_planning_leak` failures exist within the 50-scenario monitoring pool (`support-0182`, `support-0184`, `support-0186`, and `support-0190`, or at least `8%` true prevalence), none of which happened to fall into the `n = 10` random draw. We cannot conclude that true failure prevalence is zero or detect small shifts without a larger random sample.

### 3. What did the risk groups reveal that the random estimate did not?

Our pre-registered `write_action` risk group (`issue_refund` or `cancel_order`, `17` conversations in each period, `3` overlapping with the random sample) also flagged `0/17` in both periods. That revealed a **risk-slice design mismatch** that the random estimate alone could not show: write-action conversations were completely clean for `raw_schema_or_planning_leak` across all 17 cases in both runs, whereas our earlier Homework 4 and Homework 5 human labels showed that the 4 known leaks in this 50-scenario set (`support-0182`, `support-0184`, `support-0186`, and `support-0190`) occurred on **read-only broken-database inspection and policy/catalog queries** (`get_order`, `get_store_info`, `get_policy`, `search_products`). Adding `policy_lookup` alongside `write_action` improves coverage (catching `support-0182` and `support-0184` in `before`, plus all 4 in `after`), while a dedicated read-only order/catalog inspection slice is also needed to reliably catch non-policy inspection leaks like `support-0186` and `support-0190`, kept strictly separate from the uniform random prevalence sample.

### 4. What action should happen if the estimate crosses the threshold (`0.15`)?

Never auto-promote raw LLM-judge flags directly into evaluation suites or training labels. If corrected prevalence crosses `0.15`:

1. Pull the flagged traces from Langfuse across both `raw_schema_or_planning_leak_verdict` and `raw_schema_or_planning_leak_risk_verdict`.
2. Run **human error analysis** on those flagged traces to distinguish genuine agent regressions from judge drift or false alarms.
3. Fix the root cause in the agent prompt or tools (or recalibrate and re-validate a new judge version if the judge drifted).
4. Convert **human-confirmed failures** into new cases in the Homework 6 Harbor CI suite (`eval_cases/cases.jsonl`), tracking unfixed failures as `capability` cases and promoting fixed cases to `kind: regression` so future PRs are blocked if they recur.
