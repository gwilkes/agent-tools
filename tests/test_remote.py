"""Hosts: a real `gci serve` over loopback, serving a second scratch tree and
driven through the tools; a server in this process, for what it guards; and
the client's side of the wire, against stand-ins for a host."""

import contextlib
import io
import os
import re
import subprocess
import tarfile
import tempfile
import threading
import unittest
from pathlib import Path

from gci import core
from gci.client import Remote, TransportError, converge_version, follow_job
from gci.core import CONFIG, LOCAL, VERSION, ToolError
from gci.server import HostServer, Job, own_module
from gci.sync import pack_sources
from gci.wire import DEFAULT_KEY_ENV, FILES, MANIFEST, api_key, missing_key
from tests.helpers import ProjectCase, job_id, py


class RemoteTest(ProjectCase):
    @classmethod
    def setUpClass(cls):
        cls.saved_key = os.environ.get(DEFAULT_KEY_ENV)
        os.environ[DEFAULT_KEY_ENV] = os.urandom(8).hex()
        cls.far_dir = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.far = Path(cls.far_dir.name).resolve()
        argv, env = core.self_command("serve", "--port", "0", "--bind", "127.0.0.1")
        cls.server = subprocess.Popen(
            argv,
            cwd=cls.far,
            env={**env, "GCI_ROOT": str(cls.far)},
            stdout=subprocess.PIPE,
            stderr=subprocess.STDOUT,
            text=True,
        )
        first = cls.server.stdout.readline()
        found = re.search(r"port (\d+)", first)
        if not found:
            cls.tearDownClass()
            raise RuntimeError(f"the server did not start: {first}")
        cls.port = int(found.group(1))
        # Its log must go somewhere, or a full pipe stalls it mid-request.
        threading.Thread(target=cls.server.stdout.read, daemon=True).start()

    @classmethod
    def tearDownClass(cls):
        cls.server.kill()
        cls.server.wait()
        cls.server.stdout.close()
        cls.far_dir.cleanup()
        if cls.saved_key is None:
            os.environ.pop(DEFAULT_KEY_ENV, None)
        else:
            os.environ[DEFAULT_KEY_ENV] = cls.saved_key

    def setUp(self):
        super().setUp()
        settings = self.settings()
        settings["scripts"]["cat"] = {
            "args": True,
            "command": py(
                "import pathlib, sys; print(pathlib.Path(sys.argv[1]).read_text())"
            ),
        }
        settings["remote"] = {
            "port": self.port,
            "hosts": {"box": "127.0.0.1", "dead": "http://127.0.0.1:9"},
        }
        self.write_settings(settings)
        self.put("src/a.py", "x = 1\n")
        # A dotfile rule like this tree's own: .gci/ must travel regardless.
        self.put(".gitignore", ".*\n")

    def test_hosts_answer_and_a_dead_one_fails_the_call(self):
        text, failed = self.call("hosts")
        self.assertIn("box: up at", text)
        self.assertIn("dead: cannot reach", text)
        self.assertTrue(failed)
        self.ok("kill", "nothing running", hosts=["box"])

    def test_sync_sends_what_is_allowed_and_deletes_what_is_gone(self):
        self.put("src/far.txt", "remote hello\n")
        self.ok("sync", "would be sent", hosts=["box"], dry_run=True)
        self.ok(
            "run_script",
            "remote hello",
            name="cat",
            args=["src/far.txt"],
            hosts=["box"],
        )
        far = self.far
        self.assertEqual(
            (far / "src" / "a.py").read_bytes(),
            (self.root / "src" / "a.py").read_bytes(),
        )
        self.assertFalse((far / ".env").exists(), "a denied file synced")
        self.assertFalse((far / "secrets").exists(), "a denied directory synced")
        self.assertTrue(
            (far / CONFIG).is_file(), "the settings did not travel past a `.*` rule"
        )
        (self.root / "src" / "far.txt").unlink()
        self.ok("sync", "deleted 1", hosts=["box"])
        self.assertFalse(
            (far / "src" / "far.txt").exists(), "a deletion did not travel"
        )

    def test_lanes_and_unreachable_hosts(self):
        self.refused("run_script", "=== box: exit 3", name="fail", hosts=["box", LOCAL])
        self.refused(
            "run_script", "=== local: exit 3", name="fail", hosts=["box", LOCAL]
        )
        self.refused(
            "run_script", "=== dead: no exit code", name="hello", hosts=["dead"]
        )

    def test_a_remote_gauntlet_keeps_member_output_for_the_log(self):
        text, _ = self.call("run_script", name="gate", hosts=["box"])
        self.assertIn("=== box: exit 3", text)
        self.assertNotIn("--- hello", text)
        logs = sorted((self.root / "tmp" / "gci" / "logs").glob("box-gate-*.log"))
        self.assertTrue(logs and "--- hello" in logs[-1].read_text())
        self.assertTrue(any((self.far / "tmp" / "gci" / "logs").glob("gate-*.log")))

    def test_a_run_on_a_host_is_a_job_too(self):
        text = self.ok(
            "run_script", "=== box: ", name="hello", args=["far"], hosts=["box"], wait=0
        )
        self.assertRegex(text, re.compile(r"^log: \S+$", re.MULTILINE))
        text = self.ok("job", "=== box: exit 0 in", id=job_id(text), wait=60)
        self.assertIn("hi far", text)
        self.assertIn("ended in", text)

    def test_agents_reach_only_the_hosts_the_settings_name(self):
        self.refused(
            "run_script",
            "unknown host(s): 127.0.0.1",
            name="hello",
            hosts=["127.0.0.1"],
        )
        self.refused("sync", "unknown host(s): elsewhere", hosts=["elsewhere"])
        self.refused("sync", "nothing here to sync", hosts=["local"])
        self.refused("kill", "use job", hosts=["local"])

    def remote(self):
        return Remote(
            "box", f"http://127.0.0.1:{self.port}", os.environ[DEFAULT_KEY_ENV]
        )

    def test_paths_from_the_wire_cannot_leave_the_hosts_tree(self):
        with self.assertRaisesRegex(TransportError, "bad path: ../x"):
            self.remote().post_json("/manifest", {"files": {"../x": "h"}})
        for name in ("../evil.py", "/abs.py", "a/../../b.py"):
            buf = io.BytesIO()
            with tarfile.open(fileobj=buf, mode="w:gz") as tar:
                info = tarfile.TarInfo(name)
                info.size = 1
                tar.addfile(info, io.BytesIO(b"x"))
            with self.assertRaisesRegex(TransportError, "bad path"):
                self.remote().request(
                    "POST", "/files", buf.getvalue(), ctype="application/gzip"
                )
        self.assertFalse((self.far.parent / "evil.py").exists())

    def test_a_self_update_that_is_not_this_package_is_refused(self):
        with self.assertRaisesRegex(TransportError, "not this package"):
            self.remote().request(
                "POST",
                "/self-update",
                pack_sources({"x.py": b""}),
                ctype="application/gzip",
            )

    def test_a_wrong_key_is_refused(self):
        with self.assertRaises(TransportError) as caught:
            Remote("box", f"http://127.0.0.1:{self.port}", "wrong").get_json("/info")
        self.assertEqual(caught.exception.status, 401)


def tar_of(*names):
    """A /files body holding a one-byte file under each name, unchecked."""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as tar:
        for name in names:
            info = tarfile.TarInfo(name)
            info.size = 1
            tar.addfile(info, io.BytesIO(b"x"))
    return buf.getvalue()


class InProcessHostTest(ProjectCase):
    """A host server on this tree, in this process, with the package's own
    directory moved inside the tree, as it is in the repo that develops it."""

    def setUp(self):
        super().setUp()
        self.saved_pkg = core.PKG_DIR
        core.PKG_DIR = self.root / "gci"
        self.put("gci/__init__.py", "")
        # Its one line per request is for a person at the host, not the test's output.
        self.hush = contextlib.redirect_stdout(io.StringIO())
        self.hush.__enter__()
        self.server = HostServer(("127.0.0.1", 0), "k")
        threading.Thread(target=self.server.serve_forever, daemon=True).start()
        port = self.server.server_address[1]
        self.remote = Remote("here", f"http://127.0.0.1:{port}", "k")

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.hush.__exit__(None, None, None)
        core.PKG_DIR = self.saved_pkg
        super().tearDown()

    def test_tools_may_not_edit_the_package_unless_the_settings_say(self):
        self.put("gci/mod.py", "a = 1\n")
        self.refused("edit", "protected", path="gci/mod.py", old="a = 1", new="a = 2")
        settings = self.settings()
        settings["edit_self"] = True
        self.write_settings(settings)
        self.ok("edit", "+a = 2", path="gci/mod.py", old="a = 1", new="a = 2")

    def test_a_sync_never_deletes_or_writes_the_package(self):
        reply = self.remote.post_json(MANIFEST, {"files": {}, "dry": True})
        self.assertIn("blob.bin", reply["deleted"])
        self.assertNotIn("gci/__init__.py", reply["deleted"])
        with self.assertRaisesRegex(TransportError, "bad path: gci/x.py"):
            self.remote.request(
                "POST", FILES, tar_of("gci/x.py"), ctype="application/gzip"
            )
        self.assertFalse((self.root / "gci" / "x.py").exists())

    def test_a_refused_batch_writes_nothing(self):
        with self.assertRaisesRegex(TransportError, "bad path: ../x"):
            self.remote.request(
                "POST", FILES, tar_of("ok.txt", "../x"), ctype="application/gzip"
            )
        self.assertFalse((self.root / "ok.txt").exists())

    def test_a_symlink_on_the_host_cannot_carry_a_write_onto_a_denied_file(self):
        (self.root / "link.txt").symlink_to(self.root / ".env")
        with self.assertRaisesRegex(TransportError, "bad path: link.txt"):
            self.remote.request(
                "POST", FILES, tar_of("link.txt"), ctype="application/gzip"
            )
        self.assertEqual(self.text_of(".env"), "TOKEN=x\n")

    def test_a_bad_body_is_a_400_not_a_crash(self):
        for body in (b"not json", b"[1]", b'{"files": 3}'):
            with self.assertRaises(TransportError) as caught:
                self.remote.request("POST", MANIFEST, body)
            self.assertEqual(caught.exception.status, 400, body)


class HostJobTest(unittest.TestCase):
    def test_a_job_hands_out_lines_by_index_and_flags_quiet_ones(self):
        job = Job("x")
        job.append("one")
        job.append("watch", quiet=True)
        job.finish(0)
        self.assertEqual(job.read(1, 0), ([["watch", True]], 0))
        job.stop("first")
        job.stop("second")
        self.assertEqual(job.poll_stop(), "first")

    def test_own_modules_are_the_packages(self):
        self.assertTrue(own_module("gci", "gci/core.py"))
        self.assertFalse(own_module("gci", "gcix/core.py"))
        self.assertFalse(own_module(None, "gci/core.py"))


class KeysTest(unittest.TestCase):
    def test_the_key_comes_from_the_environment_or_a_file(self):
        cfg = {
            "remote": {"key_env": "GCI_TEST_NO_SUCH_KEY", "key_file": "/nonexistent"}
        }
        self.assertIsNone(api_key(cfg))
        self.assertIn("GCI_TEST_NO_SUCH_KEY", missing_key(cfg))

    def test_an_unreadable_key_file_is_not_a_missing_one(self):
        with tempfile.TemporaryDirectory() as directory:
            cfg = {"remote": {"key_env": "GCI_TEST_NO_SUCH_KEY", "key_file": directory}}
            with self.assertRaisesRegex(ToolError, "unreadable"):
                api_key(cfg)


class StubRemote(Remote):
    """A host that answers from a script of replies, or raises them."""

    def __init__(self, replies):
        super().__init__("stub", "http://stub", "k")
        self.replies = list(replies)

    def request(self, method, path, body=None, ctype="", timeout=0):
        reply = self.replies.pop(0) if len(self.replies) > 1 else self.replies[0]
        if isinstance(reply, Exception):
            raise reply
        return reply


class ClientTest(unittest.TestCase):
    def test_a_reply_must_be_an_object_with_its_keys(self):
        remote = StubRemote([b"{}"])
        with self.assertRaisesRegex(TransportError, "not a JSON object"):
            remote.json_of(b"[1]")
        with self.assertRaisesRegex(TransportError, "stub: the reply has no job"):
            remote.json_of(b"{}", ("job",))

    def test_following_a_job_rides_out_a_lost_host(self):
        lost = TransportError("gone")
        done = b'{"lines": [["a", false], ["q", true]], "next": 2, "code": 0}'
        out, quiet = [], []
        code = follow_job(
            StubRemote([lost, lost, done]), "j", out.append, quiet.append, 5, 0
        )
        self.assertEqual(code, 0)
        self.assertTrue(out[0].startswith("lost stub"), out)
        self.assertTrue(out[1].startswith("reconnected"), out)
        self.assertEqual((out[2:], quiet), (["a"], ["q"]))

    def test_following_gives_up_on_a_host_lost_too_long(self):
        with self.assertRaisesRegex(TransportError, "unreachable for 0.05s"):
            follow_job(
                StubRemote([TransportError("gone")]), "j", [].append, None, 0.05, 0.01
            )

    def test_a_refusal_is_not_retried(self):
        with self.assertRaisesRegex(TransportError, "no such job"):
            follow_job(
                StubRemote([TransportError("no such job", status=404)]), "j", [].append
            )

    def test_converging_waits_for_the_host_to_come_back_as_this_version(self):
        took = b'{"version": "new"}'
        back = f'{{"version": "{VERSION}", "platform": "x"}}'.encode()
        lines = []
        info = converge_version(
            StubRemote([took, TransportError("down"), back]), lines.append, 3
        )
        self.assertEqual(info["version"], VERSION)
        self.assertIn(f"stub restarted into {VERSION}", lines)
        stale = b'{"version": "old", "platform": "x"}'
        with self.assertRaisesRegex(TransportError, "still answers as old"):
            converge_version(StubRemote([took, stale]), lines.append, 0.2)


if __name__ == "__main__":
    unittest.main()
