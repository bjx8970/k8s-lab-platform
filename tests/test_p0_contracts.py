"""Fast P0 contract checks that do not pretend to validate PostgreSQL semantics."""

import json
from pathlib import Path
import tempfile
import unittest

from migrations.runner import MigrationError, discover_migrations, run_migrations
from migrations.versions.v0001_control_plane_p0 import _statements
from modules.control_plane.repositories import (
    EnvironmentApiRepository, EnvironmentStatusRepository, OperationExecutorRepository,
)
from modules.control_plane import tables

ROOT = Path(__file__).resolve().parents[1]
SCHEMA_DIR = ROOT / "schemas" / "control-plane"


class P0ContractTests(unittest.TestCase):
    def test_all_json_schemas_parse_and_have_draft_2020_12_dialect(self):
        files = sorted(SCHEMA_DIR.glob("*.json"))
        self.assertGreaterEqual(len(files), 11)
        for path in files:
            with self.subTest(path=path.name):
                schema = json.loads(path.read_text(encoding="utf-8"))
                self.assertEqual(schema["$schema"], "https://json-schema.org/draft/2020-12/schema")

    def test_field_alignment_schema_ddl_table(self):
        """Schema required fields must have corresponding DDL columns and table columns."""
        ddl = (ROOT / "migrations" / "versions" / "v0001_control_plane_p0.sql").read_text(encoding="utf-8")
        resource_schema = json.loads((SCHEMA_DIR / "resource.json").read_text(encoding="utf-8"))
        binding_schema = json.loads((SCHEMA_DIR / "binding.json").read_text(encoding="utf-8"))
        operation_schema = json.loads((SCHEMA_DIR / "operation.json").read_text(encoding="utf-8"))

        # Resource: driverId, attributes in schema -> DDL + table
        self.assertIn("driverId", resource_schema["allOf"][1]["properties"]["spec"]["required"])
        self.assertIn("driver_id", ddl)
        self.assertIn("driver_id", str(tables.resources.columns))

        # Resource: no connectionId (removed — connection is on Binding)
        self.assertNotIn("connectionId", resource_schema["allOf"][1]["properties"]["spec"]["required"])

        # Binding: driverId, externalKey in schema -> DDL
        self.assertIn("driverId", binding_schema["allOf"][1]["properties"]["spec"]["required"])
        self.assertIn("externalKey", binding_schema["allOf"][1]["properties"]["spec"]["required"])
        self.assertIn("driver_id VARCHAR(128) NOT NULL", ddl)
        self.assertIn("external_key VARCHAR(255) NOT NULL", ddl)
        self.assertIn("uq_rf_binding_identity ON rf_bindings(domain_id,driver_id,external_key)", ddl)

        # Operation: operationKey, attempt in schema -> DDL + table
        self.assertIn("operationKey", operation_schema["allOf"][1]["properties"]["spec"]["required"])
        self.assertIn("attempt", operation_schema["allOf"][1]["properties"]["spec"]["required"])
        self.assertIn("operation_key VARCHAR(255)", ddl)
        self.assertIn("attempt BIGINT", ddl)
        self.assertIn("UNIQUE(operation_key,attempt)", ddl.replace(" ", ""))
        self.assertIn("operation_key", str(tables.operations.columns))
        self.assertIn("attempt", str(tables.operations.columns))

        # Allocation: value is string only
        allocation = json.loads((SCHEMA_DIR / "allocation.json").read_text(encoding="utf-8"))
        val_schema = allocation["allOf"][1]["properties"]["spec"]["properties"]["value"]
        self.assertIn("$ref", val_schema)
        self.assertIn("VARCHAR(255)", ddl)

        # Logs: exactly-one owner
        self.assertIn("(operation_uid IS NOT NULL) <> (task_uid IS NOT NULL)", ddl)
        self.assertIn("redacted BOOLEAN NOT NULL CHECK(redacted)", ddl)

    def test_migration_and_sql_dependency_are_both_checksummed(self):
        migration = discover_migrations()[0]
        original = migration.checksum
        with tempfile.TemporaryDirectory() as directory:
            target = Path(directory)
            py = target / migration.path.name
            sql_name = migration.module.checksum_files[0]
            py.write_bytes(migration.path.read_bytes())
            (target / sql_name).write_bytes(migration.path.with_name(sql_name).read_bytes() + b"\n-- drift")
            self.assertNotEqual(discover_migrations(target)[0].checksum, original)

    def test_non_postgresql_migration_is_rejected(self):
        engine = type("Engine", (), {"dialect": type("Dialect", (), {"name": "sqlite"})()})()
        with self.assertRaises(MigrationError):
            run_migrations(engine, [])

    def test_repository_column_ownership_is_disjoint(self):
        self.assertNotIn("status", EnvironmentApiRepository.owned_columns)
        self.assertNotIn("observed_generation", EnvironmentApiRepository.owned_columns)
        self.assertNotIn("spec", EnvironmentStatusRepository.owned_columns)
        self.assertNotIn("generation", EnvironmentStatusRepository.owned_columns)
        self.assertFalse(hasattr(OperationExecutorRepository, "update_spec"))
        self.assertFalse(hasattr(OperationExecutorRepository, "update_environment"))

    def test_invalid_existing_database_config_fails_closed(self):
        source = (ROOT / "modules" / "db.py").read_text(encoding="utf-8")
        self.assertIn("except (OSError, json.JSONDecodeError) as exc", source)
        self.assertIn("数据库配置文件无效，服务拒绝启动", source)
        self.assertNotIn("except Exception:\n            pass\n    return None", source[:2500])

    def test_ddl_contains_postgresql_only_invariants(self):
        ddl = (ROOT / "migrations" / "versions" / "v0001_control_plane_p0.sql").read_text(encoding="utf-8")
        for required in (
            "JSONB", "pg_", "resource_version", "uq_cp_allocation_live",
            "uq_rf_operation_mutation", "claim_revision", "rf_domain_locks",
            "remote_job_id", "cp_outbox", "cp_schema_migrations",
            "uq_rf_binding_identity", "operation_key", "pending_external",
        ):
            with self.subTest(required=required):
                if required == "pg_":
                    runner = (ROOT / "migrations" / "runner.py").read_text(encoding="utf-8")
                    self.assertIn("pg_advisory_xact_lock", runner)
                elif required == "cp_schema_migrations":
                    runner = (ROOT / "migrations" / "runner.py").read_text(encoding="utf-8")
                    self.assertIn(required, runner)
                else:
                    self.assertIn(required, ddl)

    def test_sql_splitter_preserves_trigger_function_body(self):
        ddl = (ROOT / "migrations" / "versions" / "v0001_control_plane_p0.sql").read_text(encoding="utf-8")
        statements = _statements(ddl)
        function = [item for item in statements if item.lstrip().startswith("CREATE FUNCTION cp_reject_plan_spec_change")]
        self.assertEqual(1, len(function))
        self.assertIn("RAISE EXCEPTION", function[0])
        self.assertGreater(len(statements), 40)


if __name__ == "__main__":
    unittest.main()
