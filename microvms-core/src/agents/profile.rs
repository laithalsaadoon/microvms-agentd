// SPDX-License-Identifier: Apache-2.0
//! The profile table: every agent-specific fact L3 knows, in one place, dated.
//!
//! # Why a table and not code
//!
//! `docs/AGENT-VMS.md` records the rule this module changes: platform code used to carry
//! no agent-specific detail because a vendor's CLI flags, config format, and model ids
//! move on the vendor's cadence. The containment is that everything vendor-shaped lives
//! in the two `const` rows below and nowhere else in the crate. A profile edit is a
//! one-file change; a reader auditing "what does this library assume about Codex" reads
//! one row. Each row names the date and region it was verified against, the way
//! `docs/PLATFORM.md` dates a platform finding, because an undated vendor fact is a fact
//! with no way to tell whether it has rotted.
//!
//! The values are the ones `examples/coding-agents-on-bedrock/run.sh` measured working
//! on 2026-09-02 in us-east-1, with two changes named at their fields.

use crate::region::Region;

/// The two coding agents this library carries a recipe for. Closed on purpose
/// (AGENT-1): an agent with no row cannot be named, so there is no path that builds an
/// image for a CLI nobody knows how to invoke.
#[derive(Clone, Copy, Debug, Eq, Hash, Ord, PartialEq, PartialOrd)]
pub enum Agent {
    /// Anthropic's Claude Code CLI, driven through its native Bedrock mode.
    ClaudeCode,
    /// OpenAI's Codex CLI, driven through Bedrock's OpenAI-compatible Responses endpoint.
    Codex,
}

impl Agent {
    /// Every profile, in the order the image layers and the marker list them.
    pub const ALL: [Agent; 2] = [Agent::ClaudeCode, Agent::Codex];

    /// The spelling the CLI flag, the guest marker, and the image name use.
    pub fn as_str(self) -> &'static str {
        match self {
            Agent::ClaudeCode => "claude-code",
            Agent::Codex => "codex",
        }
    }

    /// The inverse of [`Agent::as_str`], for the marker read-back.
    pub fn parse(text: &str) -> Option<Agent> {
        Agent::ALL.into_iter().find(|agent| agent.as_str() == text)
    }

    /// This agent's row.
    pub fn profile(self) -> &'static Profile {
        match self {
            Agent::ClaudeCode => &CLAUDE_CODE,
            Agent::Codex => &CODEX,
        }
    }
}

impl std::fmt::Display for Agent {
    fn fmt(&self, f: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        f.write_str(self.as_str())
    }
}

/// One agent's recipe. Every field is a default some caller-facing knob can override;
/// see `docs/AGENT-VMS.md`, "The rule this changes".
#[derive(Debug)]
pub struct Profile {
    /// The `npm` package that installs the CLI. Unpinned by default so an image built
    /// today carries today's CLI; a `cli_version` on the spec appends `@<version>`.
    pub npm_package: &'static str,
    /// The Bedrock model id (an inference-profile id for Claude, a model id for Codex)
    /// the agent uses when the caller names none.
    pub default_model: &'static str,
    /// The date and region the row was last verified live, and against which CLI build.
    /// Prose, because it is for the reader auditing rot rather than for code.
    pub verified: &'static str,
}

/// The shared system layer both agents need: Node 22 runs both CLIs, and the rest is
/// what a coding agent expects of a working shell. Emitted once however many profiles
/// share an image.
///
/// `nodejs22-npm`, spelled out, and not the bare `npm` the example Dockerfile installs.
/// Measured 2026-09-10, us-east-1, on the first live build of this layer: with weak
/// dependencies off (`--setopt=install_weak_deps=0`, the minimal-base convention) the bare
/// `npm` resolves to Node 18's `npm-1:8.19.2`, its alternatives link fails against the
/// `/usr/bin/node` that `nodejs22` installed ("exists and it is not a symlink"), and the
/// next layer's `npm install -g` exits 127 with `npm: command not found`, three minutes
/// and one wedged image name later. The example gets away with `npm` only because it
/// leaves weak deps on, which pulls `nodejs22-npm` in beside it.
pub const SYSTEM_PACKAGES: &str =
    "nodejs22 nodejs22-npm python3 git tar gzip which findutils procps-ng";

/// Claude Code. `--allowedTools` widened from the example's `Bash` alone to the set a
/// coding task needs; each tool named is auto-approved in `-p` mode, and an unlisted one
/// is refused silently (the zero-tool-call confident report `docs/AGENT-VMS.md` warns
/// about), so the list is the permission surface rather than a convenience.
pub const CLAUDE_CODE: Profile = Profile {
    npm_package: "@anthropic-ai/claude-code",
    default_model: "global.anthropic.claude-opus-5",
    verified: "2026-09-02, us-east-1, @anthropic-ai/claude-code latest on that date",
};

/// Codex. `-s workspace-write` because the agent runs as uid 1000 in `/workspace` and
/// nothing outside it is the agent's to write; `--skip-git-repo-check` because a synced
/// tree arrives without `.git` (`microvms-cli/src/sync.rs`, `SKIPPED_DIRS`).
pub const CODEX: Profile = Profile {
    npm_package: "@openai/codex",
    default_model: "openai.gpt-5.6-sol",
    verified: "2026-09-02, us-east-1, @openai/codex latest on that date",
};

/// The environment file both agents source, and where the credentials live.
pub const ENV_FILE: &str = "/workspace/.agent-env";

/// Codex's provider config. `HOME=/workspace` in the env file is what makes Codex read
/// this path.
pub const CODEX_CONFIG_FILE: &str = "/workspace/.codex/config.toml";

/// The guest marker `install_access` writes and `installed_agents` reads (AGENT-6).
pub const MARKER_FILE: &str = "/workspace/.agent-vm.json";

/// The lines each agent adds to the environment file, given its model and the token.
///
/// Values are written through [`super::sh_double_quote`] by the caller; this returns bare
/// pairs so a test can assert on names without parsing shell.
pub fn env_pairs(agent: Agent, model: &str, token: &str) -> Vec<(&'static str, String)> {
    match agent {
        Agent::ClaudeCode => vec![
            ("CLAUDE_CODE_USE_BEDROCK", "1".to_string()),
            ("ANTHROPIC_MODEL", model.to_string()),
            ("AWS_BEARER_TOKEN_BEDROCK", token.to_string()),
        ],
        // Codex has no Bedrock mode; the bearer token is the API key of the provider
        // `codex_config` declares.
        Agent::Codex => vec![("OPENAI_API_KEY", token.to_string())],
    }
}

/// Codex's `config.toml`.
///
/// The Responses wire API lives on the Mantle host, not on `bedrock-runtime`: that
/// host's `/openai/v1` is chat-completions only, which Codex dropped. Same bearer token
/// works on both hosts (measured 2026-09-02, us-east-1).
///
/// `model_reasoning_effort` is set because Codex has no metadata row for a Bedrock model
/// id and its fallback sends none ("reasoning effort: none" in the banner). Measured
/// 2026-09-10, us-east-1, Codex 0.154.0, `openai.gpt-5.6-sol`: one of five identical
/// file-writing tasks came back as a plain refusal with zero tool calls and exit 0
/// ("I can't create or run files in this environment"); the other four wrote the file.
/// Mantle accepts `medium` for this model (verified in-VM the same day). The setting is
/// the plausible mitigation, not a measured cure: the decline rate under it is unmeasured,
/// and a caller who needs the effect verifies it the way the docs show, with an `exec`
/// that reads the file back.
pub fn codex_config(model: &str, region: &Region) -> String {
    format!(
        "model = \"{model}\"\n\
         model_provider = \"bedrock\"\n\
         model_reasoning_effort = \"medium\"\n\
         [model_providers.bedrock]\n\
         name = \"Amazon Bedrock (Mantle)\"\n\
         base_url = \"https://bedrock-mantle.{}.api.aws/openai/v1\"\n\
         env_key = \"OPENAI_API_KEY\"\n\
         wire_api = \"responses\"\n",
        region.as_str()
    )
}

/// The headless command for one task, already shell-quoted.
///
/// `task` is single-quoted for `sh` by the caller through [`super::sh_single_quote`];
/// this function receives the quoted form so the two agents' templates read as the
/// literal command lines they are.
pub fn headless_command(agent: Agent, quoted_task: &str) -> String {
    match agent {
        Agent::ClaudeCode => {
            format!("claude -p {quoted_task} --allowedTools Bash,Read,Edit,Write,Grep,Glob")
        }
        Agent::Codex => {
            format!("codex exec --skip-git-repo-check -s workspace-write {quoted_task}")
        }
    }
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn the_two_ids_are_distinct_and_round_trip() {
        for agent in Agent::ALL {
            assert_eq!(Agent::parse(agent.as_str()), Some(agent));
        }
        assert_ne!(Agent::ClaudeCode.as_str(), Agent::Codex.as_str());
        assert_eq!(Agent::parse("cursor"), None);
    }

    #[test]
    fn every_row_names_a_package_a_model_and_a_verification_date() {
        for agent in Agent::ALL {
            let profile = agent.profile();
            assert!(profile.npm_package.starts_with('@'), "{agent}");
            assert!(!profile.default_model.is_empty(), "{agent}");
            assert!(
                profile.verified.starts_with("2026-"),
                "{agent}: an undated row is a row nobody can audit for rot"
            );
        }
    }

    #[test]
    fn the_codex_config_names_the_mantle_host_for_the_region() {
        let config = codex_config("openai.gpt-5.6-sol", &Region::UsWest2);
        assert!(config.contains("https://bedrock-mantle.us-west-2.api.aws/openai/v1"));
        assert!(config.contains("wire_api = \"responses\""));
        assert!(config.starts_with("model = \"openai.gpt-5.6-sol\"\n"));
        assert!(
            config.contains("\nmodel_reasoning_effort = \"medium\"\n"),
            "the fallback metadata sends no effort, and a no-effort run declined a task:\n{config}"
        );
    }

    #[test]
    fn the_token_reaches_each_agent_under_the_variable_it_reads() {
        let claude = env_pairs(Agent::ClaudeCode, "m", "tok");
        assert!(claude.contains(&("AWS_BEARER_TOKEN_BEDROCK", "tok".to_string())));
        assert!(claude.contains(&("CLAUDE_CODE_USE_BEDROCK", "1".to_string())));
        let codex = env_pairs(Agent::Codex, "m", "tok");
        assert_eq!(codex, vec![("OPENAI_API_KEY", "tok".to_string())]);
    }
}
