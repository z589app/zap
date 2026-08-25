#!/usr/bin/env python3
"""
zap.py のテストスイート。標準ライブラリの unittest のみを使用。

実行方法:
    python3 -m unittest test_zap.py -v

構成:
  - Unit系: zap モジュールの関数を直接呼び出してロジックを検証(高速)
  - CLI系:  実際に `python3 zap.py ...` をサブプロセスとして実行し、
            標準出力・終了コード・.zap.config の中身まで含めて検証(結合テスト)

各テストは一時ディレクトリに cd してから実行し、テスト同士が
.zap.config を共有しないようにしている。
"""
import os
import sys
import time
import json
import shutil
import tempfile
import unittest
import subprocess

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import zap  # noqa: E402


ZAP_PY = os.path.join(os.path.dirname(os.path.abspath(__file__)), "zap.py")


# ---------------------------------------------------------------------------
# 共通ヘルパー
# ---------------------------------------------------------------------------

class TempDirTestCase(unittest.TestCase):
    """各テストごとに空の一時ディレクトリへ cd する基底クラス"""

    def setUp(self):
        self._orig_cwd = os.getcwd()
        self._tmpdir = tempfile.mkdtemp(prefix="zap-test-")
        os.chdir(self._tmpdir)

    def tearDown(self):
        os.chdir(self._orig_cwd)
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def run_zap(self, *args):
        """zap.py をサブプロセスとして実行し、CompletedProcess を返す"""
        return subprocess.run(
            [sys.executable, ZAP_PY, *args],
            cwd=self._tmpdir,
            capture_output=True,
            text=True,
        )


# ---------------------------------------------------------------------------
# Unit: 履歴(history)
# ---------------------------------------------------------------------------

class TestHistoryUnit(TempDirTestCase):
    def test_record_and_dedupe(self):
        config = zap.load_config()
        zap.record_history(config, "ls -ltr")
        time.sleep(0.002)
        zap.record_history(config, "npm run build")
        time.sleep(0.002)
        zap.record_history(config, "ls -ltr")

        deduped = zap.get_deduped_history(config)
        # 古い順で返る。ls -ltr は2回実行され、最後の実行が末尾(最新)に来る
        self.assertEqual([c for c, _, _ in deduped], ["npm run build", "ls -ltr"])
        counts = {c: n for c, _, n in deduped}
        self.assertEqual(counts["ls -ltr"], 2)
        self.assertEqual(counts["npm run build"], 1)

    def test_get_history_cmd_numbering(self):
        config = zap.load_config()
        zap.record_history(config, "cmd-a")
        time.sleep(0.002)
        zap.record_history(config, "cmd-b")
        time.sleep(0.002)
        zap.record_history(config, "cmd-c")

        # -1 が最新(cmd-c)、-3 が最も古い(cmd-a)
        self.assertEqual(zap.get_history_cmd(config, 1), "cmd-c")
        self.assertEqual(zap.get_history_cmd(config, 2), "cmd-b")
        self.assertEqual(zap.get_history_cmd(config, 3), "cmd-a")

    def test_get_history_cmd_out_of_range_exits(self):
        config = zap.load_config()
        zap.record_history(config, "only-one")
        with self.assertRaises(SystemExit):
            zap.get_history_cmd(config, 5)

    def test_hist_clear_removes_log_section(self):
        config = zap.load_config()
        zap.record_history(config, "cmd-a")
        self.assertTrue(config.has_section("log"))
        zap.cmd_hist(config, ["clear"])
        self.assertFalse(config.has_section("log"))


# ---------------------------------------------------------------------------
# Unit: alias 登録・展開
# ---------------------------------------------------------------------------

class TestAliasUnit(TempDirTestCase):
    def test_set_and_get_alias_roundtrip(self):
        config = zap.load_config()
        zap.set_alias_cmds(config, "super-ls", ["ls -ltr"])

        # 再読込しても同じ内容が取れる(plain textストレージの往復確認)
        config2 = zap.load_config()
        self.assertEqual(zap.get_alias_cmds(config2, "super-ls"), ["ls -ltr"])

    def test_compound_alias_storage_is_human_editable(self):
        config = zap.load_config()
        zap.set_alias_cmds(config, "full-test", ["@build", "@test"])
        with open(zap.CONFIG_FILE, encoding="utf-8") as f:
            content = f.read()
        # JSON ではなく素の "@build + @test" 形式で保存されていること
        self.assertIn("full-test = @build + @test", content)

    def test_hand_written_config_is_parsed(self):
        with open(zap.CONFIG_FILE, "w", encoding="utf-8") as f:
            f.write(
                "[alias]\n"
                "build = echo BUILD\n"
                "fetch = echo page={default}\n"
                "full-test = @build + @fetch --each t1,t2\n"
            )
        config = zap.load_config()
        self.assertEqual(zap.get_alias_cmds(config, "build"), ["echo BUILD"])
        self.assertEqual(zap.get_alias_cmds(config, "fetch"), ["echo page={default}"])
        full = zap.get_alias_cmds(config, "full-test")
        self.assertEqual(full[0], "@build")
        self.assertEqual(full[1], {"cmd": "@fetch", "each": ["t1", "t2"]})

    def test_alias_get_missing_returns_none(self):
        config = zap.load_config()
        self.assertIsNone(zap.get_alias_cmds(config, "does-not-exist"))

    def test_alias_rm(self):
        config = zap.load_config()
        zap.set_alias_cmds(config, "foo", ["echo hi"])
        zap.cmd_alias(config, ["rm", "@foo"])
        config2 = zap.load_config()
        self.assertIsNone(zap.get_alias_cmds(config2, "foo"))


class TestFlattenAliasUnit(TempDirTestCase):
    def _seed(self):
        config = zap.load_config()
        zap.set_alias_cmds(config, "build", ["echo BUILD"])
        zap.set_alias_cmds(config, "test", ["echo TEST"])
        zap.set_alias_cmds(config, "deploy", ["echo DEPLOY"])
        zap.set_alias_cmds(config, "full-test", ["@build", "@test", "@deploy"])
        return zap.load_config()

    def test_flatten_simple(self):
        config = zap.load_config()
        zap.set_alias_cmds(config, "greet", ["echo hi"])
        result = zap.flatten_alias(config, "greet")
        self.assertEqual(result, [{"cmd": "echo hi", "each": None}])

    def test_flatten_compound(self):
        config = self._seed()
        result = zap.flatten_alias(config, "full-test")
        self.assertEqual(
            [r["cmd"] for r in result], ["echo BUILD", "echo TEST", "echo DEPLOY"]
        )

    def test_flatten_with_exclude(self):
        config = self._seed()
        result = zap.flatten_alias(config, "full-test", exclude={"build"})
        self.assertEqual([r["cmd"] for r in result], ["echo TEST", "echo DEPLOY"])

    def test_flatten_missing_alias_exits(self):
        config = zap.load_config()
        with self.assertRaises(SystemExit):
            zap.flatten_alias(config, "not-registered")

    def test_flatten_circular_reference_exits(self):
        config = zap.load_config()
        zap.set_alias_cmds(config, "a", ["@b"])
        zap.set_alias_cmds(config, "b", ["@a"])
        with self.assertRaises(SystemExit):
            zap.flatten_alias(config, "a")

    def test_each_embedded_on_plain_command(self):
        config = zap.load_config()
        zap.set_alias_cmds(config, "fetch", [{"cmd": "echo page={default}", "each": ["a", "b"]}])
        result = zap.flatten_alias(config, "fetch")
        self.assertEqual(result, [{"cmd": "echo page={default}", "each": ["a", "b"]}])

    def test_each_embedded_on_alias_ref_applies_to_last_step_only(self):
        config = zap.load_config()
        zap.set_alias_cmds(config, "build", ["echo BUILD"])
        zap.set_alias_cmds(config, "fetch", ["echo page={default}"])
        zap.set_alias_cmds(config, "abc2", ["@build", "@fetch"])
        zap.set_alias_cmds(config, "a2", [{"cmd": "@abc2", "each": ["x", "y"]}])

        result = zap.flatten_alias(config, "a2")
        self.assertEqual(result[0], {"cmd": "echo BUILD", "each": None})
        self.assertEqual(result[1], {"cmd": "echo page={default}", "each": ["x", "y"]})

    def test_each_embedded_on_alias_ref_without_placeholder_exits(self):
        config = zap.load_config()
        zap.set_alias_cmds(config, "noph", ["echo plain"])
        zap.set_alias_cmds(config, "a3", [{"cmd": "@noph", "each": ["1", "2"]}])
        with self.assertRaises(SystemExit):
            zap.flatten_alias(config, "a3")


# ---------------------------------------------------------------------------
# Unit: プレースホルダー・引数上書き
# ---------------------------------------------------------------------------

class TestPlaceholderAndOverrideUnit(unittest.TestCase):
    def test_apply_placeholder_replaces_all_occurrences(self):
        self.assertEqual(
            zap.apply_placeholder("echo {default} and {default}", "X"),
            "echo X and X",
        )

    def test_resolve_default_placeholder_uses_inner_text(self):
        self.assertEqual(
            zap.resolve_default_placeholder("echo {default}"),
            "echo default",
        )

    def test_resolve_default_placeholder_no_placeholder_noop(self):
        self.assertEqual(zap.resolve_default_placeholder("echo hi"), "echo hi")

    def test_apply_overrides_replaces_existing_flag(self):
        self.assertEqual(
            zap.apply_overrides("echo -n=0 hello", ["-n=1"]),
            "echo -n=1 hello",
        )

    def test_apply_overrides_appends_when_absent(self):
        self.assertEqual(
            zap.apply_overrides("echo hello", ["-n=1"]),
            "echo hello -n=1",
        )

    def test_split_segments_by_plus(self):
        self.assertEqual(
            zap.split_segments(["@a", "+", "@b", "work"]),
            [["@a"], ["@b", "work"]],
        )

    def test_build_alias_item_plain(self):
        self.assertEqual(zap.build_alias_item(["ls", "-ltr"]), "ls -ltr")

    def test_build_alias_item_with_each(self):
        self.assertEqual(
            zap.build_alias_item(["echo", "{default}", "--each", "a,b,c"]),
            {"cmd": "echo {default}", "each": ["a", "b", "c"]},
        )


# ---------------------------------------------------------------------------
# CLI結合テスト
# ---------------------------------------------------------------------------

class TestCLIBasic(TempDirTestCase):
    def test_simple_execution_records_history(self):
        r = self.run_zap("echo", "hello")
        self.assertEqual(r.returncode, 0)
        self.assertIn("hello", r.stdout)

        r = self.run_zap("hist")
        self.assertIn("echo hello", r.stdout)
        self.assertIn("-1", r.stdout)

    def test_history_run_by_number(self):
        self.run_zap("echo", "first")
        self.run_zap("echo", "second")
        r = self.run_zap("-1")
        self.assertEqual(r.returncode, 0)
        self.assertIn("second", r.stdout)

    def test_hist_clear(self):
        self.run_zap("echo", "hello")
        r = self.run_zap("hist", "clear")
        self.assertIn("cleared", r.stdout)
        r = self.run_zap("hist")
        self.assertIn("no history", r.stdout)

    def test_bare_zap_shows_usage_and_alias_list(self):
        r = self.run_zap()
        self.assertEqual(r.returncode, 0)
        self.assertIn("usage", r.stdout)
        self.assertIn("no alias", r.stdout)

    def test_dry_run_does_not_execute_or_record(self):
        r = self.run_zap("--dry", "echo", "hello")
        self.assertEqual(r.returncode, 0)
        self.assertNotIn("hello\n", r.stdout.replace("$ echo hello\n", ""))
        r = self.run_zap("hist")
        self.assertIn("no history", r.stdout)

    def test_failed_command_stops_with_nonzero_exit(self):
        r = self.run_zap("false")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("error", r.stderr)


class TestCLIAlias(TempDirTestCase):
    def test_alias_add_and_run(self):
        r = self.run_zap("alias", "add", "@super-ls", "ls", "-ltr")
        self.assertEqual(r.returncode, 0)
        self.assertIn("added", r.stdout)

        r = self.run_zap("@super-ls")
        self.assertEqual(r.returncode, 0)
        self.assertIn("$ ls -ltr", r.stdout)

    def test_alias_shorthand_a(self):
        r = self.run_zap("a", "add", "@foo", "echo", "bar")
        self.assertEqual(r.returncode, 0)
        r = self.run_zap("a")
        self.assertIn("@foo", r.stdout)

    def test_alias_bare_lists_when_empty_and_populated(self):
        r = self.run_zap("alias")
        self.assertIn("no alias", r.stdout)

        self.run_zap("alias", "add", "@foo", "echo", "bar")
        r = self.run_zap("alias")
        self.assertIn("@foo", r.stdout)

    def test_alias_rm(self):
        self.run_zap("alias", "add", "@foo", "echo", "bar")
        r = self.run_zap("alias", "rm", "@foo")
        self.assertIn("removed", r.stdout)
        r = self.run_zap("alias", "list")
        self.assertIn("no alias", r.stdout)

    def test_alias_add_from_history(self):
        self.run_zap("echo", "hello")
        r = self.run_zap("alias", "add", "@greet", "-1")
        self.assertEqual(r.returncode, 0)
        r = self.run_zap("--dry", "@greet")
        self.assertIn("echo hello", r.stdout)

    def test_compound_alias_and_exclude(self):
        self.run_zap("alias", "add", "@build", "echo", "BUILD")
        self.run_zap("alias", "add", "@test", "echo", "TEST")
        self.run_zap("alias", "add", "@full-test", "@build", "+", "@test")

        r = self.run_zap("@full-test")
        self.assertIn("BUILD", r.stdout)
        self.assertIn("TEST", r.stdout)

        r = self.run_zap("@full-test", "-@build")
        self.assertNotIn("BUILD", r.stdout)
        self.assertIn("TEST", r.stdout)

    def test_argument_override(self):
        self.run_zap("alias", "add", "@greet", "echo", "-n=0", "hello")
        r = self.run_zap("@greet", "-n=1")
        self.assertIn("-n=1 hello", r.stdout)

    def test_trailing_extras_appended_after_expansion(self):
        self.run_zap("alias", "add", "@super-ls", "ls", "-ltr")
        r = self.run_zap("--dry", "@super-ls", "work")
        self.assertIn("$ ls -ltr work", r.stdout)

    def test_each_via_cli(self):
        self.run_zap("alias", "add", "@fetch", "echo", "page={default}")
        r = self.run_zap("--dry", "@fetch", "--each", "1,2,3")
        self.assertIn("page=1", r.stdout)
        self.assertIn("page=2", r.stdout)
        self.assertIn("page=3", r.stdout)

    def test_each_without_cli_uses_default_literal(self):
        self.run_zap("alias", "add", "@fetch", "echo", "page={default}")
        r = self.run_zap("--dry", "@fetch")
        self.assertIn("page=default", r.stdout)

    def test_each_embedded_at_registration(self):
        self.run_zap("alias", "add", "@fetch", "echo", "page={default}", "--each", "a,b")
        r = self.run_zap("--dry", "@fetch")
        self.assertIn("page=a", r.stdout)
        self.assertIn("page=b", r.stdout)

    def test_cli_each_overrides_embedded_each(self):
        self.run_zap("alias", "add", "@fetch", "echo", "page={default}", "--each", "a,b")
        r = self.run_zap("--dry", "@fetch", "--each", "x,y")
        self.assertNotIn("page=a", r.stdout)
        self.assertIn("page=x", r.stdout)
        self.assertIn("page=y", r.stdout)

    def test_exclude_and_each_together_is_error(self):
        self.run_zap("alias", "add", "@build", "echo", "BUILD")
        self.run_zap("alias", "add", "@fetch", "echo", "page={default}")
        self.run_zap("alias", "add", "@full", "@build", "+", "@fetch")
        r = self.run_zap("@full", "-@build", "--each", "1,2")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("cannot be used together", r.stderr)

    def test_alias_ref_each_requires_placeholder(self):
        self.run_zap("alias", "add", "@noph", "echo", "plain")
        self.run_zap("alias", "add", "@a3", "@noph", "--each", "1,2")
        r = self.run_zap("--dry", "@a3")
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("{default}", r.stderr)

    def test_config_is_human_editable_ini(self):
        self.run_zap(
            "alias", "add", "@full-test", "@build", "+", "@fetch", "--each", "t1,t2"
        )
        with open(".zap.config", encoding="utf-8") as f:
            content = f.read()
        self.assertIn("[alias]", content)
        self.assertIn("full-test = @build + @fetch --each t1,t2", content)
        # JSON特有の記号(引用符・角括弧)が含まれていないこと
        self.assertNotIn('"', content)
        self.assertNotIn("[\"", content)


class TestCLICompletionHooks(TempDirTestCase):
    def test_complete_topcmds(self):
        r = self.run_zap("--complete-topcmds")
        self.assertEqual(r.returncode, 0)
        for kw in ("hist", "history", "alias", "a", "--dry", "--help"):
            self.assertIn(kw, r.stdout)

    def test_complete_aliases_lists_registered_names(self):
        self.run_zap("alias", "add", "@foo", "echo", "bar")
        self.run_zap("alias", "add", "@baz", "echo", "qux")
        r = self.run_zap("--complete-aliases")
        names = r.stdout.split()
        self.assertIn("foo", names)
        self.assertIn("baz", names)

    def test_complete_aliases_empty_when_none_registered(self):
        r = self.run_zap("--complete-aliases")
        self.assertEqual(r.returncode, 0)
        self.assertEqual(r.stdout.strip(), "")


if __name__ == "__main__":
    unittest.main()
