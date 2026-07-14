# ConWeave Reproduction and HPC Extension

This repository contains the code, deterministic traces, derived results, and report for a reproduction and HPC-inspired extension of:

**Song et al., “Network Load Balancing with In-network Reordering Support for RDMA,” ACM SIGCOMM 2023.**

- Paper: https://doi.org/10.1145/3603269.3604849
- Upstream artifact: https://github.com/conweave-project/conweave-ns3
- Upstream commit: `236a801a00e35de9078635e04acae2f701c21ded`
- Simulator: NS-3.19
- Docker image used: `cw-sim:sigcomm23ae`

## Report

- [PDF report](Report/load_balancing_report.pdf)
- [Markdown source](Report/load_balancing_report.md)

## Contents

- `reproduction/`: launcher, analysis scripts, validation manifests, and figures for the paper’s 50% load experiment.
- `extension/`: scripts, deterministic traces, results, and figures for synchronized all-to-all, skewed all-to-all, and incast workloads.
- `Report/`: final report and its figures.

Raw simulator outputs and logs are omitted because they are large and can be regenerated.

## Important scope note

The extension uses the 128-host leaf-spine topology, but the all-to-all workloads contain **16 participating hosts**, with two hosts selected from each rack. They are therefore 16-participant all-to-all experiments on a 128-host topology, not full 128-host all-to-all runs.

## Reproducing

Clone the upstream artifact and check out the pinned commit:

```bash
git clone https://github.com/conweave-project/conweave-ns3.git
cd conweave-ns3
git checkout 236a801a00e35de9078635e04acae2f701c21ded
```

Build it inside the compatible NS-3.19/Docker environment:

```bash
./waf configure --build-profile=optimized
./waf -j"$(nproc)"
```

Copy the files from `reproduction/scripts/` and `extension/source/` into their corresponding paths in the upstream artifact.

Run the baseline reproduction:

```bash
./autorun_50.sh
python3 analysis/build_reproduction_artifacts.py
```

Run the extension:

```bash
python3 mix/tools/generate_hpc_traces.py
./run_hpc_extension.sh smoke-retry
./run_hpc_extension.sh official
python3 analysis/build_hpc_extension_results.py
```

Detailed methodology, validation, results, and limitations are documented in the report.