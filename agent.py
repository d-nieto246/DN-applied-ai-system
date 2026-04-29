"""PawPal+ Agentic Workflow.

Claude plans a care schedule, acts via tools that write into the live Scheduler,
checks its own work for conflicts, and self-corrects before reporting.
"""

from __future__ import annotations

import json
import logging
import os
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any

import anthropic

from pawpal_system import Owner, Pet, Scheduler, Task

# ── Logging: file + console ───────────────────────────────────────────────────

_log_handler_file = logging.FileHandler("pawpal_agent.log", encoding="utf-8")
_log_handler_console = logging.StreamHandler()
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
    handlers=[_log_handler_file, _log_handler_console],
)
_logger = logging.getLogger("pawpal.agent")

# ── Agent step record ─────────────────────────────────────────────────────────


@dataclass
class AgentStep:
    kind: str   # "start" | "tool_call" | "tool_result" | "final" | "error"
    label: str
    detail: str
    ok: bool = True


# ── Tool schemas (passed to Claude) ──────────────────────────────────────────

TOOLS: list[dict[str, Any]] = [
    {
        "name": "list_pets",
        "description": "List all pets owned by this owner, with name and species.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "list_scheduled_entries",
        "description": "Return all currently scheduled entries, sorted by time.",
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "add_and_schedule_task",
        "description": (
            "Create a new care task for a pet and immediately schedule it. "
            "Use ISO-8601 format for scheduled_at: YYYY-MM-DDTHH:MM"
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pet_name": {
                    "type": "string",
                    "description": "Name of the pet (must already exist in the system)",
                },
                "task_type": {
                    "type": "string",
                    "description": "Short label for the task, e.g. 'Feeding', 'Walking'",
                },
                "task_description": {
                    "type": "string",
                    "description": "A sentence describing what the task involves",
                },
                "frequency": {
                    "type": "string",
                    "enum": ["Daily", "Weekly", "Monthly", "Once"],
                },
                "scheduled_at": {
                    "type": "string",
                    "description": "ISO-8601 datetime, e.g. 2026-04-28T08:00",
                },
            },
            "required": [
                "pet_name",
                "task_type",
                "task_description",
                "frequency",
                "scheduled_at",
            ],
        },
    },
    {
        "name": "check_conflicts",
        "description": (
            "Detect scheduling conflicts (two or more tasks at the exact same datetime). "
            "Always call this after adding tasks to verify the schedule is clean."
        ),
        "input_schema": {"type": "object", "properties": {}, "required": []},
    },
    {
        "name": "reschedule_entry",
        "description": (
            "Move a scheduled entry to a new time to resolve a conflict. "
            "Identify the entry by pet name, task type, and its current ISO-8601 datetime."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "pet_name": {"type": "string"},
                "task_type": {"type": "string"},
                "current_scheduled_at": {
                    "type": "string",
                    "description": "Current datetime of the conflicting entry (ISO-8601)",
                },
                "new_scheduled_at": {
                    "type": "string",
                    "description": "New datetime to move the entry to (ISO-8601)",
                },
            },
            "required": [
                "pet_name",
                "task_type",
                "current_scheduled_at",
                "new_scheduled_at",
            ],
        },
    },
]

# ── System prompt ─────────────────────────────────────────────────────────────

SYSTEM_PROMPT = """\
You are the PawPal+ AI Care Planner, an expert at building realistic daily pet care schedules.
Your job is to help the owner by planning, scheduling, verifying, and correcting a care plan.

## Required workflow — follow this order every time

1. **Discover** — Call `list_pets` and `list_scheduled_entries` to understand what exists.
2. **Plan** — Decide which care tasks are appropriate for each pet's species and the user's request.
3. **Act** — Call `add_and_schedule_task` for every task. Space tasks sensibly:
   - Morning tasks: 07:00–09:00
   - Midday tasks: 11:00–13:00
   - Afternoon tasks: 15:00–17:00
   - Evening tasks: 18:00–20:00
4. **Check** — Call `check_conflicts` after adding all tasks.
5. **Fix** — If any conflicts exist, call `reschedule_entry` to shift each conflicting task \
   by 30–60 minutes. Then call `check_conflicts` again to confirm zero conflicts.
6. **Report** — Respond in plain language: summarise every task you scheduled, explain why \
   each is appropriate for that pet species, and note any issues you resolved.

## Rules
- Only schedule tasks for pets that already exist (confirmed by `list_pets`).
- Do not add a duplicate task of the same type for the same pet at the exact same time.
- Always end with zero conflicts — verify with `check_conflicts` before finishing.
- If a tool returns an error, acknowledge it and take a corrective action before proceeding.
- Be specific in task descriptions; avoid generic placeholders.
"""

# ── Agent ─────────────────────────────────────────────────────────────────────

MAX_ITERATIONS = 12


class PawPalAgent:
    """Agentic care planner: plan → act → check → fix using the live Scheduler."""

    def __init__(self, owner: Owner, scheduler: Scheduler) -> None:
        self.owner = owner
        self.scheduler = scheduler
        self.steps: list[AgentStep] = []

        api_key = os.environ.get("ANTHROPIC_API_KEY")
        if not api_key:
            raise EnvironmentError(
                "ANTHROPIC_API_KEY is not set. "
                "Add it to a .env file or set it as an environment variable."
            )
        self.client = anthropic.Anthropic(api_key=api_key)

    # ── Internal logging ──────────────────────────────────────────────────────

    def _step(self, kind: str, label: str, detail: str, ok: bool = True) -> None:
        self.steps.append(AgentStep(kind=kind, label=label, detail=detail, ok=ok))
        level = logging.INFO if ok else logging.WARNING
        _logger.log(level, "[%s] %s | %s", kind.upper(), label, detail[:300])

    # ── Tool implementations ──────────────────────────────────────────────────

    def _tool_list_pets(self) -> str:
        pets = self.owner.get_pets()
        if not pets:
            return "No pets found. The owner has not added any pets yet."
        return json.dumps(
            [
                {
                    "name": p.name,
                    "species": p.species,
                    "existing_task_count": len(p.get_tasks()),
                }
                for p in pets
            ]
        )

    def _tool_list_scheduled_entries(self) -> str:
        entries = self.scheduler.view_scheduler()
        if not entries:
            return "No scheduled entries yet."
        return json.dumps(
            [
                {
                    "pet": e[0].name,
                    "task_type": e[1].type,
                    "description": e[1].description,
                    "frequency": e[1].frequency,
                    "scheduled_at": e[2].strftime("%Y-%m-%dT%H:%M"),
                    "completed": e[1].completed,
                }
                for e in entries
            ]
        )

    def _tool_add_and_schedule_task(self, inputs: dict[str, str]) -> str:
        pet_name = inputs["pet_name"].strip()
        task_type = inputs["task_type"].strip()
        task_description = inputs["task_description"].strip()
        frequency = inputs["frequency"].strip()
        scheduled_at_str = inputs["scheduled_at"].strip()

        if not pet_name or not task_type or not task_description:
            return "Error: pet_name, task_type, and task_description must not be empty."

        pet = self.owner.get_pet_by_name(pet_name)
        if pet is None:
            known = [p.name for p in self.owner.get_pets()]
            return (
                f"Error: pet '{pet_name}' not found. "
                f"Known pets: {known}. Only schedule for existing pets."
            )

        try:
            scheduled_at = datetime.fromisoformat(scheduled_at_str)
        except ValueError:
            return (
                f"Error: invalid datetime '{scheduled_at_str}'. "
                "Use ISO-8601 format YYYY-MM-DDTHH:MM, e.g. 2026-04-28T08:00."
            )

        # Guard: skip exact duplicate (same pet + type + time)
        duplicate = next(
            (
                e
                for e in self.scheduler.entries
                if e[0] is pet
                and e[1].type.lower() == task_type.lower()
                and e[2] == scheduled_at
            ),
            None,
        )
        if duplicate:
            return (
                f"Skipped: {pet_name} already has '{task_type}' at {scheduled_at_str}. "
                "Choose a different time or task type."
            )

        task = Task(type=task_type, description=task_description, frequency=frequency)
        pet.add_task(task)
        entry, warning = self.scheduler.add_entry_with_warning(pet, task, scheduled_at)

        msg = f"Scheduled '{task_type}' for {pet_name} at {scheduled_at_str} ({frequency})."
        if warning:
            msg += f" Conflict warning: {warning}"
        return msg

    def _tool_check_conflicts(self) -> str:
        conflicts = self.scheduler.detect_time_conflicts()
        if not conflicts:
            return "No conflicts detected. Schedule is clean."
        rows = [
            {
                "time": c[0][2].strftime("%Y-%m-%dT%H:%M"),
                "entry_1": f"{c[0][0].name} — {c[0][1].type}",
                "entry_2": f"{c[1][0].name} — {c[1][1].type}",
            }
            for c in conflicts
        ]
        return f"{len(conflicts)} conflict(s) found:\n" + json.dumps(rows, indent=2)

    def _tool_reschedule_entry(self, inputs: dict[str, str]) -> str:
        pet_name = inputs["pet_name"].strip()
        task_type = inputs["task_type"].strip()
        current_str = inputs["current_scheduled_at"].strip()
        new_str = inputs["new_scheduled_at"].strip()

        pet = self.owner.get_pet_by_name(pet_name)
        if pet is None:
            return f"Error: pet '{pet_name}' not found."

        try:
            current_dt = datetime.fromisoformat(current_str)
            new_dt = datetime.fromisoformat(new_str)
        except ValueError as exc:
            return f"Error: invalid datetime — {exc}"

        match = next(
            (
                e
                for e in self.scheduler.entries
                if e[0] is pet
                and e[1].type.lower() == task_type.lower()
                and e[2] == current_dt
            ),
            None,
        )
        if match is None:
            return (
                f"Error: no entry found for {pet_name} / '{task_type}' at {current_str}. "
                "Check list_scheduled_entries to confirm the exact values."
            )

        self.scheduler.remove_entry(match)
        pet_obj, task_obj, _ = match
        self.scheduler.add_entry(pet_obj, task_obj, new_dt)
        return f"Moved {pet_name}'s '{task_type}' from {current_str} → {new_str}."

    # ── Tool dispatcher ───────────────────────────────────────────────────────

    def _execute_tool(self, tool_name: str, tool_input: dict[str, Any]) -> str:
        self._step("tool_call", tool_name, json.dumps(tool_input))
        try:
            if tool_name == "list_pets":
                result = self._tool_list_pets()
            elif tool_name == "list_scheduled_entries":
                result = self._tool_list_scheduled_entries()
            elif tool_name == "add_and_schedule_task":
                result = self._tool_add_and_schedule_task(tool_input)
            elif tool_name == "check_conflicts":
                result = self._tool_check_conflicts()
            elif tool_name == "reschedule_entry":
                result = self._tool_reschedule_entry(tool_input)
            else:
                result = f"Error: unknown tool '{tool_name}'."

            ok = not result.startswith("Error:")
            self._step("tool_result", tool_name, result, ok=ok)
            return result

        except Exception as exc:
            msg = f"Error: tool '{tool_name}' raised an unexpected exception: {exc}"
            _logger.exception("Unhandled exception in tool '%s'", tool_name)
            self._step("tool_result", tool_name, msg, ok=False)
            return msg

    # ── Agentic loop ──────────────────────────────────────────────────────────

    def run(self, user_request: str, plan_date: datetime | None = None) -> str:
        """Run the full plan → act → check → fix loop; return the final text response."""
        self.steps.clear()
        effective_date = plan_date or datetime.now()
        date_label = effective_date.strftime("%A, %Y-%m-%d")

        self._step("start", "Agent started", f"Request: {user_request!r} | Date: {date_label}")

        messages: list[dict[str, Any]] = [
            {
                "role": "user",
                "content": (
                    f"Today is {date_label}. Plan date: {effective_date.strftime('%Y-%m-%d')}.\n\n"
                    f"{user_request}"
                ),
            }
        ]

        final_text = ""

        for iteration in range(MAX_ITERATIONS):
            _logger.info("--- Iteration %d / %d ---", iteration + 1, MAX_ITERATIONS)

            try:
                response = self.client.messages.create(
                    model="claude-sonnet-4-6",
                    max_tokens=4096,
                    system=SYSTEM_PROMPT,
                    tools=TOOLS,
                    messages=messages,
                )
            except anthropic.APIError as exc:
                _logger.error("Claude API error: %s", exc)
                self._step("error", "API error", str(exc), ok=False)
                return f"The AI planner encountered an API error and could not complete the plan: {exc}"

            # Build assistant message content
            assistant_blocks: list[dict[str, Any]] = []
            tool_use_blocks: list[Any] = []

            for block in response.content:
                if block.type == "text":
                    final_text = block.text
                    assistant_blocks.append({"type": "text", "text": block.text})
                elif block.type == "tool_use":
                    tool_use_blocks.append(block)
                    assistant_blocks.append(
                        {
                            "type": "tool_use",
                            "id": block.id,
                            "name": block.name,
                            "input": block.input,
                        }
                    )

            messages.append({"role": "assistant", "content": assistant_blocks})

            # No tool calls → agent is done
            if not tool_use_blocks or response.stop_reason == "end_turn":
                _logger.info("Agent finished after %d iteration(s).", iteration + 1)
                break

            # Execute every tool call and collect results
            tool_results: list[dict[str, Any]] = []
            for block in tool_use_blocks:
                result_text = self._execute_tool(block.name, block.input)
                tool_results.append(
                    {
                        "type": "tool_result",
                        "tool_use_id": block.id,
                        "content": result_text,
                    }
                )
            messages.append({"role": "user", "content": tool_results})

        else:
            self._step(
                "error",
                "Max iterations reached",
                f"Agent stopped after {MAX_ITERATIONS} iterations without a clean finish.",
                ok=False,
            )

        self._step("final", "Agent finished", (final_text[:300] + "...") if len(final_text) > 300 else final_text)
        return final_text or "The AI planner completed its work. Check the updated schedule above."
