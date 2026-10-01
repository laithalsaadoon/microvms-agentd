// SPDX-License-Identifier: Apache-2.0
//! `image-versions`, `image-set-status`, `image-builds`: an image's versions and builds, by
//! identifier (#264).
//!
//! Each is one core call through [`crate::seam::CoreSeam::control_plane`], after the image is
//! named: an ARN passes through, and a name is looked up in the account by core's
//! `resolve_image_arn`, the resolution `run --image` uses. The envelopes carry the service's
//! readback in its own spelling, serialized from core's wire types, so no field is renamed or
//! dropped here.

use serde_json::{Map, json};

use crate::cli::{ImageBuildsArgs, ImageSetStatusArgs, ImageVersionsArgs};
use crate::commands::{Ctx, Rendered, response_type};
use crate::exit::CliError;

/// `ListMicrovmImageVersions`, every page.
pub async fn versions<O: std::io::Write, E: std::io::Write>(
    ctx: &mut Ctx<'_, O, E>,
    args: &ImageVersionsArgs,
) -> Result<Rendered, CliError> {
    let region = args.region.resolve(ctx.env)?;
    let plane = ctx.seam.control_plane(region).await?;
    let image_arn = plane.resolve_image_arn(&args.image).await?;
    let versions = plane.list_image_versions(&image_arn).await?;

    let mut text = vec![format!("{image_arn}: {} version(s)", versions.len())];
    text.extend(
        versions
            .iter()
            .map(|version| format!("  {}", version.describe())),
    );
    let dense: Vec<String> = versions
        .iter()
        .map(|version| {
            format!(
                "{}\t{}\t{}",
                version.image_version, version.state, version.status
            )
        })
        .collect();
    let mut data = Map::new();
    data.insert("imageArn".into(), json!(image_arn));
    data.insert("versions".into(), json!(versions));
    let (kind, _) = response_type("image-versions");
    Ok(Rendered::ok(kind, data, text.join("\n"), dense.join("\n")))
}

/// `UpdateMicrovmImageVersion`, with core's readback check.
pub async fn set_status<O: std::io::Write, E: std::io::Write>(
    ctx: &mut Ctx<'_, O, E>,
    args: &ImageSetStatusArgs,
) -> Result<Rendered, CliError> {
    let region = args.region.resolve(ctx.env)?;
    let plane = ctx.seam.control_plane(region).await?;
    let image_arn = plane.resolve_image_arn(&args.image).await?;
    ctx.out.progress(&format!(
        "setting version {} of {image_arn} {}",
        args.image_version, args.status
    ));
    let updated = plane
        .set_image_version_status(&image_arn, &args.image_version, args.status)
        .await?;

    let mut data = Map::new();
    data.insert("imageArn".into(), json!(image_arn));
    data.insert("imageVersion".into(), json!(updated.image_version));
    data.insert("status".into(), json!(updated.status));
    data.insert("version".into(), json!(updated));
    let (kind, _) = response_type("image-set-status");
    Ok(Rendered::ok(
        kind,
        data,
        format!("{image_arn}: {}", updated.describe()),
        format!(
            "{}\t{}\t{}",
            updated.image_version, updated.state, updated.status
        ),
    ))
}

/// `ListMicrovmImageBuilds` for one version, every page, or `GetMicrovmImageBuild` for one.
pub async fn builds<O: std::io::Write, E: std::io::Write>(
    ctx: &mut Ctx<'_, O, E>,
    args: &ImageBuildsArgs,
) -> Result<Rendered, CliError> {
    let region = args.region.resolve(ctx.env)?;
    let plane = ctx.seam.control_plane(region).await?;
    let image_arn = plane.resolve_image_arn(&args.image).await?;

    // One key either way, so a consumer reads `builds` whether or not it asked for one: the
    // single read is the listing's item plus its snapshot sizes.
    let (builds, lines, dense) = match args.build_id.as_deref() {
        Some(build_id) => {
            let build = plane
                .get_image_build(&image_arn, &args.image_version, build_id)
                .await?;
            let dense = format!(
                "{}\t{}\t{}",
                build.build_id, build.build_state, build.chipset_generation
            );
            (json!([build]), vec![build.describe()], vec![dense])
        }
        None => {
            let builds = plane
                .list_image_builds(&image_arn, &args.image_version)
                .await?;
            let lines = builds.iter().map(|build| build.describe()).collect();
            let dense = builds
                .iter()
                .map(|build| {
                    format!(
                        "{}\t{}\t{}",
                        build.build_id, build.build_state, build.chipset_generation
                    )
                })
                .collect();
            (json!(builds), lines, dense)
        }
    };

    let mut text = vec![format!("{image_arn} version {}:", args.image_version)];
    text.extend(lines.into_iter().map(|line: String| format!("  {line}")));
    let mut data = Map::new();
    data.insert("imageArn".into(), json!(image_arn));
    data.insert("imageVersion".into(), json!(args.image_version));
    data.insert("builds".into(), builds);
    let (kind, _) = response_type("image-builds");
    Ok(Rendered::ok(kind, data, text.join("\n"), dense.join("\n")))
}
