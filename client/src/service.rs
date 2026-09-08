//! Starting `heron-agent run` at login: a launchd agent on macOS, a systemd user
//! unit on Linux. The file contents are built on every platform (and tested);
//! only the calls to launchctl/systemctl are platform-specific.

use std::path::Path;

use anyhow::Result;

// Used by the macOS module and the tests; on Linux only by the tests.
#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub const LABEL: &str = "net.heron.agent";
#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub const UNIT: &str = "heron-agent.service";

#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
pub fn launchd_plist(exe: &Path, log: &Path) -> String {
    format!(
        r#"<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key>
  <string>{LABEL}</string>
  <key>ProgramArguments</key>
  <array>
    <string>{exe}</string>
    <string>run</string>
  </array>
  <key>RunAtLoad</key>
  <true/>
  <key>KeepAlive</key>
  <true/>
  <key>StandardOutPath</key>
  <string>{log}</string>
  <key>StandardErrorPath</key>
  <string>{log}</string>
</dict>
</plist>
"#,
        exe = xml_escape(&exe.display().to_string()),
        log = xml_escape(&log.display().to_string()),
    )
}

#[cfg_attr(not(target_os = "linux"), allow(dead_code))]
pub fn systemd_unit(exe: &Path) -> String {
    format!(
        "[Unit]\n\
         Description=Heron agent: ships transcripts and terminal recordings\n\
         After=network-online.target\n\
         \n\
         [Service]\n\
         ExecStart={exe} run\n\
         Restart=always\n\
         RestartSec=10\n\
         \n\
         [Install]\n\
         WantedBy=default.target\n",
        exe = exe.display(),
    )
}

#[cfg_attr(not(target_os = "macos"), allow(dead_code))]
fn xml_escape(s: &str) -> String {
    s.replace('&', "&amp;").replace('<', "&lt;").replace('>', "&gt;")
}

#[cfg(any(target_os = "macos", target_os = "linux"))]
fn run(program: &str, args: &[&std::ffi::OsStr]) -> Result<()> {
    use anyhow::{bail, Context};
    let status = std::process::Command::new(program)
        .args(args)
        .status()
        .with_context(|| format!("running {program}"))?;
    if !status.success() {
        bail!("{program} {} failed ({status})", args.iter().map(|a| a.to_string_lossy()).collect::<Vec<_>>().join(" "));
    }
    Ok(())
}

#[cfg(target_os = "macos")]
mod platform {
    use super::*;
    use crate::config::home;
    use std::fs;
    use std::path::PathBuf;

    fn plist_path() -> Result<PathBuf> {
        Ok(home()?.join("Library/LaunchAgents").join(format!("{LABEL}.plist")))
    }

    pub fn install(exe: &Path) -> Result<()> {
        let log = home()?.join("Library/Logs/heron-agent.log");
        fs::create_dir_all(log.parent().unwrap())?;
        let plist = plist_path()?;
        fs::create_dir_all(plist.parent().unwrap())?;
        // launchctl refuses to load a label that is already loaded; unloading a
        // previous install first is harmless when there is none.
        let _ = std::process::Command::new("launchctl").arg("unload").arg(&plist).output();
        fs::write(&plist, launchd_plist(exe, &log))?;
        run("launchctl", &["load".as_ref(), "-w".as_ref(), plist.as_os_str()])?;
        println!("loaded launchd agent {LABEL} ({}); log: {}", plist.display(), log.display());
        Ok(())
    }

    pub fn uninstall() -> Result<()> {
        let plist = plist_path()?;
        if !plist.exists() {
            return Ok(());
        }
        let _ = run("launchctl", &["unload".as_ref(), "-w".as_ref(), plist.as_os_str()]);
        fs::remove_file(&plist)?;
        println!("removed launchd agent {LABEL}");
        Ok(())
    }
}

#[cfg(target_os = "linux")]
mod platform {
    use super::*;
    use crate::config::home;
    use std::fs;
    use std::path::PathBuf;

    fn unit_path() -> Result<PathBuf> {
        Ok(home()?.join(".config/systemd/user").join(UNIT))
    }

    pub fn install(exe: &Path) -> Result<()> {
        let unit = unit_path()?;
        fs::create_dir_all(unit.parent().unwrap())?;
        fs::write(&unit, systemd_unit(exe))?;
        run("systemctl", &["--user".as_ref(), "daemon-reload".as_ref()])?;
        run("systemctl", &["--user".as_ref(), "enable".as_ref(), "--now".as_ref(), UNIT.as_ref()])?;
        println!("enabled systemd user unit {UNIT} ({}); logs: journalctl --user -u {UNIT}", unit.display());
        Ok(())
    }

    pub fn uninstall() -> Result<()> {
        let unit = unit_path()?;
        if !unit.exists() {
            return Ok(());
        }
        let _ = run("systemctl", &["--user".as_ref(), "disable".as_ref(), "--now".as_ref(), UNIT.as_ref()]);
        fs::remove_file(&unit)?;
        let _ = run("systemctl", &["--user".as_ref(), "daemon-reload".as_ref()]);
        println!("removed systemd user unit {UNIT}");
        Ok(())
    }
}

#[cfg(not(any(target_os = "macos", target_os = "linux")))]
mod platform {
    use super::*;

    pub fn install(_exe: &Path) -> Result<()> {
        anyhow::bail!("no background service on this platform; run `heron-agent run` yourself")
    }

    pub fn uninstall() -> Result<()> {
        Ok(())
    }
}

pub use platform::{install, uninstall};

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn plist_runs_the_binary_at_load_and_keeps_it_alive() {
        let p = launchd_plist(Path::new("/Applications/Heron.app/Contents/MacOS/heron-agent"), Path::new("/Users/me/Library/Logs/heron-agent.log"));
        assert!(p.contains("<string>/Applications/Heron.app/Contents/MacOS/heron-agent</string>\n    <string>run</string>"));
        assert!(p.contains("<key>RunAtLoad</key>\n  <true/>"));
        assert!(p.contains("<key>KeepAlive</key>\n  <true/>"));
        assert!(p.contains("<string>/Users/me/Library/Logs/heron-agent.log</string>"));
        assert!(p.contains(&format!("<string>{LABEL}</string>")));
    }

    #[test]
    fn plist_escapes_xml() {
        let p = launchd_plist(Path::new("/tmp/a&b/heron-agent"), Path::new("/tmp/log"));
        assert!(p.contains("/tmp/a&amp;b/heron-agent"));
    }

    #[test]
    fn unit_runs_and_restarts() {
        let u = systemd_unit(Path::new("/usr/local/bin/heron-agent"));
        assert!(u.contains("ExecStart=/usr/local/bin/heron-agent run\n"));
        assert!(u.contains("Restart=always\n"));
        assert!(u.contains("WantedBy=default.target\n"));
    }
}
