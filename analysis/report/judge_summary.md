# Homework 5: LLM-as-Judge Evaluation Report (`raw_schema_or_planning_leak`)

## 1. Selected Failure Mode & Binary Criterion (Part A)

- **Mode ID:** `raw_schema_or_planning_leak` (`RESP-6` & `RESP-5` in `SPEC.md`)
- **Evaluation Question:** Does any customer-facing assistant reply in the conversation expose raw internal database/JSON field identifiers (`refund_eligible: False`, `delivered_at` is `None`, `status: 'refunded'`, `queued_for_approval`), leak unrequested internal numeric `Store ID` / `User ID` primary keys (`(Store ID 1)`, `(Store ID 4)`), or prepend internal pre-tool planning narration (`"I am going to check the details and status of order..."`)?
- **HW5 Label Convention (`analysis/state/hw5_labels/raw_schema_or_planning_leak.jsonl`):**
  - **`1 = Pass`** (Positive class: failure absent; assistant translates tool outputs into clean customer-facing prose)
  - **`0 = Fail`** (Negative class: failure present; assistant leaks raw schema tokens, unrequested numeric store/user IDs, or planning narration)
- **Boundary Rules (Close Negatives that count as `Pass = 1`):**
  - Official policy citations (`cw-returns`, `cw-shipping`, `cw-refunds`, `cw-disputes`, `store-<slug>-policy`) are required by `RESP-1` and count as `Pass`.
  - Duplicate catalog disambiguation (`Product ID` alongside duplicate product titles at different prices) is required by `RESP-3` and counts as `Pass`.
  - Standard customer order numbers (`order #1990`) count as `Pass`.

---

## 2. Labelled Dataset & Stratified `20% / 40% / 40%` Split (Part B)

- **Clean Judge Inputs (`analysis/state/hw5_trace_inputs.json`):** `117` multi-turn conversation records (`user`, `tool_call`, `tool_result`, `assistant` messages across all turns), stripped of all human open-coding notes, failure labels, and scenario `extra`/`expected` metadata.
- **Disjoint Stratified Split (`analysis/state/splits.json`, `seed=7`, `min_per_class=10`):**

| Split | Fraction | Total Traces | Pass (`1`) | Fail (`0`) | Purpose |
| :--- | ---: | ---: | ---: | ---: | :--- |
| **Training (`train`)** | `20%` | `23` | `16` | `7` | Few-shot prompt examples only (never evaluated). |
| **Development (`dev`)** | `40%` | `47` | `31` *(32 pre-flip)* | `16` *(15 pre-flip)* | Disagreement inspection and prompt refinement (`v0 -> v1`). |
| **Test (`test`)** | `40%` | `47` | `32` | `15` | Locked held-out evaluation run once after `freeze_judge`. |
| **Total** | `100%` | **`117`** | **`79`** *(80 pre-flip)* | **`38`** *(37 pre-flip)* | Exceeds the `>=30 Pass` and `>=30 Fail` requirement. |

---

## 3. Prompt Refinement & Development Disagreements (Part C)

### Iteration Hill-Climbing Progression (`gemini/gemini-3.1-flash-lite` via DocETL)

| Version | Prompt File | Dev `TP` | Dev `FN` | Dev `TN` | Dev `FP` | Dev `TPR` (95% Wilson CI) | Dev `TNR` (95% Wilson CI) | Dev Agreement | Label Flips | Summary of Change |
| :--- | :--- | ---: | ---: | ---: | ---: | :--- | :--- | ---: | ---: | :--- |
| **`v0`** | `raw_schema_or_planning_leak-v0.txt` | `31` | `1` | `14` | `1` | **`96.88%`** `[84.26%, 99.45%]` | **`93.33%`** `[70.18%, 98.81%]` | `95.74%` (`45/47`) | `0` | Initial prompt with `RESP-6`/`RESP-5` rules and 4 `train` examples. |
| **`v1` (Frozen)** | `raw_schema_or_planning_leak-v1.txt` | `31` | `0` | `15` | `1` | **`100.00%`** `[88.97%, 100.00%]` | **`93.75%`** `[71.67%, 98.89%]` | `97.87%` (`46/47`) | `1` | Clarified unrequested `(Store ID <N>)` leak rule (`RESP-6`), added `train` example `2889050716f3ae52a25c288e9cd7719d`, and fixed 1 human label mistake (`997383a856f2a4c01ea9698c4a5c1faf`). |

### What We Learned from Inspecting the `v0` Development Disagreements

1. **Disagreement #1 (`997383a856f2a4c01ea9698c4a5c1faf` / `support-0184`) - False Fail (`FN`: Human = `Pass`, Judge = `Fail`):**
   - **Assistant Text:** *"However, looking at the order record, this item has not been marked as delivered yet (`` `delivered_at` `` is currently `` `None` ``), so a 30-day return deadline cannot be calculated at this time."*
   - **Judge Critique:** *"The assistant failed by exposing the internal database field name and its corresponding null value (`delivered_at` is currently `None`) directly to the user."*
   - **Action Taken (Human Label Fix):** The judge caught a genuine human labelling oversight from HW4 where `support-0184` had been marked `"no failure observed"` despite leaking `` (`delivered_at` is currently `None`) ``. Rather than teaching the judge to ignore a real leak, we recorded an append-only label flip (`997383a856f2a4c01ea9698c4a5c1faf#1`, setting `superseded_by` on the prior record).

2. **Disagreement #2 (`92749cd39c47f498c9176dbdf8fb0d02` / `support-0217`) - False Pass (`FP`: Human = `Fail`, Judge = `Pass`):**
   - **Assistant Text:** *"I was unable to view order #62 as it is not associated with your merchant account (Store ID 1)."*
   - **Judge Critique (`v0`):** *"Mentioning 'Store ID 1' is appropriate in this context to explain the access error regarding the merchant account..."*
   - **Action Taken (Prompt Boundary Refinement in `v1`):** Under `RESP-6` (*"Do not expose internal database column names, raw boolean flags, or internal user/store IDs unless the user asked for their own ID"*), exposing the numeric database key `(Store ID 1)` instead of the store name (`Blue Heron Ceramics`) is a failure. In `raw_schema_or_planning_leak-v1.txt`, we explicitly stated that unrequested `(Store ID <N>)` disclosures in merchant replies are `Fail` and added **Training-split** example `2889050716f3ae52a25c288e9cd7719d` (`Copperline Tools (Store ID 4)`). In `v1`, the judge correctly flagged `92749cd39c47f498c9176dbdf8fb0d02` as `Fail`.

3. **Why We Stopped Revising After `v1`:**
   - On `dev`, `v1` reached `TPR = 100.0%` (`31/31`) and `TNR = 93.75%` (`15/16`), with only 1 borderline disagreement remaining (`5a3b7fb8bbd00df00202198352dee211`). Tuning further for a single trace would risk overfitting to the development split.

---

## 4. Frozen Held-Out Test Evaluation & Deployment Decision (Part D)

After freezing `raw_schema_or_planning_leak-v1` (`status: "frozen"`), we evaluated the **47 held-out `test` traces** (`32 Pass`, `15 Fail`) once (`analysis/report/test-raw_schema_or_planning_leak-v1.json`):

| Held-Out Test Metric (`n = 47`) | Point Estimate | 95% Wilson Confidence Interval | Confusion Counts (`Pass = 1`, `Fail = 0`) |
| :--- | :--- | :--- | :--- |
| **Test TPR (Pass Sensitivity)** | **`100.00%`** (`32 / 32`) | **`[89.28%, 100.00%]`** | `TP = 32`, `FN = 0` |
| **Test TNR (Fail Specificity)** | **`100.00%`** (`15 / 15`) | **`[79.61%, 100.00%]`** | `TN = 15`, `FP = 0` |
| **Overall Test Agreement** | **`100.00%`** (`47 / 47`) | `0` test disagreements | Class counts: `32 Pass`, `15 Fail` |

### Would We Use This Judge in Production?
- **Yes, with confidence-interval awareness:**
  1. **Point Estimates:** On held-out `test` traces, `raw_schema_or_planning_leak-v1` achieves **`TPR = 1.0000`** (`32/32` true passes) and **`TNR = 1.0000`** (`15/15` true failures), well above the `2 * (TPR + TNR - 1) > 0` usefulness threshold.
  2. **Uncertainty (95% Wilson Intervals):** Because our held-out test split has `32` Pass cases and `15` Fail cases, the 95% Wilson interval for `TPR` is **`[89.28%, 100.00%]`** and for `TNR` is **`[79.61%, 100.00%]`**. Even at the conservative lower bounds of the 95% confidence intervals (`TPR >= 89.3%`, `TNR >= 79.6%`), `TPR + TNR - 1 >= 0.689 > 0`, meaning the judge is reliable enough both for **CI regression gating** and for **Rogan-Gladen prevalence estimation** across production traces.
