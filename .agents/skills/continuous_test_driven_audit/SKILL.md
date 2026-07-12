---
name: continuous_test_driven_audit
description: "Rules for automatically auditing and expanding test coverage, and updating offline test data after major architectural changes."
---

# 🧪 Continuous Test-Driven Audit (CTDA) Protocol

To guarantee that the trading system remains absolutely stable and that our offline test harnesses do not become stale, you MUST automatically execute this protocol **immediately following any major architectural change, feature addition, or core logic refactoring**, without waiting for the user to prompt you.

## 1. 🔍 Automatic Coverage Gap Analysis
- After completing a refactor, automatically use read tools to analyze the modified files.
- Compare the modified logic against the `quant-strategy/tests/` directory.
- Identify: Are there new branches, new edge cases, or new failure modes introduced by this change that lack test coverage?

## 2. 🔄 Fresh Data Injection (Live to Offline Sync)
- The user generates daily quantitative reports, which automatically caches the freshest live market data into the production database.
- Before running the final validation, you MUST automatically sync this fresh data into the offline test sandbox.
- **Action**: Copy the latest production `market_data_cache.db` (usually in `quant-strategy/` or the designated app data dir) and overwrite `quant-strategy/tests/test_data/frozen_market_cache.db`. 
- This ensures our tests are always running against the most recent, realistic market structure and corporate actions (e.g., recent dividends or circuit breakers).

## 3. 🛡️ Autonomous Test Iteration
- Automatically write the missing test cases identified in Step 1.
- Run `pytest quant-strategy/tests/` to perform a full regression test against the newly injected fresh data.
- Fix any broken tests (pseudo-tests or outdated assertions) that fail due to the architectural change.

## 4. 📝 Reporting
- When outputting your final `Context & Memory State` block to the user, include a specific note stating: *"CTDA Protocol Executed: Synced fresh daily report data to offline sandbox and updated X test cases."*
