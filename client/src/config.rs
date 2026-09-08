//! agent.toml and the directories the agent reads from and writes to.

use std::fs;
use std::path::{Path, PathBuf};

use anyhow::{bail, Context, Result};
use serde::{Deserialize, Serialize};

use crate::Overrides;

#[derive(Debug, Clone, Serialize, Deserialize)]
pub struct Config {
    pub server: String,
    pub token: String,
    #[serde(default, skip_serializing_if = "Option::is_none")]
    pub host: Option<String>,
    #[serde(default = "default_poll_seconds")]
    pub poll_seconds: u64,
    #[serde(default = "default_max_age_days")]
    pub max_age_days: u64,
}

fn default_poll_seconds() -> u64 {
    30
}

fn default_max_age_days() -> u64 {
    14
}

impl Default for Config {
    fn default() -> Self {
        Config {
            server: String::new(),
            token: String::new(),
            host: None,
            poll_seconds: default_poll_seconds(),
            max_age_days: default_max_age_days(),
        }
    }
}

impl Config {
    /// A missing file is fine when the flags supply server and token (tests, cron one-offs).
    pub fn load(path: &Path, o: &Overrides) -> Result<Config> {
        let mut cfg = if path.exists() {
            let text = fs::read_to_string(path).with_context(|| format!("reading {}", path.display()))?;
            toml::from_str(&text).with_context(|| format!("parsing {}", path.display()))?
        } else {
            Config::default()
        };
        if let Some(s) = &o.server {
            cfg.server = s.clone();
        }
        if let Some(t) = &o.token {
            cfg.token = t.clone();
        }
        if o.host.is_some() {
            cfg.host = o.host.clone();
        }
        if cfg.server.is_empty() || cfg.token.is_empty() {
            bail!("no server/token: run `heron-agent install --server URL --token TOKEN` (config: {})", path.display());
        }
        cfg.server = cfg.server.trim_end_matches('/').to_string();
        Ok(cfg)
    }

    pub fn save(&self, path: &Path) -> Result<()> {
        if let Some(dir) = path.parent() {
            fs::create_dir_all(dir)?;
        }
        fs::write(path, toml::to_string(self)?).with_context(|| format!("writing {}", path.display()))
    }

    pub fn host(&self) -> String {
        self.host.clone().unwrap_or_else(default_host)
    }
}

/// The hostname, restricted to the characters the upload protocol allows in `host`.
fn default_host() -> String {
    let raw = gethostname::gethostname().to_string_lossy().into_owned();
    let clean: String = raw
        .chars()
        .map(|c| if c.is_ascii_alphanumeric() || "._-".contains(c) { c } else { '-' })
        .collect();
    if clean.is_empty() {
        "unknown".into()
    } else {
        clean
    }
}

/// Where everything lives. Each directory can be moved with an environment variable
/// so tests (and unusual setups) never touch the real home directory.
pub struct Paths {
    pub config: PathBuf,
    pub config_dir: PathBuf,
    pub claude: PathBuf,
    pub codex: PathBuf,
    pub terminals: PathBuf,
    pub state_dir: PathBuf,
}

impl Paths {
    pub fn from_env() -> Result<Paths> {
        let home = home()?;
        let config_dir = home.join(".config/heron");
        Ok(Paths {
            config: env_path("HERON_AGENT_CONFIG").unwrap_or_else(|| config_dir.join("agent.toml")),
            config_dir,
            claude: env_path("HERON_CLAUDE_DIR").unwrap_or_else(|| home.join(".claude/projects")),
            codex: env_path("HERON_CODEX_DIR").unwrap_or_else(|| home.join(".codex/sessions")),
            terminals: env_path("HERON_TERMINALS_DIR")
                .unwrap_or_else(|| home.join(".local/share/heron/terminals")),
            state_dir: env_path("HERON_STATE_DIR").unwrap_or_else(|| home.join(".local/state/heron")),
        })
    }

    pub fn state_file(&self) -> PathBuf {
        self.state_dir.join("shipped.json")
    }
}

pub fn home() -> Result<PathBuf> {
    dirs::home_dir().context("cannot determine the home directory")
}

fn env_path(var: &str) -> Option<PathBuf> {
    std::env::var_os(var).filter(|v| !v.is_empty()).map(PathBuf::from)
}
