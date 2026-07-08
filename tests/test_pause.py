"""Unit tests for Claire's pause/resume + quiet hours — pure logic, no network."""

from datetime import datetime

from config import settings
from agents.claire_agent import (
    ClaireAgent,
    _in_quiet_hours,
    _match_pause_command,
    _quiet_hours_end,
)


def _t(hour, minute=0):
    return datetime(2026, 7, 7, hour, minute)


class TestQuietHoursWindow:
    def test_spanning_midnight(self):
        for hour, expected in [(20, False), (21, True), (23, True), (0, True),
                               (6, True), (7, False), (12, False)]:
            assert _in_quiet_hours(_t(hour), "21:00", "07:00") == expected, hour

    def test_non_spanning(self):
        assert _in_quiet_hours(_t(14), "13:00", "15:00")
        assert not _in_quiet_hours(_t(12), "13:00", "15:00")
        assert not _in_quiet_hours(_t(15), "13:00", "15:00")

    def test_edges(self):
        # Start is inclusive, end is exclusive.
        assert _in_quiet_hours(_t(21, 0), "21:00", "07:00")
        assert not _in_quiet_hours(_t(7, 0), "21:00", "07:00")

    def test_degenerate_window_never_active(self):
        assert not _in_quiet_hours(_t(21), "21:00", "21:00")

    def test_quiet_hours_end_same_day(self):
        assert _quiet_hours_end(_t(6), "07:00") == _t(7)

    def test_quiet_hours_end_crosses_midnight(self):
        assert _quiet_hours_end(_t(22), "07:00") == datetime(2026, 7, 8, 7, 0)


class TestMatchPauseCommand:
    def test_pause_words(self):
        for text in ("pause", "Pause", "PAUSE!", "good night", "Goodnight.", "sleep"):
            assert _match_pause_command(text) == "pause", text

    def test_resume_words(self):
        for text in ("resume", "Resume!", "wake up", "Good Morning", "unpause"):
            assert _match_pause_command(text) == "resume", text

    def test_exact_match_only(self):
        # Containment must NOT trigger — ordinary sentences stay commands-free.
        for text in ("don't pause", "pause the notifications", "let's resume later",
                     "I slept well", "", "got it"):
            assert _match_pause_command(text) is None, text


class TestComputePaused:
    def test_manual_wins(self, monkeypatch):
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_ENABLED", False)
        assert ClaireAgent._compute_paused({"pause": {"manual": True}}) == (True, "manual")

    def test_active_by_default(self, monkeypatch):
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_ENABLED", False)
        assert ClaireAgent._compute_paused({}) == (False, "")
        assert ClaireAgent._compute_paused({"pause": {"manual": False}}) == (False, "")

    def test_quiet_hours(self, monkeypatch):
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_ENABLED", True)
        # All-day window guarantees "now" is inside it.
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_START", "00:00")
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_END", "23:59")
        assert ClaireAgent._compute_paused({}) == (True, "quiet_hours")

    def test_override_unpauses_quiet_hours(self, monkeypatch):
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_ENABLED", True)
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_START", "00:00")
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_END", "23:59")
        state = {"pause": {"manual": False, "override_until": "2099-01-01T00:00:00"}}
        assert ClaireAgent._compute_paused(state) == (False, "")

    def test_expired_override_re_pauses(self, monkeypatch):
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_ENABLED", True)
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_START", "00:00")
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_END", "23:59")
        state = {"pause": {"manual": False, "override_until": "2020-01-01T00:00:00"}}
        assert ClaireAgent._compute_paused(state) == (True, "quiet_hours")


class TestApplyPauseResume:
    def test_apply_pause(self):
        state = {}
        ClaireAgent._apply_pause(state)
        assert state["pause"]["manual"] is True
        assert state["pause"]["announced"] == "quiet"

    def test_apply_resume_outside_quiet_hours(self, monkeypatch):
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_ENABLED", False)
        state = {"pause": {"manual": True, "override_until": "", "announced": "quiet"}}
        ClaireAgent._apply_resume(state)
        assert state["pause"]["manual"] is False
        assert state["pause"]["announced"] == "active"
        assert state["pause"]["override_until"] == ""

    def test_apply_resume_inside_quiet_hours_sets_override(self, monkeypatch):
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_ENABLED", True)
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_START", "00:00")
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_END", "23:59")
        state = {}
        ClaireAgent._apply_resume(state)
        assert state["pause"]["override_until"] != ""
        # And the override must actually unpause.
        assert ClaireAgent._compute_paused(state) == (False, "")


def _bare_agent(state):
    """ClaireAgent without __init__ (no Google clients), with stubbed I/O."""
    agent = ClaireAgent.__new__(ClaireAgent)
    agent._load_state = lambda: state
    agent._save_state = lambda s: None
    agent.sent = []
    agent._send_tracked = lambda text: agent.sent.append(text)
    agent.calls = []
    agent._poll_chat_replies = lambda: agent.calls.append("poll") or 0
    agent._sync_pending_state = lambda: agent.calls.append("sync") or 0
    agent._expire_stale_entries = lambda: agent.calls.append("expire") or 0
    agent._send_nudges = lambda: agent.calls.append("nudge") or 0
    agent._scan_new_emails = lambda: agent.calls.append("scan") or 0
    return agent


class TestRunCycleGate:
    def test_paused_polls_chat_only(self, monkeypatch):
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_ENABLED", False)
        agent = _bare_agent({"pause": {"manual": True, "announced": "quiet"}})
        summary = agent.run_cycle()
        assert summary["paused"] == "manual"
        assert agent.calls == ["poll"]

    def test_active_runs_full_cycle(self, monkeypatch):
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_ENABLED", False)
        agent = _bare_agent({"pause": {"manual": False, "announced": "active"}})
        summary = agent.run_cycle()
        assert summary["paused"] == ""
        assert agent.calls == ["sync", "expire", "nudge", "poll", "scan"]

    def test_quiet_hours_entry_announced_once(self, monkeypatch):
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_ENABLED", True)
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_START", "00:00")
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_END", "23:59")
        state = {"pause": {"manual": False, "override_until": "", "announced": "active"}}
        agent = _bare_agent(state)
        agent.run_cycle()
        assert len(agent.sent) == 1 and "Quiet hours" in agent.sent[0]
        agent.run_cycle()  # second cycle: already announced, stay silent
        assert len(agent.sent) == 1

    def test_morning_auto_resume_announced(self, monkeypatch):
        monkeypatch.setattr(settings, "CLAIRE_QUIET_HOURS_ENABLED", False)
        state = {"pause": {"manual": False, "override_until": "", "announced": "quiet"}}
        agent = _bare_agent(state)
        agent.run_cycle()
        assert any("resuming" in t.lower() for t in agent.sent)
        assert "scan" in agent.calls
