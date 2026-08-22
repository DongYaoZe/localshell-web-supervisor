import sqlite3
import tempfile
import unittest
from pathlib import Path

from lws.runtime_state import (
    durable_registry_path,
    resolve_default_registry_path,
)


class RuntimeStateTests(unittest.TestCase):
    def test_windows_default_is_outside_checkout_and_under_local_app_data(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            checkout = root / "clone"
            checkout.mkdir()
            appdata = root / "profile" / "Local"
            path = resolve_default_registry_path(
                cwd=checkout,
                environ={"LOCALAPPDATA": str(appdata)},
                platform="nt",
                home=root / "home",
            )
            self.assertEqual(
                path,
                appdata / "LocalShellWebSupervisor" / "registry.sqlite3",
            )
            self.assertFalse(str(path).startswith(str(checkout)))

    def test_lws_db_remains_an_exact_backward_compatible_override(self):
        with tempfile.TemporaryDirectory() as td:
            explicit = Path(td) / "explicit.sqlite3"
            path = resolve_default_registry_path(
                cwd=Path(td) / "clone",
                environ={"LWS_DB": str(explicit), "LOCALAPPDATA": str(Path(td) / "state")},
                platform="nt",
                home=Path(td) / "home",
            )
            self.assertEqual(path, explicit)

    def test_lws_state_home_can_relocate_implicit_runtime_state(self):
        with tempfile.TemporaryDirectory() as td:
            state_home = Path(td) / "durable"
            path = durable_registry_path(
                environ={"LWS_STATE_HOME": str(state_home)},
                platform="nt",
                home=Path(td) / "home",
            )
            self.assertEqual(path, state_home / "registry.sqlite3")

    def test_legacy_repo_registry_is_migrated_with_wal_content_and_left_intact(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            checkout = root / "clone"
            legacy = checkout / ".lws" / "registry.sqlite3"
            legacy.parent.mkdir(parents=True)
            source = sqlite3.connect(legacy)
            try:
                source.execute("PRAGMA journal_mode = WAL")
                source.execute("CREATE TABLE marker (value TEXT NOT NULL)")
                source.execute("INSERT INTO marker(value) VALUES ('durable')")
                source.commit()

                appdata = root / "profile" / "Local"
                resolved = resolve_default_registry_path(
                    cwd=checkout,
                    environ={"LOCALAPPDATA": str(appdata)},
                    platform="nt",
                    home=root / "home",
                )
                expected = appdata / "LocalShellWebSupervisor" / "registry.sqlite3"
                self.assertEqual(resolved, expected)
                self.assertTrue(expected.exists())
                self.assertTrue(legacy.exists())
                migrated = sqlite3.connect(expected)
                try:
                    row = migrated.execute("SELECT value FROM marker").fetchone()
                    self.assertEqual(row[0], "durable")
                finally:
                    migrated.close()
            finally:
                source.close()

    def test_fresh_legacy_watchdog_lease_defers_migration_to_avoid_split_brain(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            checkout = root / "clone"
            legacy = checkout / ".lws" / "registry.sqlite3"
            legacy.parent.mkdir(parents=True)
            conn = sqlite3.connect(legacy)
            try:
                conn.execute(
                    "CREATE TABLE watchdog_leases ("
                    "name TEXT PRIMARY KEY, owner_id TEXT, pid INTEGER, host TEXT, "
                    "started_at REAL, heartbeat_at REAL, expires_at REAL)"
                )
                conn.execute(
                    "INSERT INTO watchdog_leases VALUES ('default','owner',123,'host',0,90,200)"
                )
                conn.commit()
            finally:
                conn.close()

            appdata = root / "profile" / "Local"
            resolved = resolve_default_registry_path(
                cwd=checkout,
                environ={"LOCALAPPDATA": str(appdata)},
                platform="nt",
                home=root / "home",
                now=100,
            )
            self.assertEqual(resolved, legacy)
            self.assertFalse((appdata / "LocalShellWebSupervisor" / "registry.sqlite3").exists())

    def test_stale_legacy_watchdog_lease_allows_migration(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            checkout = root / "clone"
            legacy = checkout / ".lws" / "registry.sqlite3"
            legacy.parent.mkdir(parents=True)
            conn = sqlite3.connect(legacy)
            try:
                conn.execute(
                    "CREATE TABLE watchdog_leases ("
                    "name TEXT PRIMARY KEY, owner_id TEXT, pid INTEGER, host TEXT, "
                    "started_at REAL, heartbeat_at REAL, expires_at REAL)"
                )
                conn.execute(
                    "INSERT INTO watchdog_leases VALUES ('default','owner',123,'host',0,40,50)"
                )
                conn.execute("CREATE TABLE marker (value TEXT)")
                conn.execute("INSERT INTO marker VALUES ('kept')")
                conn.commit()
            finally:
                conn.close()

            appdata = root / "profile" / "Local"
            expected = appdata / "LocalShellWebSupervisor" / "registry.sqlite3"
            resolved = resolve_default_registry_path(
                cwd=checkout,
                environ={"LOCALAPPDATA": str(appdata)},
                platform="nt",
                home=root / "home",
                now=100,
            )
            self.assertEqual(resolved, expected)
            migrated = sqlite3.connect(expected)
            try:
                self.assertEqual(migrated.execute("SELECT value FROM marker").fetchone()[0], "kept")
            finally:
                migrated.close()

    def test_existing_durable_registry_always_wins_after_reclone(self):
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            appdata = root / "profile" / "Local"
            durable = appdata / "LocalShellWebSupervisor" / "registry.sqlite3"
            durable.parent.mkdir(parents=True)
            durable.write_bytes(b"already durable")
            fresh_clone = root / "fresh-clone"
            fresh_clone.mkdir()

            resolved = resolve_default_registry_path(
                cwd=fresh_clone,
                environ={"LOCALAPPDATA": str(appdata)},
                platform="nt",
                home=root / "home",
            )
            self.assertEqual(resolved, durable)


if __name__ == "__main__":
    unittest.main()
