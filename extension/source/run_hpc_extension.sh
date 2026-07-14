#!/bin/bash

set -uo pipefail

ROOT="$(cd "$(dirname "$0")" && pwd)"
STATE_DIR="$ROOT/hpc_extension"
LOG_DIR="$STATE_DIR/logs"
EXIT_DIR="$STATE_DIR/exitcodes"
HISTORY_FILE="$ROOT/mix/hpc_extension.history"
TOPOLOGY="leaf_spine_128_100G_OS2"

mkdir -p "$LOG_DIR" "$EXIT_DIR"

write_history_header() {
  if [[ ! -e "$HISTORY_FILE" ]]; then
    printf '%s\n' 'date,id,ccmode,lbmode,cwh_tx_expiry_time,cwh_extra_reply_deadline,cwh_path_pause_time,cwh_extra_voq_flush_time,cwh_default_voq_waiting_time,pfc,irn,has_win,var_win,topo,bw,cdf,load,time' > "$HISTORY_FILE"
  fi
}

run_case() {
  local workload="$1"
  local synchronization="$2"
  local algorithm="$3"
  local run_id="$4"
  local trace="$5"
  local label="${6:-${workload}_${algorithm}}"
  local stdout_log="$LOG_DIR/${label}.log"
  local exit_file="$EXIT_DIR/${label}.exitcode"

  if [[ -e "$ROOT/mix/output/$run_id" ]]; then
    echo "Refusing to reuse existing run ID $run_id" | tee "$stdout_log"
    echo 98 > "$exit_file"
    return 98
  fi

  set +e
  (
    cd "$ROOT" || exit 97
    python3 run.py \
      --lb "$algorithm" \
      --pfc 1 \
      --irn 0 \
      --simul_time 0.02 \
      --netload 50 \
      --topo "$TOPOLOGY" \
      --cdf "$workload" \
      --flow-file "hpc_extension/$trace" \
      --run-id "$run_id" \
      --history-file "$HISTORY_FILE" \
      --skip-analysis
    run_status=$?
    if [[ $run_status -ne 0 ]]; then
      exit "$run_status"
    fi
    python3 analysis/analyze_hpc_run.py \
      --run-id "$run_id" \
      --workload "$workload" \
      --algorithm "$algorithm" \
      --trace "config/hpc_extension/$trace.txt"
  ) > "$stdout_log" 2>&1
  local status=$?
  set -e
  echo "$status" > "$exit_file"
  return "$status"
}

write_official_manifest() {
  cat > "$STATE_DIR/hpc_extension_run_manifest.csv" <<'EOF'
run_id,workload,synchronization,algorithm,trace_path,expected_flows,output_path,stdout_log,exitcode_path
910200101,alltoall_sync,exact,fecmp,config/hpc_extension/hpc_alltoall_sync.txt,240,mix/output/910200101,hpc_extension/logs/alltoall_sync_fecmp.log,hpc_extension/exitcodes/alltoall_sync_fecmp.exitcode
910200102,alltoall_sync,exact,letflow,config/hpc_extension/hpc_alltoall_sync.txt,240,mix/output/910200102,hpc_extension/logs/alltoall_sync_letflow.log,hpc_extension/exitcodes/alltoall_sync_letflow.exitcode
910200103,alltoall_sync,exact,conweave,config/hpc_extension/hpc_alltoall_sync.txt,240,mix/output/910200103,hpc_extension/logs/alltoall_sync_conweave.log,hpc_extension/exitcodes/alltoall_sync_conweave.exitcode
910200201,alltoall_skew50us,50us_skew,fecmp,config/hpc_extension/hpc_alltoall_skew50us.txt,240,mix/output/910200201,hpc_extension/logs/alltoall_skew50us_fecmp.log,hpc_extension/exitcodes/alltoall_skew50us_fecmp.exitcode
910200202,alltoall_skew50us,50us_skew,letflow,config/hpc_extension/hpc_alltoall_skew50us.txt,240,mix/output/910200202,hpc_extension/logs/alltoall_skew50us_letflow.log,hpc_extension/exitcodes/alltoall_skew50us_letflow.exitcode
910200203,alltoall_skew50us,50us_skew,conweave,config/hpc_extension/hpc_alltoall_skew50us.txt,240,mix/output/910200203,hpc_extension/logs/alltoall_skew50us_conweave.log,hpc_extension/exitcodes/alltoall_skew50us_conweave.exitcode
910200301,incast_sync,exact,fecmp,config/hpc_extension/hpc_incast_sync.txt,15,mix/output/910200301,hpc_extension/logs/incast_sync_fecmp.log,hpc_extension/exitcodes/incast_sync_fecmp.exitcode
910200302,incast_sync,exact,letflow,config/hpc_extension/hpc_incast_sync.txt,15,mix/output/910200302,hpc_extension/logs/incast_sync_letflow.log,hpc_extension/exitcodes/incast_sync_letflow.exitcode
910200303,incast_sync,exact,conweave,config/hpc_extension/hpc_incast_sync.txt,15,mix/output/910200303,hpc_extension/logs/incast_sync_conweave.log,hpc_extension/exitcodes/incast_sync_conweave.exitcode
EOF
}

run_batch() {
  local -a pids=()
  local status=0
  while [[ $# -gt 0 ]]; do
    run_case "$1" "$2" "$3" "$4" "$5" &
    pids+=("$!")
    shift 5
  done
  for pid in "${pids[@]}"; do
    if ! wait "$pid"; then
      status=1
    fi
  done
  return "$status"
}

write_history_header

case "${1:-}" in
  smoke)
    cat > "$STATE_DIR/hpc_extension_smoke_manifest.csv" <<'EOF'
run_id,workload,synchronization,algorithm,trace_path,expected_flows,output_path,stdout_log,exitcode_path
910200000,alltoall_sync,exact,fecmp,hpc_extension/failed_smoke/910200000/trace.txt,240,mix/output/910200000,hpc_extension/failed_smoke/910200000/runner.log,hpc_extension/failed_smoke/910200000/exitcode
EOF
    run_case alltoall_sync exact fecmp 910200000 hpc_alltoall_sync alltoall_sync_fecmp_smoke
    ;;
  smoke-retry)
    cat >> "$STATE_DIR/hpc_extension_smoke_manifest.csv" <<'EOF'
910200001,alltoall_sync,exact,fecmp,config/hpc_extension/hpc_alltoall_sync.txt,240,mix/output/910200001,hpc_extension/logs/alltoall_sync_fecmp_smoke_retry1.log,hpc_extension/exitcodes/alltoall_sync_fecmp_smoke_retry1.exitcode
EOF
    run_case alltoall_sync exact fecmp 910200001 hpc_alltoall_sync alltoall_sync_fecmp_smoke_retry1
    ;;
  official)
    write_official_manifest
    overall=0
    run_batch \
      alltoall_sync exact fecmp 910200101 hpc_alltoall_sync \
      alltoall_sync exact letflow 910200102 hpc_alltoall_sync \
      alltoall_sync exact conweave 910200103 hpc_alltoall_sync || overall=1
    run_batch \
      alltoall_skew50us 50us_skew fecmp 910200201 hpc_alltoall_skew50us \
      alltoall_skew50us 50us_skew letflow 910200202 hpc_alltoall_skew50us \
      alltoall_skew50us 50us_skew conweave 910200203 hpc_alltoall_skew50us || overall=1
    run_batch \
      incast_sync exact fecmp 910200301 hpc_incast_sync \
      incast_sync exact letflow 910200302 hpc_incast_sync \
      incast_sync exact conweave 910200303 hpc_incast_sync || overall=1
    exit "$overall"
    ;;
  *)
    echo "usage: $0 {smoke|smoke-retry|official}" >&2
    exit 2
    ;;
esac
