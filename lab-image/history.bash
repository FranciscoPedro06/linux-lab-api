# Command history of Linux Lab. Installed as /etc/linuxlab/history.bash and sourced
# at the end of /etc/bash.bashrc, so it only runs in interactive bash shells: the
# terminal's login shell and any interactive bash started from it. Scripts, `bash -c`,
# other shells and the platform's own execs never load it.
#
# For each command line bash accepts, PS0 (expanded after the line is read and
# before it runs) writes a private marker to the terminal:
#
#   ESC ] 7771 ; <base64 of the command line in UTF-8> BEL
#
# The API takes the marker out of the output and stores the line
# (linuxlab.history.marker in linux-lab-api). The line is read from bash's own
# history, so it is what bash recorded: lines starting with a space
# (HISTCONTROL=ignorespace), empty lines, lines with a syntax error and lines typed
# into programs are not written. Only an entry added since the last prompt is
# written, and only up to 4096 bytes. The line is passed to base64 as data and never
# run or expanded.
#
# This is not a control: the student can change or turn off all of it, or write
# markers of their own. The history only serves the student.

[[ $- == *i* && -n ${BASH_VERSION-} ]] || return 0

# The history number the next entry will get.
__linuxlab_history_prompt() {
    __linuxlab_history_next=$HISTCMD
}

__linuxlab_history_emit() {
    local LC_ALL=C line number command
    line=$(HISTTIMEFORMAT='' builtin history 1 2>/dev/null) || return 0
    number=${line#"${line%%[![:space:]]*}"}
    number=${number%%[!0-9]*}
    [[ -n $number && $number == "${__linuxlab_history_next-}" ]] || return 0
    command=${line#*[0-9]  }
    [[ -n $command ]] || return 0
    (( ${#command} > 4096 )) && return 0
    builtin printf '\033]7771;%s\007' "$(builtin printf '%s' "$command" | /usr/bin/base64 -w 0)"
}

PROMPT_COMMAND="__linuxlab_history_prompt${PROMPT_COMMAND:+; $PROMPT_COMMAND}"
PS0='$(__linuxlab_history_emit)'"${PS0-}"
