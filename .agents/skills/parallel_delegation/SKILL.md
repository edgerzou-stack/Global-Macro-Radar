---
name: parallel_delegation
description: Rules for using invoke_subagent for parallel execution
---

# Parallel Delegation Protocol

## Core Rule
To maximize system efficiency, all complex or multi-dimensional tasks MUST be automatically delegated to parallel subagents using the `invoke_subagent` tool.

## Mandatory Isolation (Anti-Overwrite Protection)
To completely resolve serial overwrite issues and git merge conflicts when multiple subagents modify the codebase simultaneously:
1. **Workspace Branching**: When invoking subagents that will modify code, you MUST use `Workspace: "branch"` (or `"share"`) in the `invoke_subagent` arguments. This creates isolated local workspaces for each subagent so they do not step on each other's toes in the same directory.
2. **Git Branch Isolation**: Subagents MUST create their own feature branches (e.g., `git checkout -b fix/weekend-fallback`) and push to the remote. The main agent (you) or a designated reviewer will merge them later.
3. **Never `Workspace: inherit` for Parallel Code Changes**: Do not use the default `inherit` workspace mode if subagents will be running `git add` or `sed`/`replace_file_content` on the same git repository concurrently, as it leads to corrupted commits and lock conflicts.
