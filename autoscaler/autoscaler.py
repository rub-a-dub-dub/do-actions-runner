#!/usr/bin/env python3
"""
GitHub Actions Runner Autoscaler for DigitalOcean App Platform.

Polls GitHub API for job demand (queued + in_progress) and scales runner workers accordingly.
Includes cooldown periods, hysteresis thresholds, and stabilization windows
to prevent thrashing.
"""

import logging
import os
import sys
import time
from dataclasses import dataclass, field

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
POLL_INTERVAL = int(os.environ.get("POLL_INTERVAL", "60"))

# Cooldown configuration (anti-thrashing)
SCALE_UP_COOLDOWN = int(os.environ.get("SCALE_UP_COOLDOWN", "60"))  # seconds
SCALE_DOWN_COOLDOWN = int(os.environ.get("SCALE_DOWN_COOLDOWN", "180"))  # seconds

# Hysteresis thresholds
SCALE_UP_THRESHOLD = float(os.environ.get("SCALE_UP_THRESHOLD", "1.5"))
SCALE_DOWN_THRESHOLD = float(os.environ.get("SCALE_DOWN_THRESHOLD", "0.25"))

# Step sizes for scaling (these are maximums when using proportional scaling)
SCALE_UP_STEP = int(os.environ.get("SCALE_UP_STEP", "2"))
SCALE_DOWN_STEP = int(os.environ.get("SCALE_DOWN_STEP", "1"))

# Proportional scaling factors (fraction of deficit/excess to scale by)
SCALE_UP_PROPORTION = float(os.environ.get("SCALE_UP_PROPORTION", "0.5"))
SCALE_DOWN_PROPORTION = float(os.environ.get("SCALE_DOWN_PROPORTION", "0.5"))

# Runner filtering
RUNNER_NAME_PREFIX = os.environ.get("RUNNER_NAME_PREFIX", "")

# Stabilization with time decay
STABILIZATION_WINDOW_MINUTES = int(os.environ.get("STABILIZATION_WINDOW_MINUTES", "3"))
DECAY_HALF_LIFE_SECONDS = float(os.environ.get("DECAY_HALF_LIFE_SECONDS", "30"))
BREACH_THRESHOLD = float(os.environ.get("BREACH_THRESHOLD", "2.0"))

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
    if SCALE_DOWN_STEP < 1:
        errors.append("SCALE_DOWN_STEP must be >= 1")
    if SCALE_UP_THRESHOLD <= 0:
        errors.append("SCALE_UP_THRESHOLD must be > 0")
    if SCALE_DOWN_THRESHOLD < 0:
        errors.append("SCALE_DOWN_THRESHOLD must be >= 0")
    if STABILIZATION_WINDOW_MINUTES <= 0:
        errors.append("STABILIZATION_WINDOW_MINUTES must be > 0")
    if DECAY_HALF_LIFE_SECONDS <= 0:
        errors.append("DECAY_HALF_LIFE_SECONDS must be > 0")
    if BREACH_THRESHOLD <= 0:
        errors.append("BREACH_THRESHOLD must be > 0")
    if not 0 < SCALE_UP_PROPORTION <= 1:
        errors.append("SCALE_UP_PROPORTION must be > 0 and <= 1")
    if not 0 < SCALE_DOWN_PROPORTION <= 1:
        errors.append("SCALE_DOWN_PROPORTION must be > 0 and <= 1")

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
    """Tracks scaling state for cooldown and stabilization."""

    last_scale_up_time: float = 0
    last_scale_down_time: float = 0
    # Time-weighted breach history: (timestamp, direction) where direction is "up" or "down"
    breach_history: list = field(default_factory=list)


def get_job_demand() -> int:
    """Get count of jobs needing runner capacity from GitHub.

    This counts both queued (waiting for a runner) and in_progress (currently
    running) jobs. We include in_progress because those jobs are actively using
    runner capacity and shouldn't trigger scale-down.

    Returns:
        Total job demand (queued + in_progress).
    """
    headers = {
        "Authorization": f"token {GITHUB_TOKEN}",
        "Accept": "application/vnd.github.v3+json",
    }

    # First, get runs that are queued or in_progress (jobs may be waiting)
    if ORG:
        runs_url = f"{GITHUB_API}/orgs/{ORG}/actions/runs?status=queued&per_page=100"
        in_progress_url = f"{GITHUB_API}/orgs/{ORG}/actions/runs?status=in_progress&per_page=100"
    elif OWNER and REPO:
        runs_url = f"{GITHUB_API}/repos/{OWNER}/{REPO}/actions/runs?status=queued&per_page=100"
        in_progress_url = f"{GITHUB_API}/repos/{OWNER}/{REPO}/actions/runs?status=in_progress&per_page=100"
    else:
        log.error("ORG or (OWNER and REPO) must be set")
        sys.exit(1)

    # Collect all runs that might have active jobs
    all_runs = []

    resp = requests.get(runs_url, headers=headers)
    resp.raise_for_status()
    all_runs.extend(resp.json().get("workflow_runs", []))

    resp = requests.get(in_progress_url, headers=headers)
    resp.raise_for_status()
    all_runs.extend(resp.json().get("workflow_runs", []))

    # Count queued and in_progress jobs across all runs
    queued_count = 0
    in_progress_count = 0
    for run in all_runs:
        run_id = run.get("id")
        if not run_id:
            continue

        # Get jobs for this run
        if ORG:
            # For org-level, we need the repo info from the run
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
                status = job.get("status")
                runner_name = job.get("runner_name")

                if status == "queued":
                    # Count queued jobs targeting self-hosted runners
                    if is_self_hosted_job(job):
                        queued_count += 1
                elif status == "in_progress":
                    # Count in_progress jobs only if on our runners
                    if is_our_runner(runner_name):
                        in_progress_count += 1
        except requests.RequestException as e:
            log.warning(f"Failed to get jobs for run {run_id}: {e}")

    total_demand = queued_count + in_progress_count
    log.info(f"Job demand: {total_demand} (queued={queued_count}, in_progress={in_progress_count})")
    return total_demand


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
            log.info(f"Current {WORKER_NAME} instances: {count}")
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


def should_scale_up(demand: int, current: int) -> bool:
    """Check if scale-up threshold is met."""
    threshold = current * SCALE_UP_THRESHOLD
    return demand > threshold


def should_scale_down(demand: int, current: int) -> bool:
    """Check if scale-down threshold is met."""
    threshold = current * SCALE_DOWN_THRESHOLD
    return demand < threshold


def calculate_breach_score(state: ScalingState, direction: str) -> float:
    """Calculate time-decayed breach score for a direction.

    Uses exponential decay where the weight of each breach halves every
    DECAY_HALF_LIFE_SECONDS. Recent breaches count more than old ones.
    """
    now = time.time()
    window_start = now - (STABILIZATION_WINDOW_MINUTES * 60)

    # Prune old entries outside the window
    state.breach_history = [
        (ts, d) for ts, d in state.breach_history if ts > window_start
    ]

    score = 0.0
    for ts, d in state.breach_history:
        if d == direction:
            age_seconds = now - ts
            # Exponential decay: weight halves every DECAY_HALF_LIFE_SECONDS
            weight = 0.5 ** (age_seconds / DECAY_HALF_LIFE_SECONDS)
            score += weight

    return score


def record_breach(state: ScalingState, direction: str) -> None:
    """Record a threshold breach for the given direction."""
    state.breach_history.append((time.time(), direction))


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
    demand: int, current: int, state: ScalingState
) -> tuple[str, int]:
    """
    Evaluate scaling decision based on thresholds, time-decay stabilization, and cooldown.

    Uses a time-weighted breach score instead of consecutive counts. Recent threshold
    breaches count more than older ones via exponential decay.

    Args:
        demand: Total job demand (queued + in_progress jobs).
        current: Current instance count.
        state: Scaling state for cooldowns and breach history.

    Returns:
        tuple of (action, new_count) where action is "up", "down", or "none"
    """
    # Check scale-up condition
    if should_scale_up(demand, current):
        record_breach(state, "up")
        score = calculate_breach_score(state, "up")
        log.info(
            f"Scale-up condition met (demand {demand} > {current * SCALE_UP_THRESHOLD:.1f}), "
            f"breach score: {score:.2f}/{BREACH_THRESHOLD}"
        )

        if score >= BREACH_THRESHOLD:
            if is_cooldown_active(state, "up"):
                remaining = SCALE_UP_COOLDOWN - (time.time() - state.last_scale_up_time)
                log.info(f"Scale-up blocked by cooldown ({remaining:.0f}s remaining)")
                return ("none", current)

            # Proportional scaling: scale by fraction of deficit, capped at SCALE_UP_STEP
            deficit = demand - current
            step = min(max(int(deficit * SCALE_UP_PROPORTION), 1), SCALE_UP_STEP)
            new_count = min(current + step, MAX_INSTANCES)
            log.info(f"Scale-up step: {step} (deficit={deficit}, proportion={SCALE_UP_PROPORTION})")
            if new_count > current:
                return ("up", new_count)
            else:
                log.info(f"Already at MAX_INSTANCES ({MAX_INSTANCES})")

    # Check scale-down condition
    elif should_scale_down(demand, current):
        record_breach(state, "down")
        score = calculate_breach_score(state, "down")
        log.info(
            f"Scale-down condition met (demand {demand} < {current * SCALE_DOWN_THRESHOLD:.1f}), "
            f"breach score: {score:.2f}/{BREACH_THRESHOLD}"
        )

        if score >= BREACH_THRESHOLD:
            if is_cooldown_active(state, "down"):
                remaining = SCALE_DOWN_COOLDOWN - (time.time() - state.last_scale_down_time)
                log.info(f"Scale-down blocked by cooldown ({remaining:.0f}s remaining)")
                return ("none", current)

            # Proportional scaling: scale by fraction of excess, capped at SCALE_DOWN_STEP
            excess = current - demand
            step = min(max(int(excess * SCALE_DOWN_PROPORTION), 1), SCALE_DOWN_STEP)
            new_count = max(current - step, MIN_INSTANCES)
            log.info(f"Scale-down step: {step} (excess={excess}, proportion={SCALE_DOWN_PROPORTION})")
            if new_count < current:
                return ("down", new_count)
            else:
                log.info(f"Already at MIN_INSTANCES ({MIN_INSTANCES})")

    # No scaling condition met
    else:
        log.debug("Demand stable (no scaling thresholds met)")

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


def main():
    """Main autoscaler loop (runs continuously)."""
    log.info("Starting GitHub Actions Runner Autoscaler")
    log.info(f"  Worker: {WORKER_NAME}")
    log.info(f"  Min instances: {MIN_INSTANCES}")
    log.info(f"  Max instances: {MAX_INSTANCES}")
    log.info(f"  Poll interval: {POLL_INTERVAL}s")
    log.info(f"  Scale-up threshold: {SCALE_UP_THRESHOLD}x capacity")
    log.info(f"  Scale-down threshold: {SCALE_DOWN_THRESHOLD}x capacity")
    log.info(f"  Scale-up cooldown: {SCALE_UP_COOLDOWN}s")
    log.info(f"  Scale-down cooldown: {SCALE_DOWN_COOLDOWN}s")
    log.info(f"  Stabilization window: {STABILIZATION_WINDOW_MINUTES}min")
    log.info(f"  Decay half-life: {DECAY_HALF_LIFE_SECONDS}s")
    log.info(f"  Breach threshold: {BREACH_THRESHOLD}")
    log.info(f"  Scale-up step: +{SCALE_UP_STEP} (max)")
    log.info(f"  Scale-down step: -{SCALE_DOWN_STEP} (max)")
    log.info(f"  Scale-up proportion: {SCALE_UP_PROPORTION}")
    log.info(f"  Scale-down proportion: {SCALE_DOWN_PROPORTION}")
    log.info(f"  Runner name prefix: '{RUNNER_NAME_PREFIX}' (empty=all self-hosted)")

    if not all([GITHUB_TOKEN, DO_API_TOKEN, APP_ID]):
        log.error("GITHUB_TOKEN, DO_API_TOKEN, and APP_ID are required")
        sys.exit(1)

    validate_config()

    state = ScalingState()

    while True:
        try:
            # Clean up any dead runners first
            cleanup_dead_runners()

            demand = get_job_demand()
            current = get_current_instance_count()

            action, new_count = evaluate_scaling(demand, current, state)

            if action != "none":
                log.info(f"Scaling {WORKER_NAME}: {current} -> {new_count}")
                scale_worker(new_count)
                # Set cooldown for the direction that was scaled
                if action == "up":
                    state.last_scale_up_time = time.time()
                else:
                    state.last_scale_down_time = time.time()
                # Clear breach history for this direction after scaling
                state.breach_history = [
                    (ts, d) for ts, d in state.breach_history if d != action
                ]
            else:
                log.debug(f"No scaling action ({current} instances)")

        except requests.RequestException as e:
            log.error(f"API error: {e}")
        except Exception as e:
            log.exception(f"Unexpected error: {e}")

        time.sleep(POLL_INTERVAL)


if __name__ == "__main__":
    main()
