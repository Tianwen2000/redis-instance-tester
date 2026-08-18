#!/usr/bin/env python3
"""Configurable, non-destructive Redis instance test runner."""

from __future__ import annotations

import argparse
import copy
import getpass
import json
import os
import platform
import re
import signal
import socket
import sys
import threading
import time
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import asdict, dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

try:
    import redis
except ImportError:  # Allows --help and --list-suites before installation.
    redis = None  # type: ignore[assignment]


DEFAULT_CONFIG: dict[str, Any] = {
    "connection": {
        "host": "127.0.0.1",
        "port": 6379,
        "db": 0,
        "ssl": False,
        "ssl_cert_reqs": "required",
        "ssl_ca_certs": None,
        "ssl_certfile": None,
        "ssl_keyfile": None,
        "ssl_check_hostname": True,
        "client_name": "redis-instance-tester",
        "socket_connect_timeout": 5.0,
        "socket_timeout": 5.0,
        "retry_attempts": 3,
        "retry_backoff_seconds": 0.25,
    },
    "security_group": {
        "probe_timeout_seconds": 2.0,
        "attempts": 2,
        "interval_seconds": 0.25,
        "checks": [],
    },
    "authentication": {
        "mode": "prompt",
        "username": None,
        "password_env": "REDIS_PASSWORD",
        "required": True,
    },
    "execution": {
        "profile": "standard",
        "suites": None,
        "namespace": "zhuque:redis-test",
        "cleanup": "always",
        "ttl_seconds": 3,
    },
    "expectations": {
        "architecture": "master-slave",
        "version_prefix": "4.",
        "replicas": 1,
        "max_replica_lag_seconds": 2,
        "require_engine_persistence": False,
    },
    "atomicity": {
        "requests": 1000,
        "concurrency": 10,
        "max_duration_seconds": 120,
    },
    "performance": {
        "requests": 5000,
        "concurrency": 10,
        "value_size": 128,
        "keyspace": 500,
        "max_duration_seconds": 300,
        "min_throughput": None,
        "max_p95_ms": None,
        "max_p99_ms": None,
    },
    "health": {
        "max_memory_ratio": 0.90,
        "min_fragmentation_ratio": 0.80,
        "max_fragmentation_ratio": 2.00,
        "max_blocked_clients": 0,
        "warn_on_historical_rejections": False,
    },
    "report": {
        "directory": "reports",
    },
}


PROFILES: dict[str, list[str]] = {
    "smoke": ["network", "authentication", "ping", "string"],
    "standard": [
        "network",
        "authentication",
        "ping",
        "server",
        "string",
        "hash",
        "list",
        "set",
        "zset",
        "ttl",
        "transaction",
        "lua",
        "negative",
        "atomicity",
        "replication",
        "persistence",
        "health",
    ],
    "performance": [
        "network",
        "authentication",
        "ping",
        "server",
        "string",
        "hash",
        "list",
        "set",
        "zset",
        "ttl",
        "transaction",
        "lua",
        "negative",
        "atomicity",
        "replication",
        "persistence",
        "health",
        "performance",
    ],
    "cluster": [
        "network",
        "authentication",
        "ping",
        "server",
        "string",
        "hash",
        "list",
        "set",
        "zset",
        "ttl",
        "transaction",
        "lua",
        "negative",
        "atomicity",
        "cluster",
        "persistence",
        "health",
    ],
}

AVAILABLE_SUITES = {
    "network",
    "security_group",
    "authentication",
    "ping",
    "server",
    "string",
    "hash",
    "list",
    "set",
    "zset",
    "ttl",
    "transaction",
    "lua",
    "negative",
    "atomicity",
    "replication",
    "cluster",
    "persistence",
    "health",
    "performance",
}
REDIS_PY_REQUIRED_SUITES = AVAILABLE_SUITES - {"network", "security_group"}
PASSWORD_REQUIRED_SUITES = REDIS_PY_REQUIRED_SUITES - {"authentication"}

NAMESPACE_PATTERN = re.compile(r"^[A-Za-z0-9][A-Za-z0-9_.:-]{0,127}$")
NUMERIC_LIMITS: dict[tuple[str, str], tuple[int, int]] = {
    ("connection", "retry_attempts"): (1, 5),
    ("security_group", "attempts"): (1, 5),
    ("execution", "ttl_seconds"): (1, 60),
    ("expectations", "replicas"): (0, 100),
    ("expectations", "max_replica_lag_seconds"): (0, 3600),
    ("atomicity", "requests"): (1, 100_000),
    ("atomicity", "concurrency"): (1, 128),
    ("atomicity", "max_duration_seconds"): (1, 600),
    ("performance", "requests"): (1, 100_000),
    ("performance", "concurrency"): (1, 128),
    ("performance", "value_size"): (1, 1_048_576),
    ("performance", "keyspace"): (1, 100_000),
    ("performance", "max_duration_seconds"): (1, 3600),
    ("health", "max_blocked_clients"): (0, 100_000),
}

REPORT_SCHEMA_VERSION = 2


class CaseWarning(Exception):
    """A completed test with an observation that needs review."""


class CaseSkip(Exception):
    """A test that does not apply to the selected instance."""


class RunInterrupted(BaseException):
    """A termination signal that should still allow cleanup and reporting."""

    def __init__(self, signum: int) -> None:
        super().__init__(f"signal {signum}")
        self.signum = signum


@dataclass
class TestResult:
    name: str
    status: str
    duration_ms: float
    detail: str


def utc_now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def deep_merge(base: dict[str, Any], override: dict[str, Any]) -> dict[str, Any]:
    merged = copy.deepcopy(base)
    for key, value in override.items():
        if isinstance(value, dict) and isinstance(merged.get(key), dict):
            merged[key] = deep_merge(merged[key], value)
        else:
            merged[key] = value
    return merged


def load_config(path: str | None) -> dict[str, Any]:
    if not path:
        return copy.deepcopy(DEFAULT_CONFIG)
    config_path = Path(path).expanduser().resolve()
    try:
        data = json.loads(config_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"Config file not found: {config_path}") from exc
    except json.JSONDecodeError as exc:
        raise ValueError(
            f"Invalid JSON in {config_path} at line {exc.lineno}, column {exc.colno}: {exc.msg}"
        ) from exc
    except (OSError, UnicodeError) as exc:
        raise ValueError(f"Cannot read config file {config_path}: {exc}") from exc
    if not isinstance(data, dict):
        raise ValueError("Config root must be a JSON object")
    return deep_merge(DEFAULT_CONFIG, data)


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run safe, repeatable Redis smoke and standard tests.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
    )
    parser.add_argument("--config", help="JSON configuration file")
    parser.add_argument("--host", help="Redis host override")
    parser.add_argument("--port", type=int, help="Redis port override")
    parser.add_argument("--profile", choices=sorted(PROFILES), help="Test profile override")
    parser.add_argument(
        "--suites",
        help="Comma-separated suite names; overrides the selected profile",
    )
    parser.add_argument(
        "--architecture",
        choices=["standalone", "master-slave", "cluster"],
        help="Expected Redis architecture",
    )
    parser.add_argument("--username", help="Redis ACL username override")
    parser.add_argument(
        "--password-env",
        help="Read the Redis password from this environment variable",
    )
    parser.add_argument(
        "--no-auth",
        action="store_true",
        help="Do not provide a password and do not require authentication",
    )
    parser.add_argument(
        "--cleanup",
        choices=["always", "on-success", "never"],
        help="Test key cleanup policy",
    )
    parser.add_argument("--requests", type=int, help="Performance request count override")
    parser.add_argument("--concurrency", type=int, help="Performance concurrency override")
    parser.add_argument("--namespace", help="Test key namespace override")
    parser.add_argument("--report", help="Write the JSON report to this path")
    parser.add_argument(
        "--set",
        dest="config_overrides",
        action="append",
        default=[],
        metavar="SECTION.OPTION=VALUE",
        help="Override any declared config option; VALUE accepts JSON or plain text",
    )
    parser.add_argument(
        "--expect-reachable",
        action="append",
        default=[],
        metavar="HOST:PORT",
        help="Add a TCP endpoint that the security_group suite must reach",
    )
    parser.add_argument(
        "--expect-blocked",
        action="append",
        default=[],
        metavar="HOST:PORT",
        help="Add a TCP endpoint that the security_group suite must not reach",
    )
    parser.add_argument(
        "--list-suites",
        action="store_true",
        help="List available profiles and suites, then exit",
    )
    return parser


def parse_config_override(expression: str) -> tuple[str, str, Any]:
    if "=" not in expression:
        raise ValueError("--set must use SECTION.OPTION=VALUE")
    path, raw_value = expression.split("=", 1)
    if "." not in path:
        raise ValueError("--set must use SECTION.OPTION=VALUE")
    section, option = (part.strip() for part in path.split(".", 1))
    if section not in DEFAULT_CONFIG or option not in DEFAULT_CONFIG[section]:
        raise ValueError(f"Unknown config option: {section}.{option}")
    try:
        value = json.loads(raw_value)
    except json.JSONDecodeError:
        value = raw_value
    return section, option, value


def parse_tcp_endpoint(value: str) -> tuple[str, int]:
    endpoint = value.strip()
    if endpoint.startswith("["):
        closing_bracket = endpoint.find("]")
        if closing_bracket < 0 or endpoint[closing_bracket + 1 : closing_bracket + 2] != ":":
            raise ValueError(f"Invalid TCP endpoint {value!r}; use [IPv6]:PORT")
        host = endpoint[1:closing_bracket]
        port_text = endpoint[closing_bracket + 2 :]
    else:
        if ":" not in endpoint:
            raise ValueError(f"Invalid TCP endpoint {value!r}; use HOST:PORT")
        host, port_text = endpoint.rsplit(":", 1)
    host = host.strip()
    if not host or len(host) > 253:
        raise ValueError(f"Invalid host in TCP endpoint {value!r}")
    try:
        port = int(port_text)
    except ValueError as exc:
        raise ValueError(f"Invalid port in TCP endpoint {value!r}") from exc
    if not 1 <= port <= 65535:
        raise ValueError(f"Port in TCP endpoint {value!r} must be between 1 and 65535")
    return host, port


def apply_cli_overrides(config: dict[str, Any], args: argparse.Namespace) -> None:
    for expression in args.config_overrides:
        section, option, value = parse_config_override(expression)
        config[section][option] = value
    if args.host:
        config["connection"]["host"] = args.host
    if args.port is not None:
        config["connection"]["port"] = args.port
    if args.profile:
        config["execution"]["profile"] = args.profile
        if args.profile == "cluster" and not args.architecture:
            config["expectations"]["architecture"] = "cluster"
    if args.architecture:
        config["expectations"]["architecture"] = args.architecture
    if args.username:
        config["authentication"]["username"] = args.username
    if args.password_env:
        config["authentication"]["mode"] = "environment"
        config["authentication"]["password_env"] = args.password_env
    if args.no_auth:
        config["authentication"]["mode"] = "none"
        config["authentication"]["required"] = False
        config["authentication"]["username"] = None
    if args.cleanup:
        config["execution"]["cleanup"] = args.cleanup
    if args.requests is not None:
        config["performance"]["requests"] = args.requests
    if args.concurrency is not None:
        config["performance"]["concurrency"] = args.concurrency
    if args.namespace:
        config["execution"]["namespace"] = args.namespace
    checks = config["security_group"]["checks"]
    if not isinstance(checks, list):
        if args.expect_reachable or args.expect_blocked:
            raise ValueError(
                "security_group.checks must be an array before adding CLI checks"
            )
        return
    next_index = len(checks) + 1
    for expected, endpoints in [
        ("reachable", args.expect_reachable),
        ("blocked", args.expect_blocked),
    ]:
        for endpoint in endpoints:
            host, port = parse_tcp_endpoint(endpoint)
            checks.append(
                {
                    "name": f"cli-{expected}-{next_index}",
                    "host": host,
                    "port": port,
                    "expected": expected,
                }
            )
            next_index += 1


def require(condition: bool, message: str) -> None:
    """Raise a test failure even when Python is running with optimization enabled."""
    if not condition:
        raise AssertionError(message)


def _integer(config: dict[str, Any], section: str, key: str) -> int:
    value = config[section].get(key)
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"{section}.{key} must be an integer")
    return value


def _number(config: dict[str, Any], section: str, key: str) -> float:
    value = config[section].get(key)
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{section}.{key} must be a number")
    return float(value)


def _optional_positive_number(config: dict[str, Any], section: str, key: str) -> None:
    value = config[section].get(key)
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ValueError(f"{section}.{key} must be null or a number")
    if float(value) <= 0:
        raise ValueError(f"{section}.{key} must be greater than zero when configured")


def validate_config(config: dict[str, Any]) -> None:
    if not isinstance(config, dict):
        raise ValueError("Config root must be a JSON object")

    unknown_sections = sorted(set(config) - set(DEFAULT_CONFIG))
    if unknown_sections:
        raise ValueError(f"Unknown config section(s): {', '.join(unknown_sections)}")

    for section_name, defaults in DEFAULT_CONFIG.items():
        section = config.get(section_name)
        if not isinstance(section, dict):
            raise ValueError(f"{section_name} must be a JSON object")
        unknown_keys = sorted(set(section) - set(defaults))
        if unknown_keys:
            raise ValueError(
                f"Unknown {section_name} option(s): {', '.join(unknown_keys)}"
            )

    connection = config["connection"]
    security_group = config["security_group"]
    authentication = config["authentication"]
    execution = config["execution"]
    expectations = config["expectations"]
    health = config["health"]

    host_value = connection.get("host")
    if not isinstance(host_value, str):
        raise ValueError("connection.host must be a string")
    host = host_value.strip()
    port = _integer(config, "connection", "port")
    db = _integer(config, "connection", "db")
    profile = execution.get("profile")
    cleanup = execution.get("cleanup")
    architecture = expectations.get("architecture")
    auth_mode = authentication.get("mode")

    if not host:
        raise ValueError("connection.host cannot be empty")
    if len(host) > 253:
        raise ValueError("connection.host must not exceed 253 characters")
    if not 1 <= port <= 65535:
        raise ValueError("connection.port must be between 1 and 65535")
    if not 0 <= db <= 65535:
        raise ValueError("connection.db must be between 0 and 65535")
    if not isinstance(connection.get("ssl"), bool):
        raise ValueError("connection.ssl must be true or false")
    ssl_cert_reqs = connection.get("ssl_cert_reqs")
    if ssl_cert_reqs not in {"required", "optional", "none"}:
        raise ValueError("connection.ssl_cert_reqs must be required, optional, or none")
    if not isinstance(connection.get("ssl_check_hostname"), bool):
        raise ValueError("connection.ssl_check_hostname must be true or false")
    if ssl_cert_reqs == "none" and connection.get("ssl_check_hostname"):
        raise ValueError(
            "connection.ssl_check_hostname must be false when ssl_cert_reqs is none"
        )
    for path_key in ["ssl_ca_certs", "ssl_certfile", "ssl_keyfile"]:
        path_value = connection.get(path_key)
        if path_value is not None and not isinstance(path_value, str):
            raise ValueError(f"connection.{path_key} must be null or a path string")
        if connection.get("ssl") and path_value:
            resolved_path = Path(path_value).expanduser()
            if not resolved_path.is_file():
                raise ValueError(f"connection.{path_key} file not found: {resolved_path}")
    if bool(connection.get("ssl_certfile")) != bool(connection.get("ssl_keyfile")):
        raise ValueError(
            "connection.ssl_certfile and connection.ssl_keyfile must be configured together"
        )
    client_name = connection.get("client_name")
    if client_name is not None and (
        not isinstance(client_name, str)
        or not NAMESPACE_PATTERN.fullmatch(client_name)
    ):
        raise ValueError(
            "connection.client_name must be null or use the namespace character set"
        )
    for timeout_key in ["socket_connect_timeout", "socket_timeout"]:
        timeout = connection.get(timeout_key)
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)):
            raise ValueError(f"connection.{timeout_key} must be a number")
        if not 0 < float(timeout) <= 60:
            raise ValueError(f"connection.{timeout_key} must be between 0 and 60 seconds")
    retry_backoff = _number(config, "connection", "retry_backoff_seconds")
    if not 0 <= retry_backoff <= 5:
        raise ValueError("connection.retry_backoff_seconds must be between 0 and 5")
    probe_timeout = _number(config, "security_group", "probe_timeout_seconds")
    if not 0 < probe_timeout <= 60:
        raise ValueError(
            "security_group.probe_timeout_seconds must be between 0 and 60"
        )
    probe_interval = _number(config, "security_group", "interval_seconds")
    if not 0 <= probe_interval <= 5:
        raise ValueError("security_group.interval_seconds must be between 0 and 5")
    checks = security_group.get("checks")
    if not isinstance(checks, list):
        raise ValueError("security_group.checks must be an array")
    if len(checks) > 32:
        raise ValueError("security_group.checks must not contain more than 32 entries")
    check_names: set[str] = set()
    allowed_check_keys = {"name", "host", "port", "expected"}
    for index, check in enumerate(checks):
        path = f"security_group.checks[{index}]"
        if not isinstance(check, dict):
            raise ValueError(f"{path} must be an object")
        unknown_check_keys = sorted(set(check) - allowed_check_keys)
        if unknown_check_keys:
            raise ValueError(
                f"Unknown {path} option(s): {', '.join(unknown_check_keys)}"
            )
        name = check.get("name")
        if not isinstance(name, str) or not NAMESPACE_PATTERN.fullmatch(name):
            raise ValueError(
                f"{path}.name must use the namespace character set and be 1-128 characters"
            )
        if name in check_names:
            raise ValueError(f"Duplicate security group check name: {name}")
        check_names.add(name)
        check_host = check.get("host")
        if check_host is not None and (
            not isinstance(check_host, str)
            or not check_host.strip()
            or len(check_host.strip()) > 253
        ):
            raise ValueError(f"{path}.host must be null or a non-empty host string")
        check_port = check.get("port")
        if check_port is not None and (
            isinstance(check_port, bool)
            or not isinstance(check_port, int)
            or not 1 <= check_port <= 65535
        ):
            raise ValueError(f"{path}.port must be null or an integer from 1 to 65535")
        if check.get("expected") not in {"reachable", "blocked"}:
            raise ValueError(f"{path}.expected must be reachable or blocked")
    if profile not in PROFILES:
        raise ValueError(f"Unknown profile: {profile}")
    if cleanup not in {"always", "on-success", "never"}:
        raise ValueError(f"Unknown cleanup policy: {cleanup}")
    if architecture not in {"standalone", "master-slave", "cluster"}:
        raise ValueError(f"Unknown architecture: {architecture}")
    if auth_mode not in {"prompt", "environment", "none"}:
        raise ValueError(f"Unknown authentication mode: {auth_mode}")
    if not isinstance(authentication.get("required"), bool):
        raise ValueError("authentication.required must be true or false")
    username = authentication.get("username")
    if username is not None and not isinstance(username, str):
        raise ValueError("authentication.username must be null or a string")
    if auth_mode == "none" and authentication.get("required"):
        raise ValueError("authentication.required cannot be true when mode is none")
    if auth_mode == "none" and username:
        raise ValueError("authentication.username must be null when mode is none")
    password_env = authentication.get("password_env")
    if not isinstance(password_env, str) or not password_env.strip():
        raise ValueError("authentication.password_env must be a non-empty string")

    configured_suites = execution.get("suites")
    if configured_suites is not None and not isinstance(configured_suites, list):
        raise ValueError("execution.suites must be null or an array of suite names")
    if configured_suites is not None and not all(
        isinstance(item, str) for item in configured_suites
    ):
        raise ValueError("execution.suites entries must be strings")
    namespace = execution.get("namespace")
    if not isinstance(namespace, str) or not NAMESPACE_PATTERN.fullmatch(namespace):
        raise ValueError(
            "execution.namespace must be 1-128 characters using only letters, "
            "digits, dots, underscores, colons, and hyphens"
        )
    if profile == "cluster" and architecture != "cluster":
        raise ValueError("The cluster profile requires expectations.architecture=cluster")
    if architecture == "cluster" and db != 0:
        raise ValueError("connection.db must be 0 for Redis Cluster")

    version_prefix = expectations.get("version_prefix")
    if version_prefix is not None and not isinstance(version_prefix, str):
        raise ValueError("expectations.version_prefix must be null or a string")
    if not isinstance(expectations.get("require_engine_persistence"), bool):
        raise ValueError("expectations.require_engine_persistence must be true or false")

    for (section, key), (minimum, maximum) in NUMERIC_LIMITS.items():
        value = _integer(config, section, key)
        if not minimum <= value <= maximum:
            raise ValueError(
                f"{section}.{key} must be between {minimum} and {maximum}"
            )

    for key in ["min_throughput", "max_p95_ms", "max_p99_ms"]:
        _optional_positive_number(config, "performance", key)

    max_memory_ratio = _number(config, "health", "max_memory_ratio")
    min_fragmentation = _number(config, "health", "min_fragmentation_ratio")
    max_fragmentation = _number(config, "health", "max_fragmentation_ratio")
    if not 0 < max_memory_ratio <= 1:
        raise ValueError("health.max_memory_ratio must be greater than 0 and at most 1")
    if min_fragmentation < 0:
        raise ValueError("health.min_fragmentation_ratio cannot be negative")
    if max_fragmentation < min_fragmentation:
        raise ValueError(
            "health.max_fragmentation_ratio must be at least min_fragmentation_ratio"
        )
    if not isinstance(health.get("warn_on_historical_rejections"), bool):
        raise ValueError("health.warn_on_historical_rejections must be true or false")

    report_directory = config["report"].get("directory")
    if not isinstance(report_directory, str) or not report_directory.strip():
        raise ValueError("report.directory must be a non-empty string")


def resolve_password(config: dict[str, Any]) -> str | None:
    auth = config["authentication"]
    mode = auth["mode"]
    if mode == "none":
        return None
    if mode == "environment":
        env_name = str(auth["password_env"])
        password = os.getenv(env_name)
        if password is None:
            raise ValueError(f"Environment variable {env_name} is not set")
    else:
        password = getpass.getpass("Redis password: ")
    if auth.get("required") and not password:
        raise ValueError("A non-empty password is required")
    return password or None


def percentile(values: list[float], percent: float) -> float:
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, max(0, int(round((len(ordered) - 1) * percent))))
    return ordered[index]


class RedisTestRunner:
    def __init__(self, config: dict[str, Any], password: str | None) -> None:
        self.config = config
        self.password = password
        self.run_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
        namespace = str(config["execution"]["namespace"]).rstrip(":")
        # The hash tag keeps all test keys in one slot when testing Redis Cluster.
        self.prefix = f"{namespace}:{{{self.run_id}}}"
        self.results: list[TestResult] = []
        self._client: Any = None
        self._client_lock = threading.Lock()

    @property
    def host(self) -> str:
        return str(self.config["connection"]["host"])

    @property
    def port(self) -> int:
        return int(self.config["connection"]["port"])

    def key(self, suffix: str) -> str:
        return f"{self.prefix}:{suffix}"

    def make_client(self, authenticated: bool = True) -> Any:
        if redis is None:
            raise RuntimeError("redis-py is not installed")
        connection = self.config["connection"]
        auth = self.config["authentication"]
        password = self.password if authenticated else None
        username = auth.get("username") if authenticated else None
        common: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "username": username,
            "password": password,
            "client_name": connection.get("client_name"),
            "decode_responses": True,
            "socket_connect_timeout": float(connection["socket_connect_timeout"]),
            "socket_timeout": float(connection["socket_timeout"]),
            "ssl": bool(connection.get("ssl", False)),
        }
        if common["ssl"]:
            common.update(
                {
                    "ssl_cert_reqs": connection["ssl_cert_reqs"],
                    "ssl_ca_certs": connection.get("ssl_ca_certs"),
                    "ssl_certfile": connection.get("ssl_certfile"),
                    "ssl_keyfile": connection.get("ssl_keyfile"),
                    "ssl_check_hostname": connection["ssl_check_hostname"],
                }
            )
        architecture = self.config["expectations"]["architecture"]
        if architecture == "cluster":
            return redis.RedisCluster(**common, skip_full_coverage_check=True)
        return redis.Redis(db=int(connection.get("db", 0)), **common)

    @property
    def client(self) -> Any:
        if self._client is None:
            with self._client_lock:
                if self._client is None:
                    self._client = self.make_client(authenticated=True)
        return self._client

    def close(self) -> str | None:
        if self._client is None:
            return None
        try:
            self._client.close()
        except Exception as exc:
            return f"{type(exc).__name__}: {exc}"
        finally:
            self._client = None
        return None

    def run_case(self, name: str, function: Callable[[], str]) -> TestResult:
        started = time.perf_counter()
        status = "PASS"
        detail = ""
        try:
            detail = function()
        except CaseWarning as exc:
            status = "WARN"
            detail = str(exc)
        except CaseSkip as exc:
            status = "SKIP"
            detail = str(exc)
        except AssertionError as exc:
            status = "FAIL"
            detail = str(exc)
        except Exception as exc:  # Each suite must be reported independently.
            status = "FAIL"
            detail = f"{type(exc).__name__}: {exc}"
        duration_ms = (time.perf_counter() - started) * 1000
        result = TestResult(
            name=name,
            status=status,
            duration_ms=round(duration_ms, 2),
            detail=detail,
        )
        self.results.append(result)
        print(f"[{status:<4}] {name:<16} {duration_ms:>9.2f} ms  {detail}")
        return result

    def _run_with_retries(self, function: Callable[[], Any]) -> tuple[Any, int]:
        attempts = int(self.config["connection"]["retry_attempts"])
        backoff = float(self.config["connection"]["retry_backoff_seconds"])
        retryable: tuple[type[BaseException], ...] = (OSError,)
        if redis is not None:
            retryable += (
                redis.exceptions.ConnectionError,
                redis.exceptions.TimeoutError,
            )
        for attempt in range(1, attempts + 1):
            try:
                return function(), attempt
            except retryable:
                if attempt == attempts:
                    raise
                time.sleep(min(backoff * (2 ** (attempt - 1)), 5.0))
        raise RuntimeError("Retry loop ended unexpectedly")

    def test_network(self) -> str:
        timeout = float(self.config["connection"]["socket_connect_timeout"])

        def connect() -> None:
            with socket.create_connection((self.host, self.port), timeout=timeout):
                return None

        _, attempts = self._run_with_retries(connect)
        return f"TCP reachable at {self.host}:{self.port}, attempts={attempts}"

    def test_security_group(self) -> str:
        settings = self.config["security_group"]
        checks = settings["checks"]
        if not checks:
            raise CaseSkip("No security_group.checks are configured")
        timeout = float(settings["probe_timeout_seconds"])
        attempts = int(settings["attempts"])
        interval = float(settings["interval_seconds"])
        matched: list[str] = []
        mismatched: list[str] = []

        for check in checks:
            name = str(check["name"])
            host = str(check.get("host") or self.host)
            port = int(check.get("port") or self.port)
            expected = str(check["expected"])
            errors: list[str] = []
            reachable = False
            attempts_used = 0

            for attempt in range(1, attempts + 1):
                attempts_used = attempt
                connection: Any = None
                try:
                    connection = socket.create_connection((host, port), timeout=timeout)
                    reachable = True
                except OSError as exc:
                    errors.append(f"{type(exc).__name__}: {exc}")
                finally:
                    if connection is not None:
                        try:
                            connection.close()
                        except OSError:
                            pass

                if reachable:
                    break
                if attempt < attempts and interval:
                    time.sleep(interval)

            observed = "reachable" if reachable else "blocked"
            detail = (
                f"{name}={host}:{port} expected={expected} observed={observed} "
                f"attempts={attempts_used}"
            )
            if errors and not reachable:
                detail += f" last_error={errors[-1]}"
            if observed == expected:
                matched.append(detail)
            else:
                mismatched.append(detail)

        summary = f"matched={len(matched)}/{len(checks)}"
        if mismatched:
            raise AssertionError(summary + "; mismatched: " + "; ".join(mismatched))
        return summary + "; " + "; ".join(matched)

    def test_authentication(self) -> str:
        if not self.config["authentication"].get("required"):
            raise CaseSkip("Authentication is not required by configuration")
        connection = self.config["connection"]
        unauthenticated_options: dict[str, Any] = {
            "host": self.host,
            "port": self.port,
            "db": int(connection.get("db", 0)),
            "decode_responses": True,
            "socket_connect_timeout": float(connection["socket_connect_timeout"]),
            "socket_timeout": float(connection["socket_timeout"]),
            "ssl": bool(connection.get("ssl", False)),
        }
        if unauthenticated_options["ssl"]:
            unauthenticated_options.update(
                {
                    "ssl_cert_reqs": connection["ssl_cert_reqs"],
                    "ssl_ca_certs": connection.get("ssl_ca_certs"),
                    "ssl_certfile": connection.get("ssl_certfile"),
                    "ssl_keyfile": connection.get("ssl_keyfile"),
                    "ssl_check_hostname": connection["ssl_check_hostname"],
                }
            )
        unauthenticated = redis.Redis(**unauthenticated_options)
        try:
            response, _ = self._run_with_retries(unauthenticated.ping)
        except redis.exceptions.AuthenticationError:
            return "Unauthenticated connection was rejected"
        except redis.exceptions.ResponseError as exc:
            message = str(exc).upper()
            if "NOAUTH" in message or "AUTH" in message or "WRONGPASS" in message:
                return "Unauthenticated connection was rejected"
            raise
        finally:
            try:
                unauthenticated.close()
            except Exception:
                pass
        if response:
            raise AssertionError("Unauthenticated PING succeeded while authentication is required")
        raise AssertionError(f"Unexpected unauthenticated response: {response!r}")

    def test_ping(self) -> str:
        def ping() -> Any:
            return self.client.ping()

        response, attempts = self._run_with_retries(ping)
        require(response is True, "PING did not return PONG")
        return f"PONG, attempts={attempts}"

    def test_server(self) -> str:
        if self.config["expectations"]["architecture"] == "cluster":
            raise CaseSkip("Server INFO differs by cluster node; covered by cluster suite")
        info = self.client.info("server")
        version = str(info.get("redis_version", "unknown"))
        mode = str(info.get("redis_mode", "unknown"))
        expected = str(self.config["expectations"].get("version_prefix") or "")
        if expected and not version.startswith(expected):
            raise AssertionError(
                f"Redis version {version} does not start with expected prefix {expected}"
            )
        return (
            f"version={version}, mode={mode}, "
            f"uptime_days={info.get('uptime_in_days', 'unknown')}"
        )

    def test_string(self) -> str:
        key = self.key("string")
        require(self.client.set(key, "hello") is True, "SET did not return success")
        require(self.client.get(key) == "hello", "GET did not return the stored value")
        require(self.client.append(key, " redis") == 11, "APPEND returned an unexpected length")
        require(self.client.get(key) == "hello redis", "APPEND result was not persisted")
        counter = self.key("counter")
        self.client.set(counter, 0)
        require(self.client.incrby(counter, 10) == 10, "INCRBY returned an unexpected value")
        require(self.client.decrby(counter, 3) == 7, "DECRBY returned an unexpected value")
        multi = {self.key("m1"): "value1", self.key("m2"): "value2"}
        require(self.client.mset(multi) is True, "MSET did not return success")
        require(
            self.client.mget(list(multi)) == ["value1", "value2"],
            "MGET did not return the stored values",
        )
        return "SET/GET/APPEND/counter/MSET/MGET passed"

    def test_hash(self) -> str:
        key = self.key("hash")
        require(
            self.client.hset(
                key,
                mapping={"name": "longge", "subject": "physics", "score": 100},
            )
            == 3,
            "HSET returned an unexpected field count",
        )
        require(self.client.hget(key, "subject") == "physics", "HGET returned an unexpected value")
        require(self.client.hincrby(key, "score", 5) == 105, "HINCRBY returned an unexpected value")
        require(
            self.client.hgetall(key)
            == {"name": "longge", "subject": "physics", "score": "105"},
            "HGETALL returned unexpected fields",
        )
        return "HSET/HGET/HGETALL/HINCRBY passed"

    def test_list(self) -> str:
        key = self.key("list")
        require(
            self.client.rpush(key, "first", "second", "third") == 3,
            "RPUSH returned an unexpected length",
        )
        require(
            self.client.lrange(key, 0, -1) == ["first", "second", "third"],
            "LRANGE returned an unexpected list",
        )
        require(self.client.lpop(key) == "first", "LPOP returned an unexpected value")
        require(
            self.client.lrange(key, 0, -1) == ["second", "third"],
            "List contents were incorrect after LPOP",
        )
        return "RPUSH/LRANGE/LPOP passed"

    def test_set(self) -> str:
        key = self.key("set")
        require(
            self.client.sadd(key, "apple", "banana", "orange", "apple") == 3,
            "SADD returned an unexpected member count",
        )
        require(self.client.scard(key) == 3, "SCARD returned an unexpected count")
        require(bool(self.client.sismember(key, "banana")), "SISMEMBER did not find banana")
        require(
            self.client.smembers(key) == {"apple", "banana", "orange"},
            "SMEMBERS returned unexpected members",
        )
        return "SADD/SCARD/SISMEMBER/SMEMBERS passed"

    def test_zset(self) -> str:
        key = self.key("zset")
        require(
            self.client.zadd(key, {"alice": 95, "bob": 88, "carol": 92}) == 3,
            "ZADD returned an unexpected member count",
        )
        require(
            self.client.zrange(key, 0, -1, withscores=True)
            == [("bob", 88.0), ("carol", 92.0), ("alice", 95.0)],
            "ZRANGE returned an unexpected ordering",
        )
        require(
            self.client.zrevrange(key, 0, -1) == ["alice", "carol", "bob"],
            "ZREVRANGE returned an unexpected ordering",
        )
        return "ZADD/ZRANGE/ZREVRANGE passed"

    def test_ttl(self) -> str:
        ttl_seconds = int(self.config["execution"]["ttl_seconds"])
        key = self.key("ttl")
        require(
            self.client.set(key, "temporary", ex=ttl_seconds) is True,
            "SET EX did not return success",
        )
        remaining = self.client.ttl(key)
        require(0 < remaining <= ttl_seconds, f"Unexpected TTL immediately after SET: {remaining}")
        deadline = time.monotonic() + ttl_seconds + 1.0
        while self.client.exists(key) and time.monotonic() < deadline:
            time.sleep(0.1)
        require(self.client.exists(key) == 0, "Key still exists after TTL elapsed")
        return f"Key expired after {ttl_seconds}s"

    def test_transaction(self) -> str:
        if self.config["expectations"]["architecture"] == "cluster":
            raise CaseSkip(
                "Cluster transaction behavior is client-specific; Lua covers atomic execution"
            )
        value_key = self.key("tx-value")
        counter_key = self.key("tx-counter")
        pipeline = self.client.pipeline(transaction=True)
        pipeline.set(value_key, "tx-value")
        pipeline.incr(counter_key)
        result = pipeline.execute()
        require(result == [True, 1], f"Unexpected EXEC result: {result!r}")
        require(
            self.client.mget(value_key, counter_key) == ["tx-value", "1"],
            "MULTI/EXEC did not persist both values",
        )
        return "MULTI/EXEC committed both commands"

    def test_lua(self) -> str:
        key = self.key("lua-counter")
        script = "return redis.call('INCRBY', KEYS[1], ARGV[1])"
        require(
            self.client.eval(script, 1, key, 5) == 5,
            "First Lua increment returned an unexpected value",
        )
        require(
            self.client.eval(script, 1, key, 5) == 10,
            "Second Lua increment returned an unexpected value",
        )
        return "EVAL atomic increment passed"

    def test_negative(self) -> str:
        list_key = self.key("negative-list")
        self.client.rpush(list_key, "item")
        try:
            self.client.get(list_key)
        except redis.exceptions.ResponseError as exc:
            require("WRONGTYPE" in str(exc).upper(), f"Unexpected type error: {exc}")
        else:
            raise AssertionError("GET against a List did not return WRONGTYPE")

        counter_key = self.key("invalid-counter")
        self.client.set(counter_key, "abc")
        try:
            self.client.incr(counter_key)
        except redis.exceptions.ResponseError as exc:
            require("INTEGER" in str(exc).upper(), f"Unexpected counter error: {exc}")
        else:
            raise AssertionError("INCR against a non-integer did not fail")
        require(
            self.client.get(counter_key) == "abc",
            "Failed INCR changed the original value",
        )
        return "WRONGTYPE and invalid integer handling passed"

    def test_atomicity(self) -> str:
        requests = int(self.config["atomicity"]["requests"])
        concurrency = min(int(self.config["atomicity"]["concurrency"]), requests)
        max_duration = int(self.config["atomicity"]["max_duration_seconds"])
        key = self.key("atomic-counter")
        client = self.client
        client.delete(key)
        base, remainder = divmod(requests, concurrency)
        counts = [base + (1 if index < remainder else 0) for index in range(concurrency)]
        deadline = time.monotonic() + max_duration

        def increment_many(count: int) -> None:
            for _ in range(count):
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        f"Atomicity suite exceeded {max_duration}s duration budget"
                    )
                client.incr(key)

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(increment_many, count) for count in counts]
            for future in as_completed(futures):
                future.result()
        elapsed = time.perf_counter() - started
        actual = int(client.get(key) or 0)
        require(actual == requests, f"Atomic counter expected {requests}, got {actual}")
        return f"{requests} concurrent INCR operations, final={actual}, elapsed={elapsed:.3f}s"

    def test_replication(self) -> str:
        architecture = self.config["expectations"]["architecture"]
        if architecture != "master-slave":
            raise CaseSkip(f"Replication suite does not apply to architecture={architecture}")
        info = self.client.info("replication")
        role = str(info.get("role", "unknown"))
        require(role == "master", f"Expected master role, got {role}")
        actual = int(info.get("connected_slaves", 0))
        expected = int(self.config["expectations"].get("replicas", 0))
        require(
            actual >= expected,
            f"Expected at least {expected} online replica(s), got {actual}",
        )
        max_lag = int(self.config["expectations"].get("max_replica_lag_seconds", 2))
        for index in range(actual):
            slave = info.get(f"slave{index}")
            require(
                isinstance(slave, dict),
                f"Missing or invalid slave{index} replication metadata",
            )
            require(slave.get("state") == "online", f"slave{index} state={slave.get('state')}")
            require("lag" in slave, f"slave{index} replication lag is missing")
            lag = int(slave["lag"])
            require(lag >= 0, f"slave{index} lag cannot be negative: {lag}")
            require(lag <= max_lag, f"slave{index} lag={lag}s exceeds {max_lag}s")
        return f"role=master, online_replicas={actual}"

    def test_cluster(self) -> str:
        if self.config["expectations"]["architecture"] != "cluster":
            raise CaseSkip("Cluster suite only applies to architecture=cluster")
        info = self.client.cluster_info()
        state = str(info.get("cluster_state", "unknown"))
        require(state == "ok", f"cluster_state={state}")
        slots_ok = int(info.get("cluster_slots_ok", 0))
        slots_assigned = int(info.get("cluster_slots_assigned", 0))
        slots_pfail = int(info.get("cluster_slots_pfail", 0))
        slots_fail = int(info.get("cluster_slots_fail", 0))
        require(slots_assigned == 16384, f"Expected 16384 assigned slots, got {slots_assigned}")
        require(slots_ok == 16384, f"Expected 16384 healthy slots, got {slots_ok}")
        require(slots_pfail == 0, f"cluster_slots_pfail={slots_pfail}")
        require(slots_fail == 0, f"cluster_slots_fail={slots_fail}")
        return (
            f"cluster_state=ok, assigned_slots={slots_assigned}, "
            f"known_nodes={info.get('cluster_known_nodes', 'unknown')}, "
            f"cluster_size={info.get('cluster_size', 'unknown')}"
        )

    def test_persistence(self) -> str:
        if self.config["expectations"]["architecture"] == "cluster":
            raise CaseSkip("Persistence INFO must be evaluated per cluster node")
        info = self.client.info("persistence")
        aof_enabled = int(info.get("aof_enabled", 0))
        save_rules: dict[str, Any] = {}
        config_inspection_failed = False
        try:
            save_rules = self.client.config_get("save")
        except redis.exceptions.ResponseError:
            config_inspection_failed = True
        save_value = str(save_rules.get("save", "unknown"))
        rdb_status = str(info.get("rdb_last_bgsave_status", "unknown")).lower()
        aof_write_status = str(info.get("aof_last_write_status", "unknown")).lower()
        aof_rewrite_status = str(info.get("aof_last_bgrewrite_status", "unknown")).lower()
        detail = (
            f"aof_enabled={aof_enabled}, save={save_value!r}, "
            f"rdb_last_bgsave_status={rdb_status}, "
            f"aof_last_write_status={aof_write_status}, "
            f"aof_last_bgrewrite_status={aof_rewrite_status}"
        )
        require_persistence = bool(
            self.config["expectations"].get("require_engine_persistence")
        )
        status_problems: list[str] = []
        observations: list[str] = []
        if rdb_status not in {"ok", "unknown"}:
            status_problems.append(f"last RDB background save status is {rdb_status}")
        if aof_enabled and aof_write_status not in {"ok", "unknown"}:
            status_problems.append(f"last AOF write status is {aof_write_status}")
        if aof_enabled and aof_rewrite_status not in {"ok", "unknown"}:
            status_problems.append(f"last AOF rewrite status is {aof_rewrite_status}")
        if config_inspection_failed:
            observations.append("CONFIG GET save is unavailable")
        if require_persistence and aof_enabled == 0 and save_value in {"", "unknown"}:
            raise AssertionError("Neither AOF nor verifiable automatic RDB save rules are enabled")
        if require_persistence and status_problems:
            raise AssertionError(detail + "; " + "; ".join(status_problems))
        if aof_enabled == 0 and save_value == "":
            observations.append("verify platform-level backup policy")
        observations = status_problems + observations
        if observations:
            raise CaseWarning(detail + "; " + "; ".join(observations))
        return detail

    def test_health(self) -> str:
        if self.config["expectations"]["architecture"] == "cluster":
            raise CaseSkip("Health INFO must be evaluated per cluster node")
        memory = self.client.info("memory")
        clients = self.client.info("clients")
        stats = self.client.info("stats")
        used = int(memory.get("used_memory", 0))
        maximum = int(memory.get("maxmemory", 0))
        ratio = used / maximum if maximum else 0.0
        fragmentation = float(memory.get("mem_fragmentation_ratio", 0.0))
        connected = int(clients.get("connected_clients", 0))
        blocked = int(clients.get("blocked_clients", 0))
        rejected = int(stats.get("rejected_connections", 0))
        settings = self.config["health"]
        max_memory_ratio = float(settings["max_memory_ratio"])
        min_fragmentation = float(settings["min_fragmentation_ratio"])
        max_fragmentation = float(settings["max_fragmentation_ratio"])
        max_blocked = int(settings["max_blocked_clients"])
        detail = (
            f"memory={ratio:.1%}, fragmentation={fragmentation:.2f}, "
            f"connected={connected}, blocked={blocked}, rejected_total={rejected}"
        )
        warnings: list[str] = []
        if ratio >= max_memory_ratio:
            warnings.append(f"memory usage is at least {max_memory_ratio:.0%}")
        if fragmentation and not min_fragmentation <= fragmentation <= max_fragmentation:
            warnings.append(f"fragmentation ratio is {fragmentation:.2f}")
        if blocked > max_blocked:
            warnings.append(
                f"blocked clients {blocked} exceeds configured maximum {max_blocked}"
            )
        if rejected and settings["warn_on_historical_rejections"]:
            warnings.append(f"historical rejected_connections={rejected}")
        if warnings:
            raise CaseWarning(detail + "; " + "; ".join(warnings))
        return detail

    def test_performance(self) -> str:
        settings = self.config["performance"]
        requests = int(settings["requests"])
        concurrency = min(int(settings["concurrency"]), requests)
        value = "x" * int(settings["value_size"])
        keyspace = int(settings["keyspace"])
        max_duration = int(settings["max_duration_seconds"])
        client = self.client
        base, remainder = divmod(requests, concurrency)
        counts = [base + (1 if index < remainder else 0) for index in range(concurrency)]
        deadline = time.monotonic() + max_duration

        def set_get_many(worker_id: int, count: int) -> list[float]:
            latencies: list[float] = []
            for index in range(count):
                if time.monotonic() >= deadline:
                    raise AssertionError(
                        f"Performance suite exceeded {max_duration}s duration budget"
                    )
                key = self.key(f"perf:{(worker_id + index) % keyspace}")
                started = time.perf_counter()
                require(client.set(key, value) is True, "Performance SET did not return success")
                require(client.get(key) == value, "Performance GET returned an unexpected value")
                latencies.append((time.perf_counter() - started) * 1000)
            return latencies

        started = time.perf_counter()
        all_latencies: list[float] = []
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [
                executor.submit(set_get_many, worker_id, count)
                for worker_id, count in enumerate(counts)
            ]
            for future in as_completed(futures):
                all_latencies.extend(future.result())
        elapsed = time.perf_counter() - started
        throughput = requests / elapsed if elapsed else 0.0
        p50 = percentile(all_latencies, 0.50)
        p95 = percentile(all_latencies, 0.95)
        p99 = percentile(all_latencies, 0.99)
        detail = (
            f"logical_requests={requests}, concurrency={concurrency}, "
            f"throughput={throughput:.1f}/s, p50={p50:.2f}ms, "
            f"p95={p95:.2f}ms, p99={p99:.2f}ms"
        )
        violations: list[str] = []
        min_throughput = settings.get("min_throughput")
        max_p95 = settings.get("max_p95_ms")
        max_p99 = settings.get("max_p99_ms")
        if min_throughput is not None and throughput < float(min_throughput):
            violations.append(f"throughput is below {float(min_throughput):.1f}/s")
        if max_p95 is not None and p95 > float(max_p95):
            violations.append(f"p95 exceeds {float(max_p95):.2f}ms")
        if max_p99 is not None and p99 > float(max_p99):
            violations.append(f"p99 exceeds {float(max_p99):.2f}ms")
        if violations:
            raise AssertionError(detail + "; " + "; ".join(violations))
        return detail

    def cleanup(self) -> str:
        if self._client is None:
            return "No authenticated Redis commands were executed; nothing to clean"
        pattern = f"{self.prefix}:*"
        keys = list(self.client.scan_iter(match=pattern, count=200))
        deleted = 0
        for index in range(0, len(keys), 100):
            batch = keys[index : index + 100]
            if batch:
                deleted += int(self.client.delete(*batch))
        remaining = sum(1 for _ in self.client.scan_iter(match=pattern, count=200))
        require(remaining == 0, f"Cleanup left {remaining} test key(s)")
        return f"deleted={deleted}, remaining=0, pattern={pattern}"

    @property
    def suites(self) -> dict[str, Callable[[], str]]:
        return {
            "network": self.test_network,
            "security_group": self.test_security_group,
            "authentication": self.test_authentication,
            "ping": self.test_ping,
            "server": self.test_server,
            "string": self.test_string,
            "hash": self.test_hash,
            "list": self.test_list,
            "set": self.test_set,
            "zset": self.test_zset,
            "ttl": self.test_ttl,
            "transaction": self.test_transaction,
            "lua": self.test_lua,
            "negative": self.test_negative,
            "atomicity": self.test_atomicity,
            "replication": self.test_replication,
            "cluster": self.test_cluster,
            "persistence": self.test_persistence,
            "health": self.test_health,
            "performance": self.test_performance,
        }


def choose_suites(config: dict[str, Any], suite_override: str | None) -> list[str]:
    if suite_override:
        selected = [item.strip() for item in suite_override.split(",") if item.strip()]
    elif config["execution"].get("suites") is not None:
        selected = [
            str(item).strip()
            for item in config["execution"]["suites"]
            if str(item).strip()
        ]
    else:
        selected = list(PROFILES[config["execution"]["profile"]])
    unknown = [name for name in selected if name not in AVAILABLE_SUITES]
    if unknown:
        raise ValueError(f"Unknown suite(s): {', '.join(unknown)}")
    if not selected:
        raise ValueError("At least one suite must be selected")
    seen: set[str] = set()
    duplicates: list[str] = []
    for name in selected:
        if name in seen and name not in duplicates:
            duplicates.append(name)
        seen.add(name)
    if duplicates:
        raise ValueError(f"Duplicate suite(s): {', '.join(duplicates)}")
    return selected


def sanitized_config(config: dict[str, Any], password: str | None) -> dict[str, Any]:
    safe: dict[str, Any] = {}
    for section_name, defaults in DEFAULT_CONFIG.items():
        source = config.get(section_name, {})
        if not isinstance(source, dict):
            continue
        safe[section_name] = {
            key: copy.deepcopy(source.get(key, default_value))
            for key, default_value in defaults.items()
        }
    safe["authentication"]["password_present"] = bool(password)
    return safe


def write_report(
    runner: RedisTestRunner,
    config: dict[str, Any],
    password: str | None,
    started_at: str,
    report_override: str | None,
    *,
    selected_suites: list[str] | None = None,
    exit_code: int | None = None,
    total_duration_ms: float | None = None,
    interrupted: bool = False,
) -> Path:
    summary = {
        status: sum(1 for result in runner.results if result.status == status)
        for status in ["PASS", "FAIL", "WARN", "SKIP"]
    }
    payload = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "tool": "redis-instance-tester",
        "run_id": runner.run_id,
        "started_at": started_at,
        "finished_at": utc_now(),
        "duration_ms": round(total_duration_ms, 2) if total_duration_ms is not None else None,
        "target": f"{runner.host}:{runner.port}",
        "test_prefix": runner.prefix,
        "selected_suites": list(selected_suites or []),
        "exit_code": exit_code,
        "interrupted": interrupted,
        "environment": {
            "hostname": socket.gethostname(),
            "python_version": platform.python_version(),
            "python_implementation": platform.python_implementation(),
            "platform": platform.platform(),
            "redis_py_version": getattr(redis, "__version__", "unavailable"),
        },
        "config": sanitized_config(config, password),
        "summary": summary,
        "results": [asdict(result) for result in runner.results],
    }
    if report_override:
        report_path = Path(report_override).expanduser().resolve()
    else:
        report_dir = Path(__file__).resolve().parent / str(config["report"]["directory"])
        report_path = report_dir / f"redis-test-{runner.run_id}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    temporary_path = report_path.with_name(
        f".{report_path.name}.{uuid.uuid4().hex}.tmp"
    )
    try:
        temporary_path.write_text(
            json.dumps(payload, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )
        os.replace(temporary_path, report_path)
    finally:
        try:
            temporary_path.unlink(missing_ok=True)
        except OSError:
            pass
    return report_path


def print_suite_list() -> None:
    print("Profiles:")
    for name, suites in PROFILES.items():
        print(f"  {name:<12} {','.join(suites)}")
    print("\nSuites:")
    for suite in sorted(AVAILABLE_SUITES):
        print(f"  {suite}")


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if args.list_suites:
        print_suite_list()
        return 0

    try:
        config = load_config(args.config)
        apply_cli_overrides(config, args)
        validate_config(config)
        selected_suites = choose_suites(config, args.suites)
        if (
            args.expect_reachable or args.expect_blocked
        ) and "security_group" not in selected_suites:
            selected_suites.insert(0, "security_group")
    except ValueError as exc:
        parser.error(str(exc))

    if redis is None and any(
        suite in REDIS_PY_REQUIRED_SUITES for suite in selected_suites
    ):
        print(
            "ERROR: redis-py is not installed. Run: python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2

    password: str | None = None
    if any(suite in PASSWORD_REQUIRED_SUITES for suite in selected_suites):
        try:
            password = resolve_password(config)
        except ValueError as exc:
            parser.error(str(exc))

    started_at = utc_now()
    started_monotonic = time.perf_counter()
    runner = RedisTestRunner(config, password)
    print(f"Target:       {runner.host}:{runner.port}")
    print(f"Profile:      {config['execution']['profile']}")
    print(f"Architecture: {config['expectations']['architecture']}")
    print(f"Test prefix:  {runner.prefix}")
    print(f"Suites:       {','.join(selected_suites)}")
    print()

    interrupted = False
    interruption_exit_code = 130
    previous_sigterm_handler: Any = None
    sigterm_handler_installed = False

    def handle_sigterm(signum: int, _frame: Any) -> None:
        raise RunInterrupted(signum)

    if hasattr(signal, "SIGTERM"):
        try:
            previous_sigterm_handler = signal.getsignal(signal.SIGTERM)
            signal.signal(signal.SIGTERM, handle_sigterm)
            sigterm_handler_installed = True
        except (OSError, ValueError):
            pass

    try:
        for suite_name in selected_suites:
            result = runner.run_case(suite_name, runner.suites[suite_name])
            if suite_name in {"network", "ping"} and result.status == "FAIL":
                print("Stopping because the target is not ready for further tests.")
                break
    except KeyboardInterrupt:
        interrupted = True
        detail = "Execution interrupted by user"
        runner.results.append(TestResult("execution", "FAIL", 0.0, detail))
        print(f"\n[FAIL] {'execution':<16} {0.0:>9.2f} ms  {detail}")
    except RunInterrupted as exc:
        interrupted = True
        interruption_exit_code = 128 + exc.signum
        detail = f"Execution interrupted by signal {exc.signum}"
        runner.results.append(TestResult("execution", "FAIL", 0.0, detail))
        print(f"\n[FAIL] {'execution':<16} {0.0:>9.2f} ms  {detail}")
    finally:
        if sigterm_handler_installed:
            signal.signal(signal.SIGTERM, previous_sigterm_handler)
        has_failures_before_cleanup = any(
            result.status == "FAIL" for result in runner.results
        )
        cleanup_policy = config["execution"]["cleanup"]
        should_cleanup = cleanup_policy == "always" or (
            cleanup_policy == "on-success" and not has_failures_before_cleanup
        )
        try:
            if should_cleanup:
                runner.run_case("cleanup", runner.cleanup)
            else:
                detail = f"Cleanup policy is {cleanup_policy}; test keys were retained"
                result = TestResult("cleanup", "SKIP", 0.0, detail)
                runner.results.append(result)
                print(f"[SKIP] {'cleanup':<16} {0.0:>9.2f} ms  {detail}")
        except KeyboardInterrupt:
            interrupted = True
            detail = "Cleanup interrupted by user"
            runner.results.append(TestResult("cleanup", "FAIL", 0.0, detail))
            print(f"\n[FAIL] {'cleanup':<16} {0.0:>9.2f} ms  {detail}")
        finally:
            close_error = runner.close()
            if close_error:
                detail = f"Redis client close failed: {close_error}"
                runner.results.append(TestResult("client-close", "WARN", 0.0, detail))
                print(f"[WARN] {'client-close':<16} {0.0:>9.2f} ms  {detail}")

    counts = {
        status: sum(1 for result in runner.results if result.status == status)
        for status in ["PASS", "FAIL", "WARN", "SKIP"]
    }
    exit_code = interruption_exit_code if interrupted else (1 if counts["FAIL"] else 0)
    total_duration_ms = (time.perf_counter() - started_monotonic) * 1000

    try:
        report_path = write_report(
            runner,
            config,
            password,
            started_at,
            args.report,
            selected_suites=selected_suites,
            exit_code=exit_code,
            total_duration_ms=total_duration_ms,
            interrupted=interrupted,
        )
    except (OSError, TypeError, ValueError) as exc:
        print(f"ERROR: Could not write JSON report: {exc}", file=sys.stderr)
        return 2

    print()
    print(
        "Summary: "
        + ", ".join(f"{status}={counts[status]}" for status in ["PASS", "FAIL", "WARN", "SKIP"])
    )
    print(f"Report:  {report_path}")
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
