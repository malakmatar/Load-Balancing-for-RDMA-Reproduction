#!/bin/bash

set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
STATE="$ROOT/hpc_128host"
TRACE="hpc_128host/hpc_alltoall_sync_128host_4mib"
TRACE_PATH="config/${TRACE}.txt"
HISTORY="$STATE/hpc_128host.history"
mkdir -p "$STATE/logs" "$STATE/exitcodes" "$STATE/resources"

if [[ ! -e "$HISTORY" ]]; then
  printf '%s\n' 'date,id,ccmode,lbmode,cwh_tx_expiry_time,cwh_extra_reply_deadline,cwh_path_pause_time,cwh_extra_voq_flush_time,cwh_default_voq_waiting_time,pfc,irn,has_win,var_win,topo,bw,cdf,load,time' > "$HISTORY"
fi

tree_pids() {
  local root_pid="$1" child
  echo "$root_pid"
  for child in $(pgrep -P "$root_pid" 2>/dev/null || true); do
    tree_pids "$child"
  done
}

run_measured() {
  local resource_file="$1"
  shift
  local start_ns end_ns peak_kb=0 rss_kb pid child value status
  start_ns=$(date +%s%N)
  "$@" &
  pid=$!
  while kill -0 "$pid" 2>/dev/null; do
    rss_kb=0
    for child in $(tree_pids "$pid"); do
      if [[ -r "/proc/$child/status" ]]; then
        value=$(awk '/^VmRSS:/ {print $2}' "/proc/$child/status" 2>/dev/null || true)
        rss_kb=$((rss_kb + ${value:-0}))
      fi
    done
    (( rss_kb > peak_kb )) && peak_kb=$rss_kb
    sleep 0.5
  done
  wait "$pid"
  status=$?
  end_ns=$(date +%s%N)
  {
    echo "runtime_ns=$((end_ns - start_ns))"
    awk -v ns="$((end_ns - start_ns))" 'BEGIN {printf "runtime_seconds=%.6f\n", ns / 1000000000}'
    echo "peak_process_tree_rss_kb=$peak_kb"
    echo "command_exit_code=$status"
  } > "$resource_file"
  return "$status"
}

run_case() {
  local algorithm="$1" run_id="$2" setting="$3" extra_us="$4" label="$5"
  local log="$STATE/logs/${label}.log" exit_file="$STATE/exitcodes/${label}.exitcode"
  local resource_file="$STATE/resources/${label}.txt"
  if [[ -e "$ROOT/mix/output/$run_id" ]]; then
    echo "Refusing to reuse run ID $run_id" | tee "$log"
    echo 98 > "$exit_file"
    return 98
  fi
  local -a timeout_arg=()
  if [[ "$extra_us" != "default" ]]; then
    timeout_arg=(--cwh-extra-reply-deadline-us "$extra_us")
  fi
  set +e
  (
    cd "$ROOT" || exit 97
    run_measured "$resource_file" python3 run.py \
      --lb "$algorithm" --pfc 1 --irn 0 --simul_time 0.2 --netload 50 \
      --topo leaf_spine_128_100G_OS2 --cdf alltoall_sync_128host_4mib \
      --flow-file "$TRACE" --run-id "$run_id" --history-file "$HISTORY" \
      --skip-analysis "${timeout_arg[@]}"
    status=$?
    [[ $status -eq 0 ]] || exit "$status"
    python3 analysis/analyze_hpc_128host_run.py --run-id "$run_id" \
      --algorithm "$algorithm" --trace "$TRACE_PATH" \
      --reply-timeout-setting "$setting" --resource-file "$resource_file"
  ) > "$log" 2>&1
  local status=$?
  set -e
  echo "$status" > "$exit_file"
  return "$status"
}

case "${1:-}" in
  smoke)
    run_case fecmp 920128000 na default smoke_fecmp
    ;;
  main-sequential)
    run_case fecmp 920128101 na default alltoall_128host_fecmp || exit 1
    run_case letflow 920128102 na default alltoall_128host_letflow || exit 1
    run_case conweave 920128103 default default alltoall_128host_conweave_default || exit 1
    ;;
  main-parallel)
    run_case fecmp 920128101 na default alltoall_128host_fecmp & p1=$!
    run_case letflow 920128102 na default alltoall_128host_letflow & p2=$!
    run_case conweave 920128103 default default alltoall_128host_conweave_default & p3=$!
    status=0
    wait "$p1" || status=1
    wait "$p2" || status=1
    wait "$p3" || status=1
    exit "$status"
    ;;
  timeout)
    run_case conweave 920128104 half 2 alltoall_128host_conweave_half & p1=$!
    run_case conweave 920128105 double 8 alltoall_128host_conweave_double & p2=$!
    status=0
    wait "$p1" || status=1
    wait "$p2" || status=1
    exit "$status"
    ;;
  *)
    echo "usage: $0 {smoke|main-sequential|main-parallel|timeout}" >&2
    exit 2
    ;;
esac
