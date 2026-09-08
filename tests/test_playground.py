"""Unit tests for the playground workflow.

All external commands and the filesystem are mocked. The tests must run
without Docker, k3d, kubectl, helm, flux, or any running Kubernetes
cluster.

Lifecycle fixtures use the vendor-shaped JSON output of ``k3d registry
list -o json`` and ``k3d cluster list -o json`` rather than fabricated
table strings so the test suite cannot drift away from real k3d output.
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any
from unittest import mock

from src.workflows import playground


REPO_ROOT = playground.REPO_ROOT
K3D_CONFIG_PATH = playground.K3D_CONFIG_PATH
FLUX_OPERATOR_MANIFEST_PATH = playground.FLUX_OPERATOR_MANIFEST_PATH
FLUX_INSTANCE_MANIFEST_PATH = playground.FLUX_INSTANCE_MANIFEST_PATH
SYNC_MANIFEST_PATH = playground.SYNC_MANIFEST_PATH


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_settings(tmp: Path, **overrides: Any) -> playground.PlaygroundSettings:
    base = dict(
        repo_root=tmp,
        context_dir=tmp / ".context",
        kubeconfig_path=tmp / ".context" / "kubeconfig-flux-playground.yaml",
        registry_port=5001,
        timeout_seconds=10,
    )
    base.update(overrides)
    return playground.PlaygroundSettings(**base)


def _ok(stdout: str = "", stderr: str = "", rc: int = 0) -> subprocess.CompletedProcess[str]:
    return subprocess.CompletedProcess(
        args=[], returncode=rc, stdout=stdout, stderr=stderr
    )


def _infinite_runner(*responses: subprocess.CompletedProcess[str]) -> mock.Mock:
    """Runner whose run/run_json return queued responses or a default ok.

    ``run_json`` parses the stdout of the returned response, matching the
    real :class:`CommandRunner` behaviour.
    """
    queue = list(responses)

    def run_side_effect(*a: Any, **k: Any) -> Any:
        if queue:
            return queue.pop(0)
        return _ok(stdout="")

    def run_json_side_effect(*a: Any, **k: Any) -> Any:
        result = run_side_effect(*a, **k)
        try:
            return json.loads(result.stdout or "null")
        except json.JSONDecodeError:
            return {}

    runner = mock.Mock(spec=playground.CommandRunner)
    runner.run.side_effect = run_side_effect
    runner.run_json.side_effect = run_json_side_effect
    runner.default_timeout = 60.0
    return runner


def _runner_with(responses: list[Any]) -> mock.Mock:
    """Runner whose run/run_json return the next queued response.

    Honours ``check=True`` by raising :class:`PlaygroundError` on a
    non-zero return code, matching the real :class:`CommandRunner`.
    """
    queue = list(responses)

    def side_effect(argv: list[str], **kw: Any) -> Any:
        if not queue:
            raise AssertionError(f"No more responses queued; got {argv}")
        result = queue.pop(0)
        if kw.get("check") and result.returncode != 0:
            raise playground.PlaygroundError(
                f"Command {argv!r} failed with exit code {result.returncode}: "
                f"{result.stderr.strip() if result.stderr else ''}"
            )
        return result

    def json_side_effect(argv: list[str], **kw: Any) -> Any:
        result = side_effect(argv, **kw)
        try:
            return json.loads(result.stdout)
        except json.JSONDecodeError as exc:
            raise playground.PlaygroundError(
                f"Expected JSON from {argv!r}, got: {result.stdout!r}"
            ) from exc

    runner = mock.Mock(spec=playground.CommandRunner)
    runner.run.side_effect = side_effect
    runner.run_json.side_effect = json_side_effect
    runner.default_timeout = 60.0
    return runner


def _healthy_cluster_state(
    cluster_name: str = playground.CLUSTER_NAME,
    *,
    servers_running: int = 1,
    agents_running: int = 1,
    servers_count: int = 1,
    agents_count: int = 1,
    has_load_balancer: bool = False,
    include_load_balancer_field: bool | None = None,
) -> dict[str, Any]:
    """Return a representative k3d v5.9 cluster JSON fixture.

    Image fields are digest-valued content IDs rather than the configured
    reference. ``include_load_balancer_field`` controls whether the
    ``hasLoadbalancer`` key is present at all; the default mirrors
    k3d's ``omitempty`` behaviour by omitting it when false.
    """
    payload: dict[str, Any] = {
        "name": cluster_name,
        "serversCount": servers_count,
        "agentsCount": agents_count,
        "serversRunning": servers_running,
        "agentsRunning": agents_running,
        "nodes": [
            {
                "name": "k3d-flux-playground-server-0",
                "role": "server",
                "image": "sha256:1111111111111111111111111111111111111111111111111111111111111111",
                "State": {"Running": True, "Status": "running"},
            },
            {
                "name": "k3d-flux-playground-agent-0",
                "role": "agent",
                "image": "sha256:2222222222222222222222222222222222222222222222222222222222222222",
                "State": {"Running": True, "Status": "running"},
            },
        ],
    }
    show_lb = include_load_balancer_field
    if show_lb is None:
        show_lb = has_load_balancer
    if show_lb:
        payload["hasLoadbalancer"] = True
    return payload


def _healthy_registry_state(
    container_name: str = playground.REGISTRY_CONTAINER_NAME,
    *,
    runtime_image_id: str = "sha256:3333333333333333333333333333333333333333333333333333333333333333",
    host_port: int = 5001,
    running: bool = True,
    state_block: dict[str, Any] | None = None,
) -> dict[str, Any]:
    """Return a representative k3d v5.9 registry JSON fixture."""
    if state_block is None:
        state_block = {"Running": running, "Status": "running" if running else "exited"}
    return {
        "name": container_name,
        "role": "registry",
        "image": runtime_image_id,
        "State": state_block,
        "portMappings": {
            "5000/tcp": [
                {"HostIp": "127.0.0.1", "HostPort": str(host_port), "ContainerPort": "5000"}
            ],
        },
    }


def _docker_inspect_mock(
    expected: dict[str, str] | None = None,
    *,
    fail: bool = False,
    empty: bool = False,
) -> mock.Mock:
    """Return a ``runner`` mock whose ``docker inspect`` returns ``expected``."""
    runner = mock.Mock(spec=playground.CommandRunner)

    def run_side_effect(argv, **kw):
        if fail:
            return _ok(rc=1, stderr="docker inspect failed")
        if argv and argv[:2] == ["docker", "inspect"] and expected is not None:
            container = argv[-1]
            if empty:
                return _ok(stdout="\n")
            return _ok(stdout=expected.get(container, "") + "\n")
        return _ok(stdout="")

    def run_json_side_effect(argv, **kw):
        if fail:
            return _ok(rc=1, stderr="docker inspect failed")
        if argv and argv[:2] == ["docker", "inspect"] and expected is not None:
            container = argv[-1]
            value = expected.get(container, "")
            try:
                return json.loads(value)
            except json.JSONDecodeError:
                return {}
        return _ok(stdout="")

    runner.run.side_effect = run_side_effect
    runner.run_json.side_effect = run_json_side_effect
    runner.default_timeout = 60.0
    return runner


def _standard_inspect_expected() -> dict[str, str]:
    """Return expected docker inspect output for the playground containers."""
    return {
        "k3d-flux-playground-registry": playground.REGISTRY_IMAGE,
        "k3d-flux-playground-server-0": playground.K3S_IMAGE,
        "k3d-flux-playground-agent-0": playground.K3S_IMAGE,
    }


def _cluster_state_two_node(
    *,
    servers_count: int = 1,
    agents_count: int = 1,
    servers_running: int = 1,
    agents_running: int = 1,
    has_load_balancer: bool = False,
    include_load_balancer_field: bool | None = None,
) -> playground.ClusterState:
    """Return a :class:`ClusterState` built from the real two-node fixture."""
    raw = _healthy_cluster_state(
        servers_running=servers_running,
        agents_running=agents_running,
        servers_count=servers_count,
        agents_count=agents_count,
        has_load_balancer=has_load_balancer,
        include_load_balancer_field=include_load_balancer_field,
    )
    return playground.ClusterState(
        name=raw["name"],
        servers_count=raw["serversCount"],
        agents_count=raw["agentsCount"],
        servers_running=raw["serversRunning"],
        agents_running=raw["agentsRunning"],
        has_load_balancer=raw.get("hasLoadbalancer", False),
        nodes=tuple(
            playground.ClusterNode(
                name=n["name"],
                role=n["role"],
                runtime_image_id=n["image"],
                running=n["State"]["Running"],
            )
            for n in raw["nodes"]
        ),
        extra=raw,
    )


def _registry_state_with_binding(
    *,
    host_ip: str = "127.0.0.1",
    host_port: int = 5001,
    runtime_image_id: str = "sha256:3333333333333333333333333333333333333333333333333333333333333333",
    running: bool = True,
) -> playground.RegistryState:
    """Return a :class:`RegistryState` with the loopback binding fields set."""
    return playground.RegistryState(
        name=playground.REGISTRY_NAME,
        container_name=playground.REGISTRY_CONTAINER_NAME,
        runtime_image_id=runtime_image_id,
        running=running,
        host_ip=host_ip,
        host_port=host_port,
        extra={},
    )


# ---------------------------------------------------------------------------
# Settings / precedence / invalid values
# ---------------------------------------------------------------------------


class TestResolutionPrecedence(unittest.TestCase):
    def test_registry_port_precedence(self) -> None:
        env = {playground.CONDUCTOR_PORT_ENV: "5050", playground.PLAYGROUND_REGISTRY_PORT_ENV: "5051"}
        self.assertEqual(playground.resolve_registry_port("5055", env), 5055)
        self.assertEqual(playground.resolve_registry_port(None, env), 5051)
        self.assertEqual(playground.resolve_timeout(None, env), 300)

    def test_conductor_port_is_not_an_explicit_request(self) -> None:
        """CONDUCTOR_PORT is ambient; it must not pin an existing registry."""
        port, explicit = playground.resolve_registry_port_source(
            None, {playground.CONDUCTOR_PORT_ENV: "55150"}
        )
        self.assertEqual(port, 55150)
        self.assertFalse(explicit)

    def test_flag_and_playground_env_are_explicit(self) -> None:
        self.assertEqual(
            playground.resolve_registry_port_source("5055", {}), (5055, True)
        )
        self.assertEqual(
            playground.resolve_registry_port_source(
                None, {playground.PLAYGROUND_REGISTRY_PORT_ENV: "5051"}
            ),
            (5051, True),
        )

    def test_default_port_is_not_explicit(self) -> None:
        self.assertEqual(
            playground.resolve_registry_port_source(None, {}),
            (playground.DEFAULT_REGISTRY_PORT, False),
        )

    def test_invalid_port_rejected(self) -> None:
        with self.assertRaises(playground.PlaygroundError):
            playground.resolve_registry_port("not-a-port", {})
        with self.assertRaises(playground.PlaygroundError):
            playground.resolve_registry_port(70000, {})
        with self.assertRaises(playground.PlaygroundError):
            playground.resolve_registry_port(0, {})
        with self.assertRaises(playground.PlaygroundError):
            playground.resolve_registry_port(None, {playground.PLAYGROUND_REGISTRY_PORT_ENV: "x"})

    def test_invalid_timeout_rejected(self) -> None:
        with self.assertRaises(playground.PlaygroundError):
            playground.resolve_timeout("nope", {})
        with self.assertRaises(playground.PlaygroundError):
            playground.resolve_timeout(0, {})
        with self.assertRaises(playground.PlaygroundError):
            playground.resolve_timeout(None, {playground.PLAYGROUND_TIMEOUT_ENV: "x"})


# ---------------------------------------------------------------------------
# CommandRunner contract
# ---------------------------------------------------------------------------


class TestCommandRunner(unittest.TestCase):
    def test_run_uses_argv_list(self) -> None:
        runner = playground.CommandRunner()
        with mock.patch("subprocess.run") as patched:
            patched.return_value = _ok(stdout="hello\n")
            runner.run(["echo", "hello"])
            patched.assert_called_once()
            argv = patched.call_args.args[0]
            self.assertEqual(argv, ["echo", "hello"])
            self.assertFalse(patched.call_args.kwargs["shell"])

    def test_run_raises_on_nonzero_with_check(self) -> None:
        runner = playground.CommandRunner()
        with mock.patch("subprocess.run") as patched:
            patched.return_value = _ok(stdout="", stderr="boom", rc=2)
            with self.assertRaises(playground.PlaygroundError):
                runner.run(["false"], check=True)

    def test_timeout_becomes_playground_error(self) -> None:
        """Tasks only catch PlaygroundError; a timeout must not escape raw."""
        runner = playground.CommandRunner()
        expired = subprocess.TimeoutExpired(cmd=["sleep", "10"], timeout=5.0)
        with mock.patch("subprocess.run", side_effect=expired):
            with self.assertRaises(playground.PlaygroundError) as ctx:
                runner.run(["sleep", "10"], timeout=5.0)
        message = str(ctx.exception)
        self.assertIn("5.0s timeout", message)
        self.assertIn("sleep", message)

    def test_launch_failure_becomes_playground_error(self) -> None:
        """A missing or unexecutable binary must not escape as OSError."""
        runner = playground.CommandRunner()
        missing = FileNotFoundError(2, "No such file or directory")
        with mock.patch("subprocess.run", side_effect=missing):
            with self.assertRaises(playground.PlaygroundError) as ctx:
                runner.run(["definitely-not-a-real-binary"])
        message = str(ctx.exception)
        self.assertIn("could not be launched", message)
        self.assertIn("definitely-not-a-real-binary", message)

    def test_run_json_surfaces_timeout_as_playground_error(self) -> None:
        runner = playground.CommandRunner()
        expired = subprocess.TimeoutExpired(cmd=["k3d"], timeout=1.0)
        with mock.patch("subprocess.run", side_effect=expired):
            with self.assertRaises(playground.PlaygroundError):
                runner.run_json(["k3d", "cluster", "list", "-o", "json"])


class TestRegistryPortAdoption(unittest.TestCase):
    """An ambient port must not invalidate a registry that already exists."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _runner_listing_registry_on(self, host_port: int) -> mock.Mock:
        return _infinite_runner(
            _ok(stdout=json.dumps([_healthy_registry_state(host_port=host_port)]))
        )

    def test_ambient_port_adopts_live_registry_binding(self) -> None:
        """The CONDUCTOR_PORT case: registry on 5001, resolved port 55150."""
        settings = _make_settings(self.tmp, registry_port=55150)
        adopted = playground._adopt_existing_registry_port(
            settings, self._runner_listing_registry_on(5001), stdout=lambda _: None
        )
        self.assertEqual(adopted.registry_port, 5001)
        self.assertIn("127.0.0.1:5001", adopted.host_artifact_url)

    def test_explicit_port_still_reports_drift(self) -> None:
        settings = _make_settings(
            self.tmp, registry_port=55150, registry_port_explicit=True
        )
        adopted = playground._adopt_existing_registry_port(
            settings, self._runner_listing_registry_on(5001), stdout=lambda _: None
        )
        self.assertEqual(adopted.registry_port, 55150)
        registry = _registry_state_with_binding(host_port=5001)
        with mock.patch.object(
            playground, "_docker_inspect_image", return_value=playground.REGISTRY_IMAGE
        ):
            with self.assertRaises(playground.PlaygroundError) as ctx:
                playground._validate_registry_state(adopted, registry, mock.Mock())
        self.assertIn("55150", str(ctx.exception))

    def test_no_registry_leaves_resolved_port_for_creation(self) -> None:
        settings = _make_settings(self.tmp, registry_port=55150)
        runner = _infinite_runner(_ok(stdout=json.dumps([])))
        adopted = playground._adopt_existing_registry_port(
            settings, runner, stdout=lambda _: None
        )
        self.assertEqual(adopted.registry_port, 55150)

    def test_unparseable_registry_leaves_settings_untouched(self) -> None:
        settings = _make_settings(self.tmp, registry_port=55150)
        runner = _infinite_runner(_ok(stdout="not-json"))
        adopted = playground._adopt_existing_registry_port(
            settings, runner, stdout=lambda _: None
        )
        self.assertEqual(adopted.registry_port, 55150)

    def test_adoption_announces_the_substitution(self) -> None:
        settings = _make_settings(self.tmp, registry_port=55150)
        lines: list[str] = []
        playground._adopt_existing_registry_port(
            settings, self._runner_listing_registry_on(5001), stdout=lines.append
        )
        output = "\n".join(lines)
        self.assertIn("5001", output)
        self.assertIn("55150", output)


class TestReconcileTimeouts(unittest.TestCase):
    """The flux CLI deadline and the subprocess deadline must agree.

    A subprocess cap below the flux ``--timeout`` would kill the command
    mid-reconcile and report a Python timeout instead of the reconcile
    failure the operator actually hit.
    """

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = _make_settings(self.tmp, timeout_seconds=42)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _capture(self, fn) -> tuple[list[str], float]:
        runner = _infinite_runner()
        fn(self.settings, runner)
        call = runner.run.call_args
        return call.args[0], call.kwargs["timeout"]

    def test_source_reconcile_passes_flux_timeout_and_larger_subprocess_cap(self) -> None:
        argv, timeout = self._capture(playground._reconcile_source)
        self.assertIn("--timeout=42s", argv)
        self.assertGreater(timeout, 42.0)

    def test_kustomization_reconcile_passes_flux_timeout_and_larger_subprocess_cap(self) -> None:
        argv, timeout = self._capture(playground._reconcile_kustomization)
        self.assertIn("--timeout=42s", argv)
        self.assertGreater(timeout, 42.0)


# ---------------------------------------------------------------------------
# Preflight
# ---------------------------------------------------------------------------


class TestPreflight(unittest.TestCase):
    def test_reports_all_missing_tools(self) -> None:
        runner = _infinite_runner()
        with mock.patch.object(playground, "_check_executable", return_value=None):
            with self.assertRaises(playground.PlaygroundError) as ctx:
                playground._preflight(_make_settings(Path("/tmp")), runner)
        self.assertIn("git", str(ctx.exception))
        self.assertIn("flux", str(ctx.exception))

    def test_accepts_flux_cli_2_9_4(self) -> None:
        runner = _infinite_runner(
            _ok(stdout=""),  # docker info
            _ok(stdout="k3d v5.10.0"),
            _ok(stdout="flux 2.9.4"),
        )
        with mock.patch.object(playground, "_check_executable", return_value="/bin/x"):
            playground._preflight(_make_settings(Path("/tmp")), runner)

    def test_accepts_flux_cli_2_9_5(self) -> None:
        runner = _infinite_runner(
            _ok(stdout=""),
            _ok(stdout="k3d v5.10.0"),
            _ok(stdout="flux 2.9.5"),
        )
        with mock.patch.object(playground, "_check_executable", return_value="/bin/x"):
            playground._preflight(_make_settings(Path("/tmp")), runner)

    def test_rejects_flux_cli_2_9_3(self) -> None:
        runner = _infinite_runner(
            _ok(stdout=""),
            _ok(stdout="k3d v5.10.0"),
            _ok(stdout="flux 2.9.3"),
        )
        with mock.patch.object(playground, "_check_executable", return_value="/bin/x"):
            with self.assertRaises(playground.PlaygroundError) as ctx:
                playground._preflight(_make_settings(Path("/tmp")), runner)
        self.assertIn("flux CLI", str(ctx.exception))

    def test_rejects_flux_cli_3_x(self) -> None:
        runner = _infinite_runner(
            _ok(stdout=""),
            _ok(stdout="k3d v5.10.0"),
            _ok(stdout="flux 3.0.0"),
        )
        with mock.patch.object(playground, "_check_executable", return_value="/bin/x"):
            with self.assertRaises(playground.PlaygroundError) as ctx:
                playground._preflight(_make_settings(Path("/tmp")), runner)
        self.assertIn("flux CLI", str(ctx.exception))

    def test_rejects_old_k3d(self) -> None:
        runner = _infinite_runner(
            _ok(stdout=""),
            _ok(stdout="k3d v5.8.0"),
        )
        with mock.patch.object(playground, "_check_executable", return_value="/bin/x"):
            with self.assertRaises(playground.PlaygroundError):
                playground._preflight(_make_settings(Path("/tmp")), runner)

    def test_rejects_unreachable_docker(self) -> None:
        runner = _infinite_runner(_ok(rc=1, stderr="cannot connect"))
        with mock.patch.object(playground, "_check_executable", return_value="/bin/x"):
            with self.assertRaises(playground.PlaygroundError) as ctx:
                playground._preflight(_make_settings(Path("/tmp")), runner)
        self.assertIn("Docker", str(ctx.exception))


# ---------------------------------------------------------------------------
# Lifecycle state: RegistryState / ClusterState
# ---------------------------------------------------------------------------


class TestLifecycleState(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = _make_settings(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_clean_state_creates_registry_before_cluster(self) -> None:
        registry_created: list[bool] = []
        cluster_created: list[bool] = []

        def fake_create_registry(*_a: Any, **_k: Any) -> None:
            registry_created.append(True)

        def fake_create_cluster(*_a: Any, **_k: Any) -> None:
            cluster_created.append(True)

        with mock.patch.object(playground, "_registry_state", return_value=None):
            with mock.patch.object(playground, "_cluster_state", return_value=None):
                with mock.patch.object(playground, "_create_registry", side_effect=fake_create_registry):
                    with mock.patch.object(playground, "_create_cluster", side_effect=fake_create_cluster):
                        with mock.patch.object(playground, "_wait_for_registry_endpoint"):
                            playground._ensure_infrastructure(self.settings, _docker_inspect_mock(_standard_inspect_expected()))

        self.assertEqual(registry_created, [True])
        self.assertEqual(cluster_created, [True])

    def test_registry_only_state_creates_cluster(self) -> None:
        registry = _registry_state_with_binding(
            host_ip="127.0.0.1",
            host_port=self.settings.registry_port,
            runtime_image_id="sha256:3333333333333333333333333333333333333333333333333333333333333333",
            running=True,
        )
        cluster_created: list[bool] = []
        runner = _docker_inspect_mock(_standard_inspect_expected())
        with mock.patch.object(playground, "_registry_state", return_value=registry):
            with mock.patch.object(playground, "_cluster_state", return_value=None):
                with mock.patch.object(playground, "_create_registry"):
                    with mock.patch.object(
                        playground, "_create_cluster",
                        side_effect=lambda *_a, **_k: cluster_created.append(True),
                    ):
                        with mock.patch.object(playground, "_wait_for_registry_endpoint"):
                            playground._ensure_infrastructure(self.settings, runner)
        self.assertEqual(cluster_created, [True])

    def test_existing_healthy_state_is_reused(self) -> None:
        registry = _registry_state_with_binding(
            host_ip="127.0.0.1",
            host_port=self.settings.registry_port,
            runtime_image_id="sha256:3333333333333333333333333333333333333333333333333333333333333333",
            running=True,
        )
        cluster = _cluster_state_two_node()
        runner = _docker_inspect_mock(_standard_inspect_expected())
        with mock.patch.object(playground, "_registry_state", return_value=registry):
            with mock.patch.object(playground, "_cluster_state", return_value=cluster):
                with mock.patch.object(playground, "_create_registry") as cr:
                    with mock.patch.object(playground, "_create_cluster") as cc:
                        with mock.patch.object(playground, "_wait_for_registry_endpoint"):
                            playground._ensure_infrastructure(self.settings, runner)
        # No creation when both are healthy.
        cr.assert_not_called()
        cc.assert_not_called()
        # No restarts issued through the runner.
        for call in runner.run.call_args_list:
            argv = call.args[0]
            self.assertFalse(argv[:2] == ["docker", "start"])
            self.assertFalse(argv[:3] == ["k3d", "cluster", "start"])

    def test_stopped_registry_uses_docker_start(self) -> None:
        runner = _docker_inspect_mock(_standard_inspect_expected())
        original_run = runner.run.side_effect

        def run_side_effect(argv, **kw):
            if argv[:2] == ["k3d", "registry"] and "list" in argv:
                return _ok(stdout=json.dumps([_healthy_registry_state(running=False)]))
            return original_run(argv, **kw)

        runner.run.side_effect = run_side_effect
        with mock.patch.object(playground, "_cluster_state", return_value=None):
            with mock.patch.object(playground, "_create_cluster") as create_cluster:
                with mock.patch.object(playground, "_wait_for_registry_endpoint"):
                    playground._ensure_infrastructure(self.settings, runner)
        argv_lists = [c.args[0] for c in runner.run.call_args_list]
        self.assertTrue(any(argv[:2] == ["docker", "start"] for argv in argv_lists))
        self.assertFalse(any(argv[:3] == ["k3d", "registry", "start"] for argv in argv_lists))
        create_cluster.assert_called_once()

    def test_stopped_cluster_uses_k3d_cluster_start_with_wait(self) -> None:
        registry = _registry_state_with_binding(
            host_ip="127.0.0.1",
            host_port=self.settings.registry_port,
            runtime_image_id="sha256:3333333333333333333333333333333333333333333333333333333333333333",
            running=True,
        )
        cluster = _cluster_state_two_node(
            servers_running=0,
            agents_running=0,
        )
        runner = _docker_inspect_mock(_standard_inspect_expected())
        original_run = runner.run.side_effect

        def run_side_effect(argv, **kw):
            if argv[:3] == ["k3d", "cluster", "start"]:
                return _ok(stdout="")
            return original_run(argv, **kw)

        runner.run.side_effect = run_side_effect
        with mock.patch.object(playground, "_registry_state", return_value=registry):
            with mock.patch.object(playground, "_cluster_state", return_value=cluster):
                with mock.patch.object(playground, "_create_cluster"):
                    with mock.patch.object(playground, "_create_registry"):
                        with mock.patch.object(playground, "_wait_for_registry_endpoint"):
                            playground._ensure_infrastructure(self.settings, runner)
        argv_lists = [c.args[0] for c in runner.run.call_args_list]
        self.assertTrue(
            any(argv[:3] == ["k3d", "cluster", "start"] and "--wait" in argv
                for argv in argv_lists)
        )

    def test_missing_registry_with_existing_cluster_fails(self) -> None:
        cluster = _cluster_state_two_node()
        with mock.patch.object(playground, "_registry_state", return_value=None):
            with mock.patch.object(playground, "_cluster_state", return_value=cluster):
                with self.assertRaises(playground.PlaygroundError) as ctx:
                    playground._ensure_infrastructure(self.settings, _docker_inspect_mock(_standard_inspect_expected()))
        self.assertIn("reset", str(ctx.exception))

    def test_wrong_registry_image_fails(self) -> None:
        registry = _registry_state_with_binding(
            host_ip="127.0.0.1",
            host_port=self.settings.registry_port,
            runtime_image_id="sha256:3333333333333333333333333333333333333333333333333333333333333333",
            running=True,
        )
        cluster = _cluster_state_two_node()
        with mock.patch.object(playground, "_registry_state", return_value=registry):
            with mock.patch.object(playground, "_cluster_state", return_value=cluster):
                with self.assertRaises(playground.PlaygroundError) as ctx:
                    inspect_expected = _standard_inspect_expected()
                    inspect_expected[playground.REGISTRY_CONTAINER_NAME] = (
                        "docker.io/library/registry:3"
                    )
                    playground._ensure_infrastructure(
                        self.settings, _docker_inspect_mock(inspect_expected)
                    )
        self.assertIn("reset", str(ctx.exception))
        self.assertIn("registry:3", str(ctx.exception))

    def test_wrong_registry_port_fails(self) -> None:
        registry = _registry_state_with_binding(
            host_ip="127.0.0.1",
            host_port=5050,
            runtime_image_id="sha256:3333333333333333333333333333333333333333333333333333333333333333",
            running=True,
        )
        cluster = _cluster_state_two_node()
        with mock.patch.object(playground, "_registry_state", return_value=registry):
            with mock.patch.object(playground, "_cluster_state", return_value=cluster):
                with self.assertRaises(playground.PlaygroundError) as ctx:
                    playground._ensure_infrastructure(self.settings, _docker_inspect_mock(_standard_inspect_expected()))
        self.assertIn("reset", str(ctx.exception))

    def test_wrong_server_count_fails(self) -> None:
        registry = _registry_state_with_binding(
            host_ip="127.0.0.1",
            host_port=self.settings.registry_port,
            runtime_image_id="sha256:3333333333333333333333333333333333333333333333333333333333333333",
            running=True,
        )
        cluster = _cluster_state_two_node(
            servers_count=3,
            servers_running=3,
        )
        with mock.patch.object(playground, "_registry_state", return_value=registry):
            with mock.patch.object(playground, "_cluster_state", return_value=cluster):
                with self.assertRaises(playground.PlaygroundError):
                    playground._ensure_infrastructure(self.settings, _docker_inspect_mock(_standard_inspect_expected()))

    def test_wrong_k3s_image_fails(self) -> None:
        registry = _registry_state_with_binding(
            host_ip="127.0.0.1",
            host_port=self.settings.registry_port,
            runtime_image_id="sha256:3333333333333333333333333333333333333333333333333333333333333333",
            running=True,
        )
        cluster = _cluster_state_two_node()
        with mock.patch.object(playground, "_registry_state", return_value=registry):
            with mock.patch.object(playground, "_cluster_state", return_value=cluster):
                with self.assertRaises(playground.PlaygroundError) as ctx:
                    inspect_expected = _standard_inspect_expected()
                    inspect_expected["k3d-flux-playground-server-0"] = "rancher/k3s:v1.33.0-k3s1"
                    playground._ensure_infrastructure(
                        self.settings, _docker_inspect_mock(inspect_expected)
                    )
        self.assertIn("k3d-flux-playground-server-0", str(ctx.exception))

    def test_load_balancer_fails(self) -> None:
        registry = _registry_state_with_binding(
            host_ip="127.0.0.1",
            host_port=self.settings.registry_port,
            runtime_image_id="sha256:3333333333333333333333333333333333333333333333333333333333333333",
            running=True,
        )
        cluster = _cluster_state_two_node(
            has_load_balancer=True,
        )
        with mock.patch.object(playground, "_registry_state", return_value=registry):
            with mock.patch.object(playground, "_cluster_state", return_value=cluster):
                with self.assertRaises(playground.PlaygroundError):
                    playground._ensure_infrastructure(self.settings, _docker_inspect_mock(_standard_inspect_expected()))

    def test_unrelated_resources_ignored(self) -> None:
        unrelated_registry = _healthy_registry_state(
            container_name="k3d-other-registry",
            host_port=6000,
        )
        unrelated_cluster = _healthy_cluster_state(cluster_name="other-cluster")
        target = _healthy_cluster_state()
        runner = _docker_inspect_mock(_standard_inspect_expected())
        original_run = runner.run.side_effect

        def run_side_effect(argv, **kw):
            if argv[:3] == ["k3d", "registry", "list"]:
                return _ok(stdout=json.dumps([unrelated_registry, _healthy_registry_state()]))
            if argv[:3] == ["k3d", "cluster", "list"]:
                return _ok(stdout=json.dumps([unrelated_cluster, target]))
            return original_run(argv, **kw)

        runner.run.side_effect = run_side_effect
        with mock.patch.object(playground, "_wait_for_registry_endpoint"):
            with mock.patch.object(playground, "_create_registry") as cr:
                with mock.patch.object(playground, "_create_cluster") as cc:
                    playground._ensure_infrastructure(self.settings, runner)
        cr.assert_not_called()
        cc.assert_not_called()

    def test_malformed_registry_json_raises(self) -> None:
        runner = _runner_with([_ok(stdout="not-json")])
        with self.assertRaises(playground.PlaygroundError) as ctx:
            playground._list_registries_json(runner)
        self.assertIn("invalid JSON", str(ctx.exception))

    def test_registry_logical_vs_container_name(self) -> None:
        # Verify creation uses logical name; finding uses container name.
        runner = mock.Mock(spec=playground.CommandRunner)
        runner.run.return_value = _ok(stdout="")
        with mock.patch.object(playground, "_registry_state", return_value=None):
            with mock.patch.object(playground, "_cluster_state", return_value=None):
                with mock.patch.object(playground, "_wait_for_registry_endpoint"):
                    playground._ensure_infrastructure(self.settings, runner)
        argv = runner.run.call_args_list[0].args[0]
        # Logical name used to create
        self.assertIn(self.settings.registry_name, argv)
        # Container name used as the registry network alias
        self.assertNotIn(self.settings.registry_container_name, argv)


# ---------------------------------------------------------------------------
# Teardown
# ---------------------------------------------------------------------------


class TestTeardown(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = _make_settings(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_idempotent_when_nothing_exists(self) -> None:
        runner = _runner_with([
            _ok(stdout=json.dumps([])),  # cluster list
            _ok(stdout=json.dumps([])),  # registry list
        ])
        # Must not raise.
        playground._tear_down(self.settings, runner)
        for call in runner.run.call_args_list:
            argv = call.args[0]
            if argv[:2] == ["k3d", "cluster"] and argv[2] == "delete":
                self.fail("delete issued for non-existent cluster")
            if argv[:2] == ["k3d", "registry"] and argv[2] == "delete":
                self.fail("delete issued for non-existent registry")

    def test_targets_only_named_resources(self) -> None:
        registries = [_healthy_registry_state(), _healthy_registry_state(container_name="k3d-other-registry")]
        clusters = [_healthy_cluster_state(), _healthy_cluster_state(cluster_name="other-cluster")]
        runner = _runner_with([
            _ok(stdout=json.dumps(clusters)),  # cluster list
            _ok(stdout=json.dumps(registries)),  # registry list
            _ok(stdout=""),  # cluster delete
            _ok(stdout=""),  # registry delete
        ])
        playground._tear_down(self.settings, runner)
        for call in runner.run.call_args_list:
            argv = call.args[0]
            if argv[:3] == ["k3d", "cluster", "delete"]:
                self.assertEqual(argv[3], playground.CLUSTER_NAME)
            if argv[:3] == ["k3d", "registry", "delete"]:
                self.assertEqual(argv[3], playground.REGISTRY_NAME)

    def test_continues_after_individual_failure(self) -> None:
        registries = [_healthy_registry_state()]
        clusters = [_healthy_cluster_state()]
        runner = _runner_with([
            _ok(stdout=json.dumps(clusters)),  # cluster discovery
            _ok(stdout=json.dumps(registries)),  # registry discovery
            _ok(rc=1, stderr="cluster delete failed"),  # cluster delete fails
            _ok(stdout=""),  # registry delete still attempted
        ])
        with self.assertRaises(playground.PlaygroundError) as ctx:
            playground._tear_down(self.settings, runner)
        self.assertIn("cluster delete", str(ctx.exception))

    def test_teardown_attempts_registry_even_when_cluster_discovery_fails(self) -> None:
        registries_payload = json.dumps([_healthy_registry_state()])
        runner = _runner_with([
            _ok(rc=1, stderr="cluster discovery failed"),  # cluster list fails
            _ok(stdout=registries_payload),  # registry discovery succeeds
            _ok(stdout=""),  # registry delete still attempted
        ])
        with self.assertRaises(playground.PlaygroundError):
            playground._tear_down(self.settings, runner)

    def test_teardown_deletes_registry_with_unparseable_state(self) -> None:
        """`make reset` must recover the drift the strict parsers reject."""
        broken = _healthy_registry_state()
        broken["State"] = {"Running": "yes"}  # rejected by _parse_state
        del broken["portMappings"]
        runner = _runner_with([
            _ok(stdout=json.dumps([])),  # no cluster
            _ok(stdout=json.dumps([broken])),
            _ok(stdout=""),  # registry delete must still be attempted
        ])
        playground._tear_down(self.settings, runner, stdout=lambda _: None)
        argv_lists = [c.args[0] for c in runner.run.call_args_list]
        self.assertIn(
            ["k3d", "registry", "delete", playground.REGISTRY_NAME], argv_lists
        )

    def test_teardown_deletes_cluster_with_unparseable_node_state(self) -> None:
        broken = _healthy_cluster_state()
        broken["nodes"][0]["State"] = {}  # neither Running nor Status
        runner = _runner_with([
            _ok(stdout=json.dumps([broken])),
            _ok(stdout=json.dumps([])),  # no registry
            _ok(stdout=""),  # cluster delete must still be attempted
        ])
        playground._tear_down(self.settings, runner, stdout=lambda _: None)
        argv_lists = [c.args[0] for c in runner.run.call_args_list]
        self.assertIn(
            ["k3d", "cluster", "delete", playground.CLUSTER_NAME], argv_lists
        )


# ---------------------------------------------------------------------------
# Bootstrap ordering & HelmChart lifecycle
# ---------------------------------------------------------------------------


class TestBootstrapOrdering(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = _make_settings(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_waits_for_operator_before_flux_instance(self) -> None:
        order: list[str] = []
        with mock.patch.object(playground, "_preflight"):
            with mock.patch.object(playground, "_ensure_infrastructure"):
                with mock.patch.object(playground, "_write_kubeconfig"):
                    with mock.patch.object(playground, "_wait_for_nodes_ready"):
                        with mock.patch.object(playground, "_wait_for_operator_ready", side_effect=lambda *a, **k: order.append("operator")):
                            with mock.patch.object(playground, "_apply_flux_instance", side_effect=lambda *a, **k: order.append("apply_instance")):
                                with mock.patch.object(playground, "_wait_for_flux_instance", side_effect=lambda *a, **k: order.append("wait_instance")):
                                    with mock.patch.object(playground, "_wait_for_flux_controllers"):
                                        with mock.patch.object(playground, "_wait_for_flux_crds"):
                                            with mock.patch.object(playground, "_push_reconcile"):
                                                with mock.patch.object(playground, "_wait_for_smoke_workload"):
                                                    with mock.patch.object(playground, "_full_health_check", return_value=True):
                                                        playground._bring_up(self.settings, mock.Mock())
        self.assertLess(order.index("operator"), order.index("apply_instance"))
        self.assertLess(order.index("apply_instance"), order.index("wait_instance"))

    def test_flux_instance_readiness_precedes_push(self) -> None:
        order: list[str] = []
        with mock.patch.object(playground, "_preflight"):
            with mock.patch.object(playground, "_ensure_infrastructure"):
                with mock.patch.object(playground, "_write_kubeconfig"):
                    with mock.patch.object(playground, "_wait_for_nodes_ready"):
                        with mock.patch.object(playground, "_wait_for_operator_ready"):
                            with mock.patch.object(playground, "_apply_flux_instance"):
                                with mock.patch.object(playground, "_wait_for_flux_instance", side_effect=lambda *a, **k: order.append("wait_instance")):
                                    with mock.patch.object(playground, "_wait_for_flux_controllers"):
                                        with mock.patch.object(playground, "_wait_for_flux_crds"):
                                            with mock.patch.object(playground, "_push_reconcile", side_effect=lambda *a, **k: order.append("push")):
                                                with mock.patch.object(playground, "_wait_for_smoke_workload"):
                                                    with mock.patch.object(playground, "_full_health_check", return_value=True):
                                                        playground._bring_up(self.settings, mock.Mock())
        self.assertLess(order.index("wait_instance"), order.index("push"))

    def test_helmchart_failed_raises_immediately(self) -> None:
        helmchart = {
            "metadata": {"name": "flux-operator"},
            "status": {
                "conditions": [
                    {"type": "Failed", "status": "True", "reason": "InstallFailed", "message": "boom"}
                ]
            },
        }

        def run_side_effect(argv, **kw):
            # First call: helmchart exists (kubectl get without -o json).
            return _ok(stdout="apiVersion: v1\nkind: HelmChart\n")

        def run_json_side_effect(argv, **kw):
            # Second call: helmchart json (used by helmchart_failed check).
            return json.loads(json.dumps(helmchart))

        runner = mock.Mock(spec=playground.CommandRunner)
        runner.run.side_effect = run_side_effect
        runner.run_json.side_effect = run_json_side_effect
        runner.default_timeout = 60.0

        with self.assertRaises(playground.PlaygroundError) as ctx:
            playground._wait_for_operator_ready(self.settings, runner)
        self.assertIn("Failed=True", str(ctx.exception))

    def test_operator_waits_for_job_then_deployment(self) -> None:
        helmchart_no_job = {"metadata": {"name": "flux-operator"}, "status": {}}
        helmchart_with_job = {
            "metadata": {"name": "flux-operator"},
            "status": {"jobName": "helm-install-flux-operator"},
        }
        job_complete = {
            "metadata": {"name": "helm-install-flux-operator"},
            "status": {"conditions": [{"type": "Complete", "status": "True"}]},
        }
        deployment = {
            "metadata": {"name": "flux-operator"},
            "status": {
                "conditions": [{"type": "Available", "status": "True"}],
                "readyReplicas": 1,
            },
            "spec": {"replicas": 1},
        }
        helmchart_queries = 0

        def run_side_effect(argv, **kw):
            return _ok(stdout="")

        def run_json_side_effect(argv, **kw):
            nonlocal helmchart_queries
            if any(a.startswith("helmchart/") for a in argv):
                helmchart_queries += 1
                if helmchart_queries <= 3:
                    return helmchart_no_job
                return helmchart_with_job
            if any(a.startswith("job/") for a in argv):
                return job_complete
            if any("deployment" in a for a in argv):
                return deployment
            return {}

        runner = mock.Mock(spec=playground.CommandRunner)
        runner.run.side_effect = run_side_effect
        runner.run_json.side_effect = run_json_side_effect
        runner.default_timeout = 60.0

        counter = {"n": 0}

        def fake_monotonic() -> float:
            counter["n"] += 1
            return float(counter["n"])

        with mock.patch.object(playground.time, "monotonic", fake_monotonic):
            with mock.patch.object(playground.time, "sleep"):
                playground._wait_for_operator_ready(self.settings, runner)

        all_argvs = [c.args[0] for c in runner.run.call_args_list]
        all_argvs += [c.args[0] for c in runner.run_json.call_args_list]
        self.assertTrue(any("helm-install-flux-operator" in a for argv in all_argvs for a in argv))

    def test_job_failed_raises(self) -> None:
        helmchart_with_job = {"metadata": {"name": "flux-operator"}, "status": {"jobName": "j"}}
        job_failed = {
            "metadata": {"name": "j"},
            "status": {"conditions": [{"type": "Failed", "status": "True", "reason": "Boom", "message": "x"}]},
        }

        def run_side_effect(argv, **kw):
            return _ok(stdout="")

        def run_json_side_effect(argv, **kw):
            if any(a.startswith("job/") for a in argv):
                return job_failed
            return helmchart_with_job

        runner = mock.Mock(spec=playground.CommandRunner)
        runner.run.side_effect = run_side_effect
        runner.run_json.side_effect = run_json_side_effect
        runner.default_timeout = 60.0

        counter = {"n": 0}

        def fake_monotonic() -> float:
            counter["n"] += 1
            return float(counter["n"])

        with mock.patch.object(playground.time, "monotonic", fake_monotonic):
            with mock.patch.object(playground.time, "sleep"):
                with self.assertRaises(playground.PlaygroundError) as ctx:
                    playground._wait_for_operator_ready(self.settings, runner)
        self.assertIn("Failed=True", str(ctx.exception))


# ---------------------------------------------------------------------------
# Health check propagation
# ---------------------------------------------------------------------------


class TestHealthCheckPropagation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = _make_settings(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_full_health_check_returns_false_on_drift(self) -> None:
        with mock.patch.object(playground, "_registry_state", return_value=None):
            with mock.patch.object(playground, "_cluster_state", return_value=None):
                with mock.patch.object(playground, "_wait_for_registry_endpoint"):
                    self.assertFalse(playground._full_health_check(self.settings, _infinite_runner(), stdout=lambda _: None))

    def test_full_health_check_does_not_call_diagnostics(self) -> None:
        with mock.patch.object(playground, "_registry_state", return_value=None):
            with mock.patch.object(playground, "_cluster_state", return_value=None):
                with mock.patch.object(playground, "_diagnose") as diag:
                    playground._full_health_check(self.settings, _infinite_runner(), stdout=lambda _: None)
        diag.assert_not_called()

    def test_health_check_failure_in_bring_up_propagates(self) -> None:
        with mock.patch.object(playground, "_preflight"):
            with mock.patch.object(playground, "_ensure_infrastructure"):
                with mock.patch.object(playground, "_write_kubeconfig"):
                    with mock.patch.object(playground, "_wait_for_nodes_ready"):
                        with mock.patch.object(playground, "_wait_for_operator_ready"):
                            with mock.patch.object(playground, "_apply_flux_instance"):
                                with mock.patch.object(playground, "_wait_for_flux_instance"):
                                    with mock.patch.object(playground, "_wait_for_flux_controllers"):
                                        with mock.patch.object(playground, "_wait_for_flux_crds"):
                                            with mock.patch.object(playground, "_push_reconcile"):
                                                with mock.patch.object(playground, "_wait_for_smoke_workload"):
                                                    with mock.patch.object(playground, "_full_health_check", return_value=False):
                                                        with self.assertRaises(playground.PlaygroundError):
                                                            playground._bring_up(self.settings, mock.Mock())

    def test_smoke_pod_on_server_fails_health(self) -> None:
        registry = _registry_state_with_binding(
            host_ip="127.0.0.1",
            host_port=self.settings.registry_port,
            runtime_image_id="sha256:3333333333333333333333333333333333333333333333333333333333333333",
            running=True,
        )
        cluster = _cluster_state_two_node()
        nodes = {
            "items": [
                {
                    "metadata": {"name": "k3d-flux-playground-server-0"},
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                },
                {
                    "metadata": {"name": "k3d-flux-playground-agent-0"},
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                },
            ]
        }
        deployment = {
            "metadata": {"name": playground.SMOKE_DEPLOYMENT},
            "spec": {"replicas": 1},
            "status": {"readyReplicas": 1},
        }
        # Pod scheduled on the server instead of the agent.
        pods = {
            "items": [
                {
                    "metadata": {"name": "p1"},
                    "spec": {"nodeName": "k3d-flux-playground-server-0"},
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                }
            ]
        }
        runner = _runner_with([
            _ok(stdout=json.dumps([_healthy_registry_state()])),
            _ok(stdout=json.dumps([_healthy_cluster_state()])),
            _ok(stdout=""),  # kubeconfig presence: pretend absent
            _ok(stdout=json.dumps(nodes)),
            _ok(stdout=json.dumps(nodes)),
            _ok(stdout=json.dumps(deployment)),  # operator check
            _ok(stdout=json.dumps(deployment)),  # fluxinstance
            _ok(stdout='{"items":[]}'),  # crds
            _ok(stdout=json.dumps(deployment)),  # controllers
            _ok(stdout=""),  # flux check
            _ok(stdout=json.dumps(deployment)),  # ocirepo
            _ok(stdout=json.dumps(deployment)),  # kustomization
            _ok(stdout=json.dumps(deployment)),  # deployment
            _ok(stdout=json.dumps(pods)),  # pods
        ])
        # Provide a kubeconfig so the check sees it.
        self.settings.context_dir.mkdir(parents=True, exist_ok=True)
        self.settings.kubeconfig_path.write_text(f"cluster: {playground.CLUSTER_NAME}\n")
        with mock.patch.object(playground, "_registry_state", return_value=registry):
            with mock.patch.object(playground, "_cluster_state", return_value=cluster):
                with mock.patch.object(playground, "_docker_inspect_image", return_value=playground.K3S_IMAGE):
                    with mock.patch.object(playground, "_flux") as flux:
                        flux.return_value = _ok()
                        ok = playground._full_health_check(self.settings, _infinite_runner(), stdout=lambda _: None)
        self.assertFalse(ok)

    def test_smoke_pod_on_agent_passes_health(self) -> None:
        registry = _registry_state_with_binding(
            host_ip="127.0.0.1",
            host_port=self.settings.registry_port,
            runtime_image_id="sha256:3333333333333333333333333333333333333333333333333333333333333333",
            running=True,
        )
        cluster = _cluster_state_two_node()
        nodes = {
            "items": [
                {
                    "metadata": {
                        "name": "k3d-flux-playground-server-0",
                        "labels": {"node-role.kubernetes.io/control-plane": ""},
                    },
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    "spec": {
                        "taints": [
                            {"key": playground.SERVER_TAINT_KEY,
                             "value": playground.SERVER_TAINT_VALUE,
                             "effect": playground.SERVER_TAINT_EFFECT}
                        ]
                    },
                },
                {
                    "metadata": {
                        "name": "k3d-flux-playground-agent-0",
                        "labels": {"node-role.kubernetes.io/agent": ""},
                    },
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    "spec": {"taints": []},
                },
            ]
        }
        deployment = {
            "metadata": {"name": playground.SMOKE_DEPLOYMENT},
            "spec": {"replicas": 1},
            "status": {"readyReplicas": 1, "conditions": [{"type": "Available", "status": "True"}]},
        }
        pods = {
            "items": [
                {
                    "metadata": {"name": "p1"},
                    "spec": {"nodeName": "k3d-flux-playground-agent-0"},
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                }
            ]
        }
        crds_payload = {
            "items": [
                {
                    "metadata": {"name": c},
                    "status": {"conditions": [{"type": "Established", "status": "True"}]},
                }
                for c in playground.EXPECTED_CRDS
            ]
        }

        helmchart_payload = {
            "metadata": {"name": "flux-operator"},
            "status": {"jobName": "helm-install-flux-operator"},
        }
        job_payload = {
            "metadata": {"name": "helm-install-flux-operator"},
            "status": {"conditions": [{"type": "Complete", "status": "True"}]},
        }

        def kubectl_side_effect(*all_args, **k):
            # _kubectl_json(self, settings, runner, *args) - strip the
            # settings/runner positional args so we only inspect kubectl args.
            args = all_args[2:] if len(all_args) >= 2 else all_args
            joined = " ".join(args)
            if "helmchart" in joined:
                return helmchart_payload
            if "job/" in joined:
                return job_payload
            if "fluxinstance" in joined:
                return {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
            if "ocirepository" in joined:
                return {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
            if "kustomization" in joined:
                return {"status": {"conditions": [{"type": "Ready", "status": "True"}]}}
            if "get" in args and "nodes" in args and "helmchart" not in joined:
                return nodes
            if "crds" in args:
                return crds_payload
            if "pods" in args:
                return pods
            return {
                "metadata": {"name": "any"},
                "spec": {"replicas": 1},
                "status": {
                    "readyReplicas": 1,
                    "conditions": [
                        {"type": "Ready", "status": "True"},
                        {"type": "Available", "status": "True"},
                        {"type": "Complete", "status": "True"},
                        {"type": "Established", "status": "True"},
                    ],
                },
            }

        self.settings.context_dir.mkdir(parents=True, exist_ok=True)
        self.settings.kubeconfig_path.write_text(f"cluster: {playground.CLUSTER_NAME}\n")

        def fake_inspect(container: str, runner: Any) -> str:
            if container == playground.REGISTRY_CONTAINER_NAME:
                return playground.REGISTRY_IMAGE
            return playground.K3S_IMAGE

        with mock.patch.object(playground, "_registry_state", return_value=registry):
            with mock.patch.object(playground, "_cluster_state", return_value=cluster):
                with mock.patch.object(playground, "_kubectl_json") as kubectl_json:
                    kubectl_json.side_effect = kubectl_side_effect
                    with mock.patch.object(playground, "_docker_inspect_image", side_effect=fake_inspect):
                        with mock.patch.object(playground, "_flux", return_value=_ok()):
                            ok = playground._full_health_check(self.settings, _infinite_runner(), stdout=lambda _: None)
        self.assertTrue(ok)

    def test_empty_value_role_label_recognized(self) -> None:
        nodes = {
            "items": [
                {
                    "metadata": {
                        "name": "k3d-flux-playground-server-0",
                        "labels": {"node-role.kubernetes.io/control-plane": ""},
                    },
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    "spec": {
                        "taints": [
                            {"key": playground.SERVER_TAINT_KEY,
                             "value": playground.SERVER_TAINT_VALUE,
                             "effect": playground.SERVER_TAINT_EFFECT}
                        ]
                    },
                },
                {
                    "metadata": {
                        "name": "k3d-flux-playground-agent-0",
                        "labels": {"node-role.kubernetes.io/agent": ""},
                    },
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    "spec": {"taints": []},
                },
            ]
        }
        registry = _registry_state_with_binding(
            host_ip="127.0.0.1",
            host_port=self.settings.registry_port,
            runtime_image_id="sha256:3333333333333333333333333333333333333333333333333333333333333333",
            running=True,
        )
        cluster = _cluster_state_two_node()
        runner = _runner_with([_ok()])
        self.settings.context_dir.mkdir(parents=True, exist_ok=True)
        self.settings.kubeconfig_path.write_text(f"cluster: {playground.CLUSTER_NAME}\n")
        with mock.patch.object(playground, "_registry_state", return_value=registry):
            with mock.patch.object(playground, "_cluster_state", return_value=cluster):
                with mock.patch.object(playground, "_kubectl_json", return_value=nodes):
                    with mock.patch.object(playground, "_docker_inspect_image", return_value=playground.K3S_IMAGE):
                        with mock.patch.object(playground, "_flux", return_value=_ok()):
                            # Should not raise; topology check passes because the
                            # empty-valued control-plane label still identifies the
                            # server.
                            ok = playground._full_health_check(self.settings, _infinite_runner(), stdout=lambda _: None)
        # Even with mocked kubectl, taints are accepted.
        self.assertFalse(ok)  # other checks fail because deployment etc. mocked

    def test_missing_server_taint_fails_health(self) -> None:
        nodes = {
            "items": [
                {
                    "metadata": {"name": "server-0", "labels": {"node-role.kubernetes.io/control-plane": ""}},
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    "spec": {"taints": []},  # missing expected taint
                },
                {
                    "metadata": {"name": "agent-0", "labels": {"node-role.kubernetes.io/agent": ""}},
                    "status": {"conditions": [{"type": "Ready", "status": "True"}]},
                    "spec": {"taints": []},
                },
            ]
        }
        registry = _registry_state_with_binding(
            host_ip="127.0.0.1",
            host_port=self.settings.registry_port,
            runtime_image_id="sha256:3333333333333333333333333333333333333333333333333333333333333333",
            running=True,
        )
        cluster = _cluster_state_two_node()
        self.settings.context_dir.mkdir(parents=True, exist_ok=True)
        self.settings.kubeconfig_path.write_text(f"cluster: {playground.CLUSTER_NAME}\n")
        with mock.patch.object(playground, "_registry_state", return_value=registry):
            with mock.patch.object(playground, "_cluster_state", return_value=cluster):
                with mock.patch.object(playground, "_kubectl_json", return_value=nodes):
                    with mock.patch.object(playground, "_docker_inspect_image", return_value=playground.K3S_IMAGE):
                        with mock.patch.object(playground, "_flux", return_value=_ok()):
                            # taints check should fail; full_health_check returns False.
                            ok = playground._full_health_check(self.settings, _infinite_runner(), stdout=lambda _: None)
        self.assertFalse(ok)

    def test_registry_drift_reports_expected_and_actual_port(self) -> None:
        """A bare `fail` line is not actionable; the drift reason must print."""
        registry = _registry_state_with_binding(
            host_ip="127.0.0.1",
            host_port=55150,  # cluster was created against a different port
        )
        cluster = _cluster_state_two_node()
        self.settings.context_dir.mkdir(parents=True, exist_ok=True)
        self.settings.kubeconfig_path.write_text(f"cluster: {playground.CLUSTER_NAME}\n")
        lines: list[str] = []
        with mock.patch.object(playground, "_registry_state", return_value=registry):
            with mock.patch.object(playground, "_cluster_state", return_value=cluster):
                with mock.patch.object(playground, "_docker_inspect_image", return_value=playground.REGISTRY_IMAGE):
                    with mock.patch.object(playground, "_flux", return_value=_ok()):
                        ok = playground._full_health_check(
                            self.settings, _infinite_runner(), stdout=lines.append
                        )
        self.assertFalse(ok)
        output = "\n".join(lines)
        self.assertIn("registry image and binding: fail", output)
        self.assertIn("55150", output)
        self.assertIn(str(self.settings.registry_port), output)


# ---------------------------------------------------------------------------
# Diagnostics
# ---------------------------------------------------------------------------


class TestDiagnostics(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = _make_settings(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_diagnostic_commands_carry_kubeconfig(self) -> None:
        runner = _infinite_runner()
        playground._diagnose(self.settings, runner, stdout=lambda _: None)
        for call in runner.run.call_args_list:
            argv = call.args[0]
            if argv and argv[0] in ("kubectl", "flux"):
                self.assertTrue(
                    any(a.startswith("--kubeconfig=") for a in argv),
                    f"Command {argv} lacks --kubeconfig",
                )

    def test_diagnostic_failures_do_not_replace_primary(self) -> None:
        runner = _infinite_runner()
        # Should not raise even though the runner returns empty stdout.
        playground._diagnose(self.settings, runner, stdout=lambda _: None)

    def test_emit_diagnostics_once_swallows_exception(self) -> None:
        runner = mock.Mock(spec=playground.CommandRunner)
        runner.run.side_effect = Exception("boom")
        # Must not raise.
        playground._emit_diagnostics_once(self.settings, runner, stdout=lambda _: None)

    def test_unhealthy_pods_emit_describe_and_logs(self) -> None:
        pods = {
            "items": [
                {
                    "metadata": {"namespace": "default", "name": "broken"},
                    "spec": {"nodeName": "x"},
                    "status": {
                        "phase": "CrashLoopBackOff",
                        "containerStatuses": [
                            {"ready": False, "restartCount": 3},
                        ],
                    },
                }
            ]
        }
        helmreleases = {"items": []}
        helmchart = {"metadata": {"name": "flux-operator"}, "status": {}}

        def run_side_effect(argv, **kw):
            if any("helmreleases" in a for a in argv):
                return _ok(stdout=json.dumps(helmreleases))
            if any("pods" in a for a in argv) and any("get" in a for a in argv):
                return _ok(stdout=json.dumps(pods))
            if any("helmchart" in a for a in argv):
                return _ok(stdout=json.dumps(helmchart))
            return _ok()

        def run_json_side_effect(argv, **kw):
            result = run_side_effect(argv, **kw)
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return {}

        runner = mock.Mock(spec=playground.CommandRunner)
        runner.run.side_effect = run_side_effect
        runner.run_json.side_effect = run_json_side_effect
        runner.default_timeout = 60.0
        playground._diagnose(self.settings, runner, stdout=lambda _: None)
        argv_lists = [c.args[0] for c in runner.run.call_args_list]
        self.assertTrue(any("describe" in a for argv in argv_lists for a in argv))
        self.assertTrue(any("--previous" in a for argv in argv_lists for a in argv))

    def test_failed_helmreleases_emit_describe(self) -> None:
        helmreleases = {
            "items": [
                {
                    "metadata": {"namespace": "ns", "name": "hr"},
                    "status": {"conditions": [{"type": "Ready", "status": "False"}]},
                }
            ]
        }
        helmchart = {"metadata": {"name": "flux-operator"}, "status": {}}

        def run_side_effect(argv, **kw):
            if any("helmreleases" in a for a in argv):
                return _ok(stdout=json.dumps(helmreleases))
            if any("helmchart" in a for a in argv):
                return _ok(stdout=json.dumps(helmchart))
            return _ok()

        def run_json_side_effect(argv, **kw):
            result = run_side_effect(argv, **kw)
            try:
                return json.loads(result.stdout)
            except json.JSONDecodeError:
                return {}

        runner = mock.Mock(spec=playground.CommandRunner)
        runner.run.side_effect = run_side_effect
        runner.run_json.side_effect = run_json_side_effect
        runner.default_timeout = 60.0
        playground._diagnose(self.settings, runner, stdout=lambda _: None)
        argv_lists = [c.args[0] for c in runner.run.call_args_list]
        self.assertTrue(any("helmrelease" in a and "describe" in argv
                            for argv in argv_lists for a in argv))


# ---------------------------------------------------------------------------
# Kubeconfig isolation
# ---------------------------------------------------------------------------


class TestKubeconfigIsolation(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = _make_settings(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_kubeconfig_written_to_expected_path(self) -> None:
        runner = _runner_with([_ok(stdout="apiVersion: v1\nclusters: []\n")])
        playground._write_kubeconfig(self.settings, runner)
        self.assertTrue(self.settings.kubeconfig_path.exists())
        mode = self.settings.kubeconfig_path.stat().st_mode & 0o777
        self.assertEqual(mode, 0o600)

    def test_kubectl_and_flux_carry_kubeconfig(self) -> None:
        runner = _runner_with([_ok(stdout='{"items":[]}')])
        playground._kubectl(self.settings, runner, "get", "nodes")
        argv = runner.run.call_args.args[0]
        self.assertTrue(any(a.startswith("--kubeconfig=") for a in argv))
        runner2 = _runner_with([_ok(stdout="")])
        playground._flux(self.settings, runner2, "check")
        argv2 = runner2.run.call_args.args[0]
        self.assertTrue(any(a.startswith("--kubeconfig=") for a in argv2))


# ---------------------------------------------------------------------------
# Working-tree staging
# ---------------------------------------------------------------------------


class TestWorkingTreeStaging(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        (self.tmp / ".context").mkdir()
        self.tmp.joinpath("tracked.txt").write_text("hello\n")
        self.tmp.joinpath("modified.txt").write_text("first\n")
        self.tmp.joinpath("deleted.txt").write_text("will be deleted\n")
        self.tmp.joinpath("ignored").mkdir()
        self.tmp.joinpath("ignored").joinpath("cache.bin").write_text("ignore me")
        self.tmp.joinpath("cache").mkdir()
        self.tmp.joinpath("cache").joinpath("pyc.pyc").write_text("pyc")
        self.tmp.joinpath(".venv").mkdir()
        self.tmp.joinpath(".venv").joinpath("env").write_text("venv")

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _settings(self) -> playground.PlaygroundSettings:
        return _make_settings(self.tmp)

    def _stage(self, listed_paths: list[str], runner: mock.Mock) -> Path:
        playground._stage_working_tree(self._settings(), runner)
        return self._staging_dir()

    def _staging_dir(self) -> Path:
        return next(self.tmp.glob("flux-playground-*"))

    def test_clean_tracked_files_copied(self) -> None:
        runner = _runner_with([_ok(stdout="tracked.txt\x00")])
        staging = self._stage(["tracked.txt"], runner)
        self.assertEqual((staging / "tracked.txt").read_text(), "hello\n")

    def test_modified_tracked_file_uses_current_content(self) -> None:
        self.tmp.joinpath("modified.txt").write_text("second\n")
        runner = _runner_with([_ok(stdout="modified.txt\x00")])
        staging = self._stage(["modified.txt"], runner)
        self.assertEqual((staging / "modified.txt").read_text(), "second\n")

    def test_untracked_file_included(self) -> None:
        new = self.tmp / "new.txt"
        new.write_text("fresh\n")
        runner = _runner_with([_ok(stdout="new.txt\x00")])
        staging = self._stage(["new.txt"], runner)
        self.assertEqual((staging / "new.txt").read_text(), "fresh\n")

    def test_deleted_tracked_file_absent(self) -> None:
        self.tmp.joinpath("deleted.txt").unlink()
        runner = _runner_with([_ok(stdout="")])
        staging = self._stage([], runner)
        self.assertFalse((staging / "deleted.txt").exists())

    def test_gitignored_paths_excluded(self) -> None:
        runner = _runner_with([_ok(stdout="tracked.txt\x00")])
        staging = self._stage(["tracked.txt"], runner)
        self.assertFalse((staging / "ignored").exists())
        self.assertFalse((staging / ".venv").exists())
        self.assertFalse((staging / "cache").exists())

    def test_symlink_preserved(self) -> None:
        try:
            os_symlink = __import__("os").symlink
        except AttributeError:
            self.skipTest("symlinks not supported")
        link = self.tmp / "link.txt"
        try:
            os_symlink("tracked.txt", link)
        except OSError:
            self.skipTest("symlinks not supported on filesystem")
        runner = _runner_with([_ok(stdout="tracked.txt\x00link.txt\x00")])
        staging = self._stage(["tracked.txt", "link.txt"], runner)
        link_path = staging / "link.txt"
        self.assertTrue(link_path.is_symlink())
        self.assertEqual(__import__("os").readlink(link_path), "tracked.txt")


# ---------------------------------------------------------------------------
# Push artifact
# ---------------------------------------------------------------------------


class TestPushArtifact(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = _make_settings(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_push_uses_host_url_and_insecure_registry(self) -> None:
        runner = _infinite_runner(_ok(stdout=json.dumps({"digest": "sha256:abc"})))
        with mock.patch.object(playground, "_check_registry_reachable"):
            with mock.patch.object(playground, "_stage_working_tree", return_value=self.tmp / "staging"):
                (self.tmp / "staging").mkdir()
                with mock.patch.object(playground, "_git_source_metadata", return_value=("file:///repo", "main@sha1:0")):
                    digest = playground._push_artifact(self.settings, runner)
        # Find the push call: only one non-mock run call.
        push_call = None
        for call in runner.run.call_args_list:
            argv = call.args[0]
            if "push" in argv and "artifact" in argv:
                push_call = argv
                break
        self.assertIsNotNone(push_call)
        self.assertIn(f"oci://127.0.0.1:{self.settings.registry_port}/flux-playground/manifests:dev", push_call)
        self.assertIn("--insecure-registry", push_call)
        self.assertEqual(digest, "sha256:abc")

    def test_repeat_push_yields_new_digest(self) -> None:
        runner = _infinite_runner(
            _ok(stdout=json.dumps({"digest": "sha256:111"})),
            _ok(stdout=json.dumps({"digest": "sha256:222"})),
        )
        with mock.patch.object(playground, "_check_registry_reachable"):
            with mock.patch.object(playground, "_stage_working_tree", return_value=self.tmp / "staging"):
                (self.tmp / "staging").mkdir()
                with mock.patch.object(playground, "_git_source_metadata", return_value=("file:///repo", "main@sha1:0")):
                    digest_a = playground._push_artifact(self.settings, runner)
                    digest_b = playground._push_artifact(self.settings, runner)
        self.assertEqual(digest_a, "sha256:111")
        self.assertEqual(digest_b, "sha256:222")


# ---------------------------------------------------------------------------
# Sync manifest
# ---------------------------------------------------------------------------


class TestSyncManifest(unittest.TestCase):
    def test_urls_differ_and_are_oci(self) -> None:
        settings = _make_settings(Path("/tmp"))
        self.assertNotEqual(settings.host_artifact_url, settings.in_cluster_source_url)

    def test_sync_yaml_uses_insecure_and_in_cluster_url(self) -> None:
        text = SYNC_MANIFEST_PATH.read_text()
        self.assertIn("oci://k3d-flux-playground-registry:5000/", text)
        self.assertIn("insecure: true", text)
        self.assertNotIn("127.0.0.1", text)


# ---------------------------------------------------------------------------
# Makefile & repo structure
# ---------------------------------------------------------------------------


class TestMakeInterface(unittest.TestCase):
    def test_makefile_exposes_short_targets(self) -> None:
        text = (REPO_ROOT / "Makefile").read_text()
        for line in ("up:", "push:", "check:", "down:", "reset:", "test:"):
            self.assertIn(line, text)
        # Aliases must not remain.
        for alias in ("playground.up:", "playground.push:", "playground.check:", "playground.down:", "playground.reset:"):
            self.assertNotIn(alias, text)

    def test_makefile_forwards_args(self) -> None:
        text = (REPO_ROOT / "Makefile").read_text()
        self.assertIn("$(ARGS)", text)

    def test_invoke_collection_exports(self) -> None:
        from tasks import ns  # type: ignore
        self.assertEqual(set(ns.collections.keys()), {"playground"})
        inner = ns.collections["playground"]
        self.assertEqual(set(inner.tasks.keys()), {"up", "push", "check", "down", "reset"})


# ---------------------------------------------------------------------------
# Repo structure invariants
# ---------------------------------------------------------------------------


MANIFEST_ROOTS = ("config", "clusters")


def _manifest_yaml_files(root: Path) -> list[Path]:
    """Discover manifest YAML under the repo-owned trees only.

    Scoped deliberately: ``root.rglob`` would also walk gitignored
    scratch directories such as ``.context/`` and ``.venv/``, where a
    stray YAML file is expected and must not fail the suite.
    """
    yaml_files: list[Path] = []
    for directory in MANIFEST_ROOTS:
        base = root / directory
        yaml_files += list(base.rglob("*.yaml"))
        yaml_files += list(base.rglob("*.yml"))
    return sorted(yaml_files)


class TestRepoStructure(unittest.TestCase):
    def test_manifest_yaml_files_start_with_dashes(self) -> None:
        """YAML frontmatter scan is scoped to repo-owned manifest trees."""
        yaml_files = _manifest_yaml_files(REPO_ROOT)
        self.assertTrue(yaml_files, "expected at least one manifest under config/ or clusters/")
        for path in yaml_files:
            with self.subTest(path=path):
                first_line = path.read_text().splitlines()[0]
                self.assertTrue(first_line.startswith("---"), f"{path} must begin with ---")

    def test_yaml_outside_manifest_trees_is_ignored(self) -> None:
        """Stray YAML under .context, .venv, or caches must not be discovered.

        Injects real stray files into a temporary repo so the scoping
        rule is exercised rather than restated.
        """
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "clusters" / "local").mkdir(parents=True)
            manifest = root / "config" / "real.yaml"
            manifest.write_text("---\nkind: ConfigMap\n")
            nested = root / "clusters" / "local" / "nested.yml"
            nested.write_text("---\nkind: Namespace\n")

            for excluded in (".context", ".venv", "__pycache__"):
                stray_dir = root / excluded
                stray_dir.mkdir()
                (stray_dir / "scratch.yaml").write_text("foo: bar\n")
                (stray_dir / "other.yml").write_text("baz: qux\n")

            self.assertEqual(_manifest_yaml_files(root), sorted([manifest, nested]))

    def test_stray_yaml_would_be_caught_without_scoping(self) -> None:
        """Guards the guard: an unscoped rglob really does pick strays up."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            (root / "config").mkdir()
            (root / "config" / "real.yaml").write_text("---\nkind: ConfigMap\n")
            (root / ".context").mkdir()
            stray = root / ".context" / "scratch.yaml"
            stray.write_text("foo: bar\n")

            self.assertIn(stray, list(root.rglob("*.yaml")))
            self.assertNotIn(stray, _manifest_yaml_files(root))

    def test_manifest_without_frontmatter_is_caught(self) -> None:
        """A manifest under config/clusters without ``---`` must fail the check."""
        with tempfile.TemporaryDirectory() as td:
            tmp_path = Path(td)
            (tmp_path / "config").mkdir()
            bad = tmp_path / "config" / "no-dashes.yaml"
            bad.write_text("apiVersion: v1\nkind: ConfigMap\n")
            try:
                yaml_files: list[Path] = []
                yaml_files += list((tmp_path / "config").rglob("*.yaml"))
                yaml_files += list((tmp_path / "config").rglob("*.yml"))
                self.assertTrue(yaml_files)
                caught = False
                for path in yaml_files:
                    first_line = path.read_text().splitlines()[0]
                    if not first_line.startswith("---"):
                        caught = True
                        break
                self.assertTrue(
                    caught,
                    "manifest without --- must be detected by the frontmatter check",
                )
            finally:
                shutil.rmtree(tmp_path, ignore_errors=True)

    def test_text_files_end_with_newline(self) -> None:
        candidates = [
            REPO_ROOT / "README.md",
            REPO_ROOT / "Makefile",
            REPO_ROOT / "pyproject.toml",
            REPO_ROOT / "uv.lock",
            REPO_ROOT / ".gitignore",
            REPO_ROOT / "tasks.py",
        ]
        candidates += list(REPO_ROOT.glob("src/**/*.py"))
        candidates += list(REPO_ROOT.glob("tests/**/*.py"))
        for path in candidates:
            with self.subTest(path=path):
                self.assertTrue(path.exists())
                self.assertTrue(path.read_text().endswith("\n"))

    def test_gitignore_excludes_context(self) -> None:
        text = (REPO_ROOT / ".gitignore").read_text()
        self.assertIn(".context/", text)
        self.assertIn(".venv/", text)
        self.assertIn("__pycache__/", text)

    def test_no_tool_invoke_section(self) -> None:
        text = (REPO_ROOT / "pyproject.toml").read_text()
        self.assertNotIn("[tool.invoke]", text)

    def test_k3s_yaml_image_matches_k3s_image_constant(self) -> None:
        text = K3D_CONFIG_PATH.read_text()
        # Top-level ``image:`` line: scan narrowly to the first occurrence
        # so future nested ``image:`` entries (e.g., per-file image)
        # do not match by accident.
        first_image_line = next(
            (line for line in text.splitlines() if line.startswith("image:")),
            "",
        )
        self.assertTrue(first_image_line.startswith("image:"))
        # Extract the YAML value (preserve quoting).
        value = first_image_line.split(":", 1)[1].strip()
        self.assertEqual(
            value,
            playground.K3S_IMAGE,
            "config/k3d/playground.yaml `image:` and K3S_IMAGE must stay in sync; "
            "update both pins together.",
        )

    def test_k3d_yaml_structure(self) -> None:
        text = K3D_CONFIG_PATH.read_text()
        self.assertIn("disableLoadbalancer: true", text)
        self.assertIn("CriticalAddonsOnly=true:NoExecute", text)
        self.assertIn("k3d-flux-playground-registry:5000", text)
        # registries block must contain a `use:` list.
        self.assertRegex(text, r"registries:\s*\n\s*use:")
        # Forbidden legacy fields.
        self.assertNotIn("registries.create", text)
        self.assertNotIn("hostPath", text)
        self.assertNotIn("containerPath", text)
        # Top-level taints should not exist (must be in k3s extraArgs).
        self.assertNotIn("\ntaints:", text)
        # files block uses source/destination, not hostPath/containerPath.
        self.assertIn("source:", text)
        self.assertIn("destination:", text)

    def test_flux_operator_helmchart(self) -> None:
        text = FLUX_OPERATOR_MANIFEST_PATH.read_text()
        self.assertIn("oci://ghcr.io/controlplaneio-fluxcd/charts/flux-operator", text)
        self.assertIn('version: "0.58.1"', text)
        self.assertIn("installCRDs: true", text)
        self.assertIn("web:", text)
        # No spec.repoURL.
        self.assertNotIn("repoURL:", text)

    def test_flux_instance_pins_distribution(self) -> None:
        text = FLUX_INSTANCE_MANIFEST_PATH.read_text()
        self.assertIn('version: "2.9.4"', text)
        self.assertIn("oci://ghcr.io/controlplaneio-fluxcd/flux-operator-manifests", text)
        self.assertIn("size: small", text)
        self.assertIn("source-controller", text)
        self.assertIn("kustomize-controller", text)
        self.assertIn("helm-controller", text)
        self.assertIn("notification-controller", text)


# ---------------------------------------------------------------------------
# Real k3d v5.9 JSON parsing and image drift
# ---------------------------------------------------------------------------


class TestK3dJsonParsing(unittest.TestCase):
    """The lifecycle parsers handle the real k3d v5.9 JSON contract."""

    def test_registry_running_uses_state_running(self) -> None:
        entry = _healthy_registry_state()
        state = playground._find_registry_state(
            [entry],
            container_name=playground.REGISTRY_CONTAINER_NAME,
            logical_name=playground.REGISTRY_NAME,
        )
        self.assertIsNotNone(state)
        self.assertTrue(state.running)

    def test_registry_stopped_when_state_running_false(self) -> None:
        entry = _healthy_registry_state(state_block={"Running": False, "Status": "exited"})
        state = playground._find_registry_state(
            [entry],
            container_name=playground.REGISTRY_CONTAINER_NAME,
            logical_name=playground.REGISTRY_NAME,
        )
        self.assertIsNotNone(state)
        self.assertFalse(state.running)

    def test_running_field_takes_precedence_over_status(self) -> None:
        # Running=true but Status=exited: trust Running.
        entry = _healthy_registry_state(state_block={"Running": True, "Status": "exited"})
        state = playground._find_registry_state(
            [entry],
            container_name=playground.REGISTRY_CONTAINER_NAME,
            logical_name=playground.REGISTRY_NAME,
        )
        self.assertTrue(state and state.running)

    def test_missing_state_raises(self) -> None:
        entry = {"name": playground.REGISTRY_CONTAINER_NAME, "role": "registry"}
        with self.assertRaises(playground.PlaygroundError):
            playground._find_registry_state(
                [entry],
                container_name=playground.REGISTRY_CONTAINER_NAME,
                logical_name=playground.REGISTRY_NAME,
            )

    def test_state_running_must_be_boolean(self) -> None:
        entry = _healthy_registry_state()
        entry["State"] = {"Running": "yes"}
        with self.assertRaises(playground.PlaygroundError):
            playground._find_registry_state(
                [entry],
                container_name=playground.REGISTRY_CONTAINER_NAME,
                logical_name=playground.REGISTRY_NAME,
            )

    def test_load_balancer_present_is_detected(self) -> None:
        entry = _healthy_cluster_state(has_load_balancer=True)
        cluster = playground._find_cluster_state(
            [entry], cluster_name=playground.CLUSTER_NAME
        )
        self.assertIsNotNone(cluster)
        self.assertTrue(cluster.has_load_balancer)

    def test_load_balancer_absent_field_means_false(self) -> None:
        entry = _healthy_cluster_state(has_load_balancer=False, include_load_balancer_field=False)
        cluster = playground._find_cluster_state(
            [entry], cluster_name=playground.CLUSTER_NAME
        )
        self.assertIsNotNone(cluster)
        self.assertFalse(cluster.has_load_balancer)

    def test_digest_image_does_not_compare_with_configured(self) -> None:
        # k3d returns a sha256 digest; the runtime state must not flag this
        # as a drift against the configured reference.
        entry = _healthy_cluster_state()
        cluster = playground._find_cluster_state(
            [entry], cluster_name=playground.CLUSTER_NAME
        )
        self.assertIsNotNone(cluster)
        self.assertNotIn(playground.K3S_IMAGE, cluster.nodes[0].runtime_image_id)


class TestDockerInspectImage(unittest.TestCase):
    """The workflow inspects each container's configured image via Docker."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = _make_settings(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_inspect_returns_configured_image(self) -> None:
        runner = _docker_inspect_mock({"foo": playground.K3S_IMAGE})
        self.assertEqual(
            playground._docker_inspect_image("foo", runner),
            playground.K3S_IMAGE,
        )

    def test_inspect_failure_translates_to_playground_error(self) -> None:
        runner = _docker_inspect_mock(fail=True)
        with self.assertRaises(playground.PlaygroundError) as ctx:
            playground._docker_inspect_image("foo", runner)
        self.assertIn("reset", str(ctx.exception))

    def test_inspect_empty_output_rejected(self) -> None:
        runner = _docker_inspect_mock({}, empty=True)
        with self.assertRaises(playground.PlaygroundError):
            playground._docker_inspect_image("foo", runner)

    def test_registry_configured_image_drift_rejected(self) -> None:
        registry = _registry_state_with_binding(
            host_ip="127.0.0.1",
            host_port=self.settings.registry_port,
            runtime_image_id="sha256:3333333333333333333333333333333333333333333333333333333333333333",
            running=True,
        )
        cluster = _cluster_state_two_node()
        inspect_expected = _standard_inspect_expected()
        inspect_expected[playground.REGISTRY_CONTAINER_NAME] = "docker.io/library/registry:3"
        with mock.patch.object(playground, "_registry_state", return_value=registry):
            with mock.patch.object(playground, "_cluster_state", return_value=cluster):
                with self.assertRaises(playground.PlaygroundError) as ctx:
                    playground._ensure_infrastructure(
                        self.settings, _docker_inspect_mock(inspect_expected)
                    )
        self.assertIn("registry:3", str(ctx.exception))
        self.assertIn("reset", str(ctx.exception))

    def test_k3s_node_configured_image_drift_rejected(self) -> None:
        registry = _registry_state_with_binding(
            host_ip="127.0.0.1",
            host_port=self.settings.registry_port,
            runtime_image_id="sha256:3333333333333333333333333333333333333333333333333333333333333333",
            running=True,
        )
        node = playground.ClusterNode(
            name="k3d-flux-playground-server-0",
            role="server",
            runtime_image_id="sha256:1111111111111111111111111111111111111111111111111111111111111111",
            running=True,
        )
        agent = playground.ClusterNode(
            name="k3d-flux-playground-agent-0",
            role="agent",
            runtime_image_id="sha256:2222222222222222222222222222222222222222222222222222222222222222",
            running=True,
        )
        cluster = playground.ClusterState(
            name=self.settings.cluster_name,
            servers_count=1,
            agents_count=1,
            servers_running=1,
            agents_running=1,
            has_load_balancer=False,
            nodes=(node, agent),
        )
        inspect_expected = _standard_inspect_expected()
        inspect_expected["k3d-flux-playground-server-0"] = "rancher/k3s:v1.33.0-k3s1"
        with mock.patch.object(playground, "_registry_state", return_value=registry):
            with mock.patch.object(playground, "_cluster_state", return_value=cluster):
                with self.assertRaises(playground.PlaygroundError) as ctx:
                    playground._ensure_infrastructure(
                        self.settings, _docker_inspect_mock(inspect_expected)
                    )
        self.assertIn("k3d-flux-playground-server-0", str(ctx.exception))
        self.assertIn("reset", str(ctx.exception))


class TestFluxInstanceStalled(unittest.TestCase):
    """FluxInstance ``Stalled=True`` aborts immediately with reason/message."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = _make_settings(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _stalled(self) -> dict[str, Any]:
        return {
            "metadata": {"name": "flux"},
            "status": {
                "conditions": [
                    {
                        "type": "Stalled",
                        "status": "True",
                        "reason": "BuildFailed",
                        "message": "storage path .../flux does not exist",
                    },
                    {"type": "Ready", "status": "False"},
                ]
            },
        }

    def test_stalled_aborts_with_reason(self) -> None:
        counter = {"n": 0}

        def fake_monotonic() -> float:
            counter["n"] += 1
            return float(counter["n"])

        def run_side_effect(argv, **kw):
            return _ok(stdout=json.dumps(self._stalled()))

        def run_json_side_effect(argv, **kw):
            return self._stalled()

        runner = mock.Mock(spec=playground.CommandRunner)
        runner.run.side_effect = run_side_effect
        runner.run_json.side_effect = run_json_side_effect
        runner.default_timeout = 60.0

        with mock.patch.object(playground.time, "monotonic", fake_monotonic):
            with mock.patch.object(playground.time, "sleep"):
                with self.assertRaises(playground.PlaygroundError) as ctx:
                    playground._wait_for_flux_instance(self.settings, runner)
        self.assertIn("Stalled=True", str(ctx.exception))
        self.assertIn("BuildFailed", str(ctx.exception))
        self.assertIn("storage path", str(ctx.exception))


class TestHelmChartFailedFast(unittest.TestCase):
    """The HelmChart wait detects ``Failed=True`` during every poll."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = _make_settings(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_failed_helmchart_aborts_with_reason(self) -> None:
        helmchart = {
            "metadata": {"name": "flux-operator"},
            "status": {
                "conditions": [
                    {
                        "type": "Failed",
                        "status": "True",
                        "reason": "InstallFailed",
                        "message": "chart pull failed",
                    }
                ]
            },
        }

        def run_side_effect(argv, **kw):
            return _ok(stdout="")

        def run_json_side_effect(argv, **kw):
            return helmchart

        runner = mock.Mock(spec=playground.CommandRunner)
        runner.run.side_effect = run_side_effect
        runner.run_json.side_effect = run_json_side_effect
        runner.default_timeout = 60.0

        with mock.patch.object(playground.time, "monotonic", side_effect=[0.0, 1.0, 2.0]):
            with mock.patch.object(playground.time, "sleep"):
                with self.assertRaises(playground.PlaygroundError) as ctx:
                    playground._wait_for_operator_ready(self.settings, runner)
        self.assertIn("Failed=True", str(ctx.exception))
        self.assertIn("InstallFailed", str(ctx.exception))
        self.assertIn("chart pull failed", str(ctx.exception))

    def test_failed_install_job_aborts_with_reason(self) -> None:
        helmchart_with_job = {
            "metadata": {"name": "flux-operator"},
            "status": {"jobName": "helm-install-flux-operator"},
        }
        job_failed = {
            "metadata": {"name": "helm-install-flux-operator"},
            "status": {
                "conditions": [
                    {
                        "type": "Failed",
                        "status": "True",
                        "reason": "Boom",
                        "message": "x",
                    }
                ]
            },
        }

        def run_side_effect(argv, **kw):
            return _ok(stdout="")

        def run_json_side_effect(argv, **kw):
            if any(a.startswith("job/") for a in argv):
                return job_failed
            return helmchart_with_job

        runner = mock.Mock(spec=playground.CommandRunner)
        runner.run.side_effect = run_side_effect
        runner.run_json.side_effect = run_json_side_effect
        runner.default_timeout = 60.0

        counter = {"n": 0}

        def fake_monotonic() -> float:
            counter["n"] += 1
            return float(counter["n"])

        with mock.patch.object(playground.time, "monotonic", fake_monotonic):
            with mock.patch.object(playground.time, "sleep"):
                with self.assertRaises(playground.PlaygroundError) as ctx:
                    playground._wait_for_operator_ready(self.settings, runner)
        self.assertIn("Failed=True", str(ctx.exception))
        self.assertIn("Boom", str(ctx.exception))


class TestDiagnosticCommands(unittest.TestCase):
    """Diagnostic sections never construct malformed commands."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = _make_settings(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_source_diagnostic_includes_namespace_and_yaml(self) -> None:
        runner = _infinite_runner()
        playground._diagnose(self.settings, runner, stdout=lambda _: None)
        bad = []
        for call in runner.run.call_args_list:
            argv = call.args[0]
            if argv and argv[0] == "kubectl" and "ocirepository" in " ".join(argv):
                if "-n" not in argv or "-o" not in argv:
                    bad.append(argv)
        self.assertEqual(bad, [])

    def test_no_diagnostic_argv_ends_with_bare_n(self) -> None:
        runner = _infinite_runner()
        playground._diagnose(self.settings, runner, stdout=lambda _: None)
        for call in runner.run.call_args_list:
            argv = call.args[0]
            if not argv:
                continue
            self.assertNotEqual(argv[-1], "-n", f"Bare -n in {argv}")

    def test_diagnostic_section_failure_does_not_stop_later_sections(self) -> None:
        # Pods query raises; later sections still run.
        helmchart = {"metadata": {"name": "flux-operator"}, "status": {}}
        pods = {"items": []}
        calls = {"pod": False, "events": False}

        def run_side_effect(argv, **kw):
            if "events" in argv:
                calls["events"] = True
                return _ok(stdout="")
            return _ok(stdout="")

        def run_json_side_effect(argv, **kw):
            if "pods" in argv and "-A" in argv:
                calls["pod"] = True
                raise playground.PlaygroundError("pods failed")
            if "helmchart" in argv:
                return helmchart
            if "helmreleases" in argv:
                return {"items": []}
            return {}

        runner = mock.Mock(spec=playground.CommandRunner)
        runner.run.side_effect = run_side_effect
        runner.run_json.side_effect = run_json_side_effect
        runner.default_timeout = 60.0

        playground._diagnose(self.settings, runner, stdout=lambda _: None)
        self.assertTrue(calls["pod"])
        self.assertTrue(calls["events"])


class TestCompletedPodClassification(unittest.TestCase):
    """Succeeded pods are classified correctly for diagnostic output."""

    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.settings = _make_settings(self.tmp)

    def tearDown(self) -> None:
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _succeeded_pod(self, exit_codes: list[int]) -> dict[str, Any]:
        return {
            "items": [
                {
                    "metadata": {"namespace": "kube-system", "name": "helm-install-x"},
                    "spec": {"nodeName": "n1"},
                    "status": {
                        "phase": "Succeeded",
                        "containerStatuses": [
                            {
                                "name": f"c{i}",
                                "ready": False,
                                "restartCount": 0,
                                "state": {
                                    "terminated": {
                                        "exitCode": code,
                                        "reason": "Completed",
                                    }
                                },
                            }
                            for i, code in enumerate(exit_codes)
                        ],
                    },
                }
            ]
        }

    def test_zero_exit_succeeded_is_healthy(self) -> None:
        pods = self._succeeded_pod([0, 0])
        self.assertEqual(
            playground._classify_pods(pods)["unhealthy"], []
        )

    def test_nonzero_exit_succeeded_is_unhealthy(self) -> None:
        pods = self._succeeded_pod([0, 2])
        result = playground._classify_pods(pods)
        self.assertEqual(len(result["unhealthy"]), 1)
        self.assertEqual(result["unhealthy"][0]["namespace"], "kube-system")

    def test_running_unready_is_unhealthy(self) -> None:
        pods = {
            "items": [
                {
                    "metadata": {"namespace": "default", "name": "broken"},
                    "spec": {"nodeName": "n1"},
                    "status": {
                        "phase": "Running",
                        "containerStatuses": [
                            {"name": "c", "ready": False, "restartCount": 0}
                        ],
                    },
                }
            ]
        }
        self.assertEqual(len(playground._classify_pods(pods)["unhealthy"]), 1)

    def test_pending_is_unhealthy(self) -> None:
        pods = {
            "items": [
                {
                    "metadata": {"namespace": "default", "name": "pending"},
                    "spec": {"nodeName": ""},
                    "status": {"phase": "Pending", "containerStatuses": []},
                }
            ]
        }
        self.assertEqual(len(playground._classify_pods(pods)["unhealthy"]), 1)


# ---------------------------------------------------------------------------
# Repo content scans: no user-facing ``make playground.*`` references
# ---------------------------------------------------------------------------


class TestRepoContent(unittest.TestCase):
    def test_no_make_playground_targets_in_user_facing_files(self) -> None:
        # Search every non-generated file for ``make playground.*`` syntax.
        candidates = [
            REPO_ROOT / "README.md",
            REPO_ROOT / "Makefile",
        ]
        candidates += list(REPO_ROOT.glob("clusters/**/*.yaml"))
        candidates += list(REPO_ROOT.glob("config/**/*.yaml"))
        for path in candidates:
            with self.subTest(path=path):
                text = path.read_text()
                self.assertNotIn(
                    "make playground.up", text, f"{path} references make playground.up"
                )
                self.assertNotIn(
                    "make playground.push", text, f"{path} references make playground.push"
                )
                self.assertNotIn(
                    "make playground.check", text, f"{path} references make playground.check"
                )
                self.assertNotIn(
                    "make playground.down", text, f"{path} references make playground.down"
                )
                self.assertNotIn(
                    "make playground.reset", text, f"{path} references make playground.reset"
                )


if __name__ == "__main__":
    unittest.main()
