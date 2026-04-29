"""Unit tests for PawPalAgent tool methods.

These tests call the tool implementations directly — no Claude API calls are made.
A fake API key is injected so the Anthropic client constructs without error;
it is never used because no messages.create() call is triggered.
"""

import json
import os
from datetime import datetime

import pytest

os.environ.setdefault("ANTHROPIC_API_KEY", "sk-ant-test-fake-key-for-unit-tests")

from agent import PawPalAgent  # noqa: E402  (import after env var set)
from pawpal_system import Owner, Pet, Scheduler, Task  # noqa: E402


# ── Shared fixture ────────────────────────────────────────────────────────────


@pytest.fixture
def agent():
    """Agent with one owner and one dog (Luna). Fresh state for each test."""
    owner = Owner(name="Jordan")
    luna = Pet(name="Luna", species="dog")
    owner.add_pet(luna)
    scheduler = Scheduler(owner=owner)
    return PawPalAgent(owner=owner, scheduler=scheduler)


# ── list_pets ─────────────────────────────────────────────────────────────────


def test_list_pets_returns_correct_name_and_species(agent):
    result = json.loads(agent._tool_list_pets())
    assert len(result) == 1
    assert result[0]["name"] == "Luna"
    assert result[0]["species"] == "dog"


def test_list_pets_empty_owner_returns_message():
    owner = Owner(name="Empty")
    a = PawPalAgent(owner=owner, scheduler=Scheduler(owner=owner))
    assert "No pets found" in a._tool_list_pets()


# ── add_and_schedule_task ─────────────────────────────────────────────────────


def test_add_and_schedule_task_creates_entry(agent):
    result = agent._tool_add_and_schedule_task({
        "pet_name": "Luna",
        "task_type": "Feeding",
        "task_description": "Morning meal",
        "frequency": "Daily",
        "scheduled_at": "2026-04-28T08:00",
    })
    assert "Scheduled" in result
    assert len(agent.scheduler.entries) == 1
    entry = agent.scheduler.entries[0]
    assert entry[0].name == "Luna"
    assert entry[1].type == "Feeding"
    assert entry[2] == datetime(2026, 4, 28, 8, 0)


def test_add_and_schedule_task_unknown_pet_returns_error(agent):
    result = agent._tool_add_and_schedule_task({
        "pet_name": "Ghost",
        "task_type": "Feeding",
        "task_description": "Meal",
        "frequency": "Daily",
        "scheduled_at": "2026-04-28T08:00",
    })
    assert result.startswith("Error:")
    assert "Ghost" in result
    assert len(agent.scheduler.entries) == 0


def test_add_and_schedule_task_invalid_datetime_returns_error(agent):
    result = agent._tool_add_and_schedule_task({
        "pet_name": "Luna",
        "task_type": "Walk",
        "task_description": "Morning walk",
        "frequency": "Daily",
        "scheduled_at": "not-a-date",
    })
    assert result.startswith("Error:")
    assert "datetime" in result.lower()
    assert len(agent.scheduler.entries) == 0


def test_add_and_schedule_task_duplicate_is_skipped(agent):
    inputs = {
        "pet_name": "Luna",
        "task_type": "Feeding",
        "task_description": "Morning meal",
        "frequency": "Daily",
        "scheduled_at": "2026-04-28T08:00",
    }
    agent._tool_add_and_schedule_task(inputs)
    result = agent._tool_add_and_schedule_task(inputs)
    assert "Skipped" in result
    assert len(agent.scheduler.entries) == 1  # still only one entry


# ── check_conflicts ───────────────────────────────────────────────────────────


def test_check_conflicts_clean_schedule(agent):
    agent._tool_add_and_schedule_task({
        "pet_name": "Luna", "task_type": "Feeding",
        "task_description": "Breakfast", "frequency": "Daily",
        "scheduled_at": "2026-04-28T07:00",
    })
    agent._tool_add_and_schedule_task({
        "pet_name": "Luna", "task_type": "Walking",
        "task_description": "Morning walk", "frequency": "Daily",
        "scheduled_at": "2026-04-28T08:00",
    })
    assert "No conflicts" in agent._tool_check_conflicts()


def test_check_conflicts_detects_same_time_entries(agent):
    mochi = Pet(name="Mochi", species="cat")
    agent.owner.add_pet(mochi)
    agent._tool_add_and_schedule_task({
        "pet_name": "Luna", "task_type": "Feeding",
        "task_description": "Breakfast", "frequency": "Daily",
        "scheduled_at": "2026-04-28T07:00",
    })
    agent._tool_add_and_schedule_task({
        "pet_name": "Mochi", "task_type": "Feeding",
        "task_description": "Cat breakfast", "frequency": "Daily",
        "scheduled_at": "2026-04-28T07:00",
    })
    result = agent._tool_check_conflicts()
    assert "conflict" in result.lower()
    assert "2026-04-28T07:00" in result


# ── reschedule_entry ──────────────────────────────────────────────────────────


def test_reschedule_entry_moves_to_new_time(agent):
    agent._tool_add_and_schedule_task({
        "pet_name": "Luna", "task_type": "Feeding",
        "task_description": "Morning meal", "frequency": "Daily",
        "scheduled_at": "2026-04-28T07:00",
    })
    result = agent._tool_reschedule_entry({
        "pet_name": "Luna",
        "task_type": "Feeding",
        "current_scheduled_at": "2026-04-28T07:00",
        "new_scheduled_at": "2026-04-28T07:30",
    })
    assert "07:30" in result
    assert agent.scheduler.entries[0][2] == datetime(2026, 4, 28, 7, 30)


def test_reschedule_entry_not_found_returns_error(agent):
    result = agent._tool_reschedule_entry({
        "pet_name": "Luna",
        "task_type": "NonExistent",
        "current_scheduled_at": "2026-04-28T07:00",
        "new_scheduled_at": "2026-04-28T07:30",
    })
    assert result.startswith("Error:")


# ── Full tool chain: add → conflict → fix → verify ───────────────────────────


def test_full_conflict_resolution_chain(agent):
    """Simulates the agent's plan-act-check-fix loop without the LLM."""
    mochi = Pet(name="Mochi", species="cat")
    agent.owner.add_pet(mochi)

    # Act: both pets scheduled at the same time
    agent._tool_add_and_schedule_task({
        "pet_name": "Luna", "task_type": "Feeding",
        "task_description": "Breakfast", "frequency": "Daily",
        "scheduled_at": "2026-04-28T08:00",
    })
    agent._tool_add_and_schedule_task({
        "pet_name": "Mochi", "task_type": "Feeding",
        "task_description": "Cat breakfast", "frequency": "Daily",
        "scheduled_at": "2026-04-28T08:00",
    })

    # Check: conflict exists
    assert "conflict" in agent._tool_check_conflicts().lower()

    # Fix: shift Mochi's feeding by 30 minutes
    agent._tool_reschedule_entry({
        "pet_name": "Mochi",
        "task_type": "Feeding",
        "current_scheduled_at": "2026-04-28T08:00",
        "new_scheduled_at": "2026-04-28T08:30",
    })

    # Verify: schedule is now clean
    assert "No conflicts" in agent._tool_check_conflicts()
    times = sorted(e[2] for e in agent.scheduler.entries)
    assert times[0] == datetime(2026, 4, 28, 8, 0)
    assert times[1] == datetime(2026, 4, 28, 8, 30)
