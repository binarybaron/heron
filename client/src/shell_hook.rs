//! The snippet that wraps interactive shells in `script`, and its installation.

use std::fs;
use std::path::Path;

use anyhow::{Context, Result};

use crate::config::{home, Paths};

pub const SNIPPET: &str = r#"# Heron: record this interactive shell with script(1). Printed by `heron-agent shell-hook`.
# `script` re-executes $SHELL underneath itself; that inner shell sees HERON_RECORDING
# and returns here at once, so only the outermost shell is wrapped.
case $- in *i*) ;; *) return 0 2>/dev/null ;; esac
[ -t 0 ] && [ -t 1 ] || return 0 2>/dev/null
[ -n "$HERON_RECORDING" ] && return 0 2>/dev/null
[ "$HERON_NO_RECORD" = 1 ] && return 0 2>/dev/null
command -v script >/dev/null 2>&1 || return 0 2>/dev/null

_heron_dir="${HERON_TERMINALS_DIR:-$HOME/.local/share/heron/terminals}"
mkdir -p "$_heron_dir" 2>/dev/null || return 0 2>/dev/null
export HERON_RECORDING="$_heron_dir/$(date -u +%Y%m%dT%H%M%SZ)-$$.log"
unset _heron_dir
# util-linux script takes -f -O <file>; BSD script (macOS) takes -F <file>.
if script --version 2>/dev/null | grep -q util-linux; then
  exec script -q -f -O "$HERON_RECORDING"
else
  exec script -q -F "$HERON_RECORDING"
fi
"#;

const MARKER: &str = "# heron shell hook";

fn source_line() -> String {
    format!("[ -f \"$HOME/.config/heron/shell-hook.sh\" ] && . \"$HOME/.config/heron/shell-hook.sh\" {MARKER}\n")
}

fn rc_files() -> Result<[std::path::PathBuf; 2]> {
    let home = home()?;
    Ok([home.join(".bashrc"), home.join(".zshrc")])
}

pub fn install(paths: &Paths) -> Result<()> {
    let hook = paths.config_dir.join("shell-hook.sh");
    fs::create_dir_all(&paths.config_dir)?;
    fs::write(&hook, SNIPPET).with_context(|| format!("writing {}", hook.display()))?;
    println!("wrote {}", hook.display());
    for rc in rc_files()? {
        // Missing rc files are created: zsh on a fresh mac has none.
        let current = fs::read_to_string(&rc).unwrap_or_default();
        if current.contains(MARKER) {
            continue;
        }
        let sep = if current.is_empty() || current.ends_with('\n') { "" } else { "\n" };
        fs::write(&rc, format!("{current}{sep}{}", source_line()))
            .with_context(|| format!("writing {}", rc.display()))?;
        println!("added shell hook to {}", rc.display());
    }
    Ok(())
}

pub fn uninstall(paths: &Paths) -> Result<()> {
    for rc in rc_files()? {
        remove_marked_lines(&rc)?;
    }
    let hook = paths.config_dir.join("shell-hook.sh");
    if hook.exists() {
        fs::remove_file(&hook)?;
        println!("removed {}", hook.display());
    }
    Ok(())
}

fn remove_marked_lines(rc: &Path) -> Result<()> {
    let Ok(current) = fs::read_to_string(rc) else { return Ok(()) };
    if !current.contains(MARKER) {
        return Ok(());
    }
    let kept: String = current.lines().filter(|l| !l.contains(MARKER)).map(|l| format!("{l}\n")).collect();
    fs::write(rc, kept).with_context(|| format!("writing {}", rc.display()))?;
    println!("removed shell hook from {}", rc.display());
    Ok(())
}
