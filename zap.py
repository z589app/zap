#!/usr/bin/env python3
"""
zap - Zero dependency Alias Python
alias / npm run / make の置き換えとなるコマンドランチャー / タスクランナー
"""
import sys
import os
import re
import json
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
        dt = datetime.fromisoformat(ts)
        print(f"-{idx}\t{cmd}\t{dt.strftime('%Y/%m/%d %H:%M:%S')}\t({count}回)")


# ---------------------------------------------------------------------------
# alias
# ---------------------------------------------------------------------------

def alias_section(name):
    return f"alias.{name}"


def get_alias_cmds(config, name):
    section = alias_section(name)
    if not config.has_section(section):
        return None
    return json.loads(config.get(section, "cmd"))


def set_alias_cmds(config, name, cmd_list):
    section = alias_section(name)
    if not config.has_section(section):
        config.add_section(section)
    config.set(section, "cmd", json.dumps(cmd_list, ensure_ascii=False))
    save_config(config)


def flatten_alias(config, name, exclude=None, seen=None):
    """複合aliasを再帰的に展開し、実行可能な生コマンド文字列のリストにする"""
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
        if item.startswith("@"):
            sub_name = item[1:]
            if exclude and sub_name in exclude:
                continue
            result.extend(flatten_alias(config, sub_name, exclude=exclude, seen=seen))
        else:
            result.append(item)
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
            cmd_list = [" ".join(seg) for seg in split_segments(rest)]
            set_alias_cmds(config, name, cmd_list)
        print(f"alias @{name} added: {' + '.join(cmd_list)}")

    elif sub == "list":
        list_aliases(config)

    elif sub == "rm":
        if len(args) < 2:
            print("usage: zap alias rm @<name>", file=sys.stderr)
            sys.exit(1)
        name = args[1].lstrip("@")
        section = alias_section(name)
        if config.has_section(section):
            config.remove_section(section)
            save_config(config)
            print(f"alias @{name} removed")
        else:
            print(f"error: alias @{name} not found", file=sys.stderr)
            sys.exit(1)

    else:
        print(f"error: unknown alias subcommand: {sub}", file=sys.stderr)
        sys.exit(1)


def list_aliases(config):
    found = False
    for section in config.sections():
        if section.startswith("alias."):
            found = True
            name = section[len("alias."):]
            cmd_list = json.loads(config.get(section, "cmd"))
            print(f"@{name}\t{' + '.join(cmd_list)}")
    if not found:
        print("(no alias)")


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

        for idx, cmd_str in enumerate(cmds):
            cmd_str = apply_overrides(cmd_str, overrides)
            # extras(素のトークン)は、alias展開後の最後のコマンドの末尾に付与する
            if extras_str and idx == last_idx:
                cmd_str = f"{cmd_str} {extras_str}"
            if each_values:
                for v in each_values:
                    run_and_record(config, cmd_str.replace("{}", v), dry)
            else:
                run_and_record(config, cmd_str, dry)
    else:
        cmd_str = " ".join(tokens)
        run_and_record(config, cmd_str, dry)


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
  zap hist                      実行履歴を一覧表示
  zap hist clear                実行履歴をクリア
  zap alias add @<name> <cmd>   aliasを登録
  zap alias add @<name> -N      履歴からaliasを登録
  zap alias list                alias一覧を表示
  zap alias rm @<name>          aliasを削除
  zap @<name> -@<step>          複合alias実行時に指定ステップを除外
  zap @<name> --each v1,v2,..   {}を置換しながら逐次実行
  zap --dry <target>            実行内容を表示するのみ
"""


def main():
    argv = sys.argv[1:]
    if not argv:
        print(USAGE)
        sys.exit(0)

    if argv[0] in ("-h", "--help"):
        print(USAGE)
        sys.exit(0)

    config = load_config()

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
