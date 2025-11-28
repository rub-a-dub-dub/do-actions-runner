#!/usr/bin/env bash
set -eEuo pipefail

# Increase file descriptor limit for actions that extract many files
ulimit -n 65536 2>/dev/null || ulimit -n 32768 2>/dev/null || ulimit -n 16384 2>/dev/null || true
echo "File descriptor limit: $(ulimit -n)"

# Number of runner processes per container (default: 1)
RUNNERS_PER_INSTANCE=${RUNNERS_PER_INSTANCE:-1}
echo "Runners per instance: ${RUNNERS_PER_INSTANCE}"

# Arrays to track runner processes and directories
RUNNER_PIDS=()
RUNNER_DIRS=()

if [ -z "${TOKEN:-}" ]
then
  echo "TOKEN is required"
  exit 1
fi

# Determine mode: org-level or repo-level
if [ -n "${ORG:-}" ]
then
  MODE="org"
  echo "Mode: organization-level (${ORG})"
elif [ -n "${OWNER:-}" ] && [ -n "${REPOS:-}${REPO:-}" ]
then
  MODE="repo"
  # Build repos array from REPOS (comma-separated) or single REPO
  if [ -n "${REPOS:-}" ]; then
    IFS=',' read -ra REPO_LIST <<< "${REPOS}"
  else
    REPO_LIST=("${REPO}")
  fi
  echo "Mode: repo-level (${OWNER}/${REPO_LIST[*]})"
else
  echo "[ORG] or [OWNER and REPOS/REPO] is required"
  exit 1
fi

cleanup() {
  echo "Shutting down ${#RUNNER_PIDS[@]} ephemeral runner(s)..."
  # Ephemeral runners auto-deregister on exit, just signal them to stop
  for pid in "${RUNNER_PIDS[@]}"; do
    kill -TERM "$pid" 2>/dev/null || true
  done
}

trap 'cleanup' SIGTERM SIGINT

# Counter for unique runner directories
RUNNER_INDEX=0

start_runner() {
  local CONFIG_PATH="$1"
  local RUNNER_TOKEN="$2"
  local REPO_NAME="$3"  # empty for org-level

  RUNNER_INDEX=$((RUNNER_INDEX + 1))

  # Build runner name: {prefix}-{repo}-{hostname} or {prefix}-{hostname} for org
  if [ -n "$REPO_NAME" ]; then
    RUNNER_NAME="${NAME:+${NAME}-}${REPO_NAME}-$(hostname)"
  else
    RUNNER_NAME="${NAME:+${NAME}-}$(hostname)"
  fi

  # Add index suffix if multiple runners per instance
  if [ "$RUNNERS_PER_INSTANCE" -gt 1 ] || [ "${#REPO_LIST[@]:-1}" -gt 1 ]; then
    RUNNER_NAME="${RUNNER_NAME}-${RUNNER_INDEX}"
  fi

  # Use separate directories for multiple runners
  if [ "$RUNNER_INDEX" -eq 1 ]; then
    RUNNER_DIR="/home/actions/actions-runner"
  else
    RUNNER_DIR="/home/actions/runner-${RUNNER_INDEX}"
    if [ ! -d "$RUNNER_DIR" ]; then
      echo "Creating runner directory: ${RUNNER_DIR}"
      cp -r /home/actions/actions-runner "$RUNNER_DIR"
    fi
  fi

  # Configure runner (ephemeral: exit after one job)
  echo "Configuring runner ${RUNNER_INDEX}: ${RUNNER_NAME} -> ${CONFIG_PATH}"
  (cd "$RUNNER_DIR" && ./config.sh \
    --url "https://github.com/${CONFIG_PATH}" \
    --token "${RUNNER_TOKEN}" \
    --name "${RUNNER_NAME}" \
    --unattended \
    --ephemeral)

  # Start runner in background
  (cd "$RUNNER_DIR" && ./run.sh "$@") &
  RUNNER_PIDS+=($!)
  RUNNER_DIRS+=("$RUNNER_DIR")

  echo "Started runner ${RUNNER_INDEX}: ${RUNNER_NAME} (PID: ${RUNNER_PIDS[-1]})"
}

# Start runners based on mode
if [ "$MODE" = "org" ]; then
  # Org-level: single registration token for all runners
  API_PATH="orgs/${ORG}"
  CONFIG_PATH="${ORG}"
  RUNNER_TOKEN=$(curl -s -X POST -H "authorization: token ${TOKEN}" \
    "https://api.github.com/${API_PATH}/actions/runners/registration-token" | jq -r .token)

  for i in $(seq 1 $RUNNERS_PER_INSTANCE); do
    start_runner "$CONFIG_PATH" "$RUNNER_TOKEN" ""
  done
else
  # Repo-level: registration token per repo
  for CURRENT_REPO in "${REPO_LIST[@]}"; do
    API_PATH="repos/${OWNER}/${CURRENT_REPO}"
    CONFIG_PATH="${OWNER}/${CURRENT_REPO}"

    echo "Getting registration token for ${CONFIG_PATH}..."
    RUNNER_TOKEN=$(curl -s -X POST -H "authorization: token ${TOKEN}" \
      "https://api.github.com/${API_PATH}/actions/runners/registration-token" | jq -r .token)

    for i in $(seq 1 $RUNNERS_PER_INSTANCE); do
      start_runner "$CONFIG_PATH" "$RUNNER_TOKEN" "$CURRENT_REPO"
    done
  done
fi

echo "All ${#RUNNER_PIDS[@]} runner(s) started. Waiting..."

# Wait for all runners
wait "${RUNNER_PIDS[@]}"
