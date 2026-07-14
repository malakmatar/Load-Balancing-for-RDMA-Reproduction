#!/usr/bin/env python3

import hashlib
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[2]
TOPOLOGY = ROOT / "config/leaf_spine_128_100G_OS2.txt"
OUTPUT = ROOT / "config/hpc_128host/hpc_alltoall_sync_128host_4mib.txt"
METADATA = ROOT / "config/hpc_128host/hpc_alltoall_sync_128host_4mib_metadata.json"
HOST_COUNT = 128
FLOW_COUNT = HOST_COUNT * (HOST_COUNT - 1)
FLOW_SIZE = 4 * 1024 * 1024
START_TEXT = "2.0100000001"
START_NS = 2_010_000_000


def sha256(path):
    digest = hashlib.sha256()
    digest.update(path.read_bytes())
    return digest.hexdigest()


def host_to_rack(path):
    lines = path.read_text().splitlines()
    node_count, switch_count, link_count = map(int, lines[0].split())
    host_count = node_count - switch_count
    mapping = {}
    for line in lines[2:2 + link_count]:
        left, right = map(int, line.split()[:2])
        if left < host_count <= right:
            mapping[left] = right
        elif right < host_count <= left:
            mapping[right] = left
    if host_count != HOST_COUNT or len(mapping) != HOST_COUNT:
        raise RuntimeError("topology does not contain the expected 128 mapped hosts")
    expected = {host: 128 + host // 16 for host in range(HOST_COUNT)}
    if mapping != expected:
        raise RuntimeError("topology host-to-rack placement differs from expected mapping")
    return mapping


def validate_text(text):
    lines = text.splitlines()
    if int(lines[0]) != FLOW_COUNT or len(lines) != FLOW_COUNT + 1:
        raise RuntimeError("trace count mismatch")
    pairs = set()
    for line in lines[1:]:
        src, dst, pg, size, start = line.split()
        src, dst, pg, size = map(int, (src, dst, pg, size))
        if src == dst or not (0 <= src < HOST_COUNT and 0 <= dst < HOST_COUNT):
            raise RuntimeError("invalid endpoint pair")
        if pg != 3 or size != FLOW_SIZE or start != START_TEXT:
            raise RuntimeError("trace PG, size, or start mismatch")
        if (src, dst) in pairs:
            raise RuntimeError("duplicate directed pair")
        pairs.add((src, dst))
    expected = {(src, dst) for src in range(HOST_COUNT)
                for dst in range(HOST_COUNT) if src != dst}
    if pairs != expected:
        raise RuntimeError("trace is not a complete 128-host directed all-to-all")


def main():
    mapping = host_to_rack(TOPOLOGY)
    lines = [str(FLOW_COUNT)]
    for src in range(HOST_COUNT):
        for dst in range(HOST_COUNT):
            if src != dst:
                lines.append("{} {} 3 {} {}".format(src, dst, FLOW_SIZE, START_TEXT))
    text = "\n".join(lines) + "\n"
    validate_text(text)
    OUTPUT.parent.mkdir(parents=True, exist_ok=True)
    if OUTPUT.exists() and OUTPUT.read_text() != text:
        raise RuntimeError("refusing to overwrite a different 128-host trace")
    OUTPUT.write_text(text)

    metadata = {
        "flow_count": FLOW_COUNT,
        "flow_size_bytes": FLOW_SIZE,
        "host_count": HOST_COUNT,
        "host_to_rack": {str(host): rack for host, rack in sorted(mapping.items())},
        "participants": list(range(HOST_COUNT)),
        "priority_group": 3,
        "seed": None,
        "simulation_duration_s": 0.2,
        "start_time_ns": START_NS,
        "start_time_text": START_TEXT,
        "synchronization": "exact",
        "topology": str(TOPOLOGY.relative_to(ROOT)),
        "trace": str(OUTPUT.relative_to(ROOT)),
        "trace_sha256": sha256(OUTPUT),
        "validation": "complete deterministic 128-host directed all-to-all",
    }
    metadata_text = json.dumps(metadata, indent=2, sort_keys=True) + "\n"
    if METADATA.exists() and METADATA.read_text() != metadata_text:
        raise RuntimeError("refusing to overwrite different trace metadata")
    METADATA.write_text(metadata_text)
    print("validated trace={} hosts={} flows={} size={} start_ns={}".format(
        OUTPUT.relative_to(ROOT), HOST_COUNT, FLOW_COUNT, FLOW_SIZE, START_NS))
    print("sha256={}".format(metadata["trace_sha256"]))


if __name__ == "__main__":
    main()
