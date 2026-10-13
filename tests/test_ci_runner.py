import io
import unittest
from contextlib import redirect_stderr
from unittest.mock import MagicMock, patch

from scripts import run_ci_tests


class CiRunnerTests(unittest.TestCase):
    def test_missing_database_url_fails(self):
        with patch.dict(run_ci_tests.os.environ, {}, clear=True):
            with self.assertRaisesRegex(RuntimeError, "K8S_LAB_TEST_POSTGRES_URL"):
                run_ci_tests.check_environment()

    def test_non_postgres_url_fails(self):
        with patch.dict(run_ci_tests.os.environ, {"K8S_LAB_TEST_POSTGRES_URL": "sqlite://"}):
            with self.assertRaisesRegex(RuntimeError, "必须使用 PostgreSQL"):
                run_ci_tests.check_environment()

    def test_invalid_url_does_not_expose_credential(self):
        with patch.dict(run_ci_tests.os.environ, {"K8S_LAB_TEST_POSTGRES_URL": "secret-data"}):
            with self.assertRaisesRegex(RuntimeError, "URL 无效") as result:
                run_ci_tests.check_environment()
        self.assertNotIn("secret-data", str(result.exception))

    def test_missing_node_fails(self):
        with patch.dict(run_ci_tests.os.environ, {
                "K8S_LAB_TEST_POSTGRES_URL": "postgresql+pg8000://localhost/test"}), \
                patch.object(run_ci_tests.shutil, "which", return_value=None):
            with self.assertRaisesRegex(RuntimeError, "Node.js"):
                run_ci_tests.check_environment()

    def test_database_connection_failure_is_safe_and_disposes_engine(self):
        engine = MagicMock()
        engine.connect.side_effect = RuntimeError("secret-password")
        with patch.dict(run_ci_tests.os.environ, {
                "K8S_LAB_TEST_POSTGRES_URL": "postgresql+pg8000://localhost/test"}), \
                patch.object(run_ci_tests.shutil, "which", return_value="/usr/bin/node"), \
                patch.object(run_ci_tests, "create_engine", return_value=engine):
            with self.assertRaisesRegex(RuntimeError, "无法连接") as result:
                run_ci_tests.check_environment()
        self.assertNotIn("secret-password", str(result.exception))
        engine.dispose.assert_called_once()

    def test_environment_error_stops_discovery(self):
        with patch.object(run_ci_tests, "check_environment", side_effect=RuntimeError("unavailable")), \
                patch.object(run_ci_tests.unittest.defaultTestLoader, "discover") as discover, \
                redirect_stderr(io.StringIO()):
            self.assertEqual(1, run_ci_tests.main())
        discover.assert_not_called()

    def test_empty_suite_fails(self):
        with redirect_stderr(io.StringIO()):
            self.assertEqual(1, run_ci_tests.run_suite(unittest.TestSuite()))

    def test_skipped_test_fails(self):
        def skip():
            raise unittest.SkipTest("test skip")

        with redirect_stderr(io.StringIO()):
            suite = unittest.TestSuite([unittest.FunctionTestCase(skip)])
            self.assertEqual(1, run_ci_tests.run_suite(suite))

    def test_passing_suite_succeeds(self):
        with redirect_stderr(io.StringIO()):
            suite = unittest.TestSuite([unittest.FunctionTestCase(lambda: None)])
            self.assertEqual(0, run_ci_tests.run_suite(suite))

    def test_failing_test_fails(self):
        def fail():
            raise AssertionError("expected test failure")

        with redirect_stderr(io.StringIO()):
            suite = unittest.TestSuite([unittest.FunctionTestCase(fail)])
            self.assertEqual(1, run_ci_tests.run_suite(suite))
