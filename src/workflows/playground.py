"""Disposable local k3d Flux playground orchestration.

This module owns every command and lifecycle decision for the playground.
All Invoke tasks funnel through shared internal functions so that
``reset`` can compose ``down`` and ``up`` without spawning a nested
``invoke`` process or re-implementing lifecycle logic.

The workflow is deliberately non-interactive and CI-safe: every external
command runs through :class:`CommandRunner` with ``shell=False`` so user
input is never interpolated into shell strings, every Kubernetes command
receives the explicit ``--kubeconfig`` flag, and failures raise typed
exceptions that the Invoke tasks turn into actionable diagnostics.

Lifecycle state is read from the structured ``k3d registry list -o json``
and ``k3d cluster list -o json`` outputs so that the workflow does not
depend on the exact column layout of the human-readable table. State
helpers return small dataclasses (:class:`RegistryState`,
:class:`ClusterState`) which the state machine in
:func:`_ensure_infrastructure` consumes.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from dataclasses import dataclass, field, replace
from pathlib import Path
from typing import Any, Callable, Iterable, Mapping, NoReturn, Sequence

from invoke import task

REPO_ROOT = Path(__file__).resolve().parents[2]
KUBECONFIG_DIR = Path.home() / ".kube"
KUBECONFIG_PATH = KUBECONFIG_DIR / "k3d-flux-playground.yaml"
K3D_CONFIG_PATH = REPO_ROOT / "config" / "k3d" / "playground.yaml"
FLUX_OPERATOR_MANIFEST_PATH = REPO_ROOT / "config" / "flux" / "flux-operator.yaml"
FLUX_INSTANCE_MANIFEST_PATH = REPO_ROOT / "config" / "flux" / "flux-instance.yaml"
SYNC_MANIFEST_PATH = REPO_ROOT / "config" / "flux" / "sync.yaml"

CLUSTER_NAME = "flux-playground"
REGISTRY_NAME = "flux-playground-registry"
REGISTRY_CONTAINER_NAME = "k3d-flux-playground-registry"
REGISTRY_IMAGE = "docker.io/library/registry:2"
FLUX_NAMESPACE = "flux-system"
OCI_REPOSITORY_NAME = "playground"
KUSTOMIZATION_NAME = "playground"
ARTIFACT_REPOSITORY_PATH = "flux-playground/manifests"
MUTABLE_TAG = "dev"

K3D_MIN_VERSION = "5.9.0"
K3S_IMAGE = "rancher/k3s:v1.36.4-k3s1"
FLUX_CLI_MIN_VERSION = "2.9.4"
FLUX_CONTROLLERS: tuple[str, ...] = (
    "source-controller",
    "kustomize-controller",
    "helm-controller",
    "notification-controller",
)

EXPECTED_CRDS: tuple[str, ...] = (
    "fluxinstances.fluxcd.controlplane.io",
    "ocirepositories.source.toolkit.fluxcd.io",
    "kustomizations.kustomize.toolkit.fluxcd.io",
    "helmreleases.helm.toolkit.fluxcd.io",
)

DEFAULT_REGISTRY_PORT = 5001
DEFAULT_TIMEOUT_SECONDS = 300

REGISTRY_PORT_ARG = "--registry-port"
TIMEOUT_ARG = "--timeout"
PLAYGROUND_REGISTRY_PORT_ENV = "PLAYGROUND_REGISTRY_PORT"
CONDUCTOR_PORT_ENV = "CONDUCTOR_PORT"
PLAYGROUND_TIMEOUT_ENV = "PLAYGROUND_TIMEOUT"

REQUIRED_TOOLS: tuple[str, ...] = (
    "git",
    "docker",
    "k3d",
    "kubectl",
    "helm",
    "flux",
)

SERVER_TAINT_KEY = "CriticalAddonsOnly"
SERVER_TAINT_VALUE = "true"
SERVER_TAINT_EFFECT = "NoExecute"

SMOKE_DEPLOYMENT = "playground-smoke"
SMOKE_NAMESPACE = "flux-playground"
SMOKE_APP_LABEL = "app.kubernetes.io/name=playground-smoke"

OPERATOR_HELMCHART_NAME = "flux-operator"
OPERATOR_HELMCHART_NAMESPACE = "kube-system"
OPERATOR_DEPLOYMENT_NAME = "flux-operator"

KUBECONFIG_DIR_MODE = 0o700
KUBECONFIG_FILE_MODE = 0o600

LOG_TAIL_LINES = 200

# How long to wait for the local registry HTTP endpoint to come up after
# restarting its container.
REGISTRY_START_TIMEOUT_SECONDS = 30

# Polling interval for readiness waits.
POLL_INTERVAL_SECONDS = 2.0


class PlaygroundError(RuntimeError):
    """Raised for any user-actionable failure inside the playground workflow."""


@dataclass(frozen=True)
class PlaygroundSettings:
    """Resolved, immutable configuration for a single workflow run."""

    repo_root: Path
    kubeconfig_path: Path
    cluster_name: str = CLUSTER_NAME
    registry_name: str = REGISTRY_NAME
    registry_container_name: str = REGISTRY_CONTAINER_NAME
    flux_namespace: str = FLUX_NAMESPACE
    oci_repository_name: str = OCI_REPOSITORY_NAME
    kustomization_name: str = KUSTOMIZATION_NAME
    artifact_repository_path: str = ARTIFACT_REPOSITORY_PATH
    mutable_tag: str = MUTABLE_TAG
    k3d_min_version: str = K3D_MIN_VERSION
    k3s_image: str = K3S_IMAGE
    flux_controllers: tuple[str, ...] = FLUX_CONTROLLERS
    registry_port: int = DEFAULT_REGISTRY_PORT
    timeout_seconds: int = DEFAULT_TIMEOUT_SECONDS
    # True when the port was named for *this* tool, via ``--registry-port``
    # or ``PLAYGROUND_REGISTRY_PORT``. ``CONDUCTOR_PORT`` is ambient: it is
    # set per workspace for reasons unrelated to the registry, so it must
    # not override the binding of a registry that already exists.
    registry_port_explicit: bool = False

    @property
    def host_artifact_url(self) -> str:
        return (
            f"oci://127.0.0.1:{self.registry_port}"
            f"/{self.artifact_repository_path}:{self.mutable_tag}"
        )

    @property
    def in_cluster_source_url(self) -> str:
        return (
            f"oci://{self.registry_container_name}:5000/{self.artifact_repository_path}"
        )


@dataclass(frozen=True)
class RegistryState:
    """Structured snapshot of a single k3d registry.

    ``runtime_image_id`` is the digest-style content identifier reported
    by ``k3d registry list -o json``; it is *not* the configured image
    reference. Use ``docker inspect`` to obtain the configured reference.
    ``host_ip`` and ``host_port`` together describe the loopback binding
    of the registry's ``5000/tcp`` mapping.
    """

    name: str
    container_name: str
    runtime_image_id: str
    running: bool
    host_ip: str | None
    host_port: int | None
    extra: Mapping[str, Any] = field(default_factory=dict)


@dataclass(frozen=True)
class ClusterNode:
    """A single node in a :class:`ClusterState`.

    ``runtime_image_id`` is the digest-style content identifier reported
    by ``k3d cluster list -o json``; it is *not* the configured image
    reference. Use ``docker inspect`` to obtain the configured reference.
    """

    name: str
    role: str
    runtime_image_id: str
    running: bool


@dataclass(frozen=True)
class ClusterState:
    """Structured snapshot of a single k3d cluster."""

    name: str
    servers_count: int
    agents_count: int
    servers_running: int
    agents_running: int
    has_load_balancer: bool
    nodes: tuple[ClusterNode, ...] = field(default_factory=tuple)
    extra: Mapping[str, Any] = field(default_factory=dict)

    @property
    def running(self) -> bool:
        return self.servers_running >= 1 and self.agents_running >= 1

    @property
    def servers(self) -> tuple[ClusterNode, ...]:
        return tuple(n for n in self.nodes if n.role == "server")

    @property
    def agents(self) -> tuple[ClusterNode, ...]:
        return tuple(n for n in self.nodes if n.role == "agent")


@dataclass
class CommandRunner:
    """Injectable subprocess wrapper used by every external command.

    Tests replace ``run`` / ``run_json`` with stubs; production code uses
    the default that delegates to :func:`subprocess.run` with
    ``shell=False``.
    """

    default_timeout: float = 60.0

    def run(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        check: bool = False,
        env: Mapping[str, str] | None = None,
        cwd: Path | str | None = None,
        input: str | None = None,
        capture: bool = True,
    ) -> subprocess.CompletedProcess[str]:
        effective_timeout = self.default_timeout if timeout is None else timeout
        if input is not None and not isinstance(input, str):
            raise PlaygroundError(
                f"Command {argv!r} requires text input; got "
                f"{type(input).__name__} instead of str."
            )
        try:
            result = subprocess.run(  # noqa: S603 - argv is always a list
                list(argv),
                shell=False,
                check=False,
                env=env,
                cwd=str(cwd) if cwd is not None else None,
                input=input,
                capture_output=capture,
                text=True,
                timeout=effective_timeout,
            )
        except subprocess.TimeoutExpired as exc:
            raise PlaygroundError(
                f"Command {argv!r} exceeded the {effective_timeout}s timeout "
                "before it could complete."
            ) from exc
        except OSError as exc:
            raise PlaygroundError(
                f"Command {argv!r} could not be launched: {exc}"
            ) from exc
        if check and result.returncode != 0:
            raise PlaygroundError(
                f"Command {argv!r} failed with exit code {result.returncode}: "
                f"{result.stderr.strip() if result.stderr else ''}"
            )
        return result

    def run_json(
        self,
        argv: Sequence[str],
        *,
        timeout: float | None = None,
        env: Mapping[str, str] | None = None,
        cwd: Path | str | None = None,
    ) -> Any:
        result = self.run(
            argv,
            timeout=timeout,
            check=True,
            env=env,
            cwd=cwd,
        )
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise PlaygroundError(
                f"Expected JSON from {argv!r}, got: {result.stdout!r}"
            ) from exc


DEFAULT_RUNNER: CommandRunner = CommandRunner()


def resolve_registry_port_source(
    cli_value: Any = None,
    env: Mapping[str, str] | None = None,
) -> tuple[int, bool]:
    """Resolve the registry port and whether it was explicitly requested.

    Precedence is ``--registry-port`` → ``PLAYGROUND_REGISTRY_PORT`` →
    ``CONDUCTOR_PORT`` → :data:`DEFAULT_REGISTRY_PORT`.

    The second element is True only for the first two sources, which name
    a port for this tool specifically. ``CONDUCTOR_PORT`` and the default
    are ambient: they say which port to *bind when creating* the registry,
    not which port an existing registry must already be on.
    """
    env = os.environ if env is None else env
    if cli_value is not None and cli_value != "":
        parsed = _parse_port(cli_value)
        if parsed is None:
            raise PlaygroundError(
                f"Invalid --registry-port value: {cli_value!r}. "
                "Must be an integer between 1 and 65535."
            )
        return parsed, True
    for env_name, explicit in (
        (PLAYGROUND_REGISTRY_PORT_ENV, True),
        (CONDUCTOR_PORT_ENV, False),
    ):
        raw = env.get(env_name)
        if raw is None or raw == "":
            continue
        parsed = _parse_port(raw)
        if parsed is None:
            raise PlaygroundError(
                f"Invalid {env_name} value: {raw!r}. "
                "Must be an integer between 1 and 65535."
            )
        return parsed, explicit
    return DEFAULT_REGISTRY_PORT, False


def resolve_registry_port(
    cli_value: Any = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Resolve the registry port from CLI, env, fallback precedence."""
    return resolve_registry_port_source(cli_value, env)[0]


def resolve_timeout(
    cli_value: Any = None,
    env: Mapping[str, str] | None = None,
) -> int:
    """Resolve the timeout (seconds) from CLI, env, fallback precedence."""
    env = os.environ if env is None else env
    if cli_value is not None and cli_value != "":
        parsed = _parse_timeout(cli_value)
        if parsed is None:
            raise PlaygroundError(
                f"Invalid --timeout value: {cli_value!r}. Must be a positive integer."
            )
        return parsed
    raw = env.get(PLAYGROUND_TIMEOUT_ENV)
    if raw is not None and raw != "":
        parsed = _parse_timeout(raw)
        if parsed is None:
            raise PlaygroundError(
                f"Invalid {PLAYGROUND_TIMEOUT_ENV} value: {raw!r}. "
                "Must be a positive integer."
            )
        return parsed
    return DEFAULT_TIMEOUT_SECONDS


def _parse_port(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or not text.isdigit():
        return None
    port = int(text)
    if not 1 <= port <= 65535:
        return None
    return port


def _parse_timeout(value: Any) -> int | None:
    if isinstance(value, bool):
        return None
    text = str(value).strip()
    if not text or not text.isdigit():
        return None
    seconds = int(text)
    if seconds <= 0:
        return None
    return seconds


def _settings_from(c: Any, **overrides: Any) -> PlaygroundSettings:
    """Build a :class:`PlaygroundSettings` from an Invoke task context."""
    cli = getattr(c, "config", None)
    env = os.environ

    registry_port = overrides.get(
        "registry_port",
        getattr(cli, "registry_port", None) if cli else None,
    )
    timeout_seconds = overrides.get(
        "timeout",
        getattr(cli, "timeout", None) if cli else None,
    )

    resolved_port, port_explicit = resolve_registry_port_source(registry_port, env)
    resolved_timeout = resolve_timeout(timeout_seconds, env)
    return PlaygroundSettings(
        repo_root=REPO_ROOT,
        kubeconfig_path=KUBECONFIG_PATH,
        registry_port=resolved_port,
        timeout_seconds=resolved_timeout,
        registry_port_explicit=port_explicit,
    )


def _check_executable(name: str, runner: CommandRunner) -> str | None:
    """Return the resolved path for ``name`` or ``None`` if missing."""
    found = shutil.which(name)
    if found:
        return found
    result = runner.run(["/bin/sh", "-c", f"command -v {name}"], check=False)
    if result.returncode == 0:
        return result.stdout.strip() or None
    return None


def _preflight(
    settings: PlaygroundSettings,
    runner: CommandRunner,
    *,
    stdout: Callable[[str], None] = print,
) -> None:
    """Validate required tools, versions, and the runtime environment."""
    missing: list[str] = []
    for tool in REQUIRED_TOOLS:
        if _check_executable(tool, runner) is None:
            missing.append(tool)

    if missing:
        raise PlaygroundError(
            "Missing required executables: " + ", ".join(sorted(missing))
        )

    _require_docker_server(runner)
    _require_k3d_version(settings, runner)
    _require_flux_cli_version(runner)
    stdout(
        f"Preflight OK (k3d ≥ {K3D_MIN_VERSION}, flux CLI ≥ {FLUX_CLI_MIN_VERSION})."
    )


def _require_docker_server(runner: CommandRunner) -> None:
    result = runner.run(
        ["docker", "info", "--format", "{{.ServerVersion}}"],
        check=False,
    )
    if result.returncode != 0:
        raise PlaygroundError(
            "Docker server is unreachable. OrbStack or Docker must be running."
        )


def _require_k3d_version(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> None:
    result = runner.run(["k3d", "version"], check=False)
    if result.returncode != 0:
        raise PlaygroundError("k3d is installed but did not respond to `k3d version`.")
    raw = (result.stdout or "").strip()
    match = re.search(r"v(\d+)\.(\d+)\.(\d+)", raw)
    if not match:
        raise PlaygroundError(f"Could not parse k3d version from: {raw!r}")
    found = tuple(int(part) for part in match.groups())
    minimum = _parse_semver(settings.k3d_min_version)
    if found < minimum:
        raise PlaygroundError(
            f"k3d {settings.k3d_min_version} or newer is required, "
            f"found {'.'.join(map(str, found))}."
        )


def _require_flux_cli_version(runner: CommandRunner) -> None:
    result = runner.run(["flux", "--version"], check=False)
    if result.returncode != 0:
        raise PlaygroundError(
            "flux CLI is installed but did not respond to `flux --version`."
        )
    raw = (result.stdout or "").strip()
    match = re.search(r"(\d+)\.(\d+)\.(\d+)", raw)
    if not match:
        raise PlaygroundError(f"Could not parse flux CLI version from: {raw!r}")
    found = tuple(int(part) for part in match.groups())
    minimum = _parse_semver(FLUX_CLI_MIN_VERSION)
    if found[0] != minimum[0] or found < minimum:
        raise PlaygroundError(
            f"flux CLI {FLUX_CLI_MIN_VERSION} or later (major version 2.x) is "
            f"required, found {'.'.join(map(str, found))}."
        )


def _parse_semver(value: str) -> tuple[int, int, int]:
    parts = value.split(".")
    if len(parts) != 3 or not all(part.isdigit() for part in parts):
        raise PlaygroundError(f"Invalid semantic version: {value!r}")
    return int(parts[0]), int(parts[1]), int(parts[2])


# ---------------------------------------------------------------------------
# Structured lifecycle state from k3d JSON output
# ---------------------------------------------------------------------------


def _list_registries_json(
    runner: CommandRunner,
) -> list[Mapping[str, Any]]:
    result = runner.run(["k3d", "registry", "list", "-o", "json"], check=True)
    return _parse_json_array(result.stdout, source="k3d registry list")


def _list_clusters_json(runner: CommandRunner) -> list[Mapping[str, Any]]:
    result = runner.run(["k3d", "cluster", "list", "-o", "json"], check=True)
    return _parse_json_array(result.stdout, source="k3d cluster list")


def _parse_json_array(payload: str, *, source: str) -> list[Mapping[str, Any]]:
    text = (payload or "").strip()
    if not text:
        return []
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as exc:
        raise PlaygroundError(f"{source} returned invalid JSON: {exc}") from exc
    if not isinstance(parsed, list):
        raise PlaygroundError(
            f"{source} did not return a JSON array: {type(parsed).__name__}"
        )
    return [item for item in parsed if isinstance(item, dict)]


def _find_registry_state(
    entries: Iterable[Mapping[str, Any]],
    *,
    container_name: str,
    logical_name: str,
) -> RegistryState | None:
    for entry in entries:
        name = str(entry.get("name", ""))
        if name != container_name:
            continue
        state = _parse_state(entry.get("State") or entry.get("state"))
        running = state["running"]
        host_ip, host_port = _parse_host_binding(entry)
        return RegistryState(
            name=logical_name,
            container_name=name,
            runtime_image_id=str(entry.get("image", "")),
            running=running,
            host_ip=host_ip,
            host_port=host_port,
            extra=entry,
        )
    return None


def _parse_state(value: Any) -> dict[str, Any]:
    """Parse a k3d ``State`` block.

    Prefers the boolean ``Running`` field; falls back to a ``Status``
    string of ``running`` only when ``Running`` is absent. Raises
    :class:`PlaygroundError` on structurally invalid state.
    """
    if not isinstance(value, dict):
        raise PlaygroundError(
            "k3d registry/cluster JSON is missing a State object; "
            "the runtime contract has changed."
        )
    if "Running" in value:
        running = value["Running"]
        if not isinstance(running, bool):
            raise PlaygroundError(
                f"k3d State.Running must be a boolean, got {type(running).__name__}."
            )
        return {"running": running, "raw": value}
    status = value.get("Status")
    if isinstance(status, str) and status:
        return {"running": status.lower() == "running", "raw": value}
    raise PlaygroundError(
        "k3d State block contains neither Running nor a non-empty Status; "
        "cannot determine runtime state."
    )


def _parse_host_binding(
    entry: Mapping[str, Any],
) -> tuple[str, int]:
    """Parse the ``5000/tcp`` host binding from a k3d registry entry.

    Returns ``(host_ip, host_port)`` on success, or raises
    :class:`PlaygroundError` on any structural problem so the caller never
    silently accepts a malformed binding. Every structural failure is
    wrapped with ``Run `make reset`.`` because the value came from an
    existing k3d registry whose binding cannot be repaired in place.
    """
    def fail(message: str) -> NoReturn:
        raise PlaygroundError(f"{message} Run `make reset`.")

    ports = entry.get("portMappings") or entry.get("ports")
    if not isinstance(ports, dict):
        fail(
            "k3d registry JSON is missing a portMappings object; the "
            "runtime contract has changed."
        )
    mapping = ports.get("5000/tcp")
    if mapping is None:
        fail("k3d registry JSON is missing the 5000/tcp port mapping.")
    if not isinstance(mapping, list) or not mapping:
        fail("k3d registry 5000/tcp mapping must be a non-empty list.")
    if len(mapping) > 1:
        fail(
            f"k3d registry 5000/tcp has {len(mapping)} bindings; "
            "exactly one is required."
        )
    binding = mapping[0]
    if not isinstance(binding, dict):
        fail("k3d registry 5000/tcp binding must be a JSON object.")
    host_ip = binding.get("HostIp")
    if host_ip is None or host_ip == "":
        fail("k3d registry 5000/tcp binding is missing HostIp.")
    host_port = binding.get("HostPort")
    if host_port is None or host_port == "":
        fail("k3d registry 5000/tcp binding is missing HostPort.")
    try:
        port_value = _parse_host_port(host_port)
    except PlaygroundError as exc:
        raise PlaygroundError(f"{exc} Run `make reset`.") from exc
    return str(host_ip), port_value


def _parse_host_port(value: Any) -> int:
    """Parse a JSON integer host port in the inclusive range ``1..65535``.

    Accepts JSON integers and numeric strings. Rejects booleans, floats
    (including integral-looking values such as ``5001.0``), non-numeric
    strings, lists, objects, ``None``, empty strings, and ports outside
    ``1..65535``. The error message includes the rejected value and the
    accepted formats; lifecycle callers may add ``make reset`` guidance.
    """
    parsed = _parse_port(value)
    if parsed is None:
        raise PlaygroundError(
            f"Invalid host port {value!r}. Expected a JSON integer or a "
            "string containing an integer in 1..65535."
        )
    return parsed


def _require_int(entry: Mapping[str, Any], *, field: str) -> int:
    """Return a required, non-negative integer from a k3d JSON entry.

    Accepts JSON integers including zero. Rejects booleans, missing
    fields, strings, floats, negative numbers, lists, and objects.
    Raises :class:`PlaygroundError` (never ``TypeError`` or
    ``ValueError``) with the offending field and value.
    """
    if not isinstance(entry, dict):
        raise PlaygroundError(
            f"k3d JSON entry is not a JSON object; cannot read {field!r}."
        )
    if field not in entry:
        raise PlaygroundError(
            f"k3d JSON contract is missing required field {field!r}; "
            "the runtime contract has changed."
        )
    value = entry[field]
    # Reject booleans explicitly: ``True`` is an ``int`` subclass in Python.
    if isinstance(value, bool):
        raise PlaygroundError(
            f"k3d field {field!r} must be an integer, got boolean {value!r}."
        )
    if not isinstance(value, int):
        raise PlaygroundError(
            f"k3d field {field!r} must be an integer, got {type(value).__name__} "
            f"value {value!r}."
        )
    if value < 0:
        raise PlaygroundError(
            f"k3d field {field!r} must be non-negative, got {value}."
        )
    return value


def _find_cluster_state(
    entries: Iterable[Mapping[str, Any]],
    *,
    cluster_name: str,
) -> ClusterState | None:
    for entry in entries:
        if entry.get("name") != cluster_name:
            continue
        servers_count = _require_int(entry, field="serversCount")
        agents_count = _require_int(entry, field="agentsCount")
        servers_running = _require_int(entry, field="serversRunning")
        agents_running = _require_int(entry, field="agentsRunning")
        # k3d serializes the load-balancer flag with a lowercase 'b' and
        # omits it entirely when false; treat omission as false.
        has_load_balancer = bool(entry.get("hasLoadbalancer", False))
        nodes = _coerce_nodes(entry.get("nodes"))
        return ClusterState(
            name=cluster_name,
            servers_count=servers_count,
            agents_count=agents_count,
            servers_running=servers_running,
            agents_running=agents_running,
            has_load_balancer=has_load_balancer,
            nodes=nodes,
            extra=entry,
        )
    return None


def _coerce_nodes(value: Any) -> tuple[ClusterNode, ...]:
    if not isinstance(value, list):
        return ()
    nodes: list[ClusterNode] = []
    for raw in value:
        if not isinstance(raw, dict):
            continue
        state = _parse_state(raw.get("State") or raw.get("state"))
        nodes.append(
            ClusterNode(
                name=str(raw.get("name", "")),
                role=str(raw.get("role", "")),
                runtime_image_id=str(raw.get("image", "")),
                running=state["running"],
            )
        )
    return tuple(nodes)


def _registry_state(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> RegistryState | None:
    entries = _list_registries_json(runner)
    return _find_registry_state(
        entries,
        container_name=settings.registry_container_name,
        logical_name=settings.registry_name,
    )


def _cluster_state(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> ClusterState | None:
    entries = _list_clusters_json(runner)
    return _find_cluster_state(entries, cluster_name=settings.cluster_name)


def _adopt_existing_registry_port(
    settings: PlaygroundSettings,
    runner: CommandRunner,
    *,
    stdout: Callable[[str], None] = print,
) -> PlaygroundSettings:
    """Adopt a live registry's host port unless one was explicitly pinned.

    The resolved port says which port to bind when *creating* the
    registry. Once the container exists its binding is a fact, so an
    ambient value must not be treated as expected state: with
    ``CONDUCTOR_PORT`` set, a registry created on the 5001 default would
    otherwise fail ``check`` and ``push`` and be reported as drift needing
    ``make reset``, even though the playground is healthy.

    An explicit ``--registry-port`` / ``PLAYGROUND_REGISTRY_PORT`` is a
    request that cannot be silently ignored, so it is left alone and the
    usual drift error still fires.
    """
    if settings.registry_port_explicit:
        return settings
    try:
        registry = _registry_state(settings, runner)
    except PlaygroundError:
        return settings
    if registry is None or registry.host_port is None:
        return settings
    if registry.host_port == settings.registry_port:
        return settings
    stdout(
        f"Using existing registry port {registry.host_port} "
        f"(resolved {settings.registry_port} was not explicitly requested)."
    )
    return replace(settings, registry_port=registry.host_port)


def _registry_name_present(settings: PlaygroundSettings, runner: CommandRunner) -> bool:
    """Return True when a registry container with the expected name exists.

    Deliberately name-only. Teardown must be able to delete a registry
    whose port mappings or ``State`` block no longer parse, which is
    exactly the drift the strict parsers used by ``up``/``check`` reject.
    """
    entries = _list_registries_json(runner)
    return any(
        str(entry.get("name", "")) == settings.registry_container_name
        for entry in entries
    )


def _cluster_name_present(settings: PlaygroundSettings, runner: CommandRunner) -> bool:
    """Return True when a cluster with the expected name exists (name-only)."""
    entries = _list_clusters_json(runner)
    return any(str(entry.get("name", "")) == settings.cluster_name for entry in entries)


def _create_registry(settings: PlaygroundSettings, runner: CommandRunner) -> None:
    runner.run(
        [
            "k3d",
            "registry",
            "create",
            settings.registry_name,
            "--image",
            REGISTRY_IMAGE,
            "--port",
            f"127.0.0.1:{settings.registry_port}",
            "--no-help",
        ],
        check=True,
    )


def _create_cluster(settings: PlaygroundSettings, runner: CommandRunner) -> None:
    runner.run(
        [
            "k3d",
            "cluster",
            "create",
            "--config",
            str(K3D_CONFIG_PATH),
        ],
        check=True,
    )


def _ensure_infrastructure(
    settings: PlaygroundSettings,
    runner: CommandRunner,
    *,
    stdout: Callable[[str], None] = print,
) -> None:
    """Reconcile the registry and cluster to a healthy, expected state."""
    registry = _registry_state(settings, runner)
    cluster = _cluster_state(settings, runner)

    if registry is None and cluster is None:
        _create_registry(settings, runner)
        _wait_for_registry_endpoint(settings, runner)
        _create_cluster(settings, runner)
        stdout("Created registry and cluster.")
        return

    if registry is not None and cluster is None:
        _validate_registry_state(settings, registry, runner)
        _ensure_registry_running(settings, registry, runner)
        _wait_for_registry_endpoint(settings, runner)
        _create_cluster(settings, runner)
        stdout("Created cluster (registry already existed).")
        return

    if cluster is not None and registry is None:
        raise PlaygroundError(
            f"Cluster {settings.cluster_name!r} exists but registry "
            f"{settings.registry_name!r} is missing. Run `make reset`."
        )

    assert registry is not None and cluster is not None
    _validate_registry_state(settings, registry, runner)
    _validate_cluster_state(settings, cluster, runner)
    _ensure_registry_running(settings, registry, runner)
    _ensure_cluster_running(settings, cluster, runner)
    _wait_for_registry_endpoint(settings, runner)
    stdout("Reused existing registry and cluster.")


def _docker_inspect_image(
    container_name: str,
    runner: CommandRunner,
) -> str:
    """Return the configured image reference for a container via Docker inspect."""
    result = runner.run(
        ["docker", "inspect", "--format", "{{.Config.Image}}", container_name],
        check=False,
    )
    if result.returncode != 0:
        raise PlaygroundError(
            f"Could not inspect Docker container {container_name!r}; "
            "verify Docker is reachable. Run `make reset`."
        )
    image = (result.stdout or "").strip()
    if not image:
        raise PlaygroundError(
            f"Docker inspect returned an empty image for {container_name!r}; "
            "run `make reset`."
        )
    return image


def _validate_registry_state(
    settings: PlaygroundSettings,
    registry: RegistryState,
    runner: CommandRunner,
) -> None:
    expected_image = _docker_inspect_image(settings.registry_container_name, runner)
    if expected_image != REGISTRY_IMAGE:
        raise PlaygroundError(
            f"Registry container {settings.registry_container_name!r} is "
            f"configured with image {expected_image!r}; "
            f"expected {REGISTRY_IMAGE!r}. Run `make reset`."
        )
    if registry.host_ip != "127.0.0.1":
        raise PlaygroundError(
            f"Registry {registry.name!r} is exposed on HostIp={registry.host_ip!r}; "
            "the playground requires loopback (127.0.0.1) exposure. "
            "Run `make reset`."
        )
    if registry.host_port != settings.registry_port:
        raise PlaygroundError(
            f"Registry {registry.name!r} is exposed on "
            f"{registry.host_ip}:{registry.host_port}; "
            f"expected 127.0.0.1:{settings.registry_port}. Run `make reset`."
        )


def _ensure_registry_running(
    settings: PlaygroundSettings,
    registry: RegistryState,
    runner: CommandRunner,
) -> None:
    if registry.running:
        return
    runner.run(
        ["docker", "start", settings.registry_container_name],
        check=True,
    )


def _ensure_cluster_running(
    settings: PlaygroundSettings,
    cluster: ClusterState,
    runner: CommandRunner,
) -> None:
    if cluster.running:
        return
    runner.run(
        ["k3d", "cluster", "start", settings.cluster_name, "--wait"],
        check=True,
    )


def _validate_cluster_state(
    settings: PlaygroundSettings,
    cluster: ClusterState,
    runner: CommandRunner,
) -> None:
    # Aggregate counts must match.
    if cluster.servers_count != 1 or cluster.agents_count != 1:
        raise PlaygroundError(
            f"Cluster {settings.cluster_name!r} reports "
            f"{cluster.servers_count} servers and {cluster.agents_count} agents; "
            "expected 1 of each. Run `make reset`."
        )
    if cluster.has_load_balancer:
        raise PlaygroundError(
            f"Cluster {settings.cluster_name!r} has a load balancer; "
            "the playground topology forbids one. Run `make reset`."
        )
    # Node collection must contain exactly two entries.
    if len(cluster.nodes) != 2:
        raise PlaygroundError(
            f"Cluster {settings.cluster_name!r} reports "
            f"{len(cluster.nodes)} nodes; expected exactly 2 (1 server, 1 agent). "
            "Run `make reset`."
        )
    # Every node must have a unique, non-empty name.
    names = [n.name for n in cluster.nodes]
    if any(not n for n in names):
        raise PlaygroundError(
            f"Cluster {settings.cluster_name!r} has a node with an empty name. "
            "Run `make reset`."
        )
    if len(set(names)) != len(names):
        duplicates = sorted({n for n in names if names.count(n) > 1})
        raise PlaygroundError(
            f"Cluster {settings.cluster_name!r} has duplicate node names: "
            f"{', '.join(duplicates)}. Run `make reset`."
        )
    # Exactly one server, one agent, no unknown roles.
    server_count = sum(1 for n in cluster.nodes if n.role == "server")
    agent_count = sum(1 for n in cluster.nodes if n.role == "agent")
    unknown_roles = sorted(
        {n.role for n in cluster.nodes if n.role not in ("server", "agent")}
    )
    if server_count != 1:
        raise PlaygroundError(
            f"Cluster {settings.cluster_name!r} has {server_count} server nodes; "
            "expected exactly 1. Run `make reset`."
        )
    if agent_count != 1:
        raise PlaygroundError(
            f"Cluster {settings.cluster_name!r} has {agent_count} agent nodes; "
            "expected exactly 1. Run `make reset`."
        )
    if unknown_roles:
        raise PlaygroundError(
            f"Cluster {settings.cluster_name!r} has nodes with unknown roles: "
            f"{', '.join(unknown_roles)}. Run `make reset`."
        )
    # Image inspection runs for both node names.
    for node in cluster.nodes:
        configured = _docker_inspect_image(node.name, runner)
        if configured != settings.k3s_image:
            raise PlaygroundError(
                f"Cluster node {node.name!r} is configured with image "
                f"{configured!r}; expected {settings.k3s_image!r}. "
                "Run `make reset`."
            )


def _wait_for_registry_endpoint(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> None:
    """Wait until the loopback registry endpoint answers ``GET /v2/``."""
    deadline = time.monotonic() + REGISTRY_START_TIMEOUT_SECONDS
    url = f"http://127.0.0.1:{settings.registry_port}/v2/"
    while time.monotonic() < deadline:
        try:
            with urllib.request.urlopen(url, timeout=2.0) as response:
                if response.status < 400:
                    return
        except (urllib.error.URLError, urllib.error.HTTPError, OSError):
            pass
        time.sleep(1.0)
    raise PlaygroundError(
        f"Registry {url} did not become reachable within "
        f"{REGISTRY_START_TIMEOUT_SECONDS}s."
    )


def _write_kubeconfig(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> None:
    """Capture the k3d kubeconfig into the dedicated isolated path."""
    kubeconfig_dir = settings.kubeconfig_path.parent
    kubeconfig_dir.mkdir(
        mode=KUBECONFIG_DIR_MODE,
        parents=True,
        exist_ok=True,
    )
    result = runner.run(
        ["k3d", "kubeconfig", "get", settings.cluster_name],
        check=True,
    )
    fd, tmp_path = tempfile.mkstemp(
        prefix="kubeconfig-", suffix=".yaml", dir=str(kubeconfig_dir)
    )
    try:
        with os.fdopen(fd, "w") as handle:
            handle.write(result.stdout)
        os.chmod(tmp_path, KUBECONFIG_FILE_MODE)
        os.replace(tmp_path, settings.kubeconfig_path)
    except Exception:
        if os.path.exists(tmp_path):
            os.unlink(tmp_path)
        raise


def _kubectl(
    settings: PlaygroundSettings,
    runner: CommandRunner,
    *args: str,
    timeout: float | None = None,
    namespace: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    argv: list[str] = ["kubectl", f"--kubeconfig={settings.kubeconfig_path}"]
    if namespace is not None:
        argv.extend(["--namespace", namespace])
    argv.extend(args)
    return runner.run(argv, timeout=timeout, check=check)


def _kubectl_json(
    settings: PlaygroundSettings,
    runner: CommandRunner,
    *args: str,
    namespace: str | None = None,
    timeout: float | None = None,
) -> Any:
    argv: list[str] = [
        "kubectl",
        f"--kubeconfig={settings.kubeconfig_path}",
        "-o",
        "json",
    ]
    if namespace is not None:
        argv.extend(["--namespace", namespace])
    argv.extend(args)
    return runner.run_json(argv, timeout=timeout)


def _flux(
    settings: PlaygroundSettings,
    runner: CommandRunner,
    *args: str,
    timeout: float | None = None,
    namespace: str | None = None,
    check: bool = True,
) -> subprocess.CompletedProcess[str]:
    argv: list[str] = ["flux", f"--kubeconfig={settings.kubeconfig_path}"]
    if namespace is not None:
        argv.extend(["--namespace", namespace])
    argv.extend(args)
    return runner.run(argv, timeout=timeout, check=check)


def _condition(
    resource: Mapping[str, Any] | None,
    *,
    condition_type: str,
    status: str = "True",
) -> bool:
    """Return True when a Kubernetes resource has a matching condition."""
    if not isinstance(resource, Mapping):
        return False
    conditions = (resource.get("status") or {}).get("conditions") or []
    return any(
        isinstance(cond, dict)
        and cond.get("type") == condition_type
        and str(cond.get("status", "")).lower() == status.lower()
        for cond in conditions
    )


def _condition_message(
    resource: Mapping[str, Any] | None, *, condition_type: str
) -> str:
    if not isinstance(resource, Mapping):
        return ""
    conditions = (resource.get("status") or {}).get("conditions") or []
    for cond in conditions:
        if isinstance(cond, dict) and cond.get("type") == condition_type:
            reason = cond.get("reason", "")
            message = cond.get("message", "")
            return f"{reason}: {message}".strip(": ")
    return ""


def _wait_until(
    description: str,
    deadline_monotonic: float,
    interval: float,
    predicate: Callable[[], bool],
) -> bool:
    """Poll ``predicate`` until it returns truthy or the deadline elapses."""
    while time.monotonic() < deadline_monotonic:
        if predicate():
            return True
        time.sleep(min(interval, POLL_INTERVAL_SECONDS))
    return False


def _wait_for_nodes_ready(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> None:
    """Wait until every node reports ``Ready``."""
    deadline = time.monotonic() + settings.timeout_seconds

    def predicate() -> bool:
        try:
            data = _kubectl_json(settings, runner, "get", "nodes")
        except PlaygroundError:
            return False
        items = data.get("items", []) if isinstance(data, dict) else []
        if len(items) != 2:
            return False
        return all(_condition(item, condition_type="Ready") for item in items)

    if not _wait_until("nodes Ready", deadline, POLL_INTERVAL_SECONDS, predicate):
        raise PlaygroundError("Timed out waiting for cluster nodes to become Ready.")


def _wait_for_operator_ready(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> None:
    """Wait until the Flux Operator HelmChart, Job, and Deployment are ready.

    Each poll iteration inspects the live resource and fails fast on
    terminal conditions (``Failed=True``) before the timeout elapses.
    """
    deadline = time.monotonic() + settings.timeout_seconds

    def helmchart_present() -> bool:
        result = _kubectl(
            settings,
            runner,
            "get",
            f"helmchart/{OPERATOR_HELMCHART_NAME}",
            "-n",
            OPERATOR_HELMCHART_NAMESPACE,
            check=False,
            timeout=30.0,
        )
        return result.returncode == 0

    if not _wait_until(
        "helmchart present", deadline, POLL_INTERVAL_SECONDS, helmchart_present
    ):
        raise PlaygroundError(
            f"Timed out waiting for HelmChart/{OPERATOR_HELMCHART_NAME}."
        )

    def helmchart_job_ready() -> bool:
        try:
            data = _kubectl_json(
                settings,
                runner,
                "get",
                f"helmchart/{OPERATOR_HELMCHART_NAME}",
                "-n",
                OPERATOR_HELMCHART_NAMESPACE,
                timeout=30.0,
            )
        except PlaygroundError:
            return False
        # Fail fast on terminal conditions during every poll.
        if _condition(data, condition_type="Failed"):
            raise PlaygroundError(
                f"HelmChart/{OPERATOR_HELMCHART_NAME} reported Failed=True: "
                f"{_condition_message(data, condition_type='Failed')}"
            )
        job_name = (data.get("status") or {}).get("jobName")
        if not job_name:
            return False
        try:
            job = _kubectl_json(
                settings,
                runner,
                "get",
                f"job/{job_name}",
                "-n",
                OPERATOR_HELMCHART_NAMESPACE,
                timeout=30.0,
            )
        except PlaygroundError:
            return False
        if _condition(job, condition_type="Failed"):
            raise PlaygroundError(
                f"Helm install Job {job_name!r} reported Failed=True: "
                f"{_condition_message(job, condition_type='Failed')}"
            )
        return _condition(job, condition_type="Complete")

    if not _wait_until(
        "helmchart install job", deadline, POLL_INTERVAL_SECONDS, helmchart_job_ready
    ):
        raise PlaygroundError(
            "Timed out waiting for the Flux Operator install Job to Complete."
        )

    def deployment_available() -> bool:
        try:
            data = _kubectl_json(
                settings,
                runner,
                "get",
                "deployment",
                OPERATOR_DEPLOYMENT_NAME,
                "-n",
                settings.flux_namespace,
            )
        except PlaygroundError:
            return False
        spec_replicas = (data.get("spec", {}) or {}).get("replicas", 0) or 0
        ready = (data.get("status", {}) or {}).get("readyReplicas", 0) or 0
        return _condition(data, condition_type="Available") and ready >= max(
            spec_replicas, 1
        )

    if not _wait_until(
        "Flux Operator deployment",
        deadline,
        POLL_INTERVAL_SECONDS,
        deployment_available,
    ):
        raise PlaygroundError("Timed out waiting for the Flux Operator deployment.")


def _wait_for_flux_instance(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> None:
    """Wait for ``FluxInstance/flux`` to report ``Ready=True``.

    Each poll iteration inspects the live resource and fails fast on the
    ``Stalled=True`` terminal condition so callers do not have to wait
    for the full timeout to learn that the operator gave up.
    """
    deadline = time.monotonic() + settings.timeout_seconds

    def predicate() -> bool:
        try:
            data = _kubectl_json(
                settings,
                runner,
                "get",
                "fluxinstance",
                "flux",
                "-n",
                settings.flux_namespace,
            )
        except PlaygroundError:
            return False
        if _condition(data, condition_type="Stalled"):
            raise PlaygroundError(
                f"FluxInstance/flux reported Stalled=True: "
                f"{_condition_message(data, condition_type='Stalled')}"
            )
        return _condition(data, condition_type="Ready")

    if not _wait_until(
        "FluxInstance ready", deadline, POLL_INTERVAL_SECONDS, predicate
    ):
        raise PlaygroundError(
            "Timed out waiting for FluxInstance/flux to report Ready."
        )


def _wait_for_flux_controllers(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> None:
    deadline = time.monotonic() + settings.timeout_seconds

    def predicate() -> bool:
        for controller in settings.flux_controllers:
            try:
                data = _kubectl_json(
                    settings,
                    runner,
                    "get",
                    "deployment",
                    controller,
                    "-n",
                    settings.flux_namespace,
                )
            except PlaygroundError:
                return False
            spec_replicas = (data.get("spec", {}) or {}).get("replicas", 0) or 0
            ready = (data.get("status", {}) or {}).get("readyReplicas", 0) or 0
            if not _condition(data, condition_type="Available"):
                return False
            if ready < max(spec_replicas, 1):
                return False
        return True

    if not _wait_until("Flux controllers", deadline, POLL_INTERVAL_SECONDS, predicate):
        raise PlaygroundError("Timed out waiting for Flux controller deployments.")


def _wait_for_flux_crds(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> None:
    """Wait until every expected CRD reports ``Established=True``."""
    deadline = time.monotonic() + settings.timeout_seconds

    def predicate() -> bool:
        try:
            data = _kubectl_json(settings, runner, "get", "crds")
        except PlaygroundError:
            return False
        items = data.get("items", []) if isinstance(data, dict) else []
        established: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str((item.get("metadata") or {}).get("name", ""))
            if _condition(item, condition_type="Established"):
                established.add(name)
        return set(EXPECTED_CRDS).issubset(established)

    if not _wait_until(
        "Flux CRDs established", deadline, POLL_INTERVAL_SECONDS, predicate
    ):
        raise PlaygroundError(
            "Timed out waiting for the expected Flux CRDs to be Established."
        )


def _apply_flux_instance(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> None:
    _kubectl(
        settings,
        runner,
        "apply",
        "-f",
        str(FLUX_INSTANCE_MANIFEST_PATH),
        timeout=60.0,
    )


def _stage_working_tree(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> Path:
    """Copy tracked and non-ignored untracked files into a temp tree.

    Deleted tracked files are represented by their absence. Symlinks stay
    as symlinks; targets outside the repository are not followed.
    """
    repo = settings.repo_root
    listed = runner.run(
        [
            "git",
            "-C",
            str(repo),
            "ls-files",
            "--cached",
            "--others",
            "--exclude-standard",
            "-z",
        ],
        check=True,
    )
    paths = [item for item in (listed.stdout or "").split("\x00") if item]

    staging = Path(tempfile.mkdtemp(prefix="flux-playground-", dir=str(repo)))
    try:
        for rel in paths:
            src = repo / rel
            dst = staging / rel
            if not src.exists():
                continue
            dst.parent.mkdir(parents=True, exist_ok=True)
            if src.is_symlink():
                link_target = os.readlink(src)
                os.symlink(link_target, dst)
                continue
            shutil.copy2(src, dst, follow_symlinks=False)
        return staging
    except Exception:
        shutil.rmtree(staging, ignore_errors=True)
        raise


def _git_source_metadata(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> tuple[str, str]:
    """Return ``(source, revision)`` for ``flux push artifact``."""
    repo = settings.repo_root
    remote = runner.run(
        ["git", "-C", str(repo), "config", "--get", "remote.origin.url"],
        check=False,
    )
    source = remote.stdout.strip() if remote.returncode == 0 else f"file://{repo}"
    if not source:
        source = f"file://{repo}"

    branch = runner.run(
        ["git", "-C", str(repo), "rev-parse", "--abbrev-ref", "HEAD"],
        check=False,
    )
    branch_name = branch.stdout.strip() if branch.returncode == 0 else "detached"

    sha = runner.run(
        ["git", "-C", str(repo), "rev-parse", "HEAD"],
        check=False,
    )
    sha_value = sha.stdout.strip() if sha.returncode == 0 else "unknown"
    revision = f"{branch_name}@sha1:{sha_value}"
    return source, revision


def _push_artifact(
    settings: PlaygroundSettings,
    runner: CommandRunner,
    *,
    stdout: Callable[[str], None] = print,
) -> str:
    """Push the staged tree as a mutable OCI artifact, return its digest."""
    _check_registry_reachable(settings)
    staging = _stage_working_tree(settings, runner)
    try:
        source, revision = _git_source_metadata(settings, runner)
        argv = [
            "flux",
            f"--kubeconfig={settings.kubeconfig_path}",
            "push",
            "artifact",
            settings.host_artifact_url,
            f"--path={staging}",
            f"--source={source}",
            f"--revision={revision}",
            "--insecure-registry",
            "--output=json",
        ]
        result = runner.run(argv, timeout=120.0, check=True)
        digest = _extract_digest(result.stdout) or ""
        stdout(f"Push artifact digest: {digest or '(unknown)'}")
        return digest
    finally:
        shutil.rmtree(staging, ignore_errors=True)


def _check_registry_reachable(settings: PlaygroundSettings) -> None:
    """Confirm the local registry answers at ``/v2/``."""
    url = f"http://127.0.0.1:{settings.registry_port}/v2/"
    request = urllib.request.Request(url, method="GET")
    try:
        urllib.request.urlopen(request, timeout=5.0).read()
    except (urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise PlaygroundError(f"Local registry {url} is not reachable: {exc}") from exc


def _extract_digest(payload: str) -> str | None:
    if not payload:
        return None
    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        return None
    if isinstance(data, dict):
        for key in ("digest", "Digest"):
            value = data.get(key)
            if isinstance(value, str):
                return value
    return None


def _classify_pods(pods_payload: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Classify every pod in a kubectl-style payload as healthy/unhealthy.

    Returns a dict with two lists: ``healthy`` and ``unhealthy``. Each
    entry contains ``namespace``, ``name``, ``phase``, ``restart_count``,
    and ``not_ready`` (a boolean for ``Running`` pods only).
    """
    healthy: list[dict[str, Any]] = []
    unhealthy: list[dict[str, Any]] = []
    items = pods_payload.get("items") if isinstance(pods_payload, dict) else None
    if not isinstance(items, list):
        return {"healthy": healthy, "unhealthy": unhealthy}

    for pod in items:
        if not isinstance(pod, dict):
            continue
        metadata = pod.get("metadata") or {}
        status = pod.get("status") or {}
        namespace = str(metadata.get("namespace", ""))
        name = str(metadata.get("name", ""))
        phase = str(status.get("phase", ""))
        container_statuses = status.get("containerStatuses") or []
        restart_count = sum(
            int(cs.get("restartCount", 0) or 0) for cs in container_statuses
        )
        containers_ready = bool(container_statuses) and all(
            isinstance(cs, dict) and cs.get("ready", False) for cs in container_statuses
        )

        unhealthy_flag: bool
        if phase == "Succeeded":
            nonzero = []
            for cs in container_statuses:
                if not isinstance(cs, dict):
                    continue
                state = cs.get("state") or {}
                if not isinstance(state, dict):
                    continue
                terminated = state.get("terminated")
                if not isinstance(terminated, dict):
                    continue
                exit_code = terminated.get("exitCode", 0)
                if exit_code != 0:
                    nonzero.append(exit_code)
            if not container_statuses or not nonzero:
                unhealthy_flag = False
            else:
                unhealthy_flag = True
        elif phase == "Running":
            unhealthy_flag = not (containers_ready and restart_count == 0)
        elif phase in ("Pending", "Failed", "Unknown", "CrashLoopBackOff"):
            unhealthy_flag = True
        elif not container_statuses:
            unhealthy_flag = False
        else:
            unhealthy_flag = not containers_ready or restart_count > 0

        entry = {
            "namespace": namespace,
            "name": name,
            "phase": phase,
            "restart_count": restart_count,
            "not_ready": bool(container_statuses) and not containers_ready,
        }
        if unhealthy_flag:
            unhealthy.append(entry)
        else:
            healthy.append(entry)

    return {"healthy": healthy, "unhealthy": unhealthy}


def _apply_sync(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> None:
    _kubectl(settings, runner, "apply", "-f", str(SYNC_MANIFEST_PATH), timeout=60.0)


def _flux_timeout_arg(settings: PlaygroundSettings) -> str:
    """Return the ``--timeout=<seconds>s`` value to forward to the Flux CLI."""
    return f"--timeout={int(settings.timeout_seconds)}s"


def _reconcile_source(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> None:
    subprocess_timeout = float(settings.timeout_seconds) + 15.0
    _flux(
        settings,
        runner,
        "reconcile",
        "source",
        "oci",
        settings.oci_repository_name,
        _flux_timeout_arg(settings),
        namespace=settings.flux_namespace,
        timeout=subprocess_timeout,
    )


def _reconcile_kustomization(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> None:
    subprocess_timeout = float(settings.timeout_seconds) + 15.0
    _flux(
        settings,
        runner,
        "reconcile",
        "kustomization",
        settings.kustomization_name,
        _flux_timeout_arg(settings),
        namespace=settings.flux_namespace,
        timeout=subprocess_timeout,
    )


def _wait_for_source_ready(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> None:
    deadline = time.monotonic() + settings.timeout_seconds

    def predicate() -> bool:
        try:
            data = _kubectl_json(
                settings,
                runner,
                "get",
                "ocirepository",
                settings.oci_repository_name,
                "-n",
                settings.flux_namespace,
            )
        except PlaygroundError:
            return False
        return _condition(data, condition_type="Ready")

    if not _wait_until("OCIRepository", deadline, POLL_INTERVAL_SECONDS, predicate):
        raise PlaygroundError(
            "Timed out waiting for OCIRepository/playground to be Ready."
        )


def _wait_for_kustomization_ready(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> None:
    deadline = time.monotonic() + settings.timeout_seconds

    def predicate() -> bool:
        try:
            data = _kubectl_json(
                settings,
                runner,
                "get",
                "kustomization",
                settings.kustomization_name,
                "-n",
                settings.flux_namespace,
            )
        except PlaygroundError:
            return False
        return _condition(data, condition_type="Ready")

    if not _wait_until("Kustomization", deadline, POLL_INTERVAL_SECONDS, predicate):
        raise PlaygroundError(
            "Timed out waiting for Kustomization/playground to be Ready."
        )


def _wait_for_smoke_workload(
    settings: PlaygroundSettings,
    runner: CommandRunner,
) -> None:
    deadline = time.monotonic() + settings.timeout_seconds

    def predicate() -> bool:
        try:
            data = _kubectl_json(
                settings,
                runner,
                "get",
                "deployment",
                SMOKE_DEPLOYMENT,
                "-n",
                SMOKE_NAMESPACE,
            )
        except PlaygroundError:
            return False
        spec_replicas = (data.get("spec", {}) or {}).get("replicas", 0) or 0
        ready = (data.get("status", {}) or {}).get("readyReplicas", 0) or 0
        return ready >= max(spec_replicas, 1)

    if not _wait_until("smoke workload", deadline, POLL_INTERVAL_SECONDS, predicate):
        raise PlaygroundError("Timed out waiting for the smoke workload Deployment.")


def _push_reconcile(
    settings: PlaygroundSettings,
    runner: CommandRunner,
    *,
    stdout: Callable[[str], None] = print,
) -> None:
    """Push the working tree and reconcile the source and Kustomization."""
    _push_artifact(settings, runner, stdout=stdout)
    _apply_sync(settings, runner)
    _reconcile_source(settings, runner)
    _reconcile_kustomization(settings, runner)
    _wait_for_source_ready(settings, runner)
    _wait_for_kustomization_ready(settings, runner)


def _bring_up(
    settings: PlaygroundSettings,
    runner: CommandRunner,
    *,
    stdout: Callable[[str], None] = print,
) -> None:
    """Bring the playground up, push the initial artifact, reconcile, check."""
    _preflight(settings, runner, stdout=stdout)
    _ensure_infrastructure(settings, runner, stdout=stdout)
    _write_kubeconfig(settings, runner)
    _wait_for_nodes_ready(settings, runner)
    _wait_for_operator_ready(settings, runner)
    _apply_flux_instance(settings, runner)
    _wait_for_flux_instance(settings, runner)
    _wait_for_flux_controllers(settings, runner)
    _wait_for_flux_crds(settings, runner)
    _push_reconcile(settings, runner, stdout=stdout)
    _wait_for_smoke_workload(settings, runner)
    if not _full_health_check(settings, runner, stdout=stdout):
        raise PlaygroundError("Final health check failed.")


def _tear_down(
    settings: PlaygroundSettings,
    runner: CommandRunner,
    *,
    stdout: Callable[[str], None] = print,
) -> None:
    """Delete the cluster, registry, and the isolated kubeconfig."""
    errors: list[str] = []
    # Discovery is name-only so that teardown can still recover a
    # playground whose detailed state fails the strict parsers; those
    # remain in force for `up` and `check`.
    cluster_present = False
    registry_present = False
    try:
        cluster_present = _cluster_name_present(settings, runner)
    except PlaygroundError as exc:
        errors.append(f"cluster discovery failed: {exc}")

    try:
        registry_present = _registry_name_present(settings, runner)
    except PlaygroundError as exc:
        errors.append(f"registry discovery failed: {exc}")

    if cluster_present:
        try:
            result = runner.run(
                ["k3d", "cluster", "delete", settings.cluster_name],
                check=False,
            )
            if result.returncode != 0:
                errors.append(
                    f"cluster delete failed: {result.stderr.strip() or 'unknown'}"
                )
            else:
                stdout(f"Deleted cluster {settings.cluster_name!r}.")
        except Exception as exc:  # noqa: BLE001 - teardown must continue
            errors.append(f"cluster delete failed: {exc}")

    if registry_present:
        try:
            result = runner.run(
                ["k3d", "registry", "delete", settings.registry_name],
                check=False,
            )
            if result.returncode != 0:
                errors.append(
                    f"registry delete failed: {result.stderr.strip() or 'unknown'}"
                )
            else:
                stdout(f"Deleted registry {settings.registry_name!r}.")
        except Exception as exc:  # noqa: BLE001
            errors.append(f"registry delete failed: {exc}")

    try:
        if settings.kubeconfig_path.exists():
            settings.kubeconfig_path.unlink()
            stdout(f"Removed {settings.kubeconfig_path}.")
    except Exception as exc:  # noqa: BLE001
        errors.append(f"kubeconfig remove failed: {exc}")

    if errors:
        raise PlaygroundError("Teardown reported errors: " + "; ".join(errors))


def _full_health_check(
    settings: PlaygroundSettings,
    runner: CommandRunner,
    *,
    stdout: Callable[[str], None] = print,
) -> bool:
    """Aggregate every check; print results and return ``True`` when healthy.

    Diagnostics are the caller's responsibility so this function can be
    invoked from any task without risk of duplicate dumps. The check is
    strictly read-only: it never restarts, creates, deletes, or waits
    for an endpoint.
    """
    failures: list[str] = []
    stdout("Health check:")

    # State accessors below intentionally raise ``PlaygroundError`` on
    # parser or vendor-contract failures. The aggregation loop at the
    # bottom of this function catches every exception, prints its
    # message, marks the individual check failed, and continues with
    # later checks — which is what surfaces actionable parser reasons
    # to the operator. Missing-target cases still return ``None`` and
    # are handled as ordinary "fail" entries.

    def registry_state() -> RegistryState | None:
        return _registry_state(settings, runner)

    def cluster_state() -> ClusterState | None:
        return _cluster_state(settings, runner)

    def registry_running() -> bool:
        state = registry_state()
        return state is not None and state.running

    def cluster_running() -> bool:
        state = cluster_state()
        return state is not None and state.running

    def kubeconfig_present() -> bool:
        if not settings.kubeconfig_path.exists():
            return False
        try:
            text = settings.kubeconfig_path.read_text()
        except OSError:
            return False
        return settings.cluster_name in text

    # Drift errors are deliberately not caught here: the aggregation loop
    # below catches Exception and prints the message, which is what turns
    # a bare "fail" into an actionable expected-vs-actual line.
    def registry_validates() -> bool:
        state = registry_state()
        if state is None:
            return False
        _validate_registry_state(settings, state, runner)
        return True

    def cluster_validates() -> bool:
        state = cluster_state()
        if state is None:
            return False
        _validate_cluster_state(settings, state, runner)
        return True

    def taints() -> bool:
        try:
            data = _kubectl_json(settings, runner, "get", "nodes", "-o", "json")
        except PlaygroundError:
            return False
        items = data.get("items", []) if isinstance(data, dict) else []
        server_have_taint = False
        agent_no_taint = False
        for item in items:
            labels = (item.get("metadata") or {}).get("labels") or {}
            role_keys = (
                "node-role.kubernetes.io/control-plane",
                "node-role.kubernetes.io/master",
            )
            taints = (item.get("spec") or {}).get("taints") or []
            has_taint = any(
                isinstance(taint, dict)
                and taint.get("key") == SERVER_TAINT_KEY
                and taint.get("value") == SERVER_TAINT_VALUE
                and taint.get("effect") == SERVER_TAINT_EFFECT
                for taint in taints
            )
            # A role label *key* present identifies the server even when its
            # value is empty.
            is_server = any(key in labels for key in role_keys)
            if is_server:
                server_have_taint = server_have_taint or has_taint
            else:
                agent_no_taint = agent_no_taint or not has_taint
        return server_have_taint and agent_no_taint

    def operator_ready() -> bool:
        try:
            helmchart = _kubectl_json(
                settings,
                runner,
                "get",
                f"helmchart/{OPERATOR_HELMCHART_NAME}",
                "-n",
                OPERATOR_HELMCHART_NAMESPACE,
            )
        except PlaygroundError:
            return False
        if not isinstance(helmchart, dict):
            return False
        if _condition(helmchart, condition_type="Failed"):
            return False
        job_name = (helmchart.get("status") or {}).get("jobName")
        if not job_name:
            return False
        try:
            job = _kubectl_json(
                settings,
                runner,
                "get",
                f"job/{job_name}",
                "-n",
                OPERATOR_HELMCHART_NAMESPACE,
            )
        except PlaygroundError:
            return False
        if _condition(job, condition_type="Failed"):
            return False
        if not _condition(job, condition_type="Complete"):
            return False
        try:
            deployment = _kubectl_json(
                settings,
                runner,
                "get",
                "deployment",
                OPERATOR_DEPLOYMENT_NAME,
                "-n",
                settings.flux_namespace,
            )
        except PlaygroundError:
            return False
        return _condition(deployment, condition_type="Available")

    def flux_instance_ready() -> bool:
        try:
            data = _kubectl_json(
                settings,
                runner,
                "get",
                "fluxinstance",
                "flux",
                "-n",
                settings.flux_namespace,
            )
        except PlaygroundError:
            return False
        return _condition(data, condition_type="Ready")

    def crds_established() -> bool:
        try:
            data = _kubectl_json(settings, runner, "get", "crds")
        except PlaygroundError:
            return False
        items = data.get("items", []) if isinstance(data, dict) else []
        established: set[str] = set()
        for item in items:
            if not isinstance(item, dict):
                continue
            name = str((item.get("metadata") or {}).get("name", ""))
            if _condition(item, condition_type="Established"):
                established.add(name)
        return set(EXPECTED_CRDS).issubset(established)

    def controllers_available() -> bool:
        for controller in settings.flux_controllers:
            try:
                data = _kubectl_json(
                    settings,
                    runner,
                    "get",
                    "deployment",
                    controller,
                    "-n",
                    settings.flux_namespace,
                )
            except PlaygroundError:
                return False
            spec_replicas = (data.get("spec", {}) or {}).get("replicas", 0) or 0
            ready = (data.get("status", {}) or {}).get("readyReplicas", 0) or 0
            if not _condition(data, condition_type="Available"):
                return False
            if ready < max(spec_replicas, 1):
                return False
        return True

    def flux_check_ok() -> bool:
        result = _flux(settings, runner, "check", check=False, timeout=60.0)
        return result.returncode == 0

    def ocirepo_ready() -> bool:
        try:
            data = _kubectl_json(
                settings,
                runner,
                "get",
                "ocirepository",
                settings.oci_repository_name,
                "-n",
                settings.flux_namespace,
            )
        except PlaygroundError:
            return False
        return _condition(data, condition_type="Ready")

    def kustomization_ready() -> bool:
        try:
            data = _kubectl_json(
                settings,
                runner,
                "get",
                "kustomization",
                settings.kustomization_name,
                "-n",
                settings.flux_namespace,
            )
        except PlaygroundError:
            return False
        return _condition(data, condition_type="Ready")

    def smoke_workload_ready() -> bool:
        try:
            deployment = _kubectl_json(
                settings,
                runner,
                "get",
                "deployment",
                SMOKE_DEPLOYMENT,
                "-n",
                SMOKE_NAMESPACE,
            )
        except PlaygroundError:
            return False
        spec_replicas = (deployment.get("spec", {}) or {}).get("replicas", 0) or 0
        ready = (deployment.get("status", {}) or {}).get("readyReplicas", 0) or 0
        if ready < max(spec_replicas, 1):
            return False
        try:
            pods = _kubectl_json(
                settings,
                runner,
                "get",
                "pods",
                "-n",
                SMOKE_NAMESPACE,
                "-l",
                SMOKE_APP_LABEL,
            )
        except PlaygroundError:
            return False
        items = pods.get("items", []) if isinstance(pods, dict) else []
        if not items:
            return False
        # Identify the agent node name and require the pod to land there.
        nodes_data = _kubectl_json(settings, runner, "get", "nodes", "-o", "json")
        node_items = nodes_data.get("items", []) if isinstance(nodes_data, dict) else []
        agent_names = {
            str((n.get("metadata") or {}).get("name", ""))
            for n in node_items
            if "node-role.kubernetes.io/control-plane"
            not in ((n.get("metadata") or {}).get("labels") or {})
            and "node-role.kubernetes.io/master"
            not in ((n.get("metadata") or {}).get("labels") or {})
        }
        for pod in items:
            node_name = (pod.get("spec") or {}).get("nodeName", "")
            if not node_name:
                return False
            if agent_names and node_name not in agent_names:
                return False
            if not _condition(pod, condition_type="Ready"):
                return False
        return True

    for name, fn in [
        ("registry running", registry_running),
        ("cluster running", cluster_running),
        ("kubeconfig present", kubeconfig_present),
        ("registry image and binding", registry_validates),
        ("cluster topology and images", cluster_validates),
        ("node taints", taints),
        ("Flux Operator ready", operator_ready),
        ("FluxInstance ready", flux_instance_ready),
        ("Flux CRDs Established", crds_established),
        ("Flux controllers available", controllers_available),
        ("flux check", flux_check_ok),
        ("OCIRepository ready", ocirepo_ready),
        ("Kustomization ready", kustomization_ready),
        ("smoke workload on agent", smoke_workload_ready),
    ]:
        ok = False
        try:
            ok = bool(fn())
        except Exception as exc:  # noqa: BLE001 - aggregate, never raise
            stdout(f"  {name}: error ({exc})")
        stdout(f"  {name}: {'ok' if ok else 'fail'}")
        if not ok:
            failures.append(name)

    if failures:
        stdout("Health check failed: " + ", ".join(failures))
        return False
    stdout("Health check passed.")
    return True


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


def _diagnose(
    settings: PlaygroundSettings,
    runner: CommandRunner,
    *,
    stdout: Callable[[str], None] = print,
) -> None:
    """Print bounded, actionable diagnostics on failure.

    Each section is wrapped so a discovery failure cannot replace the
    primary failure.
    """

    def emit(title: str, argv: Sequence[str]) -> None:
        stdout(f"--- {title} ---")
        try:
            result = runner.run(argv, check=False, timeout=30.0)
            if result.stdout:
                stdout(result.stdout)
            if result.stderr:
                stdout(result.stderr)
        except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
            stdout(f"(diagnostic failed: {exc})")

    kubeconfig_arg = f"--kubeconfig={settings.kubeconfig_path}"

    emit("k3d clusters", ["k3d", "cluster", "list", "-o", "json"])
    emit("k3d registries", ["k3d", "registry", "list", "-o", "json"])
    emit(
        "kubectl get nodes",
        ["kubectl", kubeconfig_arg, "get", "nodes", "-o", "wide"],
    )
    emit(
        "kubectl get pods/deployments",
        [
            "kubectl",
            kubeconfig_arg,
            "get",
            "pods,deployments,statefulsets,daemonsets",
            "-A",
            "-o",
            "wide",
        ],
    )
    emit(
        "Flux Operator HelmChart",
        [
            "kubectl",
            kubeconfig_arg,
            "get",
            f"helmchart/{OPERATOR_HELMCHART_NAME}",
            "-n",
            OPERATOR_HELMCHART_NAMESPACE,
            "-o",
            "yaml",
        ],
    )

    # Attempt to include the install job logs when the HelmChart reports one.
    job_name = None
    try:
        helmchart = _kubectl_json(
            settings,
            runner,
            "get",
            f"helmchart/{OPERATOR_HELMCHART_NAME}",
            "-n",
            OPERATOR_HELMCHART_NAMESPACE,
        )
        if isinstance(helmchart, dict):
            job_name = (helmchart.get("status") or {}).get("jobName")
    except PlaygroundError:
        job_name = None

    if job_name:
        emit(
            f"helm install Job {job_name}",
            [
                "kubectl",
                kubeconfig_arg,
                "describe",
                f"job/{job_name}",
                "-n",
                OPERATOR_HELMCHART_NAMESPACE,
            ],
        )
        emit(
            f"helm install Job logs {job_name}",
            [
                "kubectl",
                kubeconfig_arg,
                "logs",
                f"job/{job_name}",
                "--all-containers",
                f"--tail={LOG_TAIL_LINES}",
                "-n",
                OPERATOR_HELMCHART_NAMESPACE,
            ],
        )

    emit(
        "FluxInstance",
        [
            "kubectl",
            kubeconfig_arg,
            "describe",
            "fluxinstance",
            "flux",
            "-n",
            settings.flux_namespace,
        ],
    )
    emit(
        "flux get all",
        [
            "flux",
            kubeconfig_arg,
            "get",
            "all",
            "--all-namespaces",
        ],
    )

    # Detailed views for the playground sources. The diagnostic is a
    # single, well-formed command per resource; a bare ``-n`` without a
    # namespace would be malformed and unhelpful.
    for kind, name in (
        ("ocirepository", settings.oci_repository_name),
        ("kustomization", settings.kustomization_name),
    ):
        stdout(f"--- {kind}/{name} full yaml ---")
        argv = [
            "kubectl",
            kubeconfig_arg,
            "get",
            f"{kind}/{name}",
            "-n",
            settings.flux_namespace,
            "-o",
            "yaml",
        ]
        try:
            result = runner.run(argv, check=False, timeout=30.0)
            if result.stdout:
                stdout(result.stdout)
            if result.stderr:
                stdout(result.stderr)
        except Exception as exc:  # noqa: BLE001
            stdout(f"(diagnostic failed: {exc})")

    # Per-namespace HelmRelease summaries; full describe for any failing one.
    try:
        helmreleases = _kubectl_json(settings, runner, "get", "helmreleases", "-A")
    except PlaygroundError:
        helmreleases = {}
    items = helmreleases.get("items", []) if isinstance(helmreleases, dict) else []
    stdout("--- helmrelease summary ---")
    for hr in items:
        metadata = hr.get("metadata") or {}
        status = hr.get("status") or {}
        namespace = metadata.get("namespace", "")
        name = metadata.get("name", "")
        ready = any(
            isinstance(cond, dict)
            and cond.get("type") == "Ready"
            and str(cond.get("status", "")).lower() == "true"
            for cond in (status.get("conditions") or [])
        )
        stdout(f"  {namespace}/{name}: {'Ready' if ready else 'NotReady'}")
    for hr in items:
        metadata = hr.get("metadata") or {}
        status = hr.get("status") or {}
        ready = any(
            isinstance(cond, dict)
            and cond.get("type") == "Ready"
            and str(cond.get("status", "")).lower() == "true"
            for cond in (status.get("conditions") or [])
        )
        if ready:
            continue
        namespace = metadata.get("namespace", "")
        name = metadata.get("name", "")
        emit(
            f"helmrelease describe {namespace}/{name}",
            [
                "kubectl",
                kubeconfig_arg,
                "describe",
                "helmrelease",
                name,
                "-n",
                namespace,
            ],
        )

    # Unhealthy pods across the cluster. Classification rules:
    # - ``Succeeded`` with all containers reporting exit code 0 is healthy.
    # - ``Succeeded`` with any nonzero exit code is unhealthy.
    # - ``Running`` is unhealthy if any container is not Ready or any
    #   container has a nonzero restart count.
    # - ``Pending`` / ``Failed`` / ``Unknown`` are unhealthy.
    # - Jobs use init-container-style status; the same rules apply to
    #   their termination state via ``state.terminated.exitCode``.
    try:
        pods = _kubectl_json(settings, runner, "get", "pods", "-A")
    except PlaygroundError:
        pods = {}
    pod_items = pods.get("items", []) if isinstance(pods, dict) else []
    classification = _classify_pods({"items": pod_items})
    bad_pods = []
    for entry in classification["unhealthy"]:
        bad_pods.append(
            (
                entry["namespace"],
                entry["name"],
                entry["phase"],
                entry["restart_count"],
                entry["not_ready"],
            )
        )

    stdout("--- unhealthy pods ---")
    if not bad_pods:
        stdout("  (none)")
    else:
        for namespace, name, phase, restart_count, not_ready in bad_pods:
            stdout(
                f"  {namespace}/{name} phase={phase} restarts={restart_count} "
                f"containers_not_ready={not_ready}"
            )
        for namespace, name, _, restart_count, _ in bad_pods:
            emit(
                f"describe pod {namespace}/{name}",
                [
                    "kubectl",
                    kubeconfig_arg,
                    "describe",
                    "pod",
                    name,
                    "-n",
                    namespace,
                ],
            )
            emit(
                f"logs pod {namespace}/{name}",
                [
                    "kubectl",
                    kubeconfig_arg,
                    "logs",
                    name,
                    "-n",
                    namespace,
                    "--all-containers",
                    f"--tail={LOG_TAIL_LINES}",
                ],
            )
            if restart_count:
                emit(
                    f"previous logs pod {namespace}/{name}",
                    [
                        "kubectl",
                        kubeconfig_arg,
                        "logs",
                        name,
                        "-n",
                        namespace,
                        "--all-containers",
                        f"--tail={LOG_TAIL_LINES}",
                        "--previous",
                    ],
                )

    emit(
        "kubectl events",
        [
            "kubectl",
            kubeconfig_arg,
            "get",
            "events",
            "-A",
            "--sort-by=.lastTimestamp",
        ],
    )

    for controller in (OPERATOR_DEPLOYMENT_NAME, *settings.flux_controllers):
        emit(
            f"logs {controller}",
            [
                "kubectl",
                kubeconfig_arg,
                "-n",
                settings.flux_namespace,
                "logs",
                f"deployment/{controller}",
                f"--tail={LOG_TAIL_LINES}",
            ],
        )


def _emit_diagnostics_once(
    settings: PlaygroundSettings,
    runner: CommandRunner,
    *,
    stdout: Callable[[str], None] = print,
) -> None:
    """Wrap :func:`_diagnose` to swallow every internal failure."""
    try:
        _diagnose(settings, runner, stdout=stdout)
    except Exception as exc:  # noqa: BLE001 - diagnostics must not raise
        stdout(f"(diagnostics crashed: {exc})")


# ---------------------------------------------------------------------------
# Invoke tasks
# ---------------------------------------------------------------------------


@task
def up(c, registry_port=None, timeout=None):
    """Create the disposable k3d cluster and reconcile the playground."""
    settings = _settings_from(c, registry_port=registry_port, timeout=timeout)
    runner = CommandRunner()
    try:
        # `up` may reuse an existing registry, so it adopts that
        # registry's port. `reset` deliberately does not: it destroys and
        # recreates, and the resolved port is what the new one binds.
        settings = _adopt_existing_registry_port(settings, runner)
        _bring_up(settings, runner)
    except PlaygroundError as exc:
        _emit_diagnostics_once(settings, runner)
        raise SystemExit(str(exc)) from exc


@task
def push(c, registry_port=None, timeout=None):
    """Re-push the working tree and reconcile Flux to the new artifact."""
    settings = _settings_from(c, registry_port=registry_port, timeout=timeout)
    runner = CommandRunner()
    try:
        _preflight(settings, runner)
        settings = _adopt_existing_registry_port(settings, runner)
        _check_registry_reachable(settings)
        _write_kubeconfig(settings, runner)
        _push_reconcile(settings, runner)
        _wait_for_smoke_workload(settings, runner)
        if not _full_health_check(settings, runner):
            raise PlaygroundError("Push completed but the health check failed.")
    except PlaygroundError as exc:
        _emit_diagnostics_once(settings, runner)
        raise SystemExit(str(exc)) from exc


@task
def check(c, registry_port=None, timeout=None):
    """Verify the playground is healthy without changing state."""
    settings = _settings_from(c, registry_port=registry_port, timeout=timeout)
    runner = CommandRunner()
    try:
        settings = _adopt_existing_registry_port(settings, runner)
        if not _full_health_check(settings, runner):
            _emit_diagnostics_once(settings, runner)
            raise SystemExit("Health check failed.")
    except PlaygroundError as exc:
        _emit_diagnostics_once(settings, runner)
        raise SystemExit(str(exc)) from exc


@task
def down(c, registry_port=None, timeout=None):
    """Tear down the cluster, registry, and kubeconfig."""
    settings = _settings_from(c, registry_port=registry_port, timeout=timeout)
    runner = CommandRunner()
    try:
        _tear_down(settings, runner)
    except PlaygroundError as exc:
        raise SystemExit(str(exc)) from exc


@task
def reset(c, registry_port=None, timeout=None):
    """Tear down then bring the playground back up cleanly."""
    settings = _settings_from(c, registry_port=registry_port, timeout=timeout)
    runner = CommandRunner()
    try:
        _tear_down(settings, runner)
        _bring_up(settings, runner)
    except PlaygroundError as exc:
        _emit_diagnostics_once(settings, runner)
        raise SystemExit(str(exc)) from exc
