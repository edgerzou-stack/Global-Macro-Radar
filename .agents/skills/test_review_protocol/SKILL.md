---
name: test_review_protocol
description: Enforces that any modification to test suites requires a formal review to verify integrity and prevent pseudo-tests.
---

# Test Review Protocol

## Core Rule
Every time our test suites are changed, the changes MUST be reviewed. They cannot be arbitrarily modified.

## Requirements for Test Modifications
1. **Formal Review Required**: Any modification, addition, or deletion of test cases requires a formal review by either the user (Partner) or a dedicated code-review subagent.
2. **Integrity Verification**: The review must explicitly verify the integrity of the modified tests. This includes checking for and preventing "pseudo-tests" (tests designed to pass trivially without actually validating the underlying logic).
3. **No Arbitrary Changes**: Tests must not be modified simply to make failing tests pass. Any change to a test must correspond to a legitimate change in business logic or requirements, which must be documented in the review request.
4. **Regression Guarantee**: Changes to tests must not compromise the strict 100% replicability and regression tracking rules defined in the universal SOP.

## Execution Steps for Agents
- When generating a plan that involves changing tests, highlight the test modifications as a separate, critical review item.
- Before committing test changes, explicitly ask the user: "Test suites have been modified. Please review the changes for integrity."
- Do not proceed with committing or integrating the test changes until the formal review is approved.
