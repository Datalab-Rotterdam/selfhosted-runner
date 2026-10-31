#!/usr/bin/env bash
set -euo pipefail

: "${REPO_URL:?REPO_URL required}"
: "${GITHUB_PAT:?GITHUB_PAT required}"

GITHUB_API_URL="${GITHUB_API_URL:-https://api.github.com}"
RUNNER_NAME="${RUNNER_NAME:-$(hostname)}"
RUNNER_LABELS="${RUNNER_LABELS:-}"
RUNNER_GROUP="${RUNNER_GROUP:-}"
RUNNER_EPHEMERAL="${RUNNER_EPHEMERAL:-false}"

cd /home/runner/actions-runner

# https://github.com/owner/repo -> repos/owner/repo ; https://github.com/org -> orgs/org
path="${REPO_URL#*://*/}"
path="${path%/}"
if [[ "$path" == */* ]]; then
  scope="repos/${path}"
else
  scope="orgs/${path}"
fi

token() { # $1 = registration-token | remove-token
  local out code
  out=$(curl -sS -w '\n%{http_code}' -X POST \
    -H "Authorization: Bearer ${GITHUB_PAT}" \
    -H "Accept: application/vnd.github+json" \
    "${GITHUB_API_URL}/${scope}/actions/runners/$1") || { echo "ERROR: cannot reach ${GITHUB_API_URL}" >&2; return 1; }
  code="${out##*$'\n'}"
  out="${out%$'\n'*}"
  if [[ "$code" != 2* ]]; then
    echo "ERROR: GitHub API ${code} for ${scope}: $(jq -r '.message // empty' <<<"$out")" >&2
    return 1
  fi
  jq -r .token <<<"$out"
}

# drop stale registration from a previous run of this container
rm -f .runner .credentials .credentials_rsaparams

reg_token="$(token registration-token)"

args=(--url "$REPO_URL" --token "$reg_token" --name "$RUNNER_NAME"
      --unattended --replace)
[[ -n "$RUNNER_LABELS" ]] && args+=(--labels "$RUNNER_LABELS")
[[ -n "$RUNNER_GROUP" ]] && args+=(--runnergroup "$RUNNER_GROUP")
[[ "$RUNNER_EPHEMERAL" == "true" ]] && args+=(--ephemeral)

./config.sh "${args[@]}"

cleanup() {
  echo "Deregistering runner ${RUNNER_NAME}"
  kill -TERM "$child" 2>/dev/null || true
  wait "$child" 2>/dev/null || true
  ./config.sh remove --token "$(token remove-token)" || true
}
trap cleanup TERM INT

./run.sh &
child=$!
wait "$child"
status=$?
trap - TERM INT
exit "$status"
