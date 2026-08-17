import copy
import sys
import unittest
from pathlib import Path

import fakeredis

PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from redis_instance_test import (  # noqa: E402
    DEFAULT_CONFIG,
    RedisTestRunner,
    choose_suites,
    deep_merge,
    validate_config,
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
        self.config["atomicity"] = {"requests": 200, "concurrency": 4}
        self.config["performance"] = {
            "requests": 100,
            "concurrency": 4,
            "value_size": 32,
            "keyspace": 20,
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

    def test_ttl(self) -> None:
        detail = self.runner.test_ttl()
        self.assertIn("expired", detail)

    def test_lightweight_performance_suite(self) -> None:
        detail = self.runner.test_performance()
        self.assertIn("logical_requests=100", detail)
        self.assertIn("p95=", detail)

    def test_cleanup_only_removes_current_prefix(self) -> None:
        own_key = self.runner.key("owned")
        outside_key = "another-application:key"
        self.runner.client.set(own_key, "value")
        self.runner.client.set(outside_key, "keep")

        self.runner.cleanup()

        self.assertEqual(self.runner.client.exists(own_key), 0)
        self.assertEqual(self.runner.client.get(outside_key), "keep")


if __name__ == "__main__":
    unittest.main()
