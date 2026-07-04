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
