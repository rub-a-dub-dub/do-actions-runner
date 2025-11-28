#!/usr/bin/env python3
"""Tests for the GitHub Actions Runner Autoscaler (ephemeral mode)."""

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


class TestJobFiltering:
    """Tests for job filtering functions."""

    def test_is_self_hosted_job_with_self_hosted_label(self):
        """Should return True when job has self-hosted label."""
        job = {"labels": ["self-hosted", "linux", "x64"]}
        assert autoscaler.is_self_hosted_job(job) is True

    def test_is_self_hosted_job_without_self_hosted_label(self):
        """Should return False when job doesn't have self-hosted label."""
        job = {"labels": ["ubuntu-latest"]}
        assert autoscaler.is_self_hosted_job(job) is False

    def test_is_self_hosted_job_with_empty_labels(self):
        """Should return False when job has empty labels."""
        job = {"labels": []}
        assert autoscaler.is_self_hosted_job(job) is False

    def test_is_self_hosted_job_with_no_labels(self):
        """Should return False when job has no labels key."""
        job = {}
        assert autoscaler.is_self_hosted_job(job) is False

    def test_is_our_runner_with_matching_prefix(self):
        """Should return True when runner name matches prefix."""
        with patch.object(autoscaler, "RUNNER_NAME_PREFIX", "runner-"):
            assert autoscaler.is_our_runner("runner-abc123") is True
            assert autoscaler.is_our_runner("runner-") is True

    def test_is_our_runner_with_non_matching_prefix(self):
        """Should return False when runner name doesn't match prefix."""
        with patch.object(autoscaler, "RUNNER_NAME_PREFIX", "runner-"):
            assert autoscaler.is_our_runner("other-abc123") is False
            assert autoscaler.is_our_runner("RUNNER-abc123") is False  # Case sensitive

    def test_is_our_runner_with_empty_prefix(self):
        """Should return True for any runner when prefix is empty."""
        with patch.object(autoscaler, "RUNNER_NAME_PREFIX", ""):
            assert autoscaler.is_our_runner("any-runner-name") is True
            assert autoscaler.is_our_runner("runner-abc123") is True

    def test_is_our_runner_with_none_runner_name(self):
        """Should return False when runner_name is None."""
        with patch.object(autoscaler, "RUNNER_NAME_PREFIX", "runner-"):
            assert autoscaler.is_our_runner(None) is False

    def test_is_our_runner_with_empty_runner_name(self):
        """Should return False when runner_name is empty string."""
        with patch.object(autoscaler, "RUNNER_NAME_PREFIX", "runner-"):
            assert autoscaler.is_our_runner("") is False


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
    """Tests for evaluate_scaling function (ephemeral mode)."""

    def test_scale_up_when_below_min_capacity(self):
        """Should scale up to maintain minimum capacity (bypasses cooldown)."""
        state = ScalingState(last_scale_up_time=time.time())  # In cooldown

        with patch.object(autoscaler, "MIN_INSTANCES", 2):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "RUNNERS_PER_INSTANCE", 1):
                    # 1 online runner < 2 min runners -> scale up
                    action, new_count = autoscaler.evaluate_scaling(
                        queued_jobs=0,
                        current=1,
                        online_runners=1,
                        idle_runners=1,
                        state=state,
                    )

        assert action == "up"
        assert new_count == 2  # Add 1 to reach min

    def test_scale_up_when_queued_jobs(self):
        """Should scale up when there are queued jobs."""
        state = ScalingState()

        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "RUNNERS_PER_INSTANCE", 1):
                    with patch.object(autoscaler, "SCALE_UP_STEP", 2):
                        with patch.object(autoscaler, "SCALE_UP_PROPORTION", 0.5):
                            # 3 queued jobs -> scale up
                            action, new_count = autoscaler.evaluate_scaling(
                                queued_jobs=3,
                                current=1,
                                online_runners=1,
                                idle_runners=1,
                                state=state,
                            )

        assert action == "up"
        assert new_count == 3  # 1 + ceil(3 * 0.5) = 1 + 2 = 3

    def test_scale_up_blocked_by_cooldown(self):
        """Should not scale up for queued jobs during cooldown."""
        state = ScalingState(last_scale_up_time=time.time())

        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "RUNNERS_PER_INSTANCE", 1):
                    with patch.object(autoscaler, "SCALE_UP_COOLDOWN", 60):
                        # online_runners >= min_runners, so cooldown is respected
                        action, new_count = autoscaler.evaluate_scaling(
                            queued_jobs=5,
                            current=2,
                            online_runners=2,
                            idle_runners=2,
                            state=state,
                        )

        assert action == "none"
        assert new_count == 2

    def test_scale_down_when_excess_idle(self):
        """Should scale down when idle runners exceed minimum."""
        state = ScalingState()

        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "RUNNERS_PER_INSTANCE", 1):
                    # 3 idle runners > 1 min -> scale down
                    action, new_count = autoscaler.evaluate_scaling(
                        queued_jobs=0,
                        current=3,
                        online_runners=3,
                        idle_runners=3,
                        state=state,
                    )

        assert action == "down"
        assert new_count == 2  # -1 from current

    def test_no_scale_down_when_runners_busy(self):
        """Should not scale down when runners are busy (idle < min)."""
        state = ScalingState()

        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "RUNNERS_PER_INSTANCE", 1):
                    # 3 online but only 1 idle (2 busy) <= 1 min -> no scale down
                    action, new_count = autoscaler.evaluate_scaling(
                        queued_jobs=0,
                        current=3,
                        online_runners=3,
                        idle_runners=1,
                        state=state,
                    )

        assert action == "none"
        assert new_count == 3

    def test_scale_down_blocked_by_cooldown(self):
        """Should not scale down during cooldown."""
        state = ScalingState(last_scale_down_time=time.time())

        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "RUNNERS_PER_INSTANCE", 1):
                    with patch.object(autoscaler, "SCALE_DOWN_COOLDOWN", 180):
                        action, new_count = autoscaler.evaluate_scaling(
                            queued_jobs=0,
                            current=3,
                            online_runners=3,
                            idle_runners=3,
                            state=state,
                        )

        assert action == "none"
        assert new_count == 3

    def test_respects_max_instances(self):
        """Should not scale above MAX_INSTANCES."""
        state = ScalingState()

        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 3):
                with patch.object(autoscaler, "RUNNERS_PER_INSTANCE", 1):
                    with patch.object(autoscaler, "SCALE_UP_STEP", 5):
                        with patch.object(autoscaler, "SCALE_UP_PROPORTION", 1.0):
                            action, new_count = autoscaler.evaluate_scaling(
                                queued_jobs=10,
                                current=3,
                                online_runners=3,
                                idle_runners=3,
                                state=state,
                            )

        assert action == "none"
        assert new_count == 3

    def test_respects_min_instances(self):
        """Should not scale below MIN_INSTANCES."""
        state = ScalingState()

        with patch.object(autoscaler, "MIN_INSTANCES", 2):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "RUNNERS_PER_INSTANCE", 1):
                    # idle_runners > min_runners triggers scale-down attempt
                    # but current is already at MIN_INSTANCES
                    action, new_count = autoscaler.evaluate_scaling(
                        queued_jobs=0,
                        current=2,
                        online_runners=3,
                        idle_runners=3,
                        state=state,
                    )

        assert action == "none"
        assert new_count == 2

    def test_stable_when_no_queued_and_at_min(self):
        """Should not scale when at minimum with no queued jobs."""
        state = ScalingState()

        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "RUNNERS_PER_INSTANCE", 1):
                    action, new_count = autoscaler.evaluate_scaling(
                        queued_jobs=0,
                        current=1,
                        online_runners=1,
                        idle_runners=1,
                        state=state,
                    )

        assert action == "none"
        assert new_count == 1


class TestGetQueuedJobCount:
    """Tests for get_queued_job_count function."""

    @patch("autoscaler.requests.get")
    def test_counts_only_queued_self_hosted_jobs(self, mock_get):
        """Should count only queued jobs with self-hosted label."""
        def mock_get_side_effect(url, headers=None):
            mock_resp = MagicMock()
            if "status=queued" in url and "actions/runs" in url:
                mock_resp.json.return_value = {
                    "workflow_runs": [{"id": 123}]
                }
            elif "runs/123/jobs" in url:
                mock_resp.json.return_value = {
                    "jobs": [
                        {"status": "queued", "labels": ["self-hosted", "linux"]},
                        {"status": "queued", "labels": ["self-hosted", "x64"]},
                        {"status": "queued", "labels": ["ubuntu-latest"]},  # GitHub-hosted
                        {"status": "in_progress", "labels": ["self-hosted"]},  # Not queued
                    ]
                }
            else:
                mock_resp.json.return_value = {}
            return mock_resp

        mock_get.side_effect = mock_get_side_effect

        with patch.object(autoscaler, "ORG", None):
            with patch.object(autoscaler, "OWNER", "test-owner"):
                with patch.object(autoscaler, "REPO", "test-repo"):
                    result = autoscaler.get_queued_job_count()

        # Only 2 self-hosted queued jobs counted
        assert result == 2

    @patch("autoscaler.requests.get")
    def test_org_level_api_call(self, mock_get):
        """Should call org-level API when ORG is set."""
        def mock_get_side_effect(url, headers=None):
            mock_resp = MagicMock()
            if "orgs/test-org/actions/runs" in url:
                mock_resp.json.return_value = {
                    "workflow_runs": [
                        {"id": 789, "repository": {"full_name": "test-org/repo1"}}
                    ]
                }
            elif "runs/789/jobs" in url:
                mock_resp.json.return_value = {
                    "jobs": [{"status": "queued", "labels": ["self-hosted", "linux"]}]
                }
            else:
                mock_resp.json.return_value = {}
            return mock_resp

        mock_get.side_effect = mock_get_side_effect

        with patch.object(autoscaler, "ORG", "test-org"):
            result = autoscaler.get_queued_job_count()

        assert result == 1
        calls = [str(c) for c in mock_get.call_args_list]
        assert any("orgs/test-org/actions/runs" in c for c in calls)


class TestGetOnlineRunnerCount:
    """Tests for get_online_runner_count function."""

    @patch("autoscaler.get_runners")
    def test_counts_online_runners_with_prefix(self, mock_get_runners):
        """Should count only online runners matching our prefix."""
        mock_get_runners.return_value = [
            {"id": 1, "name": "runner-abc", "status": "online", "busy": False},
            {"id": 2, "name": "runner-xyz", "status": "online", "busy": True},
            {"id": 3, "name": "other-runner", "status": "online", "busy": False},
            {"id": 4, "name": "runner-123", "status": "offline", "busy": False},
        ]

        with patch.object(autoscaler, "RUNNER_NAME_PREFIX", "runner-"):
            result = autoscaler.get_online_runner_count()

        # Only runner-abc and runner-xyz are online and match prefix
        assert result == 2

    @patch("autoscaler.get_runners")
    def test_counts_all_online_when_no_prefix(self, mock_get_runners):
        """Should count all online runners when no prefix configured."""
        mock_get_runners.return_value = [
            {"id": 1, "name": "runner-abc", "status": "online", "busy": False},
            {"id": 2, "name": "other-runner", "status": "online", "busy": True},
            {"id": 3, "name": "another", "status": "offline", "busy": False},
        ]

        with patch.object(autoscaler, "RUNNER_NAME_PREFIX", ""):
            result = autoscaler.get_online_runner_count()

        assert result == 2


class TestGetIdleRunnerCount:
    """Tests for get_idle_runner_count function."""

    @patch("autoscaler.get_runners")
    def test_counts_idle_runners_with_prefix(self, mock_get_runners):
        """Should count only online, non-busy runners matching our prefix."""
        mock_get_runners.return_value = [
            {"id": 1, "name": "runner-abc", "status": "online", "busy": False},
            {"id": 2, "name": "runner-xyz", "status": "online", "busy": True},  # Busy
            {"id": 3, "name": "other-runner", "status": "online", "busy": False},  # No match
            {"id": 4, "name": "runner-123", "status": "offline", "busy": False},  # Offline
        ]

        with patch.object(autoscaler, "RUNNER_NAME_PREFIX", "runner-"):
            result = autoscaler.get_idle_runner_count()

        # Only runner-abc is online, not busy, and matches prefix
        assert result == 1

    @patch("autoscaler.get_runners")
    def test_counts_all_idle_when_no_prefix(self, mock_get_runners):
        """Should count all idle runners when no prefix configured."""
        mock_get_runners.return_value = [
            {"id": 1, "name": "runner-abc", "status": "online", "busy": False},
            {"id": 2, "name": "other-runner", "status": "online", "busy": False},
            {"id": 3, "name": "busy-one", "status": "online", "busy": True},
        ]

        with patch.object(autoscaler, "RUNNER_NAME_PREFIX", ""):
            result = autoscaler.get_idle_runner_count()

        assert result == 2


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
        mock_get_response = MagicMock()
        mock_get_response.json.return_value = {
            "app": {
                "spec": {
                    "name": "test-app",
                    "workers": [{"name": "runner", "instance_count": 1}],
                }
            }
        }
        mock_verify_response = MagicMock()
        mock_verify_response.json.return_value = {
            "app": {
                "spec": {
                    "name": "test-app",
                    "workers": [{"name": "runner", "instance_count": 3}],
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
        mock_get_response = MagicMock()
        mock_get_response.json.return_value = {
            "app": {
                "spec": {
                    "name": "test-app",
                    "workers": [{"name": "runner", "instance_count": 1}],
                }
            }
        }
        mock_verify_response = MagicMock()
        mock_verify_response.json.return_value = {
            "app": {
                "spec": {
                    "name": "test-app",
                    "workers": [{"name": "runner", "instance_count": 2}],
                }
            }
        }
        mock_get.side_effect = [mock_get_response, mock_verify_response]

        mock_put_response = MagicMock()
        mock_put.return_value = mock_put_response

        with patch.object(autoscaler, "WORKER_NAME", "runner"):
            result = autoscaler.scale_worker(3)

        assert result is False
        mock_put.assert_called_once()


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
                                with patch.object(autoscaler, "SCALE_UP_PROPORTION", 0.5):
                                    with patch.object(autoscaler, "RUNNERS_PER_INSTANCE", 1):
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

    def test_validate_config_fails_zero_scale_up_proportion(self):
        """Should exit when SCALE_UP_PROPORTION <= 0."""
        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "SCALE_UP_PROPORTION", 0):
                    with pytest.raises(SystemExit):
                        validate_config()

    def test_validate_config_fails_scale_up_proportion_greater_than_one(self):
        """Should exit when SCALE_UP_PROPORTION > 1."""
        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "SCALE_UP_PROPORTION", 1.5):
                    with pytest.raises(SystemExit):
                        validate_config()

    def test_validate_config_fails_zero_runners_per_instance(self):
        """Should exit when RUNNERS_PER_INSTANCE < 1."""
        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "RUNNERS_PER_INSTANCE", 0):
                    with pytest.raises(SystemExit):
                        validate_config()


class TestRunnersPerInstance:
    """Tests for RUNNERS_PER_INSTANCE capacity calculations."""

    def test_scale_up_below_min_with_multiple_runners(self):
        """Should scale up when online runners < min (accounting for RUNNERS_PER_INSTANCE)."""
        state = ScalingState()

        with patch.object(autoscaler, "MIN_INSTANCES", 2):
            with patch.object(autoscaler, "MAX_INSTANCES", 10):
                with patch.object(autoscaler, "RUNNERS_PER_INSTANCE", 2):
                    # min_runners = 2 * 2 = 4
                    # 2 online < 4 min -> scale up
                    action, new_count = autoscaler.evaluate_scaling(
                        queued_jobs=0,
                        current=1,
                        online_runners=2,
                        idle_runners=2,
                        state=state,
                    )

        assert action == "up"
        # Need 2 more runners, that's 1 instance (2 runners per instance)
        assert new_count == 2

    def test_scale_down_with_multiple_runners_per_instance(self):
        """Should scale down based on idle runners vs min_runners."""
        state = ScalingState()

        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 10):
                with patch.object(autoscaler, "RUNNERS_PER_INSTANCE", 2):
                    # min_runners = 1 * 2 = 2
                    # 6 idle > 2 min -> scale down
                    action, new_count = autoscaler.evaluate_scaling(
                        queued_jobs=0,
                        current=3,
                        online_runners=6,
                        idle_runners=6,
                        state=state,
                    )

        assert action == "down"
        assert new_count == 2  # -1 from current


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
