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

VERSION = "0.0.6"
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
COMMENT_RE = re.compile(r"#.*$")
COMMON_SECTION = "common"
COMMON_RE = re.compile(r"(?<!\\)%([A-Za-z0-9_]+)%")


def get_common_value(config, key):
    if not config.has_section(COMMON_SECTION) or not config.has_option(COMMON_SECTION, key):
        return None
    return config.get(COMMON_SECTION, key)


def expand_common(config, cmd_str, overrides=None, seen=None, used=None):
    """cmd_str内の %key% を [common] の定義(またはCLIの --set による上書き)で
    再帰的に展開する。ネストした %key% も再帰的に展開し、循環参照はエラーにする。
    used が指定されている場合、実際に使用されたキー名をそこに記録する
    (--set の未使用キー検出に使う)"""
    if seen is None:
        seen = set()

    def replace(m):
        key = m.group(1)
        if key in seen:
            print(f"error: circular common reference detected at %{key}%", file=sys.stderr)
            sys.exit(1)
        if used is not None:
            used.add(key)
        if overrides and key in overrides:
            value = overrides[key]
        else:
            value = get_common_value(config, key)
            if value is None:
                print(f"error: common key '{key}' not found (referenced as %{key}%)", file=sys.stderr)
                sys.exit(1)
        # 値の中にさらに %key% が含まれていれば再帰的に展開する(--setの上書きも継続して効かせる)
        return expand_common(config, value, overrides=overrides, seen=seen | {key}, used=used)

    return COMMON_RE.sub(replace, cmd_str)

def get_alias_cmds(config, name, remove_comment=True):
    if not config.has_section(ALIAS_SECTION) or not config.has_option(ALIAS_SECTION, name):
        return None
    raw = config.get(ALIAS_SECTION, name)
    if remove_comment:
        raw = COMMENT_RE.sub("", raw)
    tokens = raw.split()
    if not tokens:
        return []
    return [build_alias_item(seg) for seg in split_segments(tokens)]


def set_alias_cmds(config, name, cmd_list):
    if not config.has_section(ALIAS_SECTION):
        config.add_section(ALIAS_SECTION)
    config.set(ALIAS_SECTION, name, format_cmd_list(cmd_list))
    save_config(config)


def flatten_alias(config, name, exclude=None, seen=None, alias_replacements=None):
    """複合aliasを再帰的に展開する。各要素は
    {"cmd": <コマンド文字列>, "each": <登録時に埋め込まれたeach値のリスト or None>}
    の辞書として返す。
    alias_replacements が指定されている場合、'@old' 形式のalias参照をそのまま
    (展開する前に)置き換えてから展開する"""
    if seen is None:
        seen = set()
    if name in seen:
        print(f"error: circular alias reference detected at @{name}", file=sys.stderr)
        sys.exit(1)
    seen = seen | {name}

    cmd_list = get_alias_cmds(config, name, remove_comment=True)
    if cmd_list is None:
        print(f"error: alias @{name} not found", file=sys.stderr)
        sys.exit(1)

    result = []
    for item in cmd_list:
        if isinstance(item, dict):
            cmd_str = item.get("cmd", "")
            each_values = item.get("each")
            item_reps = item.get("rep") or []
        else:
            cmd_str = item
            each_values = None
            item_reps = []

        # このステップに埋め込まれた --rep は、'@' で始まるものはalias参照の差し替え、
        # それ以外は展開後の文字列置換として扱う(CLIの --rep と同じ使い分け)
        item_alias_reps = [(o, n) for o, n in item_reps if o.startswith("@")]
        item_str_reps = [(o, n) for o, n in item_reps if not o.startswith("@")]

        # alias参照の差し替えは、実際にマッチしたペアをこの時点で「消費」し、
        # それより先(置換先aliasの内部)へは引き継がない。
        # 引き継いでしまうと、置換先が偶然もとの参照名を内部で使っている場合に
        # 際限なく再置換されてしまい、見せかけの循環参照になる。
        pending_alias_reps = list(alias_replacements or []) + item_alias_reps
        consumed = []
        for old, new in pending_alias_reps:
            if old in cmd_str:
                cmd_str = cmd_str.replace(old, new)
                consumed.append((old, new))
        remaining_alias_reps = [p for p in pending_alias_reps if p not in consumed]

        if cmd_str.startswith("@"):
            sub_name = cmd_str[1:]
            if exclude and sub_name in exclude:
                continue
            sub_list = flatten_alias(
                config,
                sub_name,
                exclude=exclude,
                seen=seen,
                alias_replacements=remaining_alias_reps or None,
            )
            if item_str_reps:
                # このステップに埋め込まれた文字列置換は、参照先の全ステップに適用する
                for sub_item in sub_list:
                    sub_item["cmd"] = apply_replacements(sub_item["cmd"], item_str_reps)
            if each_values and sub_list:
                # 登録時に @参照 に埋め込まれた --each は、参照先の最後のステップにのみ適用する。
                # {default} が無い場合はCLI直指定の --each と同じく静かにスルーする(1回だけ実行)
                last = sub_list[-1]
                if PLACEHOLDER_RE.search(last["cmd"]):
                    last["each"] = each_values
            result.extend(sub_list)
        else:
            if item_str_reps:
                cmd_str = apply_replacements(cmd_str, item_str_reps)
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
            print("usage: zap alias add @<name> <command>", file=sys.stderr)
            sys.exit(1)
        name = args[1].lstrip("@")
        rest = args[2:]
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
    '--each v1,v2,..' や '--rep old,new'(複数可) が含まれていれば、
    それらを埋め込んだ辞書として返す"""
    each_values = None
    reps = []
    remaining = []

    i = 0
    while i < len(seg):
        t = seg[i]
        if t == "--each":
            i += 1
            if i >= len(seg):
                print("error: --each requires a comma-separated value list", file=sys.stderr)
                sys.exit(1)
            each_values = seg[i].split(",")
        elif t == "--rep":
            i += 1
            if i >= len(seg) or "," not in seg[i]:
                print("error: --rep requires 'old,new' (comma-separated pair)", file=sys.stderr)
                sys.exit(1)
            old, new = seg[i].split(",", 1)
            reps.append([old, new])
        else:
            remaining.append(t)
        i += 1

    cmd_str = " ".join(remaining)
    if each_values is None and not reps:
        return cmd_str
    item = {"cmd": cmd_str, "each": each_values}
    if reps:
        item["rep"] = reps
    return item


def format_cmd_list(cmd_list):
    parts = []
    for item in cmd_list:
        if isinstance(item, dict):
            each = item.get("each")
            suffix = f" --each {','.join(each)}" if each else ""
            for old, new in item.get("rep") or []:
                suffix += f" --rep {old},{new}"
            parts.append(f"{item.get('cmd', '')}{suffix}")
        else:
            parts.append(item)
    return " + ".join(parts)


def list_aliases(config):
    if not config.has_section(ALIAS_SECTION) or not config.options(ALIAS_SECTION):
        print("(no alias)")
        return
    print("alias:")
    for name in config.options(ALIAS_SECTION):
        cmd_list = get_alias_cmds(config, name, remove_comment=False)
        print(f"  zap {name} = '{format_cmd_list(cmd_list)}'")


# ---------------------------------------------------------------------------
# 実行
# ---------------------------------------------------------------------------

def apply_replacements(cmd_str, replacements):
    """--rep old,new の指定に従い、cmd_str 内の 'old' という文字列を 'new' に置換する。
    'old' が見つからない場合は何もしない(スルー)。複数指定は登録順に順次適用する。"""
    for old, new in replacements:
        cmd_str = cmd_str.replace(old, new)
    return cmd_str


def run_and_record(config, cmd_str, dry, silent):
    if not silent:
        print(f"$ {cmd_str}")
    if dry:
        return
    result = subprocess.run(cmd_str, shell=True)
    if result.returncode != 0:
        print(f"error: command failed (exit code {result.returncode}): {cmd_str}", file=sys.stderr)
        sys.exit(result.returncode)
    record_history(config, cmd_str)


PLACEHOLDER_RE = re.compile(r"(?<!\\)\{([^{}]*)\}")
RANGE_RE = re.compile(r"^(-?\d+)\.\.(-?\d+)$")


def unescape_special(cmd_str):
    """\\{ , \\} , \\% をリテラルの { , } , % に戻す
    (プレースホルダー/common参照の判定を回避するためのエスケープ解除)"""
    return cmd_str.replace("\\{", "{").replace("\\}", "}").replace("\\%", "%")


def is_placeholder(cmd_str):
    return PLACEHOLDER_RE.search(cmd_str)


def apply_placeholder(cmd_str, value):
    """--each指定時: {default} のようなプレースホルダーを value に置換する
    (中の文字列は無視し、全プレースホルダーを同じ value に置換)。
    \\{ \\} でエスケープされた中括弧はプレースホルダーとして扱わず、リテラルの { } に戻す"""
    return unescape_special(PLACEHOLDER_RE.sub(value, cmd_str))


def resolve_default_placeholder(cmd_str):
    """--each未指定時: {default} のようなプレースホルダーを、
    中に書かれたデフォルト値(例: default)にそのまま置き換える。
    \\{ \\} でエスケープされた中括弧はプレースホルダーとして扱わず、リテラルの { } に戻す"""
    return unescape_special(PLACEHOLDER_RE.sub(lambda m: m.group(1), cmd_str))


def expand_each_tokens(tokens):
    """--each の値トークン列を展開する。'N..M' 形式は両端を含む整数レンジとして
    展開し(降順指定も可)、それ以外のトークンはそのまま使う。
    カンマ区切りリストとレンジ記法の混在も可能(例: '0..3,7,9')。"""
    result = []
    for token in tokens:
        m = RANGE_RE.match(token)
        if m:
            start, end = int(m.group(1)), int(m.group(2))
            step = 1 if end >= start else -1
            result.extend(str(n) for n in range(start, end + step, step))
        else:
            result.append(token)
    return result


def run_segment(config, tokens, dry, silent):
    """'+' で区切られた1タスク分を実行する。先頭トークンは常にalias名として扱う('@'は付けても付けなくてもよい)"""
    if not tokens:
        return

    name = tokens[0][1:] if tokens[0].startswith("@") else tokens[0]
    rest = tokens[1:]

    excludes = []
    each_values = None
    replacements = []
    set_overrides = {}  # --set key=value による common の上書き
    extras = []  # -@/--each/--rep/--set 以外のトークン。alias展開後、末尾に付与する

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
        elif t == "--rep":
            i += 1
            if i >= len(rest) or "," not in rest[i]:
                print("error: --rep requires 'old,new' (comma-separated pair)", file=sys.stderr)
                sys.exit(1)
            old, new = rest[i].split(",", 1)
            replacements.append((old, new))
        elif t == "--set":
            i += 1
            if i >= len(rest) or "=" not in rest[i]:
                print("error: --set requires 'key=value'", file=sys.stderr)
                sys.exit(1)
            key, value = rest[i].split("=", 1)
            set_overrides[key] = value
        else:
            extras.append(t)
        i += 1

    if excludes and each_values:
        print("error: -@ (exclude) and --each cannot be used together", file=sys.stderr)
        sys.exit(1)

    # --rep old,new のうち old が '@' で始まるものはalias参照の差し替え(展開前に適用)、
    # それ以外は展開後のコマンド文字列に対する置換として扱う
    alias_replacements = [(o, n) for o, n in replacements if o.startswith("@")]
    str_replacements = [(o, n) for o, n in replacements if not o.startswith("@")]

    cmds = flatten_alias(
        config,
        name,
        exclude=set(excludes) if excludes else None,
        alias_replacements=alias_replacements if alias_replacements else None,
    )
    extras_str = " ".join(extras)
    last_idx = len(cmds) - 1

    # 1パス目: common展開だけを先に全ステップに対して行い、使用されたキーを集計する。
    # --set で指定したキーが1つも使われていなければ、何も実行せずにエラーで止める
    # (実行を先にしてしまうと、typoのせいで途中まで副作用が発生してしまうため)
    used_keys = set()
    common_expanded = [
        expand_common(config, item["cmd"], overrides=set_overrides, used=used_keys)
        for item in cmds
    ]
    if set_overrides:
        unused = set(set_overrides.keys()) - used_keys
        if unused:
            names = ", ".join(sorted(unused))
            print(f"error: --set key(s) not used in this command: {names}", file=sys.stderr)
            sys.exit(1)

    for idx, item in enumerate(cmds):
        cmd_str = apply_replacements(common_expanded[idx], str_replacements)
        # extras(素のトークン)は、alias展開後の最後のコマンドの末尾に付与する
        if extras_str and idx == last_idx:
            cmd_str = f"{cmd_str} {extras_str}"
        # CLIの --each があればそれを優先。無ければ登録時に埋め込んだ each を使う
        effective_each = each_values if each_values else item["each"]
        if effective_each and is_placeholder(cmd_str):
            for v in expand_each_tokens(effective_each):
                run_and_record(config, apply_placeholder(cmd_str, v), dry, silent)
        else:
            run_and_record(config, resolve_default_placeholder(cmd_str), dry, silent)


def split_segments(tokens):
    """'+' でタスクを分割する"""
    segments = [[]]
    for t in tokens:
        if t == "+":
            segments.append([])
        else:
            segments[-1].append(t)
    return [seg for seg in segments if seg]


def run_chain(config, tokens, dry, silent):
    for seg in split_segments(tokens):
        run_segment(config, seg, dry, silent)


# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

HELP = f"""\
zap@{VERSION}
usage:
  zap -N                            N個前の履歴を実行
  zap <name> (@<name>)              登録済みaliasを実行('@'は付けても付けなくてもよい)
  zap hist (history)                実行履歴を一覧表示
  zap hist clear                    実行履歴をクリア
  zap alias (a) add @<name> <cmd>   aliasを登録
  zap alias (a) list                alias一覧を表示
  zap alias (a) rm @<name>          aliasを削除
  zap <name> -@<step>               複合alias実行時に指定ステップを除外
  zap <name> --each v1,v2,..        {{default}}を置換しながら逐次実行(未指定時はdefaultがそのまま使われる)
                                     N..M のレンジ記法も可(両端含む、リストと混在可: 0..3,7,9)
  zap <name> --rep old,new          コマンド内の文字列 old を new に置換(複数指定可、見つからなければ何もしない)
  zap <name> --set key=value        [common]セクションのkeyを一時的に上書き(複数指定可、%key%として参照)
  zap --dry <target>                実行内容を表示するのみ
  zap --silent <target>             コマンド非表示

alias名が hist/alias/a/history など予約語と重なる場合は '@' を付けて zap @hist のように呼べば区別できます。
[common]セクションにキーを定義し、alias内で %key% と書いて参照できます(ネスト参照可、\\% でエスケープ)。
"""


def main():
    argv = sys.argv[1:]
    config = load_config()

    if not argv:
        print(HELP)
        list_aliases(config)
        sys.exit(0)

    if argv[0] in ("-h", "--help"):
        print(HELP)
        sys.exit(0)

    if argv[0] == "--complete-aliases":
        # シェル補完スクリプトから呼び出される隠しコマンド。alias名のみを1行ずつ出力する
        if config.has_section(ALIAS_SECTION):
            for name in config.options(ALIAS_SECTION):
                print(name)
        sys.exit(0)

    if argv[0] == "--complete-topcmds":
        # シェル補完スクリプトから呼び出される隠しコマンド。トップレベルの予約語を出力する
        print("hist\nhistory\nalias\na\n--dry\n--help")
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

    silent = False
    if "--silent" in argv:
        silent = True
        argv = [a for a in argv if a != "--silent"]

    if len(argv) == 1 and re.match(r"^-\d+$", argv[0]):
        n = int(argv[0][1:])
        cmd_str = get_history_cmd(config, n)
        run_and_record(config, cmd_str, dry, silent)
        return

    run_chain(config, argv, dry, silent)


if __name__ == "__main__":
    main()
