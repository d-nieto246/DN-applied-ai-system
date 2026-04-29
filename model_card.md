# PawPal+ Model Card

## Testing Results

### What worked

Testing the tool methods without the LLM was the right call. It made every guardrail (bad pet name, bad datetime, duplicate prevention) explicit and fast to verify. The `test_full_conflict_resolution_chain` test is the most valuable: it simulates the agent's entire plan → act → check → fix loop in a single deterministic test, proving the tools compose correctly even when the LLM is not involved.

### What didn't (and why)

The LLM's planning behavior — whether it picks appropriate tasks for a given species, whether it spaces times sensibly — cannot be covered by unit tests. That layer is validated by logging (`pawpal_agent.log` records every tool call and result) and human review of the AI Plan Narrative and Agent Work Log shown in the UI after each run.

### What I'd add with more time

- Tests for three or more simultaneous conflicts at the same timestamp
- Integration tests for the Streamlit UI (conflict warning display, filter controls, session state persistence)
- An evaluation harness that replays known requests against the live API and asserts the resulting schedule matches expected entry counts and times

---

## Responsible AI

### Limitations and biases

The agent's pet care knowledge comes entirely from Claude's training data, which skews toward common household pets in English-speaking, Western contexts. A dog gets walking and feeding suggestions that reflect typical American or European care norms. Less common species — reptiles, birds, small mammals — receive shallower, more generic recommendations because the model has seen less detailed care literature about them. There is no veterinary knowledge base backing the suggestions; the system has no way to account for a pet's age, health conditions, medications, or individual behavioral history. An owner who follows the AI's plan without applying their own knowledge could miss care that matters for their specific animal.

The conflict check catches exact datetime overlaps but does nothing about near-misses. Two tasks scheduled five minutes apart for different pets is technically "clean" to the system but may be stressful for a single owner to execute. The AI also has no concept of task duration — a 30-minute walk and a 5-minute feeding are treated identically from a scheduling standpoint.

### Misuse potential and mitigations

The most realistic misuse is an owner over-trusting the plan. The AI presents its output confidently and in fluent prose, which can make it feel more authoritative than it is. A first-time pet owner with no baseline knowledge might not recognize when a suggestion is generic or wrong for their animal.

Mitigations already in place: the agent work log is visible in the UI so every tool call and result is auditable; the plan narrative always ends with a structured summary so the owner can review what was scheduled before treating it as final. What would strengthen this: adding a visible disclaimer in the UI ("Review this plan before following it — the AI does not know your pet's health history"), and including a confidence indicator in the agent's response when it is working with an unfamiliar species.

The system writes to a live Scheduler that has no undo function. If the agent schedules something wrong, the owner has to delete entries manually. An undo-last-plan feature would reduce the cost of trusting the AI and then changing your mind.

### What surprised me during reliability testing

The guardrail that surprised me most was the duplicate-prevention check. Before adding it, running the same request twice would silently double-book every task — the agent had no way to know the entries already existed. This wasn't obvious until I wrote `test_add_and_schedule_task_duplicate_is_skipped` and watched it fail the first time. The fix (checking for an existing entry before calling `add_entry_with_warning`) is three lines, but without the test it would have been invisible.

The other surprise was how well the error-string contract worked. Every tool method returns `"Error: ..."` on failure. Because Claude's system prompt says "if a tool returns an error, acknowledge it and take a corrective action," the model actually self-corrects in practice — for example, calling `list_pets` to verify a name before retrying. That behavior is not tested, but it showed up consistently during manual runs and is visible in the agent work log.

### Collaboration with AI during this project

**Helpful suggestion:** When designing the tool interface, I initially planned separate `add_task` and `schedule_task` tools — one to create the Task object, one to schedule it. Claude suggested collapsing them into a single `add_and_schedule_task` tool, reasoning that splitting the action would create partial-state failures where a task exists on a pet but has no schedule entry. That was the right call. It simplified the agent's reasoning and eliminated a whole class of inconsistent-state bugs.

**Flawed suggestion:** During the session-state integration, Claude suggested storing the agent result directly in `st.session_state` inside the form's submit block and then rendering it in the same pass, without calling `st.rerun()`. In practice, Streamlit had already rendered the schedule dashboard higher on the page before the agent ran, so the updated entries were invisible until the user interacted with something else. The fix was to store the result and then call `st.rerun()` so the full page re-renders with the new schedule visible. Claude's suggestion was logically reasonable but missed how Streamlit's top-to-bottom rendering model interacts with form submissions.

---

## Reflection

Building the agentic layer clarified something that's easy to miss when reading about AI agents: **the hard part isn't the LLM, it's the interfaces.** Claude handled the planning and self-correction well out of the box. The work was in designing the five tools so their inputs and outputs were unambiguous enough for the model to use reliably — getting the datetime format right, making error messages actionable so Claude could recover, and deciding which operations deserved their own tool versus being collapsed into one.

The check-and-fix loop was the most instructive part. Without it, the agent could produce a logically reasonable plan that silently had two tasks at the same time. Adding `check_conflicts` as a required step the agent must pass before finishing turned a best-effort output into a verifiable one. That pattern — AI does work, AI checks work, AI fixes what it got wrong — generalizes well beyond scheduling.

The biggest open question is evaluation: how do you systematically test that an agent is doing the *right* thing (appropriate tasks for each species, sensible times) rather than just the *valid* thing (no conflicts, no unknown pets)? The domain tests answer the latter. The former still requires human judgment looking at the plan narrative. That gap between "runs without errors" and "produces good plans" is where most of the interesting AI engineering work lives.
