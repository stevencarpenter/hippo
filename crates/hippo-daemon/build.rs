use std::process::Command;

fn main() {
    // Base version from Cargo.toml workspace (set by Cargo as env var for build scripts)
    let base = std::env::var("CARGO_PKG_VERSION").unwrap();

    let version = git_describe_version(&base);
    println!("cargo:rustc-env=HIPPO_VERSION_FULL={version}");

    // Rebuild when git state changes
    println!("cargo:rerun-if-changed=../../.git/HEAD");
    println!("cargo:rerun-if-changed=../../.git/refs/");
}

fn git_describe_version(base: &str) -> String {
    let describe = Command::new("git")
        .args(["describe", "--tags", "--always", "--dirty", "--match", "v*"])
        .output();

    let Ok(output) = describe else {
        return format!("{base}-dev+unknown");
    };

    if !output.status.success() {
        return format!("{base}-dev+unknown");
    }

    let raw = String::from_utf8_lossy(&output.stdout).trim().to_string();

    // Exactly at a tag: "v0.2.0" or "v0.2.0-dirty"
    if raw == format!("v{base}") {
        return base.to_string();
    }
    if raw == format!("v{base}-dirty") {
        return format!("{base}+dirty");
    }

    // After a tag: "v0.2.0-3-g63ea88d" or "v0.2.0-3-g63ea88d-dirty"
    // No tags: just a short hash "63ea88d" or "63ea88d-dirty"
    if raw.starts_with('v') {
        // Parse: v{tag}-{count}-g{sha}[-dirty]
        let dirty = raw.ends_with("-dirty");
        let clean = if dirty {
            raw.trim_end_matches("-dirty")
        } else {
            &raw
        };
        let parts: Vec<&str> = clean.splitn(4, '-').collect();
        if parts.len() >= 3 {
            let count = parts[1];
            let sha = parts[2]; // already has "g" prefix
            let dirty_suffix = if dirty { ".dirty" } else { "" };
            return format!("{base}-dev.{count}+{sha}{dirty_suffix}");
        }
    }

    // Fallback: no tags, raw is just a sha or sha-dirty
    let dirty = raw.ends_with("-dirty");
    let sha = if dirty {
        raw.trim_end_matches("-dirty")
    } else {
        &raw
    };
    let dirty_suffix = if dirty { ".dirty" } else { "" };
    format!("{base}-dev+g{sha}{dirty_suffix}")
}
