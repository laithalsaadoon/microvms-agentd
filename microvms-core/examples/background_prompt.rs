// SPDX-License-Identifier: Apache-2.0
//! Prompt an already provisioned VM. The external reference worker owns its lifecycle.
//! Run: cargo run -p microvms-core --example background_prompt
//! Required environment: REVIEW_VM_ID, REVIEW_ENDPOINT, REVIEW_AGENT_TOKEN, REVIEW_AGENT.
//! Optional: REVIEW_REGION (default us-east-1). Never print the private agent token.
use std::time::Duration;

use microvms_core::agents::{self, Agent, AgentPermissionMode, AgentSpec, PromptOptions};
use microvms_core::{Region, session::Session};

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let region: Region = std::env::var("REVIEW_REGION")
        .unwrap_or_else(|_| "us-east-1".into())
        .parse()?;
    let agent: Agent = std::env::var("REVIEW_AGENT")?.parse()?;
    let session = Session::attach(
        region,
        &std::env::var("REVIEW_VM_ID")?,
        &std::env::var("REVIEW_ENDPOINT")?,
        &std::env::var("REVIEW_AGENT_TOKEN")?,
        None,
        Some(Duration::from_secs(10)),
    )
    .await?;
    let handle = agents::prompt(
        &session,
        &AgentSpec::new(agent),
        "Review /workspace/project for code quality and write /workspace/REVIEW.md.",
        &PromptOptions {
            exec_id: Some("review-task-1".into()),
            timeout: Some(Duration::from_secs(1200)),
            permission_mode: AgentPermissionMode::Unrestricted,
            reap_group_on_exit: true,
        },
    )
    .await?;
    println!("{}", handle.exec_id());
    Ok(())
}
