#!/usr/bin/env python3
"""Tests for the GitHub Actions Runner Autoscaler."""

import os
from unittest.mock import MagicMock, patch

import pytest

# Set environment variables before importing autoscaler
os.environ["GITHUB_TOKEN"] = "test-token"
os.environ["DO_API_TOKEN"] = "test-do-token"
os.environ["APP_ID"] = "test-app-id"
os.environ["OWNER"] = "test-owner"
os.environ["REPO"] = "test-repo"

import autoscaler


class TestCalculateDesiredInstances:
    """Tests for calculate_desired_instances function."""

    def test_zero_queued_returns_min(self):
        """With no queued jobs, should return MIN_INSTANCES."""
        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "JOBS_PER_RUNNER", 1):
                    assert autoscaler.calculate_desired_instances(0) == 1

    def test_one_queued_returns_one(self):
        """One queued job needs one runner."""
        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "JOBS_PER_RUNNER", 1):
                    assert autoscaler.calculate_desired_instances(1) == 1

    def test_multiple_queued_scales_up(self):
        """Multiple queued jobs should scale up."""
        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "JOBS_PER_RUNNER", 1):
                    assert autoscaler.calculate_desired_instances(3) == 3

    def test_respects_max_instances(self):
        """Should not exceed MAX_INSTANCES."""
        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "JOBS_PER_RUNNER", 1):
                    assert autoscaler.calculate_desired_instances(10) == 5

    def test_respects_min_instances(self):
        """Should not go below MIN_INSTANCES."""
        with patch.object(autoscaler, "MIN_INSTANCES", 2):
            with patch.object(autoscaler, "MAX_INSTANCES", 5):
                with patch.object(autoscaler, "JOBS_PER_RUNNER", 1):
                    assert autoscaler.calculate_desired_instances(0) == 2

    def test_jobs_per_runner_calculation(self):
        """With multiple jobs per runner, should calculate correctly."""
        with patch.object(autoscaler, "MIN_INSTANCES", 1):
            with patch.object(autoscaler, "MAX_INSTANCES", 10):
                with patch.object(autoscaler, "JOBS_PER_RUNNER", 2):
                    # 3 jobs / 2 per runner = 2 runners (ceiling)
                    assert autoscaler.calculate_desired_instances(3) == 2
                    # 4 jobs / 2 per runner = 2 runners
                    assert autoscaler.calculate_desired_instances(4) == 2
                    # 5 jobs / 2 per runner = 3 runners (ceiling)
                    assert autoscaler.calculate_desired_instances(5) == 3


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


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
