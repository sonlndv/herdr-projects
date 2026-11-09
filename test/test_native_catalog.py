"""Native catalogs and launches do not steal sessions or infer background settlement."""
import home_guard  # noqa: F401
import json
import os
from pathlib import Path
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "mods" / "tasks" / "bin"))
sys.path.insert(0, str(ROOT / "bin"))
from herdr_task import agents, launch, revive, sessions, settings
from shepherdr_native import launch as native_launch
from shepherdr_native.common import NativeError


class NativeCatalog(unittest.TestCase):
    def test_native_role_argv_keeps_native_policy(self):
        for kind in settings.NATIVE_KINDS:
            argv = settings.argv_for({"agent": kind, "model": "provider/model", "effort": "high"})
            self.assertEqual(argv[:2], ["shepherdr-native-run", kind])
            self.assertEqual(settings.agent_key(argv), kind)
            self.assertEqual(settings.argv_model(argv), ("provider/model", "high"))
            self.assertNotIn("--no-daemon", argv)
            self.assertNotIn("--dangerously-skip-permissions", argv)
            self.assertEqual(launch.own_argv(argv), argv)
        with self.assertRaises(Exception):
            settings.argv_for({"agent": "omp", "perm": "full"})

    def test_unknown_settlement_never_makes_ready(self):
        worker = {"address": "t1", "status": "idle", "labels": {"herdr.agent": "omp"}}
        task = {"state": "ready", "agents": [{"address": "t1", "agent": "omp", "session_owner": "native"}]}
        with patch.object(agents, "native_state", return_value={"settled": None, "pendingAsyncWork": None}):
            self.assertTrue(agents.not_at_rest(task, [worker]))
            self.assertEqual(agents.task_status(task, [worker])[0], "working")
        self.assertTrue(agents.not_at_rest(task, []))
        with patch.object(agents, "native_state", return_value={"settled": True, "pendingAsyncWork": False}):
            self.assertFalse(agents.not_at_rest(task, [worker]))

    def test_native_session_lookup_is_exact_and_never_a_claude_log(self):
        with patch.object(settings, "native_request", return_value={"sessionId": "exact", "sessionFile": "/native/one"}) as request:
            self.assertEqual(sessions.session_of(123, "omp"), ("exact", "/native/one"))
            request.assert_called_once_with("omp", "discover", {"pid": 123})
        self.assertIsNone(sessions.find_log({"agent": "unknown", "session": "a"}, "T-1", ["/same/cwd"]))
        self.assertIsNone(sessions.find_log({"agent": "omp", "session": "a"}, "T-1", ["/same/cwd"]))
        self.assertIsNone(agents.agent_log({"agents": [{"agent": "omp", "log": "/native/journal"}]}))

    def test_native_children_are_not_revived_by_shepherdr(self):
        child = {"address": "child", "labels": {"herdr.task": "task", "herdr.runtime.owner": "native"}}
        self.assertFalse(revive.watched(child))
        self.assertIsNone(revive.plan(child, {"kind": "omp", "session": "child"}, set()))
        self.assertEqual(revive.base_argv(["omp", "--resume", "native-child"]), (None, None))

    def test_native_resume_keeps_exact_file_and_profile(self):
        argv = settings.argv_for({"agent": "omp", "profile": "work", "model": "p/m"})
        resumed = launch.resume_argv(argv, "/exact/session.jsonl")
        self.assertEqual(json.loads(resumed[2]), {"profile": "work", "model": "p/m", "sessionFile": "/exact/session.jsonl"})
        self.assertNotIn("--no-daemon", resumed)

    def test_omo_fresh_launch_is_explicitly_isolated(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(native_launch, "state_dir", return_value=Path(directory)), patch.object(native_launch.shutil, "which", side_effect=lambda kind: "/native/" + kind):
            first = native_launch.prepare("omo", {"model": "p/m", "effort": "off"})
            second = native_launch.prepare("omo", {})
            self.assertNotEqual(first["sessionFile"], second["sessionFile"])
            self.assertIn("--session", first["argv"])
            self.assertEqual(first["argv"][first["argv"].index("--thinking") + 1], "off")
            self.assertNotIn("--no-extensions", first["argv"])
            self.assertNotIn("--no-tools", first["argv"])
            with self.assertRaises(NativeError):
                native_launch.prepare("omo", {"profile": "not-supported"})

    def test_omp_new_overlay_does_not_modify_native_profile_config(self):
        with tempfile.TemporaryDirectory() as directory, patch.object(native_launch, "state_dir", return_value=Path(directory)), patch.object(native_launch.shutil, "which", return_value="/native/omp"), patch.object(Path, "is_file", return_value=True):
            result = native_launch.prepare("omp", {"profile": "work", "message": "first"})
            argv = result["argv"]
            self.assertTrue(argv[argv.index("--extension") + 1].endswith("/bin/shepherdr_native/omp-extension.ts"))
            overlay = Path(argv[argv.index("--config") + 1])
            self.assertEqual(overlay.read_text(), "autoResume: false\n")
            self.assertEqual(overlay.stat().st_mode & 0o077, 0)
            self.assertEqual(argv[-1], "first")
            self.assertNotIn("--mode", argv)

    def test_native_hermes_profile_selection_preserves_sticky_and_pinned_contexts(self):
        with tempfile.TemporaryDirectory() as directory:
            home = Path(directory)
            root = home / ".hermes"
            selected, pinned = root / "profiles" / "selected", root / "profiles" / "pinned"
            selected.mkdir(parents=True)
            pinned.mkdir()
            (root / "active_profile").write_text("selected\n")
            backend = Mock()
            backend.handle.return_value = {"sessionId": "fresh-session"}
            with patch.object(native_launch.Path, "home", return_value=home), \
                 patch.object(native_launch.shutil, "which", return_value="/native/omh"), \
                 patch.object(native_launch.importlib, "import_module", return_value=backend):
                with patch.dict(os.environ, {"HERMES_HOME": ""}):
                    default = native_launch.prepare("omh", {})
                with patch.dict(os.environ, {"HERMES_HOME": str(pinned)}):
                    inherited = native_launch.prepare("omh", {})
                with patch.dict(os.environ, {"HERMES_HOME": str(pinned)}):
                    explicit = native_launch.prepare("omh", {"hermesHome": str(selected)})
            self.assertEqual(default["hermesHome"], str(selected))
            self.assertEqual(inherited["hermesHome"], str(pinned))
            self.assertEqual(explicit["hermesHome"], str(selected))




class NativeCustomArguments(unittest.TestCase):
    def test_custom_cli_flags_preserved_but_never_automatically_replayed(self):
        argv = settings.agent_argv(None, json.dumps(["omp", "--extension", "/custom.ts", "user initial prompt"]))
        self.assertEqual(argv[:2], ["shepherdr-native-run", "omp"])
        self.assertEqual(json.loads(argv[2])["nativeArgs"], ["--extension", "/custom.ts", "user initial prompt"])
        self.assertEqual(revive.base_argv(argv), (None, None))
        with self.assertRaises(Exception):
            launch.resume_argv(argv, "/exact/session.jsonl")

    def test_native_session_controls_are_exact_and_unambiguous(self):
        for args in (["--continue"], ["--continue=old"], ["--resume", "prefix"], ["--no-extensions"]):
            with tempfile.TemporaryDirectory() as directory, patch.object(native_launch, "state_dir", return_value=Path(directory)), patch.object(native_launch.shutil, "which", return_value="/native/omp"), patch.object(Path, "is_file", return_value=True):
                with self.assertRaises(NativeError):
                    native_launch.prepare("omp", {"nativeArgs": args})


if __name__ == "__main__":
    unittest.main()
