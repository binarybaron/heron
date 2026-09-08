//! heron-agent: tails Claude Code / Codex transcripts and terminal recordings
//! and ships new bytes to a Heron server. See DESIGN.md at the repo root.

use anyhow::{Context, Result};
use clap::{Args, Parser, Subcommand};

/// One-line log to stderr with a UTC stamp; stderr is what launchd/systemd capture.
macro_rules! log {
    ($($arg:tt)*) => {
        eprintln!("{} {}", chrono::Utc::now().format("%Y-%m-%dT%H:%M:%SZ"), format_args!($($arg)*))
    };
}

mod agent;
mod config;
mod service;
mod shell_hook;
mod ship;
mod sources;
mod state;

use config::{Config, Paths};

#[derive(Parser)]
#[command(name = "heron-agent", version, about)]
struct Cli {
    #[command(subcommand)]
    cmd: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Poll the sources forever and ship new bytes (what the background service runs).
    Run(Overrides),
    /// Scan the sources once, ship what is new, and exit.
    Once(Overrides),
    /// Show the configuration and how much has been shipped.
    Status,
    /// Print the bash/zsh snippet that records interactive shells with `script`.
    ShellHook {
        /// Write the snippet to ~/.config/heron/shell-hook.sh and source it from ~/.bashrc and ~/.zshrc.
        #[arg(long)]
        install: bool,
    },
    /// Write the config, install the shell hook, and register the background service.
    Install {
        /// Base URL of the Heron server, e.g. https://example.net/heron
        #[arg(long)]
        server: String,
        /// The server's ingest_token
        #[arg(long)]
        token: String,
        /// Name this machine reports as (default: the hostname)
        #[arg(long)]
        host: Option<String>,
    },
    /// Remove the background service, the shell hook, and the config.
    Uninstall,
}

/// Flags that override fields of agent.toml for `run` and `once`.
#[derive(Args, Default)]
pub struct Overrides {
    #[arg(long)]
    server: Option<String>,
    #[arg(long)]
    token: Option<String>,
    #[arg(long)]
    host: Option<String>,
}

fn main() -> Result<()> {
    let cli = Cli::parse();
    let paths = Paths::from_env()?;
    match cli.cmd {
        Cmd::Run(o) => agent::run(&Config::load(&paths.config, &o)?, &paths),
        Cmd::Once(o) => {
            let cfg = Config::load(&paths.config, &o)?;
            let out = agent::once(&cfg, &paths)?;
            println!("shipped {} bytes from {} files ({} errors)", out.bytes, out.files, out.errors);
            if out.errors > 0 {
                std::process::exit(1);
            }
            Ok(())
        }
        Cmd::Status => status(&paths),
        Cmd::ShellHook { install: false } => {
            print!("{}", shell_hook::SNIPPET);
            Ok(())
        }
        Cmd::ShellHook { install: true } => shell_hook::install(&paths),
        Cmd::Install { server, token, host } => install(&paths, server, token, host),
        Cmd::Uninstall => uninstall(&paths),
    }
}

fn status(paths: &Paths) -> Result<()> {
    println!("config:  {}", paths.config.display());
    match Config::load(&paths.config, &Overrides::default()) {
        Ok(cfg) => {
            println!("server:  {}", cfg.server);
            println!("host:    {}", cfg.host());
        }
        Err(e) => println!("server:  ({e:#})"),
    }
    let state = state::State::load(paths.state_file())?;
    println!("state:   {}", state.path().display());
    println!("files tracked: {}", state.files.len());
    println!("bytes shipped: {}", state.bytes_shipped());
    Ok(())
}

fn install(paths: &Paths, server: String, token: String, host: Option<String>) -> Result<()> {
    let cfg = Config { server, token, host, ..Config::default() };
    cfg.save(&paths.config)?;
    println!("wrote {}", paths.config.display());
    shell_hook::install(paths)?;
    // The service must point at a path that still exists after this shell exits.
    let exe = std::env::current_exe()?.canonicalize().context("locating this binary")?;
    service::install(&exe)?;
    println!("done; open a new terminal to start recording");
    Ok(())
}

fn uninstall(paths: &Paths) -> Result<()> {
    service::uninstall()?;
    shell_hook::uninstall(paths)?;
    if paths.config.exists() {
        std::fs::remove_file(&paths.config)?;
        println!("removed {}", paths.config.display());
    }
    println!("kept {} (delete it to forget what was shipped)", paths.state_file().display());
    Ok(())
}
