import copy
import os
import sys
import unittest
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT_ROOT))

from redis_instance_test import DEFAULT_CONFIG, RedisTestRunner, validate_config  # noqa: E402


def env_bool(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


@unittest.skipUnless(
    os.getenv("REDIS_INTEGRATION_HOST"),
    "Set REDIS_INTEGRATION_HOST to run tests against a real Redis instance",
)
class RealRedisIntegrationTests(unittest.TestCase):
    def setUp(self) -> None:
        self.config = copy.deepcopy(DEFAULT_CONFIG)
        connection = self.config["connection"]
        authentication = self.config["authentication"]
        expectations = self.config["expectations"]

        connection["host"] = os.environ["REDIS_INTEGRATION_HOST"]
        connection["port"] = int(os.getenv("REDIS_INTEGRATION_PORT", "6379"))
        connection["db"] = int(os.getenv("REDIS_INTEGRATION_DB", "0"))
        connection["ssl"] = env_bool("REDIS_INTEGRATION_SSL")
        connection["ssl_cert_reqs"] = os.getenv(
            "REDIS_INTEGRATION_SSL_CERT_REQS", "required"
        )
        connection["ssl_check_hostname"] = env_bool(
            "REDIS_INTEGRATION_SSL_CHECK_HOSTNAME", True
        )
        connection["ssl_ca_certs"] = os.getenv("REDIS_INTEGRATION_SSL_CA_CERTS")
        connection["ssl_certfile"] = os.getenv("REDIS_INTEGRATION_SSL_CERTFILE")
        connection["ssl_keyfile"] = os.getenv("REDIS_INTEGRATION_SSL_KEYFILE")

        architecture = os.getenv("REDIS_INTEGRATION_ARCHITECTURE", "standalone")
        expectations["architecture"] = architecture
        expectations["version_prefix"] = os.getenv("REDIS_INTEGRATION_VERSION_PREFIX")
        expectations["replicas"] = int(os.getenv("REDIS_INTEGRATION_REPLICAS", "0"))
        expectations["max_replica_lag_seconds"] = int(
            os.getenv("REDIS_INTEGRATION_MAX_REPLICA_LAG", "10")
        )
        self.config["execution"]["profile"] = (
            "cluster" if architecture == "cluster" else "standard"
        )

        self.password = os.getenv("REDIS_INTEGRATION_PASSWORD")
        if self.password:
            authentication["mode"] = "environment"
            authentication["username"] = os.getenv("REDIS_INTEGRATION_USERNAME")
            authentication["required"] = True
        else:
            authentication["mode"] = "none"
            authentication["username"] = None
            authentication["required"] = False

        self.config["execution"]["ttl_seconds"] = 1
        self.config["atomicity"].update(
            {"requests": 100, "concurrency": 4, "max_duration_seconds": 30}
        )
        self.config["performance"].update(
            {
                "requests": 100,
                "concurrency": 4,
                "value_size": 32,
                "keyspace": 20,
                "max_duration_seconds": 30,
            }
        )
        validate_config(self.config)
        self.runner = RedisTestRunner(self.config, password=self.password)

    def tearDown(self) -> None:
        try:
            result = self.runner.run_case("cleanup", self.runner.cleanup)
            if result.status == "FAIL":
                self.fail(f"Integration cleanup failed: {result.detail}")
        finally:
            self.runner.close()

    def test_data_plane_and_topology(self) -> None:
        suites = [
            "network",
            "ping",
            "string",
            "hash",
            "list",
            "set",
            "zset",
            "ttl",
            "lua",
            "negative",
            "atomicity",
        ]
        if self.config["authentication"]["required"]:
            suites.insert(1, "authentication")

        architecture = self.config["expectations"]["architecture"]
        if architecture == "cluster":
            suites.append("cluster")
        else:
            suites.extend(["server", "transaction", "persistence", "health"])
        if architecture == "master-slave":
            suites.append("replication")
        if env_bool("REDIS_INTEGRATION_PERFORMANCE"):
            suites.append("performance")

        for suite_name in suites:
            result = None
            with self.subTest(suite=suite_name):
                result = self.runner.run_case(
                    suite_name,
                    self.runner.suites[suite_name],
                )
                self.assertNotEqual(
                    result.status,
                    "FAIL",
                    f"{suite_name}: {result.detail}",
                )
            if result.status == "FAIL" and suite_name in {"network", "ping"}:
                break


if __name__ == "__main__":
    unittest.main()
