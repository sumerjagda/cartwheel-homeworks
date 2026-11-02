# Cartwheel Homework 4: Human Trace Review & Failure Taxonomy Summary

## 1. Reviewed Sample Size & 4-Batch Composition (`sample_manifest.json`)
We reviewed **105 distinct multi-turn Cartwheel sessions** drawn from our 250-scenario Module 1 trace collection (`traces/support_traces.json` and live Langfuse on port `3000`). No session was counted toward more than one batch:

1. **Batch 1 - Uniform Random & Cluster Representatives (`30` sessions):** `15` uniformly sampled sessions (`seed=101`) + `15` k-means cluster representatives over standardized structural features (`turn_count`, `tool_call_count`, `distinct_tools`, `has_retrieval`, `tokens`).
2. **Batch 2 - Product Dimension Stratification (`30` sessions):** Stratified evenly across `user_role` (`10 shopper`, `10 merchant`, `10 support`) selected *before* inspecting outcomes.
3. **Batch 3 - Depth Searches & Close Negatives (`30` sessions):** Targeted retrieval for candidate failure modes (stalled turns, corrupted DB records, duplicate product titles, missing policy slugs, unescalated account changes) paired with close-negative boundary sessions.
4. **Batch 4 - Post-Taxonomy Theoretical Saturation Check (`15` sessions):** `15` additional uniformly sampled random sessions (`seed=404`) reviewed after locking our 6-mode taxonomy.

### Saturation Result in Final 15 Traces
**0 previously unseen consequential failure modes** appeared in the final 15 reviewed sessions (`Batch 4`), confirming that the 6-mode taxonomy reached theoretical saturation.

---

## 2. Final Binary Failure Modes & Sample Fractions (`patterns.json` & `labels/*.jsonl`)
> *Note on Sample Fractions:* Because Batches 1–3 use clustering, role stratification, and targeted depth searches, the percentages below are **sample fractions** within our 105-session review set rather than unbiased population prevalence estimates (which Homework 5 will compute over the full store via Rogan-Gladen correction).

| Failure Mode (`snake_case`) | `SPEC.md` Source | Evaluator Type | Sample Fraction (`n = 105`) | Total Labeled (incl. HW5 Depth Enrichment) | Confirmed Positives (`>= 3`) | Close Negatives (`>= 3`) |
| :--- | :--- | :--- | :--- | :--- | :--- | :--- |
| `stalled_or_unexecuted_turn` | `RESP-2, PURPOSE-1` | `judge` | **6 / 105 (5.7%)** | 6 Fail / 99 Pass | `support-0093, support-0094, support-0098, support-0205` | `support-0066, support-0067, support-0034` |
| `missing_policy_citation` | `RESP-1` | `code` | **3 / 105 (2.9%)** | 3 Fail / 102 Pass | `support-0115, support-0118, support-0236` | `support-0221, support-0066, support-0001` |
| `unhandled_record_inconsistency` | `RESP-3, ESC-3` | `judge` | **4 / 105 (3.8%)** | 4 Fail / 101 Pass | `support-0178, support-0176, support-0186, support-0190` | `support-0177, support-0180, support-0204` |
| `ambiguous_duplicate_product_listing` | `RESP-3` | `code` | **4 / 105 (3.8%)** | 4 Fail / 101 Pass | `support-0139, support-0140, support-0150, support-0193` | `support-0144, support-0147, support-0195` |
| `unescalated_account_or_dispute_request` | `ESC-2, ESC-3` | `code` | **3 / 105 (2.9%)** | 3 Fail / 102 Pass | `support-0250, support-0190, support-0156` | `support-0249, support-0154, support-0155` |
| `raw_schema_or_planning_leak` | `RESP-6 (SPEC.md revision), RESP-5` | `judge` | **25 / 105 (23.8%)** | 37 Fail / 80 Pass | `support-0217, support-0233, support-0092, support-0182` | `support-0221, support-0240, support-0144` |

* **Homework 5 Readiness (`>= 30` Fail & `>= 30` Pass):** During our store-wide depth scan on `raw_schema_or_planning_leak` (an `llm_judge` mode), we human-verified all store hits so `analysis/state/labels/raw_schema_or_planning_leak.jsonl` already holds **37 Fail (`1`)** and **80 Pass (`0`)** labels, exceeding the `30 Fail / 30 Pass` threshold required by Homework 5.

---

## 3. Account of One Taxonomy Revision (Product-Fix Merge/Split Rule)
During initial axial coding after Batch 1 and Batch 2, we grouped missing policy IDs (`support-0115`, `support-0118`) together with ungrounded product guesses (`support-0193`, where the agent guessed that four identically named Heavy-Duty Vases *"likely vary by size"*) under a single broad candidate bucket called `policy_or_fact_grounding_error`.

Applying the **product-change merge/split test** (*"Would one product/prompt change fix both?"*), we split that bucket into two sharper modes:
1. **`missing_policy_citation` (`RESP-1`, `code_check`):** The underlying cancellation or return window rule is accurate, but the reply omits the `policy_id` slug (`cw-cancellations`, `cw-returns`). A single prompt citation-formatting instruction fixes all instances.
2. **`ambiguous_duplicate_product_listing` (`RESP-3`, `code_check`):** When `search_products` returns multiple items with the identical title (`support-0139`, `0140`, `0150`, `0193`), omitting `product_id`s and speculating about size requires a tool-presentation & disambiguation rule rather than policy citation formatting.

---

## 4. Documented Rejected Search Suggestions (`suggestions.json`)
We treated keyword, regex, and deterministic filters strictly as retrieval signals and recorded explicit rejections when a trace fell on the `Pass (0)` side of a mode boundary:
- **Rejected Suggestion `sugg-rej-01` (`support-0066` on `stalled_or_unexecuted_turn`):** A depth search flagged `support-0066` because Turn 1 said *"I've submitted a full refund request of $158.25"* when `issue_refund` did not auto-approve. Inspecting the session showed `issue_refund` returned `status: "queued_for_approval"` (above the `$100` threshold) and the agent accurately explained that human approval was required. Marked **Rejected (Close Negative `Pass = 0`)**.
- **Rejected Suggestion `sugg-rej-02` (`support-0144` on `raw_schema_or_planning_leak`):** A regex scan for `Product ID:` flagged `support-0144`. Inspecting the trace showed two distinct `"Walnut Cutting Board"` listings (`$78.50` vs `$122.00`), where displaying `(Product ID: 35)` and `(Product ID: 38)` is required disambiguation under `RESP-3` rather than raw schema leakage. Marked **Rejected (Close Negative `Pass = 0`)**.

---

## 5. `SPEC.md` Revision (`RESP-6`) & Motivating Open-Coding Annotation
* **Motivating Annotations:** `ann-001` (`support-0233`: *"Turn 1 exposes raw internal database field/schema formatting (`refund_eligible`) directly in customer-facing prose instead of plain natural language"*) and `ann-004` (`support-0182`: *"Turn 1 leaks internal pre-tool planning narration ('I am going to look up the details for order #8002...') at the top of the final user-facing response"*).
* **Why `SPEC.md` Needed Revision:** `RESP-5` required *"direct and respectful language"*, which was too vague for two independent reviewers to agree on whether echoing backticked SQL/JSON booleans (`` `refund_eligible` is false ``) or pre-tool chain-of-thought narration (`"I am going to look up..."`) constituted a failure.
* **Recorded Revision in `SPEC.md`:** Added **`RESP-6`**: *"Translate internal tool fields into plain user-facing language; do not expose raw database column names, boolean flags (such as `refund_eligible`), or internal pre-tool planning narration at the start of customer replies."*

---

## 6. AgentDebug Taxonomy Comparison
After stabilizing our 6 modes, we compared our taxonomy against the published **AgentDebug** failure taxonomy (which separates *Memory*, *Reflection*, *Planning*, *Action*, and *System* errors):
- Our `stalled_or_unexecuted_turn` corresponds to AgentDebug's **Planning-to-Action termination gap** (halting after planning text before emitting the final synthesis or required `escalate_to_human` action).
- Our `unhandled_record_inconsistency` corresponds to AgentDebug's **Reflection / Observation Verification failure** (failing to critically reflect on impossible tool outputs like `delivered_at < shipped_at`).
- Comparing against AgentDebug confirmed no additional mode needed to be added, as our 6 modes covered all observed failures in our 105-session review set (`0` new modes in Batch 4).
