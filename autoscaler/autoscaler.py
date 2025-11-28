#!/usr/bin/env python3
"""
GitHub Actions Runner Autoscaler for DigitalOcean App Platform.

Polls GitHub API for queued jobs and runner status, then scales runner workers accordingly.
Uses ephemeral runners that self-terminate after one job, with safe scale-down
based on idle runner count.
"""

import logging
import os
import sys
import time
import math
from dataclasses import dataclass

import requests

# Configure logging with timestamps
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    handlers=[logging.StreamHandler(sys.stdout)],
)
log = logging.getLogger(__name__)

# Configuration from environment
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN")
DO_API_TOKEN = os.environ.get("DO_API_TOKEN")
APP_ID = os.environ.get("APP_ID")

# Optional: org-level or repo-level
ORG = os.environ.get("ORG")
OWNER = os.environ.get("OWNER")
REPO = os.environ.get("REPO")

# Scaling configuration
WORKER_NAME = os.environ.get("WORKER_NAME", "runner")
MIN_INSTANCES = int(os.environ.get("MIN_INSTANCES", "1"))
MAX_INSTANCES = int(os.environ.get("MAX_INSTANCES", "5"))
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "30"))

# Cooldown configuration (anti-thrashing)
SCALE_UP_COOLDOWN = int(os.environ.get("SCALE_UP_COOLDOWN", "60"))  # seconds
SCALE_DOWN_COOLDOWN = int(os.environ.get("SCALE_DOWN_COOLDOWN", "180"))  # seconds

# Scale-up configuration
SCALE_UP_STEP = int(os.environ.get("SCALE_UP_STEP", "3"))  # max instances to add at once
SCALE_UP_PROPORTION = float(os.environ.get("SCALE_UP_PROPORTION", "0.5"))  # fraction of queued jobs

# Runner filtering
RUNNER_NAME_PREFIX = os.environ.get("RUNNER_NAME_PREFIX", "")

# Multiple runners per instance
RUNNERS_PER_INSTANCE = int(os.environ.get("RUNNERS_PER_INSTANCE", "2"))

GITHUB_API = "https://api.github.com"
DO_API = "https://api.digitalocean.com/v2"


def validate_config() -> None:
    """Validate configuration on startup. Exits if invalid."""
    errors = []

    if MIN_INSTANCES > MAX_INSTANCES:
        errors.append(f"MIN_INSTANCES ({MIN_INSTANCES}) > MAX_INSTANCES ({MAX_INSTANCES})")
    if MIN_INSTANCES < 0:
        errors.append("MIN_INSTANCES must be >= 0")
    if SCALE_UP_COOLDOWN < 0:
        errors.append("SCALE_UP_COOLDOWN must be >= 0")
    if SCALE_DOWN_COOLDOWN < 0:
        errors.append("SCALE_DOWN_COOLDOWN must be >= 0")
    if POLL_INTERVAL <= 0:
        errors.append("POLL_INTERVAL must be > 0")
    if SCALE_UP_STEP < 1:
        errors.append("SCALE_UP_STEP must be >= 1")
    if not 0 < SCALE_UP_PROPORTION <= 1:
        errors.append("SCALE_UP_PROPORTION must be > 0 and <= 1")
    if RUNNERS_PER_INSTANCE < 1:
        errors.append("RUNNERS_PER_INSTANCE must be >= 1")

    if errors:
        for e in errors:
            log.error(f"Config error: {e}")
        sys.exit(1)


def is_self_hosted_job(job: dict) -> bool:
    """Check if job targets self-hosted runners."""
    labels = job.get("labels", [])
    return "self-hosted" in labels


def is_our_runner(runner_name: str | None) -> bool:
    """Check if runner_name matches our runner naming pattern.

    Args:
        runner_name: The name of the runner executing the job.

    Returns:
        True if the runner matches our prefix pattern (or no prefix is configured).
    """
    if not runner_name:
        return False
    if not RUNNER_NAME_PREFIX:
        return True  # No prefix configured, count all self-hosted in_progress jobs
    return runner_name.startswith(RUNNER_NAME_PREFIX)


@dataclass
class ScalingState:
    """Tracks scaling state for cooldown periods."""

    last_scale_up_time: float = 0
    last_scale_down_time: float = 0


def get_queued_job_count() -> int:
    """Get count of queued jobs targeting self-hosted runners.

    Only counts queued jobs (not in_progress). Ephemeral runners handle
    in_progress jobs themselves - they exit after completion.

    Returns:
        Number of queued jobs waiting for runners.
    """
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    # Get runs that are queued (jobs may be waiting for runners)
    if ORG:
        runs_url = f"{GITHUB_API}/orgs/{ORG}/actions/runs?status=queued&per_page=100"
    elif OWNER and REPO:
        runs_url = f"{GITHUB_API}/repos/{OWNER}/{REPO}/actions/runs?status=queued&per_page=100"
    else:
        log.error("ORG or (OWNER and REPO) must be set")
        sys.exit(1)

    resp = requests.get(runs_url, headers=headers)
    resp.raise_for_status()
    queued_runs = resp.json().get("workflow_runs", [])

    # Count queued jobs across all runs
    queued_count = 0
    for run in queued_runs:
        run_id = run.get("id")
        if not run_id:
            continue

        # Get jobs for this run
        if ORG:
            repo_full_name = run.get("repository", {}).get("full_name", "")
            if not repo_full_name:
                continue
            jobs_url = f"{GITHUB_API}/repos/{repo_full_name}/actions/runs/{run_id}/jobs?per_page=100"
        else:
            jobs_url = f"{GITHUB_API}/repos/{OWNER}/{REPO}/actions/runs/{run_id}/jobs?per_page=100"

        try:
            jobs_resp = requests.get(jobs_url, headers=headers)
            jobs_resp.raise_for_status()
            jobs = jobs_resp.json().get("jobs", [])

            for job in jobs:
                if job.get("status") == "queued" and is_self_hosted_job(job):
                    queued_count += 1
        except requests.RequestException as e:
            log.warning(f"Failed to get jobs for run {run_id}: {e}")

    log.info(f"Queued jobs: {queued_count}")
    return queued_count


def get_current_instance_count() -> int:
    """Get current instance count for the worker from DO App spec."""
    headers = {"Authorization": f"Bearer {DO_API_TOKEN}"}
    url = f"{DO_API}/apps/{APP_ID}"

    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    app = resp.json().get("app", {})

    for worker in app.get("spec", {}).get("workers", []):
        if worker.get("name") == WORKER_NAME:
            count = worker.get("instance_count", 1)
            capacity = count * RUNNERS_PER_INSTANCE
            log.info(f"Current {WORKER_NAME}: {count} instances, {capacity} runners capacity")
            return count

    log.warning(f"Worker '{WORKER_NAME}' not found in app spec")
    return 1


def scale_worker(desired_count: int) -> bool:
    """Update the worker instance count via DO API.

    Returns:
        True if the update was successful and verified, False otherwise.
    """
    headers = {
        "Authorization": f"Bearer {DO_API_TOKEN}",
        "Content-Type": "application/json",
    }

    # Get current app spec
    resp = requests.get(f"{DO_API}/apps/{APP_ID}", headers=headers)
    resp.raise_for_status()
    app = resp.json().get("app", {})
    spec = app.get("spec", {})

    # Update the target worker's instance count
    updated = False
    for worker in spec.get("workers", []):
        if worker.get("name") == WORKER_NAME:
            worker["instance_count"] = desired_count
            updated = True
            break

    if not updated:
        log.error(f"Worker '{WORKER_NAME}' not found")
        return False

    # Apply updated spec
    resp = requests.put(
        f"{DO_API}/apps/{APP_ID}",
        headers=headers,
        json={"spec": spec},
    )
    resp.raise_for_status()

    # Verify the change took effect (handles concurrent modifications)
    verify_resp = requests.get(f"{DO_API}/apps/{APP_ID}", headers=headers)
    verify_resp.raise_for_status()
    actual_count = None
    for worker in verify_resp.json().get("app", {}).get("spec", {}).get("workers", []):
        if worker.get("name") == WORKER_NAME:
            actual_count = worker.get("instance_count")
            break

    if actual_count != desired_count:
        log.warning(
            f"Spec update conflict: expected {desired_count}, got {actual_count}"
        )
        return False

    log.info(f"Scaled {WORKER_NAME} to {desired_count} instances")
    return True


def is_cooldown_active(state: ScalingState, direction: str) -> bool:
    """Check if cooldown period is still active for the given direction.

    Each direction has its own independent cooldown, allowing scale-up
    during scale-down cooldown and vice versa.
    """
    if direction == "up":
        if state.last_scale_up_time == 0:
            return False
        return time.time() - state.last_scale_up_time < SCALE_UP_COOLDOWN
    else:  # down
        if state.last_scale_down_time == 0:
            return False
        return time.time() - state.last_scale_down_time < SCALE_DOWN_COOLDOWN


def evaluate_scaling(
    queued_jobs: int,
    current: int,
    online_runners: int,
    idle_runners: int,
    state: ScalingState,
) -> tuple[str, int]:
    """
    Evaluate scaling decision based on queued jobs and runner capacity.

    Simplified logic for ephemeral runners:
    1. Maintain minimum capacity (bypass cooldown)
    2. Scale up when queued_jobs > 0
    3. Scale down when idle_runners > min_runners (safe - busy runners not counted)

    Args:
        queued_jobs: Number of jobs waiting for runners.
        current: Current instance count.
        online_runners: Number of online runners (busy + idle).
        idle_runners: Number of online runners that are NOT busy (safe to terminate).
        state: Scaling state for cooldowns.

    Returns:
        tuple of (action, new_count) where action is "up", "down", or "none"
    """
    min_runners = MIN_INSTANCES * RUNNERS_PER_INSTANCE

    # Priority 1: Maintain minimum capacity (bypass cooldown)
    if online_runners < min_runners and current < MAX_INSTANCES:
        runners_needed = min_runners - online_runners
        instances_to_add = math.ceil(runners_needed / RUNNERS_PER_INSTANCE)
        new_count = min(current + instances_to_add, MAX_INSTANCES)
        log.info(
            f"Below min capacity: {online_runners} online < {min_runners} min, "
            f"adding {instances_to_add} instance(s)"
        )
        return ("up", new_count)

    # Priority 2: Scale up for queued jobs (no threshold, just queued > 0)
    if queued_jobs > 0 and current < MAX_INSTANCES:
        if is_cooldown_active(state, "up"):
            remaining = SCALE_UP_COOLDOWN - (time.time() - state.last_scale_up_time)
            log.info(f"Scale-up blocked by cooldown ({remaining:.0f}s remaining)")
            return ("none", current)

        # Proportional scaling based on queued jobs
        instances_to_add = min(
            math.ceil(queued_jobs / RUNNERS_PER_INSTANCE * SCALE_UP_PROPORTION),
            SCALE_UP_STEP,
        )
        instances_to_add = max(instances_to_add, 1)  # At least 1
        new_count = min(current + instances_to_add, MAX_INSTANCES)
        log.info(
            f"Scaling up for {queued_jobs} queued job(s): "
            f"+{instances_to_add} instance(s) -> {new_count}"
        )
        if new_count > current:
            return ("up", new_count)
        else:
            log.info(f"Already at MAX_INSTANCES ({MAX_INSTANCES})")

    # Priority 3: Scale down when we have excess IDLE runners AND no queued work
    # Use idle_runners (not online) to avoid terminating busy runners
    # Don't scale down if jobs are queued - runners will pick them up soon
    if queued_jobs == 0 and idle_runners > min_runners and current > MIN_INSTANCES:
        if is_cooldown_active(state, "down"):
            remaining = SCALE_DOWN_COOLDOWN - (time.time() - state.last_scale_down_time)
            log.info(f"Scale-down blocked by cooldown ({remaining:.0f}s remaining)")
            return ("none", current)

        # Conservative: scale down by 1 instance at a time
        new_count = max(current - 1, MIN_INSTANCES)
        log.info(
            f"Scaling down: {idle_runners} idle > {min_runners} min, "
            f"-1 instance -> {new_count}"
        )
        if new_count < current:
            return ("down", new_count)
        else:
            log.info(f"Already at MIN_INSTANCES ({MIN_INSTANCES})")

    # No scaling needed
    log.debug(
        f"Stable: queued={queued_jobs}, online={online_runners}, "
        f"idle={idle_runners}, instances={current}"
    )
    return ("none", current)


def get_runners() -> list[dict]:
    """Get list of all registered runners from GitHub."""
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    if ORG:
        url = f"{GITHUB_API}/orgs/{ORG}/actions/runners?per_page=100"
    elif OWNER and REPO:
        url = f"{GITHUB_API}/repos/{OWNER}/{REPO}/actions/runners?per_page=100"
    else:
        return []

    resp = requests.get(url, headers=headers)
    resp.raise_for_status()
    data = resp.json()

    return data.get("runners", [])


def get_online_runner_count() -> int:
    """Count currently online runners matching our prefix.

    Returns:
        Number of online runners (both busy and idle).
    """
    try:
        runners = get_runners()
    except requests.RequestException as e:
        log.error(f"Failed to get runners: {e}")
        return 0

    count = sum(
        1
        for r in runners
        if r.get("status") == "online" and is_our_runner(r.get("name"))
    )
    return count


def get_idle_runner_count() -> int:
    """Count online runners that are NOT busy (safe to terminate).

    Returns:
        Number of idle runners that could be safely terminated.
    """
    try:
        runners = get_runners()
    except requests.RequestException as e:
        log.error(f"Failed to get runners: {e}")
        return 0

    count = sum(
        1
        for r in runners
        if r.get("status") == "online"
        and not r.get("busy", False)
        and is_our_runner(r.get("name"))
    )
    return count


def delete_runner(runner_id: int) -> bool:
    """Delete a runner by ID from GitHub."""
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    if ORG:
        url = f"{GITHUB_API}/orgs/{ORG}/actions/runners/{runner_id}"
    elif OWNER and REPO:
        url = f"{GITHUB_API}/repos/{OWNER}/{REPO}/actions/runners/{runner_id}"
    else:
        return False

    resp = requests.delete(url, headers=headers)
    return resp.status_code == 204


def cleanup_dead_runners() -> int:
    """Remove offline runners that are not busy.

    Returns:
        Number of runners deleted.
    """
    try:
        runners = get_runners()
    except requests.RequestException as e:
        log.error(f"Failed to get runners: {e}")
        return 0

    deleted = 0
    for runner in runners:
        status = runner.get("status", "")
        busy = runner.get("busy", False)
        name = runner.get("name", "unknown")
        runner_id = runner.get("id")

        # Only delete offline runners that are not busy
        if status == "offline" and not busy and runner_id:
            log.info(f"Removing dead runner: {name} (ID: {runner_id})")
            try:
                if delete_runner(runner_id):
                    deleted += 1
                    log.info(f"  Deleted runner {name}")
                else:
                    log.warning(f"  Failed to delete runner {name}")
            except requests.RequestException as e:
                log.error(f"  Error deleting runner {name}: {e}")

    if deleted > 0:
        log.info(f"Cleaned up {deleted} dead runner(s)")
    return deleted


AUTOSCALER_VERSION = "2.0.0"  # Ephemeral runner algorithm


def main():
    """Main autoscaler loop (runs continuously)."""
    log.info(f"Starting GitHub Actions Runner Autoscaler v{AUTOSCALER_VERSION} (ephemeral mode)")
    log.info(f"  Worker: {WORKER_NAME}")
    log.info(f"  Min instances: {MIN_INSTANCES}")
    log.info(f"  Max instances: {MAX_INSTANCES}")
    log.info(f"  Poll interval: {POLL_INTERVAL}s")
    log.info(f"  Scale-up cooldown: {SCALE_UP_COOLDOWN}s")
    log.info(f"  Scale-down cooldown: {SCALE_DOWN_COOLDOWN}s")
    log.info(f"  Scale-up step: +{SCALE_UP_STEP} (max)")
    log.info(f"  Scale-up proportion: {SCALE_UP_PROPORTION}")
    log.info(f"  Runner name prefix: '{RUNNER_NAME_PREFIX}' (empty=all self-hosted)")
    log.info(f"  Runners per instance: {RUNNERS_PER_INSTANCE}")

    if not all([GITHUB_TOKEN, DO_API_TOKEN, APP_ID]):
        log.error("GITHUB_TOKEN, DO_API_TOKEN, and APP_ID are required")
        sys.exit(1)

    validate_config()

    state = ScalingState()

    while True:
        try:
            # Clean up any dead runners first
            cleanup_dead_runners()

            # Gather metrics
            queued_jobs = get_queued_job_count()
            current = get_current_instance_count()
            online_runners = get_online_runner_count()
            idle_runners = get_idle_runner_count()

            log.info(
                f"Status: instances={current}, online={online_runners}, "
                f"idle={idle_runners}, queued={queued_jobs}"
            )

            action, new_count = evaluate_scaling(
                queued_jobs, current, online_runners, idle_runners, state
            )

            if action != "none":
                log.info(f"Scaling {WORKER_NAME}: {current} -> {new_count}")
                scale_worker(new_count)
                # Set cooldown for the direction that was scaled
                if action == "up":
                    state.last_scale_up_time = time.time()
                else:
                    state.last_scale_down_time = time.time()
            else:
                log.debug(f"No scaling action ({current} instances)")

        except requests.RequestException as e:
            log.error(f"API error: {e}")
        except Exception as e:
            log.exception(f"Unexpected error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
