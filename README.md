# flux-playground

A disposable local Flux playground built on k3d. The playground provisions
a one-server, one-agent K3s cluster, bootstraps Flux Operator `0.58.1`
through the K3s Helm Controller, reconciles a Flux `2.9.4` distribution
from an `OCIRepository` it pushes to itself, and exposes a representative
workload you can edit and reconcile without committing or pushing.

## Why this exists

Production runs RKE2 with the Rancher Helm Controller bootstrap, GitHub
App-backed `GitRepository` syncs, vSphere, and HA. None of that is
practical for local iteration. This repository gives you a single
command that produces an end-to-end Flux reconciliation loop against the
current working tree — uncommitted and untracked files included — without
GitHub credentials, a remote branch, or a vCenter.

## Prerequisites

The workflow assumes the developer machine — typically an Apple Silicon
Mac running OrbStack — and any Linux host with a Docker-compatible
runtime.

| Tool       | Version          | Install                                                                                  |
| ---------- | ---------------- | ---------------------------------------------------------------------------------------- |
| `uv`       | latest           | [astral-sh/uv](https://github.com/astral-sh/uv)                                          |
| `k3d`      | ≥ `5.9.0`        | [k3d-io/k3d](https://github.com/k3d-io/k3d)                                              |
| `kubectl`  | matches the cluster minor | [kubernetes/kubectl](https://kubernetes.io/docs/tasks/tools/)                |
| `helm`     | `v3`             | [helm/helm](https://github.com/helm/helm)                                                |
| `flux` CLI | `2.9.4` to `<3.0.0` | [fluxcd/flux2](https://github.com/fluxcd/flux2)                                                 |
| `docker`   | any Docker-compatible runtime | OrbStack or Docker Desktop                                                  |

OrbStack is the primary supported runtime; Docker and other
Docker-compatible engines are accepted.

### Flux CLI compatibility floor

The in-cluster Flux distribution is pinned to exactly `2.9.4`. The
`flux` CLI on the host accepts any `2.x` release ≥ `2.9.4` so developer machines with newer CLIs continue to work without
requiring a toolchain downgrade.

## Architecture

```text
                        ┌────────────────────────────────────────┐
  host working tree ───►│ flux push artifact (oci://127.0.0.1)  │
                        └────────────────────────────────────────┘
                                       │ mutable :dev tag
                                       ▼
                        ┌────────────────────────────────────────┐
                        │ k3d registry container                  │
                        │ (oci://k3d-flux-playground-reg:5000)   │
                        └────────────────────────────────────────┘
                                       │
                                       ▼
                        ┌────────────────────────────────────────┐
                        │ Flux OCIRepository + Kustomization     │
                        │ (clusters/local)                       │
                        └────────────────────────────────────────┘
                                       │
                                       ▼
                        ┌────────────────────────────────────────┐
                        │ smoke Deployment + ConfigMap           │
                        │ on the agent node                       │
                        └────────────────────────────────────────┘
```

The host push URL and the in-cluster source URL differ on purpose. The
host loopback port (`oci://127.0.0.1:<port>/flux-playground/manifests:dev`)
is the developer-facing push endpoint. The cluster reaches the same
artifact through its Docker network alias
(`oci://k3d-flux-playground-registry:5000/...`). The `OCIRepository`
marks `spec.insecure: true` so the plain-HTTP registry is accepted.

The Flux Operator chart `0.58.1` is installed through K3s's built-in
`helm.cattle.io/v1` Helm Controller. The operator's HelmChart manifest
is copied into K3s's standard auto-deploy directory
`/var/lib/rancher/k3s/server/manifests/`. K3s picks it up, pulls the
chart, and creates the `flux-system` namespace and CRDs.

The Flux distribution itself is sourced from the Flux Operator's
vendor artifact `oci://ghcr.io/controlplaneio-fluxcd/flux-operator-manifests`
(declared in `config/flux/flux-instance.yaml`). The artifact is a
version bundle; `spec.distribution.version` selects the `flux/v2.9.4`
content inside it.

## Normal workflow

```text
make up       # provision cluster, bootstrap Flux, push first artifact
make push     # re-package the working tree and reconcile
make check    # verify the playground is healthy
make down     # tear down cluster, registry, and kubeconfig
make reset    # destroy and recreate the playground
```

`make up` is a one-shot: it provisions the cluster, waits for the Flux
Operator HelmChart to install the operator (with job-level readiness),
applies the `FluxInstance`, waits for the Flux controllers and CRDs,
pushes the first artifact, applies the OCIRepository and Kustomization,
reconciles them, waits for the smoke workload, and runs the full health
check. A successful run means the playground is ready for iterative
work without any further setup.

### Pushing only

If the cluster is already up, `make push` skips cluster provisioning and
re-runs:

1. `flux push artifact oci://127.0.0.1:<port>/flux-playground/manifests:dev`
   from a temporary staging tree of the current working tree.
2. `kubectl apply -f config/flux/sync.yaml`.
3. `flux reconcile source oci playground` followed by
   `flux reconcile kustomization playground`.
4. Waits for the OCIRepository, Kustomization, and smoke Deployment to
   reconcile, then runs the full health check.

The mutable `:dev` tag means a fresh digest on every push — even when
`HEAD` is unchanged — so the Kustomization always has new content to
reconcile.

### Configuration

Forward arguments through `ARGS` or environment variables:

```text
make up ARGS="--registry-port=5050 --timeout=600"
PLAYGROUND_REGISTRY_PORT=5050 make up
PLAYGROUND_TIMEOUT=600 make up
```

Precedence is `--flag` → `PLAYGROUND_*` env → `CONDUCTOR_PORT` env →
default. Invalid ports (non-integer or outside `1..65535`) and
non-positive timeouts are rejected before any external command runs.

The resolved registry port says which port to **bind when creating** the
registry. The registry container is a host-level singleton, so once it
exists its binding is a fact: `up`, `push`, and `check` adopt the port it
is actually listening on and say so. Only `--registry-port` and
`PLAYGROUND_REGISTRY_PORT` name a port for this tool specifically, so
only those are treated as a request that must be honoured — a mismatch
against them is still reported as drift. `CONDUCTOR_PORT` is ambient
(Conductor sets it per workspace, for unrelated reasons), so it never
invalidates a registry that already works. `reset` does not adopt
anything: it destroys the registry and binds the resolved port afresh.

| Setting           | Default                          | Notes                              |
| ----------------- | -------------------------------- | ---------------------------------- |
| Registry port     | `5001`                           | Loopback on the host               |
| Timeout (seconds) | `300`                            | Per-wait deadline                  |
| Cluster           | `flux-playground`                | k3d                                |
| Registry          | `flux-playground-registry`       | k3d                                |
| Registry container| `k3d-flux-playground-registry`   | Docker network alias               |
| Kubeconfig        | `.context/kubeconfig-flux-playground.yaml` | `0600`, isolated       |
| Flux namespace    | `flux-system`                    |                                    |
| OCIRepository     | `playground`                     | in `flux-system`                   |
| Kustomization     | `playground`                     | in `flux-system`                   |

The Kubeconfig is isolated: every `kubectl` and `flux` invocation passes
`--kubeconfig=<absolute .context path>`. The default `KUBECONFIG`
environment variable and the user's default Kubernetes context are never
touched.

## Lifecycle state machine

The workflow reads `k3d registry list -o json` and `k3d cluster list -o
json` so it does not depend on the human-readable table layout. State is
represented by small dataclasses — `RegistryState` and `ClusterState` —
that the state machine consumes:

| State                                            | Action                                            |
| ------------------------------------------------ | ------------------------------------------------- |
| Neither cluster nor registry                     | Create registry, then cluster                     |
| Registry exists, cluster missing                 | Validate/restart registry, then create cluster    |
| Both exist and healthy                           | Reuse both                                        |
| Registry exists but is stopped                   | `docker start k3d-flux-playground-registry`, then wait for the HTTP endpoint |
| Cluster exists but has stopped nodes             | `k3d cluster start flux-playground --wait`, then re-validate |
| Cluster exists but registry is missing           | Fail with `make reset` guidance                   |
| Registry bound to a port other than an explicitly requested one | Fail with precise drift details and `make reset` guidance |
| Registry bound to a port other than an ambient/default one | Adopt the live port and continue |
| Wrong registry image, node count, K3s image, or load-balancer topology | Fail with precise drift details and `make reset` guidance |
| Unrelated k3d clusters and registries             | Ignored                                           |

`make check` inspects state without restarting or creating anything.

## Idempotency and partial state

- `make up` and `make push` succeed on a healthy, unchanged playground.
- `make down` is safe to run repeatedly; it skips resources that are
  already absent.
- `make reset` is exactly `down` followed by `up`; the underlying
  functions are shared.
- Teardown deletes the cluster first, then the registry, then the
  generated kubeconfig. Failures are aggregated into a single
  `PlaygroundError` rather than masking each other.

## Troubleshooting

`make check` aggregates every check. On failure it exits nonzero and the
failing task runs diagnostics exactly once before exiting. Each
diagnostic section is bounded and explicitly kubeconfig-scoped so it can
never replace the primary failure.

The diagnostics cover:

- k3d cluster, registry, and node listings (JSON).
- `kubectl get nodes -o wide` and
  `kubectl get pods,deployments,statefulsets,daemonsets -A -o wide`.
- The Flux Operator HelmChart YAML and the install Job's describe/logs.
- `FluxInstance` describe and `flux get all --all-namespaces`.
- Full YAML for the playground OCIRepository and Kustomization.
- Per-namespace HelmRelease summaries; full describe for any failing
  release.
- Sorted Kubernetes events.
- The last 200 log lines of the Flux Operator and each Flux controller.
- For every unhealthy or restarting pod: describe, current logs, and
  previous logs when applicable.

Common failures and the diagnostic section that surfaces them:

| Symptom                                          | Look at                                                |
| ------------------------------------------------ | ------------------------------------------------------ |
| `OCIRepository` stays not Ready                  | Registry health, `kubectl get pods -n flux-system`     |
| `Kustomization` Ready=False                      | `flux get kustomizations`, source events               |
| `HelmChart` reports `Failed=True`                | HelmChart YAML, install Job describe/logs              |
| Install Job reports `Failed=True`                | Job logs (printed inline by diagnostics)               |
| Flux controllers not Available                   | `kubectl logs` for the failing deployment              |
| Smoke pod stuck `Pending`                        | Node taint and `kubectl describe pod`                  |
| Default kubeconfig changed unexpectedly          | Verify `KUBECONFIG` env in the calling shell           |

To exercise the source diagnostics, stop the registry container
briefly (`docker stop k3d-flux-playground-registry`), run
`make check`, then restart it before the next `make push`.

## Differences from production RKE2

The playground deliberately narrows scope to keep local iteration fast
and CI-safe:

- **Cluster:** k3d + K3s instead of RKE2; one server and one agent
  instead of three-node HA. The server is tainted
  `CriticalAddonsOnly=true:NoExecute` so Flux and test workloads always
  schedule on the agent.
- **Bootstrap:** K3s's built-in `helm.cattle.io/v1` Helm Controller
  installs the Flux Operator chart from the auto-deploy directory; the
  chart is OCI-pinned to `0.58.1`.
- **Flux distribution:** the in-cluster version is exactly `2.9.4`, the
  same controller set (source, kustomize, helm, notification), and the
  `small` sizing profile.
- **Source:** a local insecure `OCIRepository` pushed by
  `flux push artifact`, no GitHub App secret, no `GitRepository`, no
  remote branch.
- **Networking:** K3s Traefik and ServiceLB are disabled and no k3d load
  balancer is created.
- **CLI compatibility:** the in-cluster distribution is exactly pinned
  while the host `flux` CLI accepts `2.x ≥ 2.9.4`.
- **Out of scope:** vSphere, kube-vip, three-node HA, persistent
  registry storage, GitHub credential testing, ingress behaviour.

## Version-upgrade procedure

When bumping pinned versions, edit every location listed below for that
pin. Most pins live in a single manifest, but the K3s image is pinned
twice: `_validate_cluster_state` compares `K3S_IMAGE` against `docker
inspect` output, so bumping only the manifest makes the next `make up`
reject its own freshly created nodes as drift.

| Pin                          | Source of truth                                       |
| ---------------------------- | ----------------------------------------------------- |
| K3s image                    | `config/k3d/playground.yaml` (`image:`) **and** `K3S_IMAGE` in `src/workflows/playground.py` — update both together |
| Flux Operator chart version  | `config/flux/flux-operator.yaml` (`spec.chart.version`) |
| Flux distribution version    | `config/flux/flux-instance.yaml` (`spec.distribution.version`) |
| Flux CLI minimum             | `FLUX_CLI_MIN_VERSION` in `src/workflows/playground.py` |
| k3d minimum                  | `K3D_MIN_VERSION` in `src/workflows/playground.py`   |

Steps:

1. Update every location listed above for the pin you are changing.
2. Verify the new Flux Operator chart version is published at the
   configured OCI URL and that its CRD range covers the desired Flux
   distribution. Confirm the new Flux distribution is present in the
   `flux-operator-manifests` bundle.
3. Confirm the new K3s image manifest lists `linux/arm64` and
   `linux/amd64` digests so Apple Silicon and Linux CI both work.
4. Run `make test` to exercise the workflow against the mocked
   boundaries.
5. Run `make reset` and confirm the smoke workload reconciles with the
   new pins.
6. Push a tracked change and confirm the Kustomization receives a new
   digest.

## Layout

```text
.
├── Makefile
├── README.md
├── pyproject.toml
├── uv.lock
├── tasks.py
├── config/
│   ├── k3d/playground.yaml          # cluster + registry topology
│   └── flux/
│       ├── flux-operator.yaml       # K3s HelmChart for Flux Operator
│       ├── flux-instance.yaml       # FluxInstance for Flux 2.9.4
│       └── sync.yaml                # OCIRepository + Kustomization
├── clusters/local/
│   ├── kustomization.yaml
│   ├── namespace.yaml
│   ├── smoke-configmap.yaml
│   └── smoke-deployment.yaml        # inert pause workload
├── src/workflows/playground.py      # orchestration
└── tests/test_playground.py         # unit tests
```

## Tests

```text
make test
```

The test suite uses only the Python standard library and mocks every
external command. It runs without Docker, k3d, kubectl, Helm, Flux,
or any running Kubernetes cluster. Lifecycle fixtures use vendor-shaped
JSON output (`k3d ... -o json`) so the tests cannot drift away from the
real k3d output.
