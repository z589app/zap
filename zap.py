#!/usr/bin/env python3
"""
zap - Zero dependency Alias Python
alias / npm run / make の置き換えとなるコマンドランチャー / タスクランナー
"""
import sys
import os
import re
import subprocess
import configparser
from datetime import datetime
from collections import OrderedDict

CONFIG_FILE = ".zap.config"


# ---------------------------------------------------------------------------
# config (.zap.config) の読み書き
# ---------------------------------------------------------------------------

def load_config():
    # delimiters=('=',) : タイムスタンプのキーに含まれる ':' がデフォルトの
    #   区切り文字と衝突するため '=' のみを区切りとする
    # optionxform = str : キーを小文字化させない(タイムスタンプの大文字小文字を保持)
    config = configparser.ConfigParser(interpolation=None, delimiters=("=",))
    config.optionxform = str
    if os.path.exists(CONFIG_FILE):
        config.read(CONFIG_FILE, encoding="utf-8")
    return config


def save_config(config):
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        config.write(f)


# ---------------------------------------------------------------------------
# 履歴 (log)
# ---------------------------------------------------------------------------

def record_history(config, cmd_str):
    if not config.has_section("log"):
        config.add_section("log")
    ts = datetime.now().isoformat(timespec="milliseconds")
    config.set("log", ts, cmd_str)
    save_config(config)


def get_raw_log_entries(config):
    """(timestamp, cmd) を古い順に返す"""
    if not config.has_section("log"):
        return []
    items = list(config.items("log"))
    items.sort(key=lambda kv: kv[0])
    return items


def get_deduped_history(config):
    """同一コマンドをまとめ、(cmd, last_ts, count) を古い順で返す"""
    entries = get_raw_log_entries(config)
    counts = OrderedDict()
    last_time = {}
    for ts, cmd in entries:
        counts[cmd] = counts.get(cmd, 0) + 1
        last_time[cmd] = ts
    ordered = sorted(counts.keys(), key=lambda c: last_time[c])
    return [(c, last_time[c], counts[c]) for c in ordered]


def get_history_cmd(config, n):
    """-N 指定のコマンド文字列を返す(N=1が最新)"""
    deduped = get_deduped_history(config)
    total = len(deduped)
    idx = total - n
    if n < 1 or idx < 0 or idx >= total:
        print(f"error: history -{n} not found", file=sys.stderr)
        sys.exit(1)
    return deduped[idx][0]


def cmd_hist(config, args):
    if args and args[0] == "clear":
        if config.has_section("log"):
            config.remove_section("log")
            save_config(config)
        print("history cleared")
        return

    deduped = get_deduped_history(config)
    total = len(deduped)
    if total == 0:
        print("(no history)")
        return
    for i, (cmd, ts, count) in enumerate(deduped):
        idx = total - i  # 古い順(先頭)ほど数字が大きく、直近(末尾)が -1
        dt = parse_timestamp(ts)
        print(f"-{idx}\t{cmd}\t{dt.strftime('%Y/%m/%d %H:%M:%S')}\t({count}回)")


def parse_timestamp(ts):
    # datetime.fromisoformat() は Python 3.7+ のため、3.6でも動くよう strptime を使う
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%f")


# ---------------------------------------------------------------------------
# alias
# ---------------------------------------------------------------------------

ALIAS_SECTION = "alias"


def get_alias_cmds(config, name):
    if not config.has_section(ALIAS_SECTION) or not config.has_option(ALIAS_SECTION, name):
        return None
    raw = config.get(ALIAS_SECTION, name)
    tokens = raw.split()
    if not tokens:
        return []
    return [build_alias_item(seg) for seg in split_segments(tokens)]


def set_alias_cmds(config, name, cmd_list):
    if not config.has_section(ALIAS_SECTION):
        config.add_section(ALIAS_SECTION)
    config.set(ALIAS_SECTION, name, format_cmd_list(cmd_list))
    save_config(config)


def flatten_alias(config, name, exclude=None, seen=None):
    """複合aliasを再帰的に展開する。各要素は
    {"cmd": <コマンド文字列>, "each": <登録時に埋め込まれたeach値のリスト or None>}
    の辞書として返す"""
    if seen is None:
        seen = set()
    if name in seen:
        print(f"error: circular alias reference detected at @{name}", file=sys.stderr)
        sys.exit(1)
    seen = seen | {name}

    cmd_list = get_alias_cmds(config, name)
    if cmd_list is None:
        print(f"error: alias @{name} not found", file=sys.stderr)
        sys.exit(1)

    result = []
    for item in cmd_list:
        if isinstance(item, dict):
            cmd_str = item.get("cmd", "")
            each_values = item.get("each")
        else:
            cmd_str = item
            each_values = None

        if cmd_str.startswith("@"):
            sub_name = cmd_str[1:]
            if exclude and sub_name in exclude:
                continue
            sub_list = flatten_alias(config, sub_name, exclude=exclude, seen=seen)
            if each_values:
                # 登録時に @参照 に埋め込まれた --each は、参照先の最後のステップにのみ適用する
                if not sub_list:
                    print(f"error: alias @{sub_name} has no executable command to apply --each to", file=sys.stderr)
                    sys.exit(1)
                last = sub_list[-1]
                if not PLACEHOLDER_RE.search(last["cmd"]):
                    print(
                        f"error: --each requires a {{default}} placeholder in the last step of "
                        f"@{sub_name} (\"{last['cmd']}\")",
                        file=sys.stderr,
                    )
                    sys.exit(1)
                last["each"] = each_values
            result.extend(sub_list)
        else:
            result.append({"cmd": cmd_str, "each": each_values})
    return result


def cmd_alias(config, args):
    if not args:
        # サブコマンド省略時は一覧表示(zap hist と同様の挙動)
        list_aliases(config)
        return

    sub = args[0]

    if sub == "add":
        if len(args) < 3:
            print("usage: zap alias add @<name> <command>  |  zap alias add @<name> -N", file=sys.stderr)
            sys.exit(1)
        name = args[1].lstrip("@")
        rest = args[2:]
        if len(rest) == 1 and re.match(r"^-\d+$", rest[0]):
            n = int(rest[0][1:])
            cmd_str = get_history_cmd(config, n)
            set_alias_cmds(config, name, [cmd_str])
            cmd_list = [cmd_str]
        else:
            # '+' 区切りがあれば複合aliasとして各セグメントを別要素にする
            # セグメント内に --each v1,v2,.. があれば、その値を埋め込んだ辞書要素にする
            cmd_list = []
            for seg in split_segments(rest):
                cmd_list.append(build_alias_item(seg))
            set_alias_cmds(config, name, cmd_list)
        print(f"alias @{name} added: {format_cmd_list(cmd_list)}")

    elif sub == "list":
        list_aliases(config)

    elif sub == "rm":
        if len(args) < 2:
            print("usage: zap alias rm @<name>", file=sys.stderr)
            sys.exit(1)
        name = args[1].lstrip("@")
        if config.has_section(ALIAS_SECTION) and config.has_option(ALIAS_SECTION, name):
            config.remove_option(ALIAS_SECTION, name)
            save_config(config)
            print(f"alias @{name} removed")
        else:
            print(f"error: alias @{name} not found", file=sys.stderr)
            sys.exit(1)

    else:
        print(f"error: unknown alias subcommand: {sub}", file=sys.stderr)
        sys.exit(1)


def build_alias_item(seg):
    """1セグメント分のトークン列から alias 保存用の要素を作る。
    '--each v1,v2,..' が含まれていれば、値を埋め込んだ辞書として返す"""
    if "--each" in seg:
        idx = seg.index("--each")
        if idx + 1 >= len(seg):
            print("error: --each requires a comma-separated value list", file=sys.stderr)
            sys.exit(1)
        each_values = seg[idx + 1].split(",")
        cmd_tokens = seg[:idx] + seg[idx + 2:]
        return {"cmd": " ".join(cmd_tokens), "each": each_values}
    return " ".join(seg)


def format_cmd_list(cmd_list):
    parts = []
    for item in cmd_list:
        if isinstance(item, dict):
            each = item.get("each")
            suffix = f" --each {','.join(each)}" if each else ""
            parts.append(f"{item.get('cmd', '')}{suffix}")
        else:
            parts.append(item)
    return " + ".join(parts)


def list_aliases(config):
    if not config.has_section(ALIAS_SECTION) or not config.options(ALIAS_SECTION):
        print("(no alias)")
        return
    for name in config.options(ALIAS_SECTION):
        cmd_list = get_alias_cmds(config, name)
        print(f"@{name}\t{format_cmd_list(cmd_list)}")


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------

def apply_overrides(cmd_str, overrides):
    """例: overrides=['-n=1'] のとき、'-n=0' を '-n=1' に置換(無ければ末尾に追加)"""
    if not overrides:
        return cmd_str
    tokens = cmd_str.split()
    for ov in overrides:
        key = ov.split("=", 1)[0]
        replaced = False
        for i, tok in enumerate(tokens):
            if tok.startswith(key + "="):
                tokens[i] = ov
                replaced = True
                break
        if not replaced:
            tokens.append(ov)
    return " ".join(tokens)


def run_and_record(config, cmd_str, dry):
    print(f"$ {cmd_str}")
    if dry:
        return
    result = subprocess.run(cmd_str, shell=True)
    if result.returncode != 0:
        print(f"error: command failed (exit code {result.returncode}): {cmd_str}", file=sys.stderr)
        sys.exit(result.returncode)
    record_history(config, cmd_str)


OVERRIDE_RE = re.compile(r"^-[A-Za-z0-9_]+=.+$")
PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")


def apply_placeholder(cmd_str, value):
    """--each指定時: {default} のようなプレースホルダーを value に置換する
    (中の文字列は無視し、全プレースホルダーを同じ value に置換)"""
    return PLACEHOLDER_RE.sub(value, cmd_str)


def resolve_default_placeholder(cmd_str):
    """--each未指定時: {default} のようなプレースホルダーを、
    中に書かれたデフォルト値(例: default)にそのまま置き換える"""
    return PLACEHOLDER_RE.sub(lambda m: m.group(1), cmd_str)


def run_segment(config, tokens, dry):
    """'+' で区切られた1タスク分を実行する"""
    if not tokens:
        return

    if tokens[0].startswith("@"):
        name = tokens[0][1:]
        rest = tokens[1:]

        excludes = []
        each_values = None
        overrides = []
        extras = []  # -@/--each/上書き以外のトークン。alias展開後、末尾に付与する

        i = 0
        while i < len(rest):
            t = rest[i]
            if t.startswith("-@"):
                excludes.append(t[2:])
            elif t == "--each":
                i += 1
                if i >= len(rest):
                    print("error: --each requires a comma-separated value list", file=sys.stderr)
                    sys.exit(1)
                each_values = rest[i].split(",")
            elif OVERRIDE_RE.match(t):
                overrides.append(t)
            else:
                extras.append(t)
            i += 1

        if excludes and each_values:
            print("error: -@ (exclude) and --each cannot be used together", file=sys.stderr)
            sys.exit(1)

        cmds = flatten_alias(config, name, exclude=set(excludes) if excludes else None)
        extras_str = " ".join(extras)
        last_idx = len(cmds) - 1

        for idx, item in enumerate(cmds):
            cmd_str = apply_overrides(item["cmd"], overrides)
            # extras(素のトークン)は、alias展開後の最後のコマンドの末尾に付与する
            if extras_str and idx == last_idx:
                cmd_str = f"{cmd_str} {extras_str}"
            # CLIの --each があればそれを優先。無ければ登録時に埋め込んだ each を使う
            effective_each = each_values if each_values else item["each"]
            if effective_each:
                for v in effective_each:
                    run_and_record(config, apply_placeholder(cmd_str, v), dry)
            else:
                run_and_record(config, resolve_default_placeholder(cmd_str), dry)
    else:
        cmd_str = " ".join(tokens)
        run_and_record(config, resolve_default_placeholder(cmd_str), dry)


def split_segments(tokens):
    """'+' でタスクを分割する"""
    segments = [[]]
    for t in tokens:
        if t == "+":
            segments.append([])
        else:
            segments[-1].append(t)
    return [seg for seg in segments if seg]


def run_chain(config, tokens, dry):
    for seg in split_segments(tokens):
        run_segment(config, seg, dry)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

USAGE = """usage:
  zap <command>                 コマンドを実行し、履歴に記録
  zap -N                        N個前の履歴を実行
  zap @<name>                   登録済みaliasを実行
  zap hist (history)            実行履歴を一覧表示
  zap hist clear                実行履歴をクリア
  zap alias (a) add @<name> <cmd>   aliasを登録
  zap alias (a) add @<name> -N      履歴からaliasを登録
  zap alias (a) list                alias一覧を表示
  zap alias (a) rm @<name>          aliasを削除
  zap @<name> -@<step>          複合alias実行時に指定ステップを除外
  zap @<name> --each v1,v2,..   {default}を置換しながら逐次実行(未指定時はdefaultがそのまま使われる)
  zap --dry <target>            実行内容を表示するのみ
"""


def main():
    argv = sys.argv[1:]
    config = load_config()

    if not argv:
        print(USAGE)
        list_aliases(config)
        sys.exit(0)

    if argv[0] in ("-h", "--help"):
        print(USAGE)
        sys.exit(0)

    if argv[0] in ("hist", "history"):
        cmd_hist(config, argv[1:])
        return
    if argv[0] in ("alias", "a"):
        cmd_alias(config, argv[1:])
        return

    dry = False
    if "--dry" in argv:
        dry = True
        argv = [a for a in argv if a != "--dry"]

    if len(argv) == 1 and re.match(r"^-\d+$", argv[0]):
        n = int(argv[0][1:])
        cmd_str = get_history_cmd(config, n)
        run_and_record(config, cmd_str, dry)
        return

    run_chain(config, argv, dry)


if __name__ == "__main__":
    main()

# ---------------------------------------------------------------------------
# 履歴 (log)
# ---------------------------------------------------------------------------

def record_history(config, cmd_str):
    if not config.has_section("log"):
        config.add_section("log")
    ts = datetime.now().isoformat(timespec="milliseconds")
    config.set("log", ts, cmd_str)
    save_config(config)


def get_raw_log_entries(config):
    """(timestamp, cmd) を古い順に返す"""
    if not config.has_section("log"):
        return []
    items = list(config.items("log"))
    items.sort(key=lambda kv: kv[0])
    return items


def get_deduped_history(config):
    """同一コマンドをまとめ、(cmd, last_ts, count) を古い順で返す"""
    entries = get_raw_log_entries(config)
    counts = OrderedDict()
    last_time = {}
    for ts, cmd in entries:
        counts[cmd] = counts.get(cmd, 0) + 1
        last_time[cmd] = ts
    ordered = sorted(counts.keys(), key=lambda c: last_time[c])
    return [(c, last_time[c], counts[c]) for c in ordered]


def get_history_cmd(config, n):
    """-N 指定のコマンド文字列を返す(N=1が最新)"""
    deduped = get_deduped_history(config)
    total = len(deduped)
    idx = total - n
    if n < 1 or idx < 0 or idx >= total:
        print(f"error: history -{n} not found", file=sys.stderr)
        sys.exit(1)
    return deduped[idx][0]


def cmd_hist(config, args):
    if args and args[0] == "clear":
        if config.has_section("log"):
            config.remove_section("log")
            save_config(config)
        print("history cleared")
        return

    deduped = get_deduped_history(config)
    total = len(deduped)
    if total == 0:
        print("(no history)")
        return
    for i, (cmd, ts, count) in enumerate(deduped):
        idx = total - i  # 古い順(先頭)ほど数字が大きく、直近(末尾)が -1
        dt = parse_timestamp(ts)
        print(f"-{idx}\t{cmd}\t{dt.strftime('%Y/%m/%d %H:%M:%S')}\t({count}回)")


def parse_timestamp(ts):
    # datetime.fromisoformat() は Python 3.7+ のため、3.6でも動くよう strptime を使う
    return datetime.strptime(ts, "%Y-%m-%dT%H:%M:%S.%f")


# ---------------------------------------------------------------------------
# alias
# ---------------------------------------------------------------------------

ALIAS_SECTION = "alias"


def get_alias_cmds(config, name):
    if not config.has_section(ALIAS_SECTION) or not config.has_option(ALIAS_SECTION, name):
        return None
    return json.loads(config.get(ALIAS_SECTION, name))


def set_alias_cmds(config, name, cmd_list):
    if not config.has_section(ALIAS_SECTION):
        config.add_section(ALIAS_SECTION)
    config.set(ALIAS_SECTION, name, json.dumps(cmd_list, ensure_ascii=False))
    save_config(config)


def flatten_alias(config, name, exclude=None, seen=None):
    """複合aliasを再帰的に展開する。各要素は
    {"cmd": <コマンド文字列>, "each": <登録時に埋め込まれたeach値のリスト or None>}
    の辞書として返す"""
    if seen is None:
        seen = set()
    if name in seen:
        print(f"error: circular alias reference detected at @{name}", file=sys.stderr)
        sys.exit(1)
    seen = seen | {name}

    cmd_list = get_alias_cmds(config, name)
    if cmd_list is None:
        print(f"error: alias @{name} not found", file=sys.stderr)
        sys.exit(1)

    result = []
    for item in cmd_list:
        if isinstance(item, dict):
            cmd_str = item.get("cmd", "")
            each_values = item.get("each")
        else:
            cmd_str = item
            each_values = None

        if cmd_str.startswith("@"):
            sub_name = cmd_str[1:]
            if exclude and sub_name in exclude:
                continue
            result.extend(flatten_alias(config, sub_name, exclude=exclude, seen=seen))
        else:
            result.append({"cmd": cmd_str, "each": each_values})
    return result


def cmd_alias(config, args):
    if not args:
        # サブコマンド省略時は一覧表示(zap hist と同様の挙動)
        list_aliases(config)
        return

    sub = args[0]

    if sub == "add":
        if len(args) < 3:
            print("usage: zap alias add @<name> <command>  |  zap alias add @<name> -N", file=sys.stderr)
            sys.exit(1)
        name = args[1].lstrip("@")
        rest = args[2:]
        if len(rest) == 1 and re.match(r"^-\d+$", rest[0]):
            n = int(rest[0][1:])
            cmd_str = get_history_cmd(config, n)
            set_alias_cmds(config, name, [cmd_str])
            cmd_list = [cmd_str]
        else:
            # '+' 区切りがあれば複合aliasとして各セグメントを別要素にする
            # セグメント内に --each v1,v2,.. があれば、その値を埋め込んだ辞書要素にする
            cmd_list = []
            for seg in split_segments(rest):
                cmd_list.append(build_alias_item(seg))
            set_alias_cmds(config, name, cmd_list)
        print(f"alias @{name} added: {format_cmd_list(cmd_list)}")

    elif sub == "list":
        list_aliases(config)

    elif sub == "rm":
        if len(args) < 2:
            print("usage: zap alias rm @<name>", file=sys.stderr)
            sys.exit(1)
        name = args[1].lstrip("@")
        if config.has_section(ALIAS_SECTION) and config.has_option(ALIAS_SECTION, name):
            config.remove_option(ALIAS_SECTION, name)
            save_config(config)
            print(f"alias @{name} removed")
        else:
            print(f"error: alias @{name} not found", file=sys.stderr)
            sys.exit(1)

    else:
        print(f"error: unknown alias subcommand: {sub}", file=sys.stderr)
        sys.exit(1)


def build_alias_item(seg):
    """1セグメント分のトークン列から alias 保存用の要素を作る。
    '--each v1,v2,..' が含まれていれば、値を埋め込んだ辞書として返す"""
    if "--each" in seg:
        idx = seg.index("--each")
        if idx + 1 >= len(seg):
            print("error: --each requires a comma-separated value list", file=sys.stderr)
            sys.exit(1)
        each_values = seg[idx + 1].split(",")
        cmd_tokens = seg[:idx] + seg[idx + 2:]
        return {"cmd": " ".join(cmd_tokens), "each": each_values}
    return " ".join(seg)


def format_cmd_list(cmd_list):
    parts = []
    for item in cmd_list:
        if isinstance(item, dict):
            each = item.get("each")
            suffix = f" --each {','.join(each)}" if each else ""
            parts.append(f"{item.get('cmd', '')}{suffix}")
        else:
            parts.append(item)
    return " + ".join(parts)


def list_aliases(config):
    if not config.has_section(ALIAS_SECTION) or not config.options(ALIAS_SECTION):
        print("(no alias)")
        return
    for name, cmd_json in config.items(ALIAS_SECTION):
        cmd_list = json.loads(cmd_json)
        print(f"@{name}\t{format_cmd_list(cmd_list)}")


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------

def apply_overrides(cmd_str, overrides):
    """例: overrides=['-n=1'] のとき、'-n=0' を '-n=1' に置換(無ければ末尾に追加)"""
    if not overrides:
        return cmd_str
    tokens = cmd_str.split()
    for ov in overrides:
        key = ov.split("=", 1)[0]
        replaced = False
        for i, tok in enumerate(tokens):
            if tok.startswith(key + "="):
                tokens[i] = ov
                replaced = True
                break
        if not replaced:
            tokens.append(ov)
    return " ".join(tokens)


def run_and_record(config, cmd_str, dry):
    print(f"$ {cmd_str}")
    if dry:
        return
    result = subprocess.run(cmd_str, shell=True)
    if result.returncode != 0:
        print(f"error: command failed (exit code {result.returncode}): {cmd_str}", file=sys.stderr)
        sys.exit(result.returncode)
    record_history(config, cmd_str)


OVERRIDE_RE = re.compile(r"^-[A-Za-z0-9_]+=.+$")
PLACEHOLDER_RE = re.compile(r"\{([^{}]*)\}")


def apply_placeholder(cmd_str, value):
    """--each指定時: {default} のようなプレースホルダーを value に置換する
    (中の文字列は無視し、全プレースホルダーを同じ value に置換)"""
    return PLACEHOLDER_RE.sub(value, cmd_str)


def resolve_default_placeholder(cmd_str):
    """--each未指定時: {default} のようなプレースホルダーを、
    中に書かれたデフォルト値(例: default)にそのまま置き換える"""
    return PLACEHOLDER_RE.sub(lambda m: m.group(1), cmd_str)


def run_segment(config, tokens, dry):
    """'+' で区切られた1タスク分を実行する"""
    if not tokens:
        return

    if tokens[0].startswith("@"):
        name = tokens[0][1:]
        rest = tokens[1:]

        excludes = []
        each_values = None
        overrides = []
        extras = []  # -@/--each/上書き以外のトークン。alias展開後、末尾に付与する

        i = 0
        while i < len(rest):
            t = rest[i]
            if t.startswith("-@"):
                excludes.append(t[2:])
            elif t == "--each":
                i += 1
                if i >= len(rest):
                    print("error: --each requires a comma-separated value list", file=sys.stderr)
                    sys.exit(1)
                each_values = rest[i].split(",")
            elif OVERRIDE_RE.match(t):
                overrides.append(t)
            else:
                extras.append(t)
            i += 1

        if excludes and each_values:
            print("error: -@ (exclude) and --each cannot be used together", file=sys.stderr)
            sys.exit(1)

        cmds = flatten_alias(config, name, exclude=set(excludes) if excludes else None)
        extras_str = " ".join(extras)
        last_idx = len(cmds) - 1

        for idx, item in enumerate(cmds):
            cmd_str = apply_overrides(item["cmd"], overrides)
            # extras(素のトークン)は、alias展開後の最後のコマンドの末尾に付与する
            if extras_str and idx == last_idx:
                cmd_str = f"{cmd_str} {extras_str}"
            # CLIの --each があればそれを優先。無ければ登録時に埋め込んだ each を使う
            effective_each = each_values if each_values else item["each"]
            if effective_each:
                for v in effective_each:
                    run_and_record(config, apply_placeholder(cmd_str, v), dry)
            else:
                run_and_record(config, resolve_default_placeholder(cmd_str), dry)
    else:
        cmd_str = " ".join(tokens)
        run_and_record(config, resolve_default_placeholder(cmd_str), dry)


def split_segments(tokens):
    """'+' でタスクを分割する"""
    segments = [[]]
    for t in tokens:
        if t == "+":
            segments.append([])
        else:
            segments[-1].append(t)
    return [seg for seg in segments if seg]


def run_chain(config, tokens, dry):
    for seg in split_segments(tokens):
        run_segment(config, seg, dry)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

USAGE = """usage:
  zap <command>                 コマンドを実行し、履歴に記録
  zap -N                        N個前の履歴を実行
  zap @<name>                   登録済みaliasを実行
  zap hist (history)            実行履歴を一覧表示
  zap hist clear                実行履歴をクリア
  zap alias (a) add @<name> <cmd>   aliasを登録
  zap alias (a) add @<name> -N      履歴からaliasを登録
  zap alias (a) list                alias一覧を表示
  zap alias (a) rm @<name>          aliasを削除
  zap @<name> -@<step>          複合alias実行時に指定ステップを除外
  zap @<name> --each v1,v2,..   {default}を置換しながら逐次実行(未指定時はdefaultがそのまま使われる)
  zap --dry <target>            実行内容を表示するのみ
"""


def main():
    argv = sys.argv[1:]
    config = load_config()

    if not argv:
        print(USAGE)
        list_aliases(config)
        sys.exit(0)

    if argv[0] in ("-h", "--help"):
        print(USAGE)
        sys.exit(0)

    if argv[0] in ("hist", "history"):
        cmd_hist(config, argv[1:])
        return
    if argv[0] in ("alias", "a"):
        cmd_alias(config, argv[1:])
        return

    dry = False
    if "--dry" in argv:
        dry = True
        argv = [a for a in argv if a != "--dry"]

    if len(argv) == 1 and re.match(r"^-\d+$", argv[0]):
        n = int(argv[0][1:])
        cmd_str = get_history_cmd(config, n)
        run_and_record(config, cmd_str, dry)
        return

    run_chain(config, argv, dry)


if __name__ == "__main__":
    main()
