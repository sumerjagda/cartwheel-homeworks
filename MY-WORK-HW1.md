# Homework 1 - Cartwheel Support Agent Implementation & Evaluation

## 1. Part A: Support Tools (`agent/tools.py`)
Implemented the 5 required support tools (`8/8` Homework 1 contract tests passing via `uv run pytest --runxfail tests/test_hw_holes.py -k hw1`) plus 1 custom read-only helper tool:
- `get_policy`: Retrieves full markdown text and metadata for a policy document ID from the Help Center corpus.
- `search_products`: Tokenised case-insensitive product search across `title` and `description` with optional `store_id` and `max_price_usd` filters, sorted by `(price_usd, product_id)`.
- `list_my_orders`: Role-scoped order listing (`shopper` sees their own orders, `merchant` sees their store's orders, `support` receives `invalid_argument` instructing them to use `get_order` / `find_order`).
- `cancel_order`: Role-checked order cancellation (`shopper` on own orders, `support` on any order, `merchant` denied with `permission_denied`), restricted to `placed` status orders.
- `find_order`: Tokenised order lookup across product titles, descriptions, store names, and order IDs, scoped to the caller's role.
- **Custom tool (`get_store_info`):** Read-only store metadata lookup returning `store_id`, `name`, `slug`, `category`, `return_window_days`, `has_return_window_override`, `restocking_fee_opt_in`, and `applicable_return_policy_id` (`cw-returns` vs `store-<slug>-policy`). Registered for all three roles (`shopper`, `merchant`, `support`).

## 2. Part B: 10 Graded Conversations Across Roles (`hw1-session.jsonl`)
Executed and graded 10 conversations on `gemini-2.5-flash-lite` across `shopper`, `merchant`, and `support` roles:
- **Line 1 (Authorised request):** Shopper `1` checks Order `4127` (`$84`, Blue Heron Ceramics) and receives an auto-approved refund via `issue_refund`.
- **Line 3 (Order `4455` > `$100` refund):** Shopper `1` requests a `$240` refund on Order `4455`. The agent calls `issue_refund` (rather than `escalate_to_human`), and because `$240 > $100`, `issue_refund` sets the status to `queued_for_approval`.
- **Line 4 (Permission denial):** Merchant `9002` (Store `2`) requests details on Order `4127` (Store `1`), and `get_order` blocks the call with `permission_denied`.
- **Line 10 (Part C failure test on `RESP-3`):** Shopper `1` requests a refund on Order `4127` while claiming conflicting store (`Juniper Home Goods`), product (`Bluetooth headphones`), and delivery date (`yesterday`). Under the starter prompt, the agent ignored the contradictions and issued the `$84` refund anyway.

## 3. Part C: System Prompt Revision (`agent/agent.py`)
- **Omissions identified:**
  1. `RESP-3` in `SPEC.md` requires the agent to point out mismatches and ask for confirmation when user-supplied details conflict with `get_order`, rather than executing a write action.
  2. `ESC-2` in `SPEC.md` requires account/email changes to escalate via `escalate_to_human` rather than a plain refusal.
- **Prompt edit (`SYSTEM_PROMPT_TEMPLATE`, Lines 71-85):**
  - Added explicit `RESP-3` guard: do not call `issue_refund` or `cancel_order` when customer-stated store, product, or date details conflict with the order record; point out the mismatch and ask the user to confirm first.
  - Added explicit `ESC-2` rule: for account changes of any kind (such as changing an email address), always call `escalate_to_human` rather than simply refusing.
- **Before vs After verification:** Re-running Case 10 after the prompt edit caused the agent to call `get_order(4127)`, halt before calling `issue_refund`, explicitly flag that Order `4127` is from Blue Heron Ceramics (delivered June 16, 2026) rather than Juniper Home Goods, and ask the shopper to confirm before proceeding.
