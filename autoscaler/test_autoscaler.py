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
from autoscaler import ScalingState


class TestScalingState:
    """Tests for ScalingState dataclass."""

    def test_default_values(self):
        """Should initialize with default values."""
        state = ScalingState()
        assert state.last_scale_time == 0
        assert state.last_scale_direction == ""
        assert state.consecutive_scale_up_readings == 0
        assert state.consecutive_scale_down_readings == 0


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
        """Should block scale-up during cooldown period."""
        state = ScalingState(last_scale_time=time.time())
        with patch.object(autoscaler, "SCALE_UP_COOLDOWN", 60):
            assert autoscaler.is_cooldown_active(state, "up") is True

    def test_scale_up_cooldown_expired(self):
        """Should allow scale-up after cooldown period."""
        state = ScalingState(last_scale_time=time.time() - 120)  # 2 minutes ago
        with patch.object(autoscaler, "SCALE_UP_COOLDOWN", 60):
            assert autoscaler.is_cooldown_active(state, "up") is False

    def test_scale_down_cooldown_active(self):
        """Should block scale-down during cooldown period."""
        state = ScalingState(last_scale_time=time.time())
        with patch.object(autoscaler, "SCALE_DOWN_COOLDOWN", 180):
            assert autoscaler.is_cooldown_active(state, "down") is True

    def test_scale_down_cooldown_expired(self):
        """Should allow scale-down after cooldown period."""
        state = ScalingState(last_scale_time=time.time() - 200)  # 200 seconds ago
        with patch.object(autoscaler, "SCALE_DOWN_COOLDOWN", 180):
            assert autoscaler.is_cooldown_active(state, "down") is False


class TestEvaluateScaling:
    """Tests for evaluate_scaling function."""

    def test_scale_up_after_stabilization(self):
        """Should scale up after stabilization window is met."""
        state = ScalingState(consecutive_scale_up_readings=2)  # Already 2 readings

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "STABILIZATION_WINDOW", 3):
                    with patch.object(autoscaler, "MAX_INSTANCES", 5):
                        # queued=4, current=2: 4 > 2*1.5=3.0, should trigger scale-up
                        action, new_count = autoscaler.evaluate_scaling(
                            queued=4, current=2, state=state
                        )

        assert action == "up"
        assert new_count == 3  # +1 from current

    def test_scale_up_blocked_before_stabilization(self):
        """Should not scale up before stabilization window is met."""
        state = ScalingState(consecutive_scale_up_readings=0)

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "STABILIZATION_WINDOW", 3):
                    with patch.object(autoscaler, "MAX_INSTANCES", 5):
                        action, new_count = autoscaler.evaluate_scaling(
                            queued=4, current=2, state=state
                        )

        assert action == "none"
        assert new_count == 2
        assert state.consecutive_scale_up_readings == 1  # Incremented

    def test_scale_up_blocked_by_cooldown(self):
        """Should not scale up during cooldown even if stabilization met."""
        state = ScalingState(
            consecutive_scale_up_readings=2,
            last_scale_time=time.time(),  # Just scaled
        )

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "STABILIZATION_WINDOW", 3):
                    with patch.object(autoscaler, "SCALE_UP_COOLDOWN", 60):
                        with patch.object(autoscaler, "MAX_INSTANCES", 5):
                            action, new_count = autoscaler.evaluate_scaling(
                                queued=4, current=2, state=state
                            )

        assert action == "none"
        assert new_count == 2

    def test_scale_down_after_stabilization(self):
        """Should scale down after stabilization window is met."""
        state = ScalingState(consecutive_scale_down_readings=2)

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "STABILIZATION_WINDOW", 3):
                    with patch.object(autoscaler, "MIN_INSTANCES", 1):
                        # queued=0, current=4: 0 < 4*0.25=1.0, should trigger scale-down
                        action, new_count = autoscaler.evaluate_scaling(
                            queued=0, current=4, state=state
                        )

        assert action == "down"
        assert new_count == 3  # -1 from current

    def test_scale_down_blocked_by_cooldown(self):
        """Should not scale down during cooldown even if stabilization met."""
        state = ScalingState(
            consecutive_scale_down_readings=2,
            last_scale_time=time.time(),
        )

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "STABILIZATION_WINDOW", 3):
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
                # queued=1, current=2: 1 < 3.0 (no scale-up), 1 > 0.5 (no scale-down)
                action, new_count = autoscaler.evaluate_scaling(
                    queued=1, current=2, state=state
                )

        assert action == "none"
        assert new_count == 2
        assert state.consecutive_scale_up_readings == 0
        assert state.consecutive_scale_down_readings == 0

    def test_respects_max_instances(self):
        """Should not scale above MAX_INSTANCES."""
        state = ScalingState(consecutive_scale_up_readings=2)

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "STABILIZATION_WINDOW", 3):
                    with patch.object(autoscaler, "MAX_INSTANCES", 5):
                        action, new_count = autoscaler.evaluate_scaling(
                            queued=10, current=5, state=state
                        )

        assert action == "none"
        assert new_count == 5

    def test_respects_min_instances(self):
        """Should not scale below MIN_INSTANCES."""
        state = ScalingState(consecutive_scale_down_readings=2)

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "STABILIZATION_WINDOW", 3):
                    with patch.object(autoscaler, "MIN_INSTANCES", 1):
                        action, new_count = autoscaler.evaluate_scaling(
                            queued=0, current=1, state=state
                        )

        assert action == "none"
        assert new_count == 1

    def test_resets_opposite_counter_on_scale_up(self):
        """Should reset scale-down counter when scale-up condition met."""
        state = ScalingState(consecutive_scale_down_readings=2)

        with patch.object(autoscaler, "SCALE_UP_THRESHOLD", 1.5):
            with patch.object(autoscaler, "SCALE_DOWN_THRESHOLD", 0.25):
                with patch.object(autoscaler, "STABILIZATION_WINDOW", 3):
                    autoscaler.evaluate_scaling(queued=4, current=2, state=state)

        assert state.consecutive_scale_down_readings == 0
        assert state.consecutive_scale_up_readings == 1


class TestGetQueuedJobs:
    """Tests for get_queued_jobs function."""

    @patch("autoscaler.requests.get")
    def test_repo_level_api_call(self, mock_get):
        """Should call repo-level API when OWNER and REPO are set."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"total_count": 5}
        mock_get.return_value = mock_response

        with patch.object(autoscaler, "ORG", None):
            with patch.object(autoscaler, "OWNER", "test-owner"):
                with patch.object(autoscaler, "REPO", "test-repo"):
                    result = autoscaler.get_queued_jobs()

        assert result == 5
        mock_get.assert_called_once()
        call_url = mock_get.call_args[0][0]
        assert "repos/test-owner/test-repo/actions/runs" in call_url
        assert "status=queued" in call_url

    @patch("autoscaler.requests.get")
    def test_org_level_api_call(self, mock_get):
        """Should call org-level API when ORG is set."""
        mock_response = MagicMock()
        mock_response.json.return_value = {"total_count": 3}
        mock_get.return_value = mock_response

        with patch.object(autoscaler, "ORG", "test-org"):
            result = autoscaler.get_queued_jobs()

        assert result == 3
        call_url = mock_get.call_args[0][0]
        assert "orgs/test-org/actions/runs" in call_url


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
        """Should update the worker instance count via PUT."""
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
        mock_get.return_value = mock_get_response

        mock_put_response = MagicMock()
        mock_put.return_value = mock_put_response

        with patch.object(autoscaler, "WORKER_NAME", "runner"):
            autoscaler.scale_worker(3)

        mock_put.assert_called_once()
        call_json = mock_put.call_args[1]["json"]
        assert call_json["spec"]["workers"][0]["instance_count"] == 3

    @patch("autoscaler.requests.put")
    @patch("autoscaler.requests.get")
    def test_does_not_update_if_worker_not_found(self, mock_get, mock_put):
        """Should not call PUT if worker is not found."""
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
            autoscaler.scale_worker(3)

        mock_put.assert_not_called()


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
