#!/usr/bin/env python3
"""Configurable, non-destructive Redis instance test runner."""

from __future__ import annotations

import argparse
import copy
import getpass
import json
import os
import socket
import sys
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
        "socket_connect_timeout": 5.0,
        "socket_timeout": 5.0,
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
    },
    "performance": {
        "requests": 5000,
        "concurrency": 10,
        "value_size": 128,
        "keyspace": 500,
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


class CaseWarning(Exception):
    """A completed test with an observation that needs review."""


class CaseSkip(Exception):
    """A test that does not apply to the selected instance."""


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
        "--list-suites",
        action="store_true",
        help="List available profiles and suites, then exit",
    )
    return parser


def apply_cli_overrides(config: dict[str, Any], args: argparse.Namespace) -> None:
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
    if args.cleanup:
        config["execution"]["cleanup"] = args.cleanup
    if args.requests is not None:
        config["performance"]["requests"] = args.requests
    if args.concurrency is not None:
        config["performance"]["concurrency"] = args.concurrency
    if args.namespace:
        config["execution"]["namespace"] = args.namespace


def validate_config(config: dict[str, Any]) -> None:
    host = str(config["connection"].get("host", "")).strip()
    port = int(config["connection"].get("port", 0))
    profile = config["execution"].get("profile")
    cleanup = config["execution"].get("cleanup")
    architecture = config["expectations"].get("architecture")
    auth_mode = config["authentication"].get("mode")

    if not host:
        raise ValueError("connection.host cannot be empty")
    if not 1 <= port <= 65535:
        raise ValueError("connection.port must be between 1 and 65535")
    if profile not in PROFILES:
        raise ValueError(f"Unknown profile: {profile}")
    if cleanup not in {"always", "on-success", "never"}:
        raise ValueError(f"Unknown cleanup policy: {cleanup}")
    if architecture not in {"standalone", "master-slave", "cluster"}:
        raise ValueError(f"Unknown architecture: {architecture}")
    if auth_mode not in {"prompt", "environment", "none"}:
        raise ValueError(f"Unknown authentication mode: {auth_mode}")
    if auth_mode == "none" and config["authentication"].get("required"):
        raise ValueError("authentication.required cannot be true when mode is none")
    configured_suites = config["execution"].get("suites")
    if configured_suites is not None and not isinstance(configured_suites, list):
        raise ValueError("execution.suites must be null or an array of suite names")
    if profile == "cluster" and architecture != "cluster":
        raise ValueError("The cluster profile requires expectations.architecture=cluster")

    for section, key in [
        ("atomicity", "requests"),
        ("atomicity", "concurrency"),
        ("performance", "requests"),
        ("performance", "concurrency"),
        ("performance", "value_size"),
        ("performance", "keyspace"),
    ]:
        if int(config[section].get(key, 0)) <= 0:
            raise ValueError(f"{section}.{key} must be greater than zero")


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
        run_id = datetime.now().strftime("%Y%m%d%H%M%S") + "-" + uuid.uuid4().hex[:8]
        namespace = str(config["execution"]["namespace"]).rstrip(":")
        # The hash tag keeps all test keys in one slot when testing Redis Cluster.
        self.prefix = f"{namespace}:{{{run_id}}}"
        self.results: list[TestResult] = []
        self._client: Any = None

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
            "decode_responses": True,
            "socket_connect_timeout": float(connection["socket_connect_timeout"]),
            "socket_timeout": float(connection["socket_timeout"]),
            "ssl": bool(connection.get("ssl", False)),
        }
        architecture = self.config["expectations"]["architecture"]
        if architecture == "cluster":
            return redis.RedisCluster(**common, skip_full_coverage_check=True)
        return redis.Redis(db=int(connection.get("db", 0)), **common)

    @property
    def client(self) -> Any:
        if self._client is None:
            self._client = self.make_client(authenticated=True)
        return self._client

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
        result = TestResult(name=name, status=status, duration_ms=round(duration_ms, 2), detail=detail)
        self.results.append(result)
        print(f"[{status:<4}] {name:<16} {duration_ms:>9.2f} ms  {detail}")
        return result

    def test_network(self) -> str:
        timeout = float(self.config["connection"]["socket_connect_timeout"])
        with socket.create_connection((self.host, self.port), timeout=timeout):
            return f"TCP reachable at {self.host}:{self.port}"

    def test_authentication(self) -> str:
        if not self.config["authentication"].get("required"):
            raise CaseSkip("Authentication is not required by configuration")
        connection = self.config["connection"]
        unauthenticated = redis.Redis(
            host=self.host,
            port=self.port,
            db=int(connection.get("db", 0)),
            decode_responses=True,
            socket_connect_timeout=float(connection["socket_connect_timeout"]),
            socket_timeout=float(connection["socket_timeout"]),
            ssl=bool(connection.get("ssl", False)),
        )
        try:
            response = unauthenticated.ping()
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
        assert self.client.ping() is True, "PING did not return PONG"
        return "PONG"

    def test_server(self) -> str:
        if self.config["expectations"]["architecture"] == "cluster":
            raise CaseSkip("Server INFO differs by cluster node; covered by cluster suite")
        info = self.client.info("server")
        version = str(info.get("redis_version", "unknown"))
        mode = str(info.get("redis_mode", "unknown"))
        expected = str(self.config["expectations"].get("version_prefix") or "")
        if expected and not version.startswith(expected):
            raise AssertionError(f"Redis version {version} does not start with expected prefix {expected}")
        return f"version={version}, mode={mode}, uptime_days={info.get('uptime_in_days', 'unknown')}"

    def test_string(self) -> str:
        key = self.key("string")
        assert self.client.set(key, "hello") is True
        assert self.client.get(key) == "hello"
        assert self.client.append(key, " redis") == 11
        assert self.client.get(key) == "hello redis"
        counter = self.key("counter")
        self.client.set(counter, 0)
        assert self.client.incrby(counter, 10) == 10
        assert self.client.decrby(counter, 3) == 7
        multi = {self.key("m1"): "value1", self.key("m2"): "value2"}
        assert self.client.mset(multi) is True
        assert self.client.mget(list(multi)) == ["value1", "value2"]
        return "SET/GET/APPEND/counter/MSET/MGET passed"

    def test_hash(self) -> str:
        key = self.key("hash")
        assert self.client.hset(key, mapping={"name": "longge", "subject": "physics", "score": 100}) == 3
        assert self.client.hget(key, "subject") == "physics"
        assert self.client.hincrby(key, "score", 5) == 105
        assert self.client.hgetall(key) == {"name": "longge", "subject": "physics", "score": "105"}
        return "HSET/HGET/HGETALL/HINCRBY passed"

    def test_list(self) -> str:
        key = self.key("list")
        assert self.client.rpush(key, "first", "second", "third") == 3
        assert self.client.lrange(key, 0, -1) == ["first", "second", "third"]
        assert self.client.lpop(key) == "first"
        assert self.client.lrange(key, 0, -1) == ["second", "third"]
        return "RPUSH/LRANGE/LPOP passed"

    def test_set(self) -> str:
        key = self.key("set")
        assert self.client.sadd(key, "apple", "banana", "orange", "apple") == 3
        assert self.client.scard(key) == 3
        assert self.client.sismember(key, "banana") == 1
        assert self.client.smembers(key) == {"apple", "banana", "orange"}
        return "SADD/SCARD/SISMEMBER/SMEMBERS passed"

    def test_zset(self) -> str:
        key = self.key("zset")
        assert self.client.zadd(key, {"alice": 95, "bob": 88, "carol": 92}) == 3
        assert self.client.zrange(key, 0, -1, withscores=True) == [
            ("bob", 88.0),
            ("carol", 92.0),
            ("alice", 95.0),
        ]
        assert self.client.zrevrange(key, 0, -1) == ["alice", "carol", "bob"]
        return "ZADD/ZRANGE/ZREVRANGE passed"

    def test_ttl(self) -> str:
        ttl_seconds = int(self.config["execution"]["ttl_seconds"])
        key = self.key("ttl")
        assert self.client.set(key, "temporary", ex=ttl_seconds) is True
        remaining = self.client.ttl(key)
        assert 0 < remaining <= ttl_seconds, f"Unexpected TTL immediately after SET: {remaining}"
        time.sleep(ttl_seconds + 0.25)
        assert self.client.exists(key) == 0, "Key still exists after TTL elapsed"
        return f"Key expired after {ttl_seconds}s"

    def test_transaction(self) -> str:
        if self.config["expectations"]["architecture"] == "cluster":
            raise CaseSkip("Cluster transaction behavior is client-specific; Lua covers atomic execution")
        value_key = self.key("tx-value")
        counter_key = self.key("tx-counter")
        pipeline = self.client.pipeline(transaction=True)
        pipeline.set(value_key, "tx-value")
        pipeline.incr(counter_key)
        result = pipeline.execute()
        assert result == [True, 1], f"Unexpected EXEC result: {result!r}"
        assert self.client.mget(value_key, counter_key) == ["tx-value", "1"]
        return "MULTI/EXEC committed both commands"

    def test_lua(self) -> str:
        key = self.key("lua-counter")
        script = "return redis.call('INCRBY', KEYS[1], ARGV[1])"
        assert self.client.eval(script, 1, key, 5) == 5
        assert self.client.eval(script, 1, key, 5) == 10
        return "EVAL atomic increment passed"

    def test_negative(self) -> str:
        list_key = self.key("negative-list")
        self.client.rpush(list_key, "item")
        try:
            self.client.get(list_key)
        except redis.exceptions.ResponseError as exc:
            assert "WRONGTYPE" in str(exc).upper(), f"Unexpected type error: {exc}"
        else:
            raise AssertionError("GET against a List did not return WRONGTYPE")

        counter_key = self.key("invalid-counter")
        self.client.set(counter_key, "abc")
        try:
            self.client.incr(counter_key)
        except redis.exceptions.ResponseError as exc:
            assert "INTEGER" in str(exc).upper(), f"Unexpected counter error: {exc}"
        else:
            raise AssertionError("INCR against a non-integer did not fail")
        assert self.client.get(counter_key) == "abc", "Failed INCR changed the original value"
        return "WRONGTYPE and invalid integer handling passed"

    def test_atomicity(self) -> str:
        requests = int(self.config["atomicity"]["requests"])
        concurrency = min(int(self.config["atomicity"]["concurrency"]), requests)
        key = self.key("atomic-counter")
        self.client.delete(key)
        base, remainder = divmod(requests, concurrency)
        counts = [base + (1 if index < remainder else 0) for index in range(concurrency)]

        def increment_many(count: int) -> None:
            for _ in range(count):
                self.client.incr(key)

        started = time.perf_counter()
        with ThreadPoolExecutor(max_workers=concurrency) as executor:
            futures = [executor.submit(increment_many, count) for count in counts]
            for future in as_completed(futures):
                future.result()
        elapsed = time.perf_counter() - started
        actual = int(self.client.get(key) or 0)
        assert actual == requests, f"Atomic counter expected {requests}, got {actual}"
        return f"{requests} concurrent INCR operations, final={actual}, elapsed={elapsed:.3f}s"

    def test_replication(self) -> str:
        architecture = self.config["expectations"]["architecture"]
        if architecture != "master-slave":
            raise CaseSkip(f"Replication suite does not apply to architecture={architecture}")
        info = self.client.info("replication")
        role = str(info.get("role", "unknown"))
        assert role == "master", f"Expected master role, got {role}"
        actual = int(info.get("connected_slaves", 0))
        expected = int(self.config["expectations"].get("replicas", 0))
        assert actual >= expected, f"Expected at least {expected} online replica(s), got {actual}"
        max_lag = int(self.config["expectations"].get("max_replica_lag_seconds", 2))
        for index in range(actual):
            slave = info.get(f"slave{index}")
            if isinstance(slave, dict):
                assert slave.get("state") == "online", f"slave{index} state={slave.get('state')}"
                lag = int(slave.get("lag", 0))
                assert lag <= max_lag, f"slave{index} lag={lag}s exceeds {max_lag}s"
        return f"role=master, online_replicas={actual}"

    def test_cluster(self) -> str:
        if self.config["expectations"]["architecture"] != "cluster":
            raise CaseSkip("Cluster suite only applies to architecture=cluster")
        info = self.client.cluster_info()
        state = str(info.get("cluster_state", "unknown"))
        assert state == "ok", f"cluster_state={state}"
        slots_ok = int(info.get("cluster_slots_ok", 0))
        slots_assigned = int(info.get("cluster_slots_assigned", 0))
        assert slots_ok == slots_assigned, f"slots_ok={slots_ok}, slots_assigned={slots_assigned}"
        return f"cluster_state=ok, assigned_slots={slots_assigned}"

    def test_persistence(self) -> str:
        if self.config["expectations"]["architecture"] == "cluster":
            raise CaseSkip("Persistence INFO must be evaluated per cluster node")
        info = self.client.info("persistence")
        aof_enabled = int(info.get("aof_enabled", 0))
        save_rules: dict[str, Any] = {}
        try:
            save_rules = self.client.config_get("save")
        except redis.exceptions.ResponseError:
            pass
        save_value = str(save_rules.get("save", "unknown"))
        detail = (
            f"aof_enabled={aof_enabled}, save={save_value!r}, "
            f"rdb_last_bgsave_status={info.get('rdb_last_bgsave_status', 'unknown')}"
        )
        if self.config["expectations"].get("require_engine_persistence"):
            if aof_enabled == 0 and save_value in {"", "unknown"}:
                raise AssertionError("Neither AOF nor automatic RDB save rules are enabled")
        if aof_enabled == 0 and save_value == "":
            raise CaseWarning(detail + "; verify platform-level backup policy")
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
        detail = (
            f"memory={ratio:.1%}, fragmentation={fragmentation:.2f}, "
            f"connected={connected}, blocked={blocked}, rejected_total={rejected}"
        )
        warnings: list[str] = []
        if ratio >= 0.90:
            warnings.append("memory usage is at least 90%")
        if fragmentation and not 0.8 <= fragmentation <= 2.0:
            warnings.append(f"fragmentation ratio is {fragmentation:.2f}")
        if blocked:
            warnings.append(f"{blocked} client(s) are blocked")
        if rejected:
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
        base, remainder = divmod(requests, concurrency)
        counts = [base + (1 if index < remainder else 0) for index in range(concurrency)]

        def set_get_many(worker_id: int, count: int) -> list[float]:
            latencies: list[float] = []
            for index in range(count):
                key = self.key(f"perf:{(worker_id + index) % keyspace}")
                started = time.perf_counter()
                assert self.client.set(key, value) is True
                assert self.client.get(key) == value
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
        return (
            f"logical_requests={requests}, concurrency={concurrency}, "
            f"throughput={throughput:.1f}/s, p50={percentile(all_latencies, 0.50):.2f}ms, "
            f"p95={percentile(all_latencies, 0.95):.2f}ms"
        )

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
        assert remaining == 0, f"Cleanup left {remaining} test key(s)"
        return f"deleted={deleted}, remaining=0, pattern={pattern}"

    @property
    def suites(self) -> dict[str, Callable[[], str]]:
        return {
            "network": self.test_network,
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
        selected = [str(item).strip() for item in config["execution"]["suites"] if str(item).strip()]
    else:
        selected = list(PROFILES[config["execution"]["profile"]])
    unknown = [name for name in selected if name not in AVAILABLE_SUITES]
    if unknown:
        raise ValueError(f"Unknown suite(s): {', '.join(unknown)}")
    if not selected:
        raise ValueError("At least one suite must be selected")
    return selected


def sanitized_config(config: dict[str, Any], password: str | None) -> dict[str, Any]:
    safe = copy.deepcopy(config)
    safe["authentication"]["password_present"] = bool(password)
    return safe


def write_report(
    runner: RedisTestRunner,
    config: dict[str, Any],
    password: str | None,
    started_at: str,
    report_override: str | None,
) -> Path:
    summary = {
        status: sum(1 for result in runner.results if result.status == status)
        for status in ["PASS", "FAIL", "WARN", "SKIP"]
    }
    payload = {
        "started_at": started_at,
        "finished_at": utc_now(),
        "target": f"{runner.host}:{runner.port}",
        "test_prefix": runner.prefix,
        "config": sanitized_config(config, password),
        "summary": summary,
        "results": [asdict(result) for result in runner.results],
    }
    if report_override:
        report_path = Path(report_override).expanduser().resolve()
    else:
        report_dir = Path(__file__).resolve().parent / str(config["report"]["directory"])
        report_path = report_dir / f"redis-test-{datetime.now().strftime('%Y%m%d-%H%M%S')}.json"
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    return report_path


def print_suite_list() -> None:
    all_suites = sorted({suite for suites in PROFILES.values() for suite in suites})
    print("Profiles:")
    for name, suites in PROFILES.items():
        print(f"  {name:<12} {','.join(suites)}")
    print("\nSuites:")
    for suite in all_suites:
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
        password = resolve_password(config)
    except ValueError as exc:
        parser.error(str(exc))

    if redis is None:
        print(
            "ERROR: redis-py is not installed. Run: python -m pip install -r requirements.txt",
            file=sys.stderr,
        )
        return 2

    started_at = utc_now()
    runner = RedisTestRunner(config, password)
    print(f"Target:       {runner.host}:{runner.port}")
    print(f"Profile:      {config['execution']['profile']}")
    print(f"Architecture: {config['expectations']['architecture']}")
    print(f"Test prefix:  {runner.prefix}")
    print(f"Suites:       {','.join(selected_suites)}")
    print()

    for suite_name in selected_suites:
        result = runner.run_case(suite_name, runner.suites[suite_name])
        if suite_name in {"network", "ping"} and result.status == "FAIL":
            print("Stopping because the target is not ready for further tests.")
            break

    has_failures_before_cleanup = any(result.status == "FAIL" for result in runner.results)
    cleanup_policy = config["execution"]["cleanup"]
    should_cleanup = cleanup_policy == "always" or (
        cleanup_policy == "on-success" and not has_failures_before_cleanup
    )
    if should_cleanup:
        runner.run_case("cleanup", runner.cleanup)
    else:
        detail = f"Cleanup policy is {cleanup_policy}; test keys were retained"
        result = TestResult("cleanup", "SKIP", 0.0, detail)
        runner.results.append(result)
        print(f"[SKIP] {'cleanup':<16} {0.0:>9.2f} ms  {detail}")

    report_path = write_report(runner, config, password, started_at, args.report)
    counts = {
        status: sum(1 for result in runner.results if result.status == status)
        for status in ["PASS", "FAIL", "WARN", "SKIP"]
    }
    print()
    print(
        "Summary: "
        + ", ".join(f"{status}={counts[status]}" for status in ["PASS", "FAIL", "WARN", "SKIP"])
    )
    print(f"Report:  {report_path}")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
