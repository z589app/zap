# zap のbash補完スクリプト
#
# 使い方:
#   1. zap.py を実行可能にして PATH の通った場所に `zap` という名前で置く
#        chmod +x zap.py && mv zap.py ~/bin/zap
#   2. このファイルを ~/.bashrc などから source する
#        source /path/to/zap-completion.bash
#
# 補完対象:
#   - 1語目: hist / history / alias / a / --dry / --help / 登録済みalias(@名前)
#   - `zap alias <TAB>` : add / list / rm
#   - `zap alias rm <TAB>` : 登録済みalias名
#   - `zap hist <TAB>` : clear
#   - `@` から始まる語: 登録済みalias名(alias参照や -@除外指定の補完にも使える)

_zap_completion() {
    local cur prev
    COMPREPLY=()
    cur="${COMP_WORDS[COMP_CWORD]}"
    prev="${COMP_WORDS[COMP_CWORD-1]}"

    local top_cmds
    top_cmds=$(zap --complete-topcmds 2>/dev/null)

    _zap_alias_candidates() {
        zap --complete-aliases 2>/dev/null | sed 's/^/@/'
    }

    # 1語目の補完
    if [[ ${COMP_CWORD} -eq 1 ]]; then
        if [[ "$cur" == @* ]]; then
            COMPREPLY=( $(compgen -W "$(_zap_alias_candidates)" -- "${cur}") )
        else
            COMPREPLY=( $(compgen -W "${top_cmds}" -- "${cur}") )
        fi
        return 0
    fi

    # zap alias / zap a のサブコマンド補完
    if [[ "${COMP_WORDS[1]}" == "alias" || "${COMP_WORDS[1]}" == "a" ]]; then
        if [[ ${COMP_CWORD} -eq 2 ]]; then
            COMPREPLY=( $(compgen -W "add list rm" -- "${cur}") )
            return 0
        fi
        if [[ "${COMP_WORDS[2]}" == "rm" && ${COMP_CWORD} -eq 3 ]]; then
            COMPREPLY=( $(compgen -W "$(_zap_alias_candidates)" -- "${cur}") )
            return 0
        fi
    fi

    # zap hist / zap history のサブコマンド補完
    if [[ "${COMP_WORDS[1]}" == "hist" || "${COMP_WORDS[1]}" == "history" ]]; then
        if [[ ${COMP_CWORD} -eq 2 ]]; then
            COMPREPLY=( $(compgen -W "clear" -- "${cur}") )
            return 0
        fi
    fi

    # '@' から始まる語(2語目以降、+ の後や --dry の後の alias 参照など)は常にalias名を候補にする
    if [[ "$cur" == @* ]]; then
        COMPREPLY=( $(compgen -W "$(_zap_alias_candidates)" -- "${cur}") )
        return 0
    fi

    return 0
}

complete -F _zap_completion zap
