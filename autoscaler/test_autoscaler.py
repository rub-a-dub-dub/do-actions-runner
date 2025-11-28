#!/usr/bin/env python3
"""Tests for the GitHub Actions Runner Autoscaler."""

import os
import time
from unittest.mock import MagicMock, patch

import pytest

# Set environment variables before importing autoscaler
os.environ["GITHUB_TOKEN"] = "test-token"
os.environ["DO_API_TOKEN"] = "test-do-token"
os.environ["APP_ID"] = "test-app-id"
os.environ["OWNER"] = "test-owner"
os.environ["REPO"] = "test-repo"

import autoscaler
from autoscaler import ScalingState, validate_config


class TestScalingState:
    """Tests for ScalingState dataclass."""

    def test_default_values(self):
        """Should initialize with default values."""
        state = ScalingState()
        assert state.last_scale_up_time == 0
        assert state.last_scale_down_time == 0
        assert state.breach_history == []


class TestBreachScore:
    """Tests for breach score calculations."""

    def test_breach_score_zero_with_no_history(self):
        """Should return 0 when no breaches recorded."""
        state = ScalingState()
        assert autoscaler.calculate_breach_score(state, "up") == 0.0
        assert autoscaler.calculate_breach_score(state, "down") == 0.0

    def test_breach_score_recent_breaches(self):
        """Recent breaches should have high weight (close to 1)."""
        state = ScalingState()
        now = time.time()
        state.breach_history = [(now, "up")]

        with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 30):
            with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 3):
                score = autoscaler.calculate_breach_score(state, "up")
                # Recent breach should have weight close to 1.0
                assert score > 0.9

    def test_breach_score_decays_over_time(self):
        """Breach score should decay exponentially over time."""
        state = ScalingState()
        now = time.time()
        # Breach from 30 seconds ago (one half-life)
        state.breach_history = [(now - 30, "up")]

        with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 30):
            with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 3):
                score = autoscaler.calculate_breach_score(state, "up")
                # After one half-life, weight should be ~0.5
                assert 0.4 < score < 0.6

    def test_breach_score_zero_outside_window(self):
        """Breaches outside window should be pruned and not counted."""
        state = ScalingState()
        now = time.time()
        # Breach from 5 minutes ago (outside 3-minute window)
        state.breach_history = [(now - 300, "up")]

        with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 30):
            with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 3):
                score = autoscaler.calculate_breach_score(state, "up")
                assert score == 0.0
                # History should be pruned
                assert len(state.breach_history) == 0

    def test_breach_score_cumulative(self):
        """Multiple breaches should accumulate."""
        state = ScalingState()
        now = time.time()
        # Multiple recent breaches
        state.breach_history = [
            (now - 1, "up"),
            (now - 2, "up"),
            (now - 3, "up"),
        ]

        with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 30):
            with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 3):
                score = autoscaler.calculate_breach_score(state, "up")
                # 3 recent breaches should have score close to 3.0
                assert score > 2.5

    def test_breach_score_ignores_opposite_direction(self):
        """Should only count breaches for the requested direction."""
        state = ScalingState()
        now = time.time()
        state.breach_history = [
            (now - 1, "up"),
            (now - 2, "down"),
        ]

        with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 30):
            with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 3):
                up_score = autoscaler.calculate_breach_score(state, "up")
                down_score = autoscaler.calculate_breach_score(state, "down")
                # Each direction should only count its own breaches
                assert up_score < 1.5
                assert down_score < 1.5

    def test_record_breach(self):
        """Should append breach to history."""
        state = ScalingState()
        autoscaler.record_breach(state, "up")
        assert len(state.breach_history) == 1
        assert state.breach_history[0][1] == "up"


class TestThresholds:
    """Tests for threshold functions."""

    def test_should_scale_up_when_above_threshold(self):
        """Should return True when queued exceeds threshold."""
        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            # 2 instances * 1.5 = 3.0 threshold, 4 > 3.0
            assert autoscaler.should_scale_up(queued=4, current=2) is True
            # 2 instances * 1.5 = 3.0 threshold, 3 == 3.0 (not greater)
            assert autoscaler.should_scale_up(queued=3, current=2) is False
            # 2 instances * 1.5 = 3.0 threshold, 2 < 3.0
            assert autoscaler.should_scale_up(queued=2, current=2) is False

    def test_should_scale_down_when_below_threshold(self):
        """Should return True when queued is below threshold."""
        with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
            # 4 instances * 0.25 = 1.0 threshold, 0 < 1.0
            assert autoscaler.should_scale_down(queued=0, current=4) is True
            # 4 instances * 0.25 = 1.0 threshold, 1 == 1.0 (not less)
            assert autoscaler.should_scale_down(queued=1, current=4) is False
            # 4 instances * 0.25 = 1.0 threshold, 2 > 1.0
            assert autoscaler.should_scale_down(queued=2, current=4) is False


class TestCooldown:
    """Tests for cooldown logic."""

    def test_cooldown_inactive_when_never_scaled(self):
        """Should return False when no scaling has occurred."""
        state = ScalingState()
        assert autoscaler.is_cooldown_active(state, "up") is False
        assert autoscaler.is_cooldown_active(state, "down") is False

    def test_scale_up_cooldown_active(self):
        """Should block scale-up during its cooldown period."""
        state = ScalingState(last_scale_up_time=time.time())
        with patch.object(autoscaler, "SCALE_UP_COOLDOWN", 60):
            assert autoscaler.is_cooldown_active(state, "up") is True

    def test_scale_up_cooldown_expired(self):
        """Should allow scale-up after its cooldown period."""
        state = ScalingState(last_scale_up_time=time.time() - 120)  # 2 minutes ago
        with patch.object(autoscaler, "SCALE_UP_COOLDOWN", 60):
            assert autoscaler.is_cooldown_active(state, "up") is False

    def test_scale_down_cooldown_active(self):
        """Should block scale-down during its cooldown period."""
        state = ScalingState(last_scale_down_time=time.time())
        with patch.object(autoscaler, "SCALE_DOWN_COOLDOWN", 180):
            assert autoscaler.is_cooldown_active(state, "down") is True

    def test_scale_down_cooldown_expired(self):
        """Should allow scale-down after its cooldown period."""
        state = ScalingState(last_scale_down_time=time.time() - 200)  # 200 seconds ago
        with patch.object(autoscaler, "SCALE_DOWN_COOLDOWN", 180):
            assert autoscaler.is_cooldown_active(state, "down") is False

    def test_scale_up_allowed_during_down_cooldown(self):
        """Should allow scale-up even when scale-down is in cooldown."""
        state = ScalingState(
            last_scale_down_time=time.time(),  # Just scaled down
            last_scale_up_time=0,  # Never scaled up
        )
        with patch.object(autoscaler, "SCALE_UP_COOLDOWN", 60):
            with patch.object(autoscaler, "SCALE_DOWN_COOLDOWN", 180):
                # Scale-down cooldown is active
                assert autoscaler.is_cooldown_active(state, "down") is True
                # But scale-up should be allowed
                assert autoscaler.is_cooldown_active(state, "up") is False

    def test_scale_down_allowed_during_up_cooldown(self):
        """Should allow scale-down even when scale-up is in cooldown."""
        state = ScalingState(
            last_scale_up_time=time.time(),  # Just scaled up
            last_scale_down_time=0,  # Never scaled down
        )
        with patch.object(autoscaler, "SCALE_UP_COOLDOWN", 60):
            with patch.object(autoscaler, "SCALE_DOWN_COOLDOWN", 180):
                # Scale-up cooldown is active
                assert autoscaler.is_cooldown_active(state, "up") is True
                # But scale-down should be allowed
                assert autoscaler.is_cooldown_active(state, "down") is False


class TestEvaluateScaling:
    """Tests for evaluate_scaling function."""

    def _create_state_with_breach_score(self, direction: str, score: float) -> ScalingState:
        """Helper to create a state with pre-populated breach history achieving target score."""
        state = ScalingState()
        now = time.time()
        # Add recent breaches to achieve desired score (each recent breach ~= 1.0)
        for i in range(int(score + 1)):
            state.breach_history.append((now - i * 0.1, direction))
        return state

    def test_scale_up_after_breach_threshold(self):
        """Should scale up by SCALE_UP_STEP after breach threshold is met."""
        # Pre-populate with enough breaches to be just under threshold
        state = self._create_state_with_breach_score("up", 1.5)

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "BREACH_THRESHOLD", 2.0):
                    with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 30):
                        with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 3):
                            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                                with patch.object(autoscaler, "SCALE_UP_STEP", 2):
                                    # queued=4, current=2: 4 > 2*1.5=3.0, should trigger scale-up
                                    action, new_count = autoscaler.evaluate_scaling(
                                        queued=4, current=2, state=state
                                    )

        assert action == "up"
        assert new_count == 4  # +2 from current (SCALE_UP_STEP=2)

    def test_scale_up_blocked_before_breach_threshold(self):
        """Should not scale up before breach threshold is met."""
        state = ScalingState()  # Empty breach history

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "BREACH_THRESHOLD", 2.0):
                    with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 30):
                        with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 3):
                            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                                action, new_count = autoscaler.evaluate_scaling(
                                    queued=4, current=2, state=state
                                )

        assert action == "none"
        assert new_count == 2
        # Breach should be recorded
        assert len(state.breach_history) == 1
        assert state.breach_history[0][1] == "up"

    def test_scale_up_blocked_by_cooldown(self):
        """Should not scale up during cooldown even if breach threshold met."""
        state = self._create_state_with_breach_score("up", 2.5)
        state.last_scale_up_time = time.time()  # Just scaled up

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "BREACH_THRESHOLD", 2.0):
                    with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 30):
                        with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 3):
                            with patch.object(autoscaler, "SCALE_UP_COOLDOWN", 60):
                                with patch.object(autoscaler, "MAX_INSTANCES", 5):
                                    action, new_count = autoscaler.evaluate_scaling(
                                        queued=4, current=2, state=state
                                    )

        assert action == "none"
        assert new_count == 2

    def test_scale_down_after_breach_threshold(self):
        """Should scale down by SCALE_DOWN_STEP after breach threshold is met."""
        state = self._create_state_with_breach_score("down", 1.5)

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "BREACH_THRESHOLD", 2.0):
                    with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 30):
                        with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 3):
                            with patch.object(autoscaler, "MIN_INSTANCES", 1):
                                with patch.object(autoscaler, "SCALE_DOWN_STEP", 1):
                                    # queued=0, current=4: 0 < 4*0.25=1.0, should trigger scale-down
                                    action, new_count = autoscaler.evaluate_scaling(
                                        queued=0, current=4, state=state
                                    )

        assert action == "down"
        assert new_count == 3  # -1 from current (SCALE_DOWN_STEP=1)

    def test_scale_down_blocked_by_cooldown(self):
        """Should not scale down during cooldown even if breach threshold met."""
        state = self._create_state_with_breach_score("down", 2.5)
        state.last_scale_down_time = time.time()  # Just scaled down

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "BREACH_THRESHOLD", 2.0):
                    with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 30):
                        with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 3):
                            with patch.object(autoscaler, "SCALE_DOWN_COOLDOWN", 180):
                                with patch.object(autoscaler, "MIN_INSTANCES", 1):
                                    action, new_count = autoscaler.evaluate_scaling(
                                        queued=0, current=4, state=state
                                    )

        assert action == "none"
        assert new_count == 4

    def test_no_scaling_when_stable(self):
        """Should not scale when queue is within thresholds."""
        state = ScalingState()

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "BREACH_THRESHOLD", 2.0):
                    with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 30):
                        with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 3):
                            # queued=1, current=2: 1 < 3.0 (no scale-up), 1 > 0.5 (no scale-down)
                            action, new_count = autoscaler.evaluate_scaling(
                                queued=1, current=2, state=state
                            )

        assert action == "none"
        assert new_count == 2
        # No breaches recorded when stable
        assert len(state.breach_history) == 0

    def test_respects_max_instances(self):
        """Should not scale above MAX_INSTANCES."""
        state = self._create_state_with_breach_score("up", 2.5)

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "BREACH_THRESHOLD", 2.0):
                    with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 30):
                        with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 3):
                            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                                action, new_count = autoscaler.evaluate_scaling(
                                    queued=10, current=5, state=state
                                )

        assert action == "none"
        assert new_count == 5

    def test_respects_min_instances(self):
        """Should not scale below MIN_INSTANCES."""
        state = self._create_state_with_breach_score("down", 2.5)

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "BREACH_THRESHOLD", 2.0):
                    with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 30):
                        with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 3):
                            with patch.object(autoscaler, "MIN_INSTANCES", 1):
                                action, new_count = autoscaler.evaluate_scaling(
                                    queued=0, current=1, state=state
                                )

        assert action == "none"
        assert new_count == 1

    def test_scale_up_records_breach(self):
        """Should record breach when scale-up condition met."""
        state = ScalingState()

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "BREACH_THRESHOLD", 2.0):
                    with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 30):
                        with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 3):
                            autoscaler.evaluate_scaling(queued=4, current=2, state=state)

        assert len(state.breach_history) == 1
        assert state.breach_history[0][1] == "up"

    def test_scale_up_by_step_respects_max(self):
        """Should not scale above MAX_INSTANCES even with step > 1."""
        state = self._create_state_with_breach_score("up", 2.5)

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "BREACH_THRESHOLD", 2.0):
                    with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 30):
                        with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 3):
                            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                                with patch.object(autoscaler, "SCALE_UP_STEP", 2):
                                    # current=4, step=2, max=5 -> should go to 5, not 6
                                    action, new_count = autoscaler.evaluate_scaling(
                                        queued=10, current=4, state=state
                                    )

        assert action == "up"
        assert new_count == 5  # Capped at MAX_INSTANCES

    def test_scale_down_by_larger_step(self):
        """Should scale down by SCALE_DOWN_STEP when configured."""
        state = self._create_state_with_breach_score("down", 2.5)

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "BREACH_THRESHOLD", 2.0):
                    with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 30):
                        with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 3):
                            with patch.object(autoscaler, "MIN_INSTANCES", 1):
                                with patch.object(autoscaler, "SCALE_DOWN_STEP", 2):
                                    # current=5, step=2 -> should go to 3
                                    action, new_count = autoscaler.evaluate_scaling(
                                        queued=0, current=5, state=state
                                    )

        assert action == "down"
        assert new_count == 3  # 5 - 2 = 3

    def test_scale_down_by_step_respects_min(self):
        """Should not scale below MIN_INSTANCES even with step > 1."""
        state = self._create_state_with_breach_score("down", 2.5)

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "BREACH_THRESHOLD", 2.0):
                    with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 30):
                        with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 3):
                            with patch.object(autoscaler, "MIN_INSTANCES", 2):
                                with patch.object(autoscaler, "SCALE_DOWN_STEP", 3):
                                    # current=4, step=3, min=2 -> should go to 2, not 1
                                    action, new_count = autoscaler.evaluate_scaling(
                                        queued=0, current=4, state=state
                                    )

        assert action == "down"
        assert new_count == 2  # Capped at MIN_INSTANCES


class TestGetQueuedJobs:
    """Tests for get_queued_jobs function."""

    @patch("autoscaler.requests.get")
    def test_counts_queued_jobs_not_runs(self, mock_get):
        """Should count actual queued jobs, not workflow runs."""
        # Mock responses for runs (queued), runs (in_progress), and jobs
        def mock_get_side_effect(url, headers=None):
            mock_resp = MagicMock()
            if "status=queued" in url and "actions/runs" in url:
                # One queued run
                mock_resp.json.return_value = {
                    "workflow_runs": [{"id": 123}]
                }
            elif "status=in_progress" in url and "actions/runs" in url:
                # One in-progress run
                mock_resp.json.return_value = {
                    "workflow_runs": [{"id": 456}]
                }
            elif "runs/123/jobs" in url:
                # Run 123 has 2 queued jobs
                mock_resp.json.return_value = {
                    "jobs": [
                        {"status": "queued"},
                        {"status": "queued"},
                    ]
                }
            elif "runs/456/jobs" in url:
                # Run 456 has 1 queued job and 1 in_progress
                mock_resp.json.return_value = {
                    "jobs": [
                        {"status": "queued"},
                        {"status": "in_progress"},
                    ]
                }
            else:
                mock_resp.json.return_value = {}
            return mock_resp

        mock_get.side_effect = mock_get_side_effect

        with patch.object(autoscaler, "ORG", None):
            with patch.object(autoscaler, "OWNER", "test-owner"):
                with patch.object(autoscaler, "REPO", "test-repo"):
                    result = autoscaler.get_queued_jobs()

        # 2 queued from run 123 + 1 queued from run 456 = 3
        assert result == 3

    @patch("autoscaler.requests.get")
    def test_org_level_api_call(self, mock_get):
        """Should call org-level API when ORG is set."""
        def mock_get_side_effect(url, headers=None):
            mock_resp = MagicMock()
            if "orgs/test-org/actions/runs" in url:
                if "status=queued" in url:
                    mock_resp.json.return_value = {
                        "workflow_runs": [
                            {"id": 789, "repository": {"full_name": "test-org/repo1"}}
                        ]
                    }
                else:
                    mock_resp.json.return_value = {"workflow_runs": []}
            elif "runs/789/jobs" in url:
                mock_resp.json.return_value = {
                    "jobs": [{"status": "queued"}]
                }
            else:
                mock_resp.json.return_value = {}
            return mock_resp

        mock_get.side_effect = mock_get_side_effect

        with patch.object(autoscaler, "ORG", "test-org"):
            result = autoscaler.get_queued_jobs()

        assert result == 1
        # Verify org-level runs API was called
        calls = [str(c) for c in mock_get.call_args_list]
        assert any("orgs/test-org/actions/runs" in c for c in calls)


class TestGetCurrentInstanceCount:
    """Tests for get_current_instance_count function."""

    @patch("autoscaler.requests.get")
    def test_returns_worker_instance_count(self, mock_get):
        """Should return instance count for the target worker."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "app": {
                "spec": {
                    "workers": [
                        {"name": "runner", "instance_count": 3},
                        {"name": "autoscaler", "instance_count": 1},
                    ]
                }
            }
        }
        mock_get.return_value = mock_response

        with patch.object(autoscaler, "WORKER_NAME", "runner"):
            result = autoscaler.get_current_instance_count()

        assert result == 3

    @patch("autoscaler.requests.get")
    def test_returns_default_when_worker_not_found(self, mock_get):
        """Should return 1 when worker is not found."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "app": {"spec": {"workers": [{"name": "other", "instance_count": 2}]}}
        }
        mock_get.return_value = mock_response

        with patch.object(autoscaler, "WORKER_NAME", "runner"):
            result = autoscaler.get_current_instance_count()

        assert result == 1


class TestScaleWorker:
    """Tests for scale_worker function."""

    @patch("autoscaler.requests.put")
    @patch("autoscaler.requests.get")
    def test_updates_instance_count(self, mock_get, mock_put):
        """Should update the worker instance count via PUT and return True."""
        # First GET for initial spec, second GET for verification
        mock_get_response = MagicMock()
        mock_get_response.json.return_value = {
            "app": {
                "spec": {
                    "name": "test-app",
                    "workers": [
                        {"name": "runner", "instance_count": 1},
                    ],
                }
            }
        }
        # After PUT, verification returns the updated count
        mock_verify_response = MagicMock()
        mock_verify_response.json.return_value = {
            "app": {
                "spec": {
                    "name": "test-app",
                    "workers": [
                        {"name": "runner", "instance_count": 3},
                    ],
                }
            }
        }
        mock_get.side_effect = [mock_get_response, mock_verify_response]

        mock_put_response = MagicMock()
        mock_put.return_value = mock_put_response

        with patch.object(autoscaler, "WORKER_NAME", "runner"):
            result = autoscaler.scale_worker(3)

        assert result is True
        mock_put.assert_called_once()
        call_json = mock_put.call_args[1]["json"]
        assert call_json["spec"]["workers"][0]["instance_count"] == 3

    @patch("autoscaler.requests.put")
    @patch("autoscaler.requests.get")
    def test_does_not_update_if_worker_not_found(self, mock_get, mock_put):
        """Should not call PUT if worker is not found and return False."""
        mock_get_response = MagicMock()
        mock_get_response.json.return_value = {
            "app": {
                "spec": {
                    "name": "test-app",
                    "workers": [{"name": "other", "instance_count": 1}],
                }
            }
        }
        mock_get.return_value = mock_get_response

        with patch.object(autoscaler, "WORKER_NAME", "runner"):
            result = autoscaler.scale_worker(3)

        assert result is False
        mock_put.assert_not_called()

    @patch("autoscaler.requests.put")
    @patch("autoscaler.requests.get")
    def test_returns_false_on_spec_conflict(self, mock_get, mock_put):
        """Should return False when verification shows different count (conflict)."""
        # First GET for initial spec
        mock_get_response = MagicMock()
        mock_get_response.json.return_value = {
            "app": {
                "spec": {
                    "name": "test-app",
                    "workers": [
                        {"name": "runner", "instance_count": 1},
                    ],
                }
            }
        }
        # After PUT, verification shows different count (concurrent modification)
        mock_verify_response = MagicMock()
        mock_verify_response.json.return_value = {
            "app": {
                "spec": {
                    "name": "test-app",
                    "workers": [
                        {"name": "runner", "instance_count": 2},  # Someone else changed it
                    ],
                }
            }
        }
        mock_get.side_effect = [mock_get_response, mock_verify_response]

        mock_put_response = MagicMock()
        mock_put.return_value = mock_put_response

        with patch.object(autoscaler, "WORKER_NAME", "runner"):
            result = autoscaler.scale_worker(3)

        assert result is False
        mock_put.assert_called_once()  # PUT was called, but verification failed


class TestGetRunners:
    """Tests for get_runners function."""

    @patch("autoscaler.requests.get")
    def test_returns_runners_list(self, mock_get):
        """Should return list of runners."""
        mock_response = MagicMock()
        mock_response.json.return_value = {
            "total_count": 2,
            "runners": [
                {"id": 1, "name": "runner-1", "status": "online", "busy": False},
                {"id": 2, "name": "runner-2", "status": "offline", "busy": False},
            ],
        }
        mock_get.return_value = mock_response

        with patch.object(autoscaler, "ORG", None):
            with patch.object(autoscaler, "OWNER", "test-owner"):
                with patch.object(autoscaler, "REPO", "test-repo"):
                    result = autoscaler.get_runners()

        assert len(result) == 2
        assert result[0]["name"] == "runner-1"

    @patch("autoscaler.requests.get")
    def test_org_level_api_call(self, mock_get):
        """Should call org-level API when ORG is set."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"runners": []}
        mock_get.return_value = mock_response

        with patch.object(autoscaler, "ORG", "test-org"):
            autoscaler.get_runners()

        call_url = mock_get.call_args[0][0]
        assert "orgs/test-org/actions/runners" in call_url


class TestDeleteRunner:
    """Tests for delete_runner function."""

    @patch("autoscaler.requests.delete")
    def test_returns_true_on_success(self, mock_delete):
        """Should return True when deletion succeeds."""
        mock_response = MagicMock()
        mock_response.status_code = 204
        mock_delete.return_value = mock_response

        with patch.object(autoscaler, "ORG", None):
            with patch.object(autoscaler, "OWNER", "test-owner"):
                with patch.object(autoscaler, "REPO", "test-repo"):
                    result = autoscaler.delete_runner(123)

        assert result is True

    @patch("autoscaler.requests.delete")
    def test_returns_false_on_failure(self, mock_delete):
        """Should return False when deletion fails."""
        mock_response = MagicMock()
        mock_response.status_code = 404
        mock_delete.return_value = mock_response

        with patch.object(autoscaler, "ORG", None):
            with patch.object(autoscaler, "OWNER", "test-owner"):
                with patch.object(autoscaler, "REPO", "test-repo"):
                    result = autoscaler.delete_runner(123)

        assert result is False


class TestCleanupDeadRunners:
    """Tests for cleanup_dead_runners function."""

    @patch("autoscaler.delete_runner")
    @patch("autoscaler.get_runners")
    def test_deletes_offline_runners(self, mock_get_runners, mock_delete):
        """Should delete offline, non-busy runners."""
        mock_get_runners.return_value = [
            {"id": 1, "name": "runner-1", "status": "online", "busy": False},
            {"id": 2, "name": "runner-2", "status": "offline", "busy": False},
            {"id": 3, "name": "runner-3", "status": "offline", "busy": True},
        ]
        mock_delete.return_value = True

        result = autoscaler.cleanup_dead_runners()

        assert result == 1
        mock_delete.assert_called_once_with(2)

    @patch("autoscaler.delete_runner")
    @patch("autoscaler.get_runners")
    def test_skips_busy_runners(self, mock_get_runners, mock_delete):
        """Should not delete busy runners even if offline."""
        mock_get_runners.return_value = [
            {"id": 1, "name": "runner-1", "status": "offline", "busy": True},
        ]

        result = autoscaler.cleanup_dead_runners()

        assert result == 0
        mock_delete.assert_not_called()

    @patch("autoscaler.delete_runner")
    @patch("autoscaler.get_runners")
    def test_skips_online_runners(self, mock_get_runners, mock_delete):
        """Should not delete online runners."""
        mock_get_runners.return_value = [
            {"id": 1, "name": "runner-1", "status": "online", "busy": False},
        ]

        result = autoscaler.cleanup_dead_runners()

        assert result == 0
        mock_delete.assert_not_called()

    @patch("autoscaler.get_runners")
    def test_handles_api_error(self, mock_get_runners):
        """Should return 0 and not crash on API error."""
        mock_get_runners.side_effect = autoscaler.requests.RequestException("API Error")

        result = autoscaler.cleanup_dead_runners()

        assert result == 0


class TestValidateConfig:
    """Tests for validate_config function."""

    def test_validate_config_passes_valid_config(self):
        """Should pass with valid configuration."""
        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "SCALE_UP_COOLDOWN", 60):
                    with patch.object(autoscaler, "SCALE_DOWN_COOLDOWN", 180):
                        with patch.object(autoscaler, "POLL_INTERVAL", 60):
                            with patch.object(autoscaler, "SCALE_UP_STEP", 2):
                                with patch.object(autoscaler, "SCALE_DOWN_STEP", 1):
                                    with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
                                        with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                                            with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 3):
                                                with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 30):
                                                    with patch.object(autoscaler, "BREACH_THRESHOLD", 2.0):
                                                        # Should not raise
                                                        validate_config()

    def test_validate_config_fails_min_greater_than_max(self):
        """Should exit when MIN_INSTANCES > MAX_INSTANCES."""
        with patch.object(autoscaler, "MIN_INSTANCES", 10):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with pytest.raises(SystemExit):
                    validate_config()

    def test_validate_config_fails_negative_min_instances(self):
        """Should exit when MIN_INSTANCES < 0."""
        with patch.object(autoscaler, "MIN_INSTANCES", -1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with pytest.raises(SystemExit):
                    validate_config()

    def test_validate_config_fails_negative_cooldown(self):
        """Should exit when cooldown is negative."""
        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "SCALE_UP_COOLDOWN", -1):
                    with pytest.raises(SystemExit):
                        validate_config()

    def test_validate_config_fails_zero_poll_interval(self):
        """Should exit when POLL_INTERVAL <= 0."""
        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "SCALE_UP_COOLDOWN", 60):
                    with patch.object(autoscaler, "SCALE_DOWN_COOLDOWN", 180):
                        with patch.object(autoscaler, "POLL_INTERVAL", 0):
                            with pytest.raises(SystemExit):
                                validate_config()

    def test_validate_config_fails_zero_step_size(self):
        """Should exit when step size < 1."""
        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "SCALE_UP_COOLDOWN", 60):
                    with patch.object(autoscaler, "SCALE_DOWN_COOLDOWN", 180):
                        with patch.object(autoscaler, "POLL_INTERVAL", 60):
                            with patch.object(autoscaler, "SCALE_UP_STEP", 0):
                                with pytest.raises(SystemExit):
                                    validate_config()

    def test_validate_config_fails_zero_scale_up_threshold(self):
        """Should exit when SCALE_UP_THRESHOLD <= 0."""
        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "SCALE_UP_COOLDOWN", 60):
                    with patch.object(autoscaler, "SCALE_DOWN_COOLDOWN", 180):
                        with patch.object(autoscaler, "POLL_INTERVAL", 60):
                            with patch.object(autoscaler, "SCALE_UP_STEP", 2):
                                with patch.object(autoscaler, "SCALE_DOWN_STEP", 1):
                                    with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 0):
                                        with pytest.raises(SystemExit):
                                            validate_config()

    def test_validate_config_fails_negative_scale_down_threshold(self):
        """Should exit when SCALE_DOWN_THRESHOLD < 0."""
        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "SCALE_UP_COOLDOWN", 60):
                    with patch.object(autoscaler, "SCALE_DOWN_COOLDOWN", 180):
                        with patch.object(autoscaler, "POLL_INTERVAL", 60):
                            with patch.object(autoscaler, "SCALE_UP_STEP", 2):
                                with patch.object(autoscaler, "SCALE_DOWN_STEP", 1):
                                    with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
                                        with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", -1):
                                            with pytest.raises(SystemExit):
                                                validate_config()

    def test_validate_config_fails_zero_stabilization_window(self):
        """Should exit when STABILIZATION_WINDOW_MINUTES <= 0."""
        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "STABILIZATION_WINDOW_MINUTES", 0):
                    with pytest.raises(SystemExit):
                        validate_config()

    def test_validate_config_fails_zero_decay_half_life(self):
        """Should exit when DECAY_HALF_LIFE_SECONDS <= 0."""
        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "DECAY_HALF_LIFE_SECONDS", 0):
                    with pytest.raises(SystemExit):
                        validate_config()

    def test_validate_config_fails_zero_breach_threshold(self):
        """Should exit when BREACH_THRESHOLD <= 0."""
        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "BREACH_THRESHOLD", 0):
                    with pytest.raises(SystemExit):
                        validate_config()


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
