# Record every interactive shell with `script`, for the heron.
#
# Sourced from ~/.bashrc. A shell that is already being recorded, or has no
# terminal, is left alone; `script` re-executes the shell underneath itself,
# and that inner shell sees HERON_RECORDING and returns here at once.
# Recordings land in $HERON_STATE/terminals and are read by collect.py.
case $- in *i*) ;; *) return 0 2>/dev/null ;; esac
[ -t 0 ] && [ -t 1 ] || return 0 2>/dev/null
[ -n "$HERON_RECORDING" ] && return 0 2>/dev/null
[ "$HERON_NO_RECORD" = 1 ] && return 0 2>/dev/null
command -v script >/dev/null 2>&1 || return 0 2>/dev/null

_heron_dir="${HERON_STATE:-/var/lib/heron}/terminals"
mkdir -p "$_heron_dir" 2>/dev/null || return 0 2>/dev/null
export HERON_RECORDING="$_heron_dir/$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
unset _heron_dir
exec script -q -f -O "$HERON_RECORDING"
