# Agent Response Format Rules

## Context and Memory Transparency
To prevent silent context loss and ensure the user is always aware of the agent's current understanding of the ongoing tasks, you MUST include a "Context & Memory State" section at the end of EVERY turn where you complete an action or provide a final output.

Format it strictly as follows at the very bottom of your response:

---
**🧠 Context & Memory State:**
*   **✅ Completed Tasks (What I just did):** [Brief summary of tasks accomplished in this turn]
*   **🔄 Active Context (What I still remember/Ongoing goals):** [List of current long-term goals, pending items, or specific constraints that are actively in the cache]
*   **❓ Forgotten/Dropped Items (What I might have lost or deferred):** [Explicitly state anything that was mentioned earlier but dropped due to priority, or state "None" if confident nothing was lost. If you feel context is getting too long or truncated, flag it here.]



## Universal "Plan-Act-Review" SOP
To ensure maximum efficiency, transparency, and alignment with the user, you MUST adhere to the following strict workflow for any non-trivial codebase modifications or architectural tasks:

1. **Phase 1: Propose & Iterate Plan (No Execution)**
   - Before writing code or modifying files, generate a detailed execution plan.
   - Continuously refine the plan based on user feedback.
   - Wait for explicit user approval before proceeding.

2. **Phase 2: Execution**
   - Execute the approved plan strictly.

3. **Phase 3: Hierarchical Memory Probe & Review**
   - After completing the execution, you MUST output the "Context & Memory State" block at the bottom of your response.
   - **Categorization**: Group active context hierarchically (e.g., Event -> Sub-tasks).
   - **Loss Recovery**: If any memory/context was dropped, explicitly ask the user if it should be retained or permanently discarded.

Format it strictly as follows:

---
**🧠 Context & Memory State:**
*   **✅ Completed Tasks:** [Summary of tasks just completed]
*   **🔄 Active Context (Hierarchical):**
    *   **[Event/Project A]**:
        *   [Sub-task/Constraint 1]
        *   [Sub-task/Constraint 2]
*   **❓ Dropped/Forgotten Items:** [List explicitly dropped items. If not empty, add: "Did you still need me to retain these for later, or can they be permanently discarded?"]

# Project Specific Rule: Code Synchronization & Git Push Strategy
- When instructed to push code to GitHub for this project (Global-Macro-Radar), you MUST strictly follow the dual-repo segregation strategy:
  1. **Public Repository (main branch)**: Commit and push all core system logic, pipelines, UI, and configuration (excluding sensitive keys and the `tests/` directory) to the `public` remote's `main` branch.
  2. **Private Repository (private-main branch)**: Switch to the `private-main` branch, merge `main`, add the `tests/` directory (which contains proprietary regression testing logic), commit, and push to the `private` remote (`git push private private-main:main`).
  3. **Cleanup**: Always switch back to the `main` branch after syncing to leave the workspace in a clean state.

# Project Specific Rule: Database Protection and State Isolation
- **No Destructive Database Operations**: You MUST NEVER execute scripts (e.g. `db_reset.py`, `scratch_test.py`) that drop tables, delete records, or otherwise clear the production database (`quant_system.db`). You MUST NEVER write or execute bare SQL queries containing `DELETE` or `DROP` against the production tables (`trade_history`, `portfolio`) without explicit, unambiguous approval from the User.
- **Respect Database Triggers**: The database is physically protected by SQLite triggers that block `DELETE` operations on `trade_history`. Do not attempt to bypass these triggers or drop them unless specifically ordered to perform a safe wipe by the user.

# Universal Rule: Plan Archiving
- Whenever you create an `implementation_plan.md` artifact in your isolated brain directory, you MUST also save a persistent copy of it to the local workspace in the `.agents/plans/` directory.
- Name the file descriptively with a timestamp, e.g., `.agents/plans/YYYYMMDD_feature_name_plan.md`.
- This ensures that all historical implementation plans are version-controlled and easily accessible to the user in their IDE.
