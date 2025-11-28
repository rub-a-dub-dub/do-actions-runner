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

if [ -n "${ORG:-}" ]
then
  API_PATH=orgs/${ORG}
  CONFIG_PATH=${ORG}
elif [ -n "${OWNER:-}" ] && [ -n "${REPO:-}" ]
then
  API_PATH=repos/${OWNER}/${REPO}
  CONFIG_PATH=${OWNER}/${REPO}
else
  echo "[ORG] or [OWNER and REPO] is required"
  exit 1
fi

RUNNER_TOKEN=$(curl -s -X POST -H "authorization: token ${TOKEN}" "https://api.github.com/${API_PATH}/actions/runners/registration-token" | jq -r .token)

cleanup() {
  echo "Cleaning up ${#RUNNER_DIRS[@]} runner(s)..."
  for dir in "${RUNNER_DIRS[@]}"; do
    echo "Deregistering runner in ${dir}..."
    (cd "$dir" && ./config.sh remove --token "${RUNNER_TOKEN}") || true
  done
}

trap 'cleanup' SIGTERM SIGINT

# Start multiple runners
for i in $(seq 1 $RUNNERS_PER_INSTANCE); do
  if [ "$RUNNERS_PER_INSTANCE" -eq 1 ]; then
    # Single runner: use original directory and naming
    RUNNER_DIR="/home/actions/actions-runner"
    RUNNER_NAME="${NAME:+${NAME}-}$(hostname)"
  else
    # Multiple runners: use separate directories with numbered names
    RUNNER_DIR="/home/actions/runner-${i}"
    RUNNER_NAME="${NAME:+${NAME}-}$(hostname)-${i}"

    # Copy runner files to separate directory if needed
    if [ ! -d "$RUNNER_DIR" ]; then
      echo "Creating runner directory: ${RUNNER_DIR}"
      cp -r /home/actions/actions-runner "$RUNNER_DIR"
    fi
  fi

  # Configure runner
  echo "Configuring runner ${i}: ${RUNNER_NAME}"
  (cd "$RUNNER_DIR" && ./config.sh \
    --url "https://github.com/${CONFIG_PATH}" \
    --token "${RUNNER_TOKEN}" \
    --name "${RUNNER_NAME}" \
    --unattended)

  # Start runner in background
  (cd "$RUNNER_DIR" && ./run.sh "$@") &
  RUNNER_PIDS+=($!)
  RUNNER_DIRS+=("$RUNNER_DIR")

  echo "Started runner ${i}: ${RUNNER_NAME} (PID: ${RUNNER_PIDS[-1]})"
done

echo "All ${#RUNNER_PIDS[@]} runner(s) started. Waiting..."

# Wait for all runners
wait "${RUNNER_PIDS[@]}"
