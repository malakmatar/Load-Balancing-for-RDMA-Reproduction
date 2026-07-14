#!/usr/bin/env python3

import argparse
import csv
import json
import math
import re
from collections import defaultdict
from pathlib import Path


ROOT = Path(__file__).resolve().parent.parent
LB_MODES = {"fecmp": "0", "letflow": "6", "conweave": "9"}
BASE_RUNS = {"fecmp": "910200101", "letflow": "910200102", "conweave": "910200103"}
EXPECTED_PARTICIPANTS = [0, 1, 16, 17, 32, 33, 48, 49,
                         64, 65, 80, 81, 96, 97, 112, 113]
EXPECTED_SIZE = 4 * 1024 * 1024
EXPECTED_T0_NS = 2_010_000_000
FATAL_RE = re.compile(
    r"assert|assertion failed|segmentation fault|fatal error|aborted|core dumped|traceback|killed",
    re.IGNORECASE,
)


def read_config(path):
    result = {}
    for line in path.read_text().splitlines():
        fields = line.split(maxsplit=1)
        if len(fields) == 2:
            result[fields[0]] = fields[1].strip()
    return result


def seconds_to_ns(value):
    whole, fraction = value.split(".")
    return int(whole) * 1_000_000_000 + int((fraction + "000000000")[:9])


def read_trace(path):
    lines = path.read_text().splitlines()
    declared = int(lines[0])
    rows = []
    for line in lines[1:]:
        src, dst, pg, size, start = line.split()
        rows.append((int(src), int(dst), int(pg), int(size), seconds_to_ns(start)))
    if declared != len(rows):
        raise RuntimeError("trace declared/actual flow count mismatch")
    return rows


def read_fct(path):
    records = []
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) != 8:
            raise RuntimeError("invalid FCT row: {}".format(line))
        values = list(map(int, fields))
        records.append({
            "src": values[0], "dst": values[1], "size": values[4],
            "start_ns": values[5], "fct_ns": values[6],
            "finish_ns": values[5] + values[6],
        })
    return records


def nearest_rank(values, percentile):
    ordered = sorted(values)
    return ordered[max(0, math.ceil(percentile / 100.0 * len(ordered)) - 1)]


def pfc_events(path):
    pauses = 0
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) != 5:
            raise RuntimeError("invalid PFC row")
        pauses += int(fields[4]) == 1
    return pauses


def cnp_events(path):
    total = 0
    for line in path.read_text().splitlines():
        fields = line.split()
        if len(fields) != 5:
            raise RuntimeError("invalid CNP row")
        total += int(fields[4])
    return total


def voq_metrics(path):
    by_time = defaultdict(lambda: [0, 0])
    for line in path.read_text().splitlines():
        fields = line.split(",")
        if len(fields) != 4:
            raise RuntimeError("invalid VOQ row")
        timestamp, _tor, queues, packets = map(int, fields)
        by_time[timestamp][0] += queues
        by_time[timestamp][1] += packets
    if not by_time:
        return 0, 0
    return max(value[0] for value in by_time.values()), max(value[1] for value in by_time.values())


def uplink_metrics(path, start_ns, finish_ns, detail_path, run_id, algorithm):
    samples = defaultdict(list)
    for line in path.read_text().splitlines():
        fields = line.split(",")
        if len(fields) != 4:
            raise RuntimeError("invalid uplink row")
        timestamp, tor, interface, tx_bytes = map(int, fields)
        samples[(tor, interface)].append((timestamp, tx_bytes))
    detail = []
    by_tor = defaultdict(list)
    for (tor, interface), values in sorted(samples.items()):
        values.sort()
        before = [value for value in values if value[0] <= start_ns]
        after = [value for value in values if value[0] >= finish_ns]
        if not before or not after:
            raise RuntimeError("uplink samples do not bracket the communication phase")
        first = before[-1]
        last = after[0]
        delta = last[1] - first[1]
        if delta < 0:
            raise RuntimeError("uplink byte counter decreased")
        detail.append({
            "run_id": run_id, "algorithm": algorithm, "tor_id": tor,
            "interface": interface, "sample_start_ns": first[0],
            "sample_finish_ns": last[0], "transmitted_bytes": delta,
        })
        by_tor[tor].append(delta)
    with detail_path.open("w", newline="") as output_file:
        writer = csv.DictWriter(output_file, fieldnames=list(detail[0].keys()), lineterminator="\n")
        writer.writeheader()
        writer.writerows(detail)
    imbalances = []
    for deltas in by_tor.values():
        average = sum(deltas) / len(deltas)
        if average > 0:
            imbalances.append((max(deltas) - min(deltas)) / average * 100.0)
    return (sum(imbalances) / len(imbalances) if imbalances else 0.0,
            sum(row["transmitted_bytes"] for row in detail), len(detail))


def log_counter(log_text, label):
    match = re.search(re.escape(label) + r"\s*:\s*(\d+)", log_text)
    if not match:
        raise RuntimeError("missing ConWeave counter: {}".format(label))
    return int(match.group(1))


def validate_config(config, algorithm, timeout_setting):
    required = {
        "TOPOLOGY_FILE": "config/leaf_spine_128_100G_OS2.txt",
        "FLOW_FILE": "config/hpc_4mib/hpc_alltoall_sync_4mib.txt",
        "FLOWGEN_START_TIME": "2.0",
        "FLOWGEN_STOP_TIME": "2.02",
        "CC_MODE": "1",
        "LB_MODE": LB_MODES[algorithm],
        "ENABLE_PFC": "1",
        "ENABLE_IRN": "0",
        "RANDOM_SEED": "1",
    }
    for key, value in required.items():
        if config.get(key) != value:
            raise RuntimeError("config mismatch for {}: {} != {}".format(key, config.get(key), value))
    expected_extra = {"na": "4", "default": "4", "half": "2", "double": "8"}[timeout_setting]
    if config.get("CONWEAVE_REPLY_TIMEOUT_EXTRA") != expected_extra:
        raise RuntimeError("reply timeout setting mismatch")

    baseline = read_config(ROOT / "mix/output" / BASE_RUNS[algorithm] / "config.txt")
    ignored = {key for key in set(config) | set(baseline) if key.endswith("_FILE")}
    ignored.add("CONWEAVE_REPLY_TIMEOUT_EXTRA")
    for key in sorted((set(config) & set(baseline)) - ignored):
        if config[key] != baseline[key]:
            raise RuntimeError("parameter {} changed from accepted 1 MiB run".format(key))


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--run-id", required=True)
    parser.add_argument("--algorithm", required=True, choices=tuple(LB_MODES))
    parser.add_argument("--trace", type=Path, required=True)
    parser.add_argument("--reply-timeout-setting", required=True,
                        choices=("na", "default", "half", "double"))
    args = parser.parse_args()

    output = ROOT / "mix/output" / args.run_id
    paths = {
        "config": output / "config.txt",
        "log": output / "config.log",
        "fct": output / (args.run_id + "_out_fct.txt"),
        "pfc": output / (args.run_id + "_out_pfc.txt"),
        "cnp": output / (args.run_id + "_out_cnp.txt"),
        "uplink": output / (args.run_id + "_out_uplink.txt"),
        "voq": output / (args.run_id + "_out_voq.txt"),
    }
    required_names = ["config", "log", "fct", "pfc", "cnp", "uplink"]
    if args.algorithm == "conweave":
        required_names.append("voq")
    for name in required_names:
        path = paths[name]
        if not path.is_file():
            raise RuntimeError("missing {} output: {}".format(name, path))
    for name in ("config", "log", "fct", "uplink"):
        if paths[name].stat().st_size == 0:
            raise RuntimeError("required {} output is empty".format(name))

    trace_path = args.trace if args.trace.is_absolute() else ROOT / args.trace
    trace = read_trace(trace_path)
    expected_pairs = {(src, dst) for src in EXPECTED_PARTICIPANTS
                      for dst in EXPECTED_PARTICIPANTS if src != dst}
    if len(trace) != 240 or {(row[0], row[1]) for row in trace} != expected_pairs:
        raise RuntimeError("trace is not the expected 16-host directed all-to-all")
    if any(row[0] == row[1] or row[2] != 3 or row[3] != EXPECTED_SIZE or
           row[4] != EXPECTED_T0_NS for row in trace):
        raise RuntimeError("trace size, PG, endpoints, or synchronized start is invalid")

    config = read_config(paths["config"])
    validate_config(config, args.algorithm, args.reply_timeout_setting)
    log_text = paths["log"].read_text(errors="replace")
    if FATAL_RE.search(log_text):
        raise RuntimeError("fatal signature found in config.log")
    if not re.search(r"finished so far:\s*240/\s*total:\s*240", log_text):
        raise RuntimeError("missing exact simulator completion marker")

    records = read_fct(paths["fct"])
    if len(records) != 240:
        raise RuntimeError("completed {} of 240 flows".format(len(records)))
    expected = sorted((row[0], row[1], row[3], row[4]) for row in trace)
    actual = sorted((row["src"], row["dst"], row["size"], row["start_ns"]) for row in records)
    if actual != expected:
        raise RuntimeError("raw FCT flow set differs from trace")
    fcts = [row["fct_ns"] for row in records]
    finishes = [row["finish_ns"] for row in records]
    if any(value <= 0 for value in fcts) or max(finishes) > 2_020_000_000:
        raise RuntimeError("FCT values are invalid or exceed simulation stop")
    completion_us = (max(finishes) - min(row["start_ns"] for row in records)) / 1000.0
    max_fct_us = max(fcts) / 1000.0
    if completion_us != max_fct_us:
        raise RuntimeError("synchronized phase completion and maximum FCT disagree")

    detail_path = output / (args.run_id + "_uplink_bytes.csv")
    imbalance, uplink_bytes, uplink_count = uplink_metrics(
        paths["uplink"], EXPECTED_T0_NS, max(finishes), detail_path, args.run_id, args.algorithm)

    conweave = args.algorithm == "conweave"
    counters = {}
    if conweave:
        labels = {
            "reroutes": "Number of Rerouting",
            "ooo_packets": "Number of OoO enqueued pkts",
            "voq_flushes_total": "Number of VOQ Flush Total",
            "voq_flushes_tail": "Number of VOQ Flush by TAIL",
            "notify_count": "Number of NOTIFY Sent",
            "timely_clear_count": "Number of Timely CLEAR (TAIL's Reply)",
            "timely_rtt_reply_count": "Number of Timely RTT_REPLY (INIT's Reply)",
        }
        counters = {key: log_counter(log_text, label) for key, label in labels.items()}
        counters["voq_flushes_timeout"] = (counters["voq_flushes_total"] -
                                            counters["voq_flushes_tail"])
        if counters["voq_flushes_timeout"] < 0:
            raise RuntimeError("VOQ flush counters are inconsistent")
        max_queues, max_packets = voq_metrics(paths["voq"])
    else:
        max_queues, max_packets = None, None

    summary = {
        "run_id": args.run_id,
        "algorithm": args.algorithm,
        "flow_size_bytes": EXPECTED_SIZE,
        "reply_timeout_setting": args.reply_timeout_setting,
        "reply_timeout_extra_us": ({"default": 4, "half": 2, "double": 8}.get(
            args.reply_timeout_setting) if conweave else None),
        "completed_flows": len(records),
        "expected_flows": 240,
        "phase_completion_time_us": completion_us,
        "mean_fct_us": sum(fcts) / len(fcts) / 1000.0,
        "p99_fct_us": nearest_rank(fcts, 99) / 1000.0,
        "max_fct_us": max_fct_us,
        "pfc_events": pfc_events(paths["pfc"]),
        "cnp_events": cnp_events(paths["cnp"]),
        "uplink_imbalance": imbalance,
        "uplink_transmitted_bytes": uplink_bytes,
        "uplink_count": uplink_count,
        "uplink_detail_path": str(detail_path.relative_to(ROOT)),
        "reroutes": counters.get("reroutes"),
        "ooo_packets": counters.get("ooo_packets"),
        "max_active_reorder_queues": max_queues,
        "max_reorder_occupancy_packets": max_packets,
        "voq_flushes_total": counters.get("voq_flushes_total"),
        "voq_flushes_tail": counters.get("voq_flushes_tail"),
        "voq_flushes_timeout": counters.get("voq_flushes_timeout"),
        "notify_count": counters.get("notify_count"),
        "timely_clear_count": counters.get("timely_clear_count"),
        "timely_rtt_reply_count": counters.get("timely_rtt_reply_count"),
        "trace_path": str(trace_path.relative_to(ROOT)),
        "topology": "leaf_spine_128_100G_OS2",
        "pfc": 1,
        "irn": 0,
        "random_seed": 1,
        "simulation_duration_s": 0.02,
        "log_fatal_error": False,
        "validation_status": "accepted",
    }
    for key in ("phase_completion_time_us", "mean_fct_us", "p99_fct_us",
                "max_fct_us", "uplink_imbalance"):
        if not math.isfinite(summary[key]):
            raise RuntimeError("non-finite metric {}".format(key))
    summary_path = output / (args.run_id + "_hpc_4mib_summary.json")
    summary_path.write_text(json.dumps(summary, indent=2, sort_keys=True) + "\n")
    print("accepted run={} algorithm={} flows=240 completion_us={:.3f} reroutes={}".format(
        args.run_id, args.algorithm, completion_us, summary["reroutes"]))
    print("summary={}".format(summary_path.relative_to(ROOT)))


if __name__ == "__main__":
    main()
