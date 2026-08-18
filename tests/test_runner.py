import copy
import io
import json
import signal
import sys
import tempfile
import threading
import unittest
from contextlib import redirect_stdout
from pathlib import Path
from unittest.mock import Mock, patch

import fakeredis

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from redis_instance_test import (  # noqa: E402
    CaseWarning,
    DEFAULT_CONFIG,
    RedisTestRunner,
    RunInterrupted,
    apply_cli_overrides,
    build_parser,
    choose_suites,
    deep_merge,
    main,
    sanitized_config,
    validate_config,
    write_report,
)


class ConfigTests(unittest.TestCase):
    def test_deep_merge_preserves_unspecified_defaults(self) -> None:
        merged = deep_merge(DEFAULT_CONFIG, {"connection": {"host": "10.0.0.17"}})
        self.assertEqual(merged["connection"]["host"], "10.0.0.17")
        self.assertEqual(merged["connection"]["port"], 6379)
        self.assertEqual(merged["execution"]["profile"], "standard")

    def test_configured_suites_override_profile(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["execution"]["suites"] = ["network", "ping"]
        self.assertEqual(choose_suites(config, None), ["network", "ping"])

    def test_invalid_port_is_rejected(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["connection"]["port"] = 70000
        with self.assertRaises(ValueError):
            validate_config(config)

    def test_null_config_section_is_rejected_cleanly(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["connection"] = None
        with self.assertRaisesRegex(ValueError, "connection must be a JSON object"):
            validate_config(config)

    def test_unknown_and_sensitive_options_are_rejected(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["authentication"]["password"] = "must-not-be-reported"
        with self.assertRaisesRegex(ValueError, "Unknown authentication option"):
            validate_config(config)

    def test_unsafe_namespace_is_rejected(self) -> None:
        for namespace in ["foo{}", "foo*", "foo?", "foo[bar]"]:
            with self.subTest(namespace=namespace):
                config = copy.deepcopy(DEFAULT_CONFIG)
                config["execution"]["namespace"] = namespace
                with self.assertRaisesRegex(ValueError, "execution.namespace"):
                    validate_config(config)

    def test_resource_limits_are_enforced(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["execution"]["ttl_seconds"] = 0
        with self.assertRaisesRegex(ValueError, "execution.ttl_seconds"):
            validate_config(config)

        config = copy.deepcopy(DEFAULT_CONFIG)
        config["performance"]["concurrency"] = 129
        with self.assertRaisesRegex(ValueError, "performance.concurrency"):
            validate_config(config)

    def test_tls_verification_configuration_is_consistent(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["connection"]["ssl_cert_reqs"] = "none"
        with self.assertRaisesRegex(ValueError, "ssl_check_hostname must be false"):
            validate_config(config)

        config["connection"]["ssl_check_hostname"] = False
        validate_config(config)

    def test_performance_thresholds_must_be_positive(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["performance"]["max_p95_ms"] = 0
        with self.assertRaisesRegex(ValueError, "performance.max_p95_ms"):
            validate_config(config)

    def test_health_thresholds_are_ordered(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["health"]["min_fragmentation_ratio"] = 2.0
        config["health"]["max_fragmentation_ratio"] = 1.0
        with self.assertRaisesRegex(ValueError, "max_fragmentation_ratio"):
            validate_config(config)

    def test_cluster_rejects_nonzero_database(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["execution"]["profile"] = "cluster"
        config["expectations"]["architecture"] = "cluster"
        config["connection"]["db"] = 1
        with self.assertRaisesRegex(ValueError, "must be 0 for Redis Cluster"):
            validate_config(config)

    def test_duplicate_suites_are_rejected(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        with self.assertRaisesRegex(ValueError, "Duplicate suite"):
            choose_suites(config, "ping,ping")

    def test_no_auth_override_removes_username(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["authentication"]["username"] = "acl-user"
        args = build_parser().parse_args(["--no-auth"])
        apply_cli_overrides(config, args)
        validate_config(config)
        self.assertIsNone(config["authentication"]["username"])

    def test_generic_cli_overrides_accept_typed_values(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        args = build_parser().parse_args(
            [
                "--set",
                "connection.host=redis.internal",
                "--host",
                "cli-redis.internal",
                "--set",
                "performance.max_p95_ms=12.5",
                "--set",
                'execution.suites=["security_group"]',
            ]
        )
        apply_cli_overrides(config, args)
        validate_config(config)
        self.assertEqual(config["connection"]["host"], "cli-redis.internal")
        self.assertEqual(config["performance"]["max_p95_ms"], 12.5)
        self.assertEqual(config["execution"]["suites"], ["security_group"])

    def test_cli_security_group_checks_are_added(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        args = build_parser().parse_args(
            [
                "--expect-reachable",
                "redis.internal:6379",
                "--expect-blocked",
                "[2001:db8::10]:22",
            ]
        )
        apply_cli_overrides(config, args)
        validate_config(config)
        checks = config["security_group"]["checks"]
        self.assertEqual(checks[0]["expected"], "reachable")
        self.assertEqual(checks[0]["host"], "redis.internal")
        self.assertEqual(checks[1]["expected"], "blocked")
        self.assertEqual(checks[1]["host"], "2001:db8::10")

    def test_invalid_security_group_check_is_rejected(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["security_group"]["checks"] = [
            {
                "name": "redis-port",
                "host": "10.0.0.17",
                "port": 6379,
                "expected": "sometimes",
            }
        ]
        with self.assertRaisesRegex(ValueError, "must be reachable or blocked"):
            validate_config(config)

    def test_sanitized_config_uses_an_allowlist(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["authentication"]["password"] = "must-not-be-reported"
        safe = sanitized_config(config, password="actual-password")
        self.assertNotIn("password", safe["authentication"])
        self.assertTrue(safe["authentication"]["password_present"])


class RunnerTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        self.config["authentication"] = {
            "mode": "none",
            "username": None,
            "password_env": "REDIS_PASSWORD",
            "required": False,
        }
        self.config["expectations"]["architecture"] = "standalone"
        self.config["execution"]["ttl_seconds"] = 1
        self.config["atomicity"] = {
            "requests": 200,
            "concurrency": 4,
            "max_duration_seconds": 30,
        }
        self.config["performance"] = {
            "requests": 100,
            "concurrency": 4,
            "value_size": 32,
            "keyspace": 20,
            "max_duration_seconds": 30,
            "min_throughput": None,
            "max_p95_ms": None,
            "max_p99_ms": None,
        }
        self.runner = RedisTestRunner(self.config, password=None)
        self.runner._client = fakeredis.FakeRedis(decode_responses=True)

    def test_regular_data_suites(self) -> None:
        self.runner.test_ping()
        self.runner.test_string()
        self.runner.test_hash()
        self.runner.test_list()
        self.runner.test_set()
        self.runner.test_zset()
        self.runner.test_transaction()
        self.runner.test_negative()
        self.runner.test_atomicity()

    def test_failed_ping_is_not_optimized_away(self) -> None:
        client = Mock()
        client.ping.return_value = False
        self.runner._client = client
        with self.assertRaisesRegex(AssertionError, "PING did not return PONG"):
            self.runner.test_ping()

    def test_transient_operation_is_retried(self) -> None:
        operation = Mock(side_effect=[OSError("temporary"), "ok"])
        with patch("redis_instance_test.time.sleep") as sleep:
            value, attempts = self.runner._run_with_retries(operation)
        self.assertEqual(value, "ok")
        self.assertEqual(attempts, 2)
        sleep.assert_called_once_with(0.25)

    def test_ping_retries_client_initialization(self) -> None:
        client = Mock()
        client.ping.return_value = True
        self.runner._client = None
        with patch.object(
            self.runner,
            "make_client",
            side_effect=[OSError("temporary"), client],
        ), patch("redis_instance_test.time.sleep"):
            detail = self.runner.test_ping()
        self.assertIn("attempts=2", detail)

    def test_ttl(self) -> None:
        detail = self.runner.test_ttl()
        self.assertIn("expired", detail)

    def test_lightweight_performance_suite(self) -> None:
        detail = self.runner.test_performance()
        self.assertIn("logical_requests=100", detail)
        self.assertIn("p95=", detail)

    def test_performance_threshold_can_fail_the_suite(self) -> None:
        self.config["performance"]["min_throughput"] = 10**12
        with self.assertRaisesRegex(AssertionError, "throughput is below"):
            self.runner.test_performance()

    def test_cleanup_only_removes_current_prefix(self) -> None:
        own_key = self.runner.key("owned")
        outside_key = "another-application:key"
        self.runner.client.set(own_key, "value")
        self.runner.client.set(outside_key, "keep")

        self.runner.cleanup()

        self.assertEqual(self.runner.client.exists(own_key), 0)
        self.assertEqual(self.runner.client.get(outside_key), "keep")

    def test_replication_rejects_missing_replica_metadata(self) -> None:
        self.config["expectations"]["architecture"] = "master-slave"
        self.config["expectations"]["replicas"] = 1
        client = Mock()
        client.info.return_value = {"role": "master", "connected_slaves": 1}
        self.runner._client = client
        with self.assertRaisesRegex(AssertionError, "slave0 replication metadata"):
            self.runner.test_replication()

    def test_persistence_error_becomes_warning(self) -> None:
        client = Mock()
        client.info.return_value = {
            "aof_enabled": 0,
            "rdb_last_bgsave_status": "err",
        }
        client.config_get.return_value = {"save": "3600 1"}
        self.runner._client = client
        with self.assertRaises(CaseWarning):
            self.runner.test_persistence()

    def test_historical_rejections_are_opt_in_warnings(self) -> None:
        client = Mock()
        client.info.side_effect = lambda section: {
            "memory": {
                "used_memory": 10,
                "maxmemory": 100,
                "mem_fragmentation_ratio": 1.0,
            },
            "clients": {"connected_clients": 1, "blocked_clients": 0},
            "stats": {"rejected_connections": 5},
        }[section]
        self.runner._client = client
        detail = self.runner.test_health()
        self.assertIn("rejected_total=5", detail)

        self.config["health"]["warn_on_historical_rejections"] = True
        with self.assertRaises(CaseWarning):
            self.runner.test_health()

    def test_tls_options_are_forwarded_to_redis_client(self) -> None:
        self.config["connection"]["ssl"] = True
        with patch("redis_instance_test.redis.Redis") as redis_client:
            self.runner.make_client()
        options = redis_client.call_args.kwargs
        self.assertEqual(options["ssl_cert_reqs"], "required")
        self.assertTrue(options["ssl_check_hostname"])
        self.assertEqual(options["client_name"], "redis-instance-tester")

    def test_close_releases_the_client(self) -> None:
        client = Mock()
        self.runner._client = client
        self.assertIsNone(self.runner.close())
        client.close.assert_called_once_with()
        self.assertIsNone(self.runner._client)

    def test_security_group_matches_reachable_and_blocked_endpoints(self) -> None:
        self.config["security_group"]["attempts"] = 2
        self.config["security_group"]["interval_seconds"] = 0
        self.config["security_group"]["checks"] = [
            {
                "name": "redis-open",
                "host": "10.0.0.17",
                "port": 6379,
                "expected": "reachable",
            },
            {
                "name": "ssh-blocked",
                "host": "10.0.0.9",
                "port": 22,
                "expected": "blocked",
            },
        ]
        open_socket = Mock()
        with patch(
            "redis_instance_test.socket.create_connection",
            side_effect=[
                open_socket,
                ConnectionRefusedError("blocked"),
                TimeoutError("blocked"),
            ],
        ):
            detail = self.runner.test_security_group()
        self.assertIn("matched=2/2", detail)
        open_socket.close.assert_called_once_with()

    def test_security_group_rejects_an_unexpectedly_reachable_port(self) -> None:
        self.config["security_group"]["checks"] = [
            {
                "name": "ssh-blocked",
                "host": "10.0.0.9",
                "port": 22,
                "expected": "blocked",
            }
        ]
        with patch(
            "redis_instance_test.socket.create_connection",
            return_value=Mock(),
        ), self.assertRaisesRegex(AssertionError, "observed=reachable"):
            self.runner.test_security_group()


class ReportTests(unittest.TestCase):
    def test_security_group_only_run_needs_no_redis_library_or_password(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "security-group.json"
            with patch("redis_instance_test.redis", None), patch(
                "redis_instance_test.getpass.getpass",
                side_effect=AssertionError("password prompt must not be used"),
            ), patch(
                "redis_instance_test.socket.create_connection",
                side_effect=ConnectionRefusedError("blocked"),
            ), redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "--suites",
                        "security_group",
                        "--expect-blocked",
                        "10.0.0.9:22",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(exit_code, 0)
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["selected_suites"], ["security_group"])
            self.assertEqual(payload["summary"]["FAIL"], 0)

    def test_smoke_profile_runs_end_to_end_over_tcp(self) -> None:
        server = fakeredis.TcpFakeServer(("127.0.0.1", 0))
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as directory:
                report_path = Path(directory) / "smoke.json"
                with redirect_stdout(io.StringIO()):
                    exit_code = main(
                        [
                            "--no-auth",
                            "--host",
                            "127.0.0.1",
                            "--port",
                            str(server.server_address[1]),
                            "--profile",
                            "smoke",
                            "--report",
                            str(report_path),
                        ]
                    )

                self.assertEqual(exit_code, 0)
                payload = json.loads(report_path.read_text(encoding="utf-8"))
                self.assertEqual(payload["summary"]["FAIL"], 0)
                self.assertEqual(payload["selected_suites"], [
                    "network",
                    "authentication",
                    "ping",
                    "string",
                ])
                self.assertEqual(payload["results"][-1]["name"], "cleanup")
                self.assertEqual(payload["results"][-1]["status"], "PASS")
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=5)

    def test_reports_are_unique_and_include_execution_metadata(self) -> None:
        config = copy.deepcopy(DEFAULT_CONFIG)
        config["authentication"]["mode"] = "none"
        config["authentication"]["required"] = False
        config["expectations"]["architecture"] = "standalone"

        with tempfile.TemporaryDirectory() as directory:
            config["report"]["directory"] = directory
            first_runner = RedisTestRunner(config, password=None)
            second_runner = RedisTestRunner(config, password=None)
            first_path = write_report(
                first_runner,
                config,
                None,
                "2026-08-18T00:00:00+00:00",
                None,
                selected_suites=["ping"],
                exit_code=0,
                total_duration_ms=12.34,
            )
            second_path = write_report(
                second_runner,
                config,
                None,
                "2026-08-18T00:00:00+00:00",
                None,
                selected_suites=["ping"],
                exit_code=0,
                total_duration_ms=12.34,
            )

            self.assertNotEqual(first_path, second_path)
            payload = json.loads(first_path.read_text(encoding="utf-8"))
            self.assertEqual(payload["run_id"], first_runner.run_id)
            self.assertEqual(payload["schema_version"], 2)
            self.assertEqual(payload["tool"], "redis-instance-tester")
            self.assertEqual(payload["selected_suites"], ["ping"])
            self.assertEqual(payload["exit_code"], 0)
            self.assertEqual(payload["duration_ms"], 12.34)
            self.assertFalse(payload["interrupted"])
            self.assertIn("hostname", payload["environment"])
            self.assertIn("python_version", payload["environment"])
            self.assertIn("redis_py_version", payload["environment"])
            self.assertFalse(list(Path(directory).glob("*.tmp")))

    def test_interrupted_main_cleans_up_and_writes_a_report(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "interrupted.json"
            with patch.object(
                RedisTestRunner,
                "test_network",
                side_effect=KeyboardInterrupt,
            ), redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "--no-auth",
                        "--suites",
                        "network",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(exit_code, 130)
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["interrupted"])
            self.assertEqual(payload["exit_code"], 130)
            self.assertEqual(payload["selected_suites"], ["network"])
            self.assertEqual(payload["summary"]["FAIL"], 1)
            self.assertEqual(payload["summary"]["PASS"], 1)

    def test_sigterm_main_cleans_up_and_returns_standard_exit_code(self) -> None:
        with tempfile.TemporaryDirectory() as directory:
            report_path = Path(directory) / "terminated.json"
            with patch.object(
                RedisTestRunner,
                "test_network",
                side_effect=RunInterrupted(signal.SIGTERM),
            ), redirect_stdout(io.StringIO()):
                exit_code = main(
                    [
                        "--no-auth",
                        "--suites",
                        "network",
                        "--report",
                        str(report_path),
                    ]
                )

            self.assertEqual(exit_code, 128 + signal.SIGTERM)
            payload = json.loads(report_path.read_text(encoding="utf-8"))
            self.assertTrue(payload["interrupted"])
            self.assertEqual(payload["exit_code"], 128 + signal.SIGTERM)


if __name__ == "__main__":
    unittest.main()
