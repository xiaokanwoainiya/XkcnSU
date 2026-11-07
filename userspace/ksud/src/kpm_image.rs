use android_bootimg::parser::{BootImage, BootImageVersion};
use android_bootimg::patcher::BootImagePatchOption;
use anyhow::{Context, Result, ensure};
#[cfg(any(target_os = "android", test))]
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};
use std::fs;
use std::io::Cursor;
use std::path::{Path, PathBuf};
use std::process::Command;

use crate::image_transaction;

// KPM patches the kernel in boot, never the ramdisk in init_boot/vendor_boot.
#[cfg(target_os = "android")]
fn kernel_partition_path() -> Result<PathBuf> {
    let slot = crate::boot_patch::get_slot_suffix(false);
    ensure!(
        matches!(slot.as_str(), "" | "_a" | "_b"),
        "invalid slot suffix"
    );
    for directory in ["/dev/block/by-name", "/dev/block/bootdevice/by-name"] {
        let candidate = Path::new(directory).join(format!("boot{slot}"));
        if candidate.exists() {
            return Ok(candidate);
        }
    }
    anyhow::bail!("Cannot locate the active boot{slot} partition")
}

#[derive(clap::Subcommand, Debug)]
pub enum ImageCommand {
    /// Report the image header, kernel and ramdisk hashes
    Inspect { boot: PathBuf },
    /// Save the original, compressed kernel without modifying its container
    ExportKernel { boot: PathBuf, out: PathBuf },
    /// Reconstruct a baseline only when repatching exactly matches the input
    RecoverKernel {
        #[arg(long)]
        boot: PathBuf,
        #[arg(long)]
        out: PathBuf,
        #[arg(long)]
        kptools: PathBuf,
        #[arg(long)]
        kpimg: PathBuf,
    },
    /// Patch only the kernel and save a separate original-kernel backup
    Patch {
        #[arg(long)]
        boot: PathBuf,
        #[arg(long)]
        out: PathBuf,
        #[arg(long)]
        kernel_backup: PathBuf,
        #[arg(long)]
        kptools: PathBuf,
        #[arg(long)]
        kpimg: PathBuf,
    },
    /// Restore an original compressed kernel while preserving the ramdisk
    Restore {
        #[arg(long)]
        boot: PathBuf,
        #[arg(long)]
        kernel: PathBuf,
        #[arg(long)]
        out: PathBuf,
    },
}

fn parse_boot(bytes: &[u8]) -> Result<BootImage<'_>> {
    ensure!(
        bytes.len() >= 4096 && bytes.starts_with(b"ANDROID!"),
        "not an Android boot image"
    );
    let boot = BootImage::parse(bytes).context("parse boot image")?;
    ensure!(
        matches!(
            boot.get_header().get_version(),
            BootImageVersion::Android(0..=4)
        ),
        "unsupported Android boot header version"
    );
    ensure!(
        boot.get_blocks().get_kernel().is_some(),
        "image has no kernel; init_boot is not a KPM target"
    );
    Ok(boot)
}

fn raw_kernel(bytes: &[u8]) -> Result<Vec<u8>> {
    let boot = parse_boot(bytes)?;
    Ok(boot
        .get_blocks()
        .get_kernel()
        .context("missing kernel")?
        .get_data()
        .to_vec())
}

fn decoded_kernel(bytes: &[u8]) -> Result<Vec<u8>> {
    let boot = parse_boot(bytes)?;
    let mut kernel = Vec::new();
    boot.get_blocks()
        .get_kernel()
        .context("missing kernel")?
        .dump(&mut kernel, false)?;
    ensure!(
        kernel.len() >= 64 && kernel.get(0x38..0x3c) == Some(b"ARM\x64"),
        "kernel is not an ARM64 Image"
    );
    Ok(kernel)
}

fn replace_kernel(source: &[u8], kernel: Vec<u8>, compressed: bool) -> Result<Vec<u8>> {
    let boot = parse_boot(source)?;
    let old_ramdisk = boot
        .get_blocks()
        .get_ramdisk()
        .map(|data| sha256::digest(data.get_data()));
    let mut patcher = BootImagePatchOption::new(&boot);
    patcher.replace_kernel(Box::new(Cursor::new(kernel)), compressed);
    let mut output = Cursor::new(Vec::with_capacity(source.len()));
    patcher.patch(&mut output)?;
    let result = output.into_inner();
    ensure!(
        result.len() <= source.len(),
        "patched boot exceeds source partition"
    );
    let check = parse_boot(&result)?;
    let new_ramdisk = check
        .get_blocks()
        .get_ramdisk()
        .map(|data| sha256::digest(data.get_data()));
    ensure!(
        old_ramdisk == new_ramdisk,
        "kernel operation unexpectedly changed the root ramdisk"
    );
    ensure!(
        boot.get_header().get_cmdline() == check.get_header().get_cmdline(),
        "kernel operation changed cmdline"
    );
    Ok(result)
}

fn checked_tool(tool: &Path, arguments: &[&std::ffi::OsStr]) -> Result<String> {
    let output = Command::new(tool)
        .args(arguments)
        .output()
        .with_context(|| format!("run {}", tool.display()))?;
    ensure!(
        output.status.success(),
        "kptools failed ({}): {}\n{}",
        output.status,
        String::from_utf8_lossy(&output.stderr),
        String::from_utf8_lossy(&output.stdout)
    );
    Ok(String::from_utf8_lossy(&output.stdout).into_owned())
}

fn validate_kernel_flags(flags: &str) -> Result<()> {
    ensure!(
        !flags.lines().any(|line| line.trim() == "CONFIG_KPM=y"),
        "kernel already contains a KPM implementation"
    );
    ensure!(
        flags
            .lines()
            .any(|line| line.trim() == "CONFIG_KALLSYMS_ALL=y"),
        "KPatch requires CONFIG_KALLSYMS_ALL=y"
    );
    Ok(())
}

fn validate_baseline(kernel: &[u8]) -> Result<()> {
    ensure!(
        kernel.len() >= 64 && kernel.get(0x38..0x3c) == Some(b"ARM\x64"),
        "kernel is not an ARM64 Image"
    );
    ensure!(
        !kernel.windows(8).any(|part| part == b"KSULKM1\0"),
        "kernel contains a root capsule; use the verified original kernel"
    );
    Ok(())
}

pub fn patch_kernel_bytes(kernel: &[u8], kptools: &Path, kpimg: &Path) -> Result<Vec<u8>> {
    use std::ffi::OsStr;
    validate_baseline(kernel)?;
    ensure!(
        kpimg.is_file() && kptools.is_file(),
        "KPatch tools or engine asset is missing"
    );
    let temporary = tempfile::tempdir()?;
    let input = temporary.path().join("kernel.original");
    let output = temporary.path().join("kernel.patched");
    fs::write(&input, kernel)?;
    let flags = checked_tool(
        kptools,
        &[OsStr::new("-f"), OsStr::new("-i"), input.as_os_str()],
    )?;
    validate_kernel_flags(&flags)?;
    let info = checked_tool(
        kptools,
        &[OsStr::new("-l"), OsStr::new("-i"), input.as_os_str()],
    )?;
    ensure!(
        info.lines().any(|line| line.trim() == "patched=false"),
        "kernel is already patched or its patch state is unknown"
    );
    checked_tool(
        kptools,
        &[
            OsStr::new("-p"),
            OsStr::new("-i"),
            input.as_os_str(),
            OsStr::new("-k"),
            kpimg.as_os_str(),
            OsStr::new("-o"),
            output.as_os_str(),
        ],
    )?;
    let patched = fs::read(&output).context("kptools did not produce a kernel")?;
    ensure!(
        !patched.is_empty() && sha256::digest(&patched) != sha256::digest(kernel),
        "kptools produced an unchanged kernel"
    );
    let info = checked_tool(
        kptools,
        &[OsStr::new("-l"), OsStr::new("-i"), output.as_os_str()],
    )?;
    ensure!(
        info.lines().any(|line| line.trim() == "patched=true"),
        "patched kernel did not pass KPatch inspection"
    );
    Ok(patched)
}

pub fn recover_kernel_bytes(kernel: &[u8], kptools: &Path, kpimg: &Path) -> Result<Vec<u8>> {
    use std::ffi::OsStr;
    ensure!(
        kernel.len() >= 64 && kernel.get(0x38..0x3c) == Some(b"ARM\x64"),
        "kernel is not an ARM64 Image"
    );
    let temporary = tempfile::tempdir()?;
    let input = temporary.path().join("kernel.patched");
    let output = temporary.path().join("kernel.recovered");
    fs::write(&input, kernel)?;
    let info = checked_tool(
        kptools,
        &[OsStr::new("-l"), OsStr::new("-i"), input.as_os_str()],
    )?;
    ensure!(
        info.lines().any(|line| line.trim() == "patched=true"),
        "kernel has no recoverable KPatch image"
    );
    checked_tool(
        kptools,
        &[
            OsStr::new("-u"),
            OsStr::new("-i"),
            input.as_os_str(),
            OsStr::new("-o"),
            output.as_os_str(),
        ],
    )?;
    let recovered = fs::read(output).context("kptools did not produce a recovery kernel")?;
    verify_recovered_kernel(kernel, recovered, |original| {
        patch_kernel_bytes(original, kptools, kpimg)
    })
}

fn verify_recovered_kernel(
    kernel: &[u8],
    recovered: Vec<u8>,
    repatch: impl FnOnce(&[u8]) -> Result<Vec<u8>>,
) -> Result<Vec<u8>> {
    validate_baseline(&recovered)?;
    ensure!(
        repatch(&recovered)? == kernel,
        "boot kernel does not match the bundled XKCNSU engine; refusing to adopt it"
    );
    Ok(recovered)
}

fn inspection(bytes: &[u8]) -> Result<Value> {
    let boot = parse_boot(bytes)?;
    let kernel = raw_kernel(bytes)?;
    Ok(json!({
        "image_sha256": sha256::digest(bytes),
        "image_bytes": bytes.len(),
        "kernel_sha256": sha256::digest(&kernel),
        "kernel_bytes": kernel.len(),
        "ramdisk_sha256": boot.get_blocks().get_ramdisk().map(|data| sha256::digest(data.get_data())),
        "cmdline": String::from_utf8_lossy(boot.get_header().get_cmdline()).trim_end_matches('\0'),
    }))
}

fn resolved_path(path: &Path) -> Result<PathBuf> {
    let absolute = if path.is_absolute() {
        path.to_owned()
    } else {
        std::env::current_dir()?.join(path)
    };
    let mut existing = absolute.as_path();
    let mut suffix = Vec::new();
    loop {
        match existing.canonicalize() {
            Ok(mut resolved) => {
                for component in suffix.iter().rev() {
                    resolved.push(component);
                }
                let mut normalized = PathBuf::new();
                for component in resolved.components() {
                    match component {
                        std::path::Component::ParentDir => {
                            normalized.pop();
                        }
                        std::path::Component::CurDir => {}
                        other => normalized.push(other.as_os_str()),
                    }
                }
                return Ok(normalized);
            }
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {
                suffix.push(
                    existing
                        .file_name()
                        .context("output has no resolvable parent")?
                        .to_owned(),
                );
                existing = existing
                    .parent()
                    .context("output has no resolvable parent")?;
            }
            Err(error) => return Err(error).with_context(|| format!("resolve {}", path.display())),
        }
    }
}

fn distinct_paths(paths: &[&Path]) -> Result<()> {
    let mut identities = std::collections::BTreeSet::new();
    #[cfg(unix)]
    let mut file_ids = std::collections::BTreeSet::new();
    for path in paths {
        ensure!(
            identities.insert(resolved_path(path)?),
            "input, output and backup paths refer to the same file"
        );
        #[cfg(unix)]
        if let Ok(metadata) = fs::metadata(path) {
            use std::os::unix::fs::MetadataExt;
            if metadata.is_file() {
                ensure!(
                    file_ids.insert((metadata.dev(), metadata.ino())),
                    "input, output and backup paths are hard links to the same file"
                );
            }
        }
    }
    Ok(())
}

fn publish_original(path: &Path, original: &[u8]) -> Result<()> {
    if path.try_exists()? {
        ensure!(
            fs::read(path)? == original,
            "existing original-kernel backup has different contents"
        );
        return Ok(());
    }
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    image_transaction::secure_dir(parent)?;
    let mut temporary = tempfile::NamedTempFile::new_in(parent)?;
    std::io::Write::write_all(&mut temporary, original)?;
    temporary.as_file().sync_all()?;
    temporary
        .persist_noclobber(path)
        .with_context(|| format!("publish original kernel {}", path.display()))?;
    #[cfg(unix)]
    fs::File::open(parent)?.sync_all()?;
    Ok(())
}

pub fn run_image_command(command: ImageCommand) -> Result<()> {
    #[cfg(target_os = "android")]
    if !matches!(&command, ImageCommand::Inspect { .. }) {
        crate::ksucalls::ensure_kernel_integrations_allowed()?;
    }
    let data = match command {
        ImageCommand::Inspect { boot } => inspection(&fs::read(boot)?)?,
        ImageCommand::ExportKernel { boot, out } => {
            distinct_paths(&[&boot, &out])?;
            let kernel = raw_kernel(&fs::read(boot)?)?;
            image_transaction::atomic_write(&out, &kernel)?;
            json!({"kernel_sha256": sha256::digest(&kernel), "out": out})
        }
        ImageCommand::RecoverKernel {
            boot,
            out,
            kptools,
            kpimg,
        } => {
            distinct_paths(&[&boot, &out, &kptools, &kpimg])?;
            let recovered =
                recover_kernel_bytes(&decoded_kernel(&fs::read(boot)?)?, &kptools, &kpimg)?;
            publish_original(&out, &recovered)?;
            json!({ "kernel_sha256": sha256::digest(&recovered), "kernel_bytes": recovered.len() })
        }
        ImageCommand::Patch {
            boot,
            out,
            kernel_backup,
            kptools,
            kpimg,
        } => {
            distinct_paths(&[&boot, &out, &kernel_backup, &kptools, &kpimg])?;
            let source = fs::read(&boot)?;
            let original = raw_kernel(&source)?;
            let kernel = patch_kernel_bytes(&decoded_kernel(&source)?, &kptools, &kpimg)?;
            let result = replace_kernel(&source, kernel, false)?;
            let report = inspection(&result)?;
            publish_original(&kernel_backup, &original)?;
            distinct_paths(&[&boot, &out, &kernel_backup, &kptools, &kpimg])?;
            image_transaction::atomic_write(&out, &result)?;
            report
        }
        ImageCommand::Restore { boot, kernel, out } => {
            distinct_paths(&[&boot, &kernel, &out])?;
            let original = fs::read(kernel)?;
            let result = replace_kernel(&fs::read(boot)?, original.clone(), true)?;
            ensure!(
                raw_kernel(&result)? == original,
                "original kernel was not restored byte for byte"
            );
            image_transaction::atomic_write(&out, &result)?;
            inspection(&result)?
        }
    };
    println!("{}", json!({"ok": true, "data": data, "error": null}));
    Ok(())
}

#[cfg(target_os = "android")]
const INTEGRATION_DIR: &str = "/data/adb/ksu/XKCNSU/kpm/integration";

#[cfg(any(target_os = "android", test))]
#[derive(Clone, Serialize, Deserialize)]
struct IntegrationState {
    version: u32,
    enabled: bool,
    slot: String,
    kmi: String,
    original_kernel: PathBuf,
    original_sha256: String,
    patched_sha256: String,
    boot_id: String,
    #[serde(default)]
    engine_sha256: String,
    #[serde(default)]
    reboot_pending: bool,
}

#[cfg(any(target_os = "android", test))]
struct IntegrationContext<'a> {
    slot: &'a str,
    kmi: &'a str,
    boot_id: &'a str,
    engine_sha256: &'a str,
    active: bool,
    runtime_ready: bool,
}

#[cfg(any(target_os = "android", test))]
fn valid_digest(value: &str) -> bool {
    value.len() == 64
        && value
            .bytes()
            .all(|byte| byte.is_ascii_digit() || (b'a'..=b'f').contains(&byte))
}

#[cfg(any(target_os = "android", test))]
fn verified_original(
    state: &IntegrationState,
    source: &[u8],
    directory: &Path,
    slot: &str,
    kmi: &str,
) -> Result<Vec<u8>> {
    ensure!(
        state.version == 1 && matches!(slot, "" | "_a" | "_b"),
        "invalid integration state or slot"
    );
    ensure!(
        state.slot == slot && state.kmi == kmi,
        "slot or kernel changed; an old KPM backup cannot be reused"
    );
    ensure!(
        valid_digest(&state.original_sha256)
            && (!state.enabled || valid_digest(&state.patched_sha256))
            && (state.engine_sha256.is_empty() || valid_digest(&state.engine_sha256)),
        "invalid integration checksum"
    );
    ensure!(
        state.original_kernel
            == directory.join(format!("kernel-{}.original", state.original_sha256)),
        "original kernel backup path does not match its checksum"
    );
    let metadata = fs::symlink_metadata(&state.original_kernel)?;
    ensure!(
        metadata.is_file() && !metadata.file_type().is_symlink(),
        "original kernel backup is not a regular file"
    );
    let original = fs::read(&state.original_kernel)?;
    ensure!(
        sha256::digest(&original) == state.original_sha256,
        "original kernel backup failed verification"
    );
    let expected = if state.enabled {
        &state.patched_sha256
    } else {
        &state.original_sha256
    };
    ensure!(
        sha256::digest(raw_kernel(source)?) == *expected,
        "boot changed; refusing to replace an unknown kernel"
    );
    Ok(original)
}

#[cfg(any(target_os = "android", test))]
fn enabled_candidate(
    state: Option<&IntegrationState>,
    source: &[u8],
    directory: &Path,
    context: &IntegrationContext<'_>,
    patch: impl FnOnce(&[u8]) -> Result<Vec<u8>>,
) -> Result<(Vec<u8>, IntegrationState)> {
    ensure!(
        valid_digest(context.engine_sha256),
        "invalid bundled engine checksum"
    );
    ensure!(
        !context.boot_id.is_empty() && !context.kmi.is_empty(),
        "missing integration boot identity"
    );
    let original = if let Some(state) = state {
        verified_original(state, source, directory, context.slot, context.kmi)?
    } else {
        ensure!(
            !context.active,
            "active KPM engine has no verified XKCNSU restoration record"
        );
        ensure!(
            matches!(context.slot, "" | "_a" | "_b"),
            "invalid slot suffix"
        );
        raw_kernel(source)?
    };
    let baseline = replace_kernel(source, original.clone(), true)?;
    let decoded = decoded_kernel(&baseline)?;
    validate_baseline(&decoded)?;
    let patched = patch(&decoded)?;
    let result = replace_kernel(source, patched, false)?;
    let original_sha256 = sha256::digest(&original);
    let patched_sha256 = sha256::digest(raw_kernel(&result)?);
    let reboot_pending =
        !context.runtime_ready || patched_sha256 != sha256::digest(raw_kernel(source)?);
    Ok((
        result,
        IntegrationState {
            version: 1,
            enabled: true,
            slot: context.slot.to_owned(),
            kmi: context.kmi.to_owned(),
            original_kernel: directory.join(format!("kernel-{original_sha256}.original")),
            original_sha256,
            patched_sha256,
            boot_id: context.boot_id.to_owned(),
            engine_sha256: context.engine_sha256.to_owned(),
            reboot_pending,
        },
    ))
}

#[cfg(any(target_os = "android", test))]
fn disabled_candidate(
    state: &IntegrationState,
    source: &[u8],
    directory: &Path,
    slot: &str,
    kmi: &str,
    boot_id: &str,
    active: bool,
) -> Result<(Vec<u8>, IntegrationState)> {
    let original = verified_original(state, source, directory, slot, kmi)?;
    let result = replace_kernel(source, original.clone(), true)?;
    ensure!(
        raw_kernel(&result)? == original,
        "restored kernel differs from original"
    );
    let mut state = state.clone();
    state.enabled = false;
    boot_id.clone_into(&mut state.boot_id);
    state.reboot_pending = active;
    Ok((result, state))
}

#[cfg(any(target_os = "android", test))]
fn apply_configuration_status(
    value: &mut Value,
    state: Option<&IntegrationState>,
    enable: Result<String>,
    disable: &Result<()>,
) {
    let enabled = state.is_some_and(|state| state.enabled);
    let can_enable = enable.is_ok();
    let can_disable = disable.is_ok();
    let configuration_error = if enabled {
        disable.as_ref().err()
    } else {
        enable.as_ref().err()
    }
    .map(|error| format!("{error:#}"));
    let bundled_engine = enable.ok();
    let upgrade_available = state.is_some_and(|state| {
        state.enabled
            && bundled_engine
                .as_ref()
                .is_some_and(|digest| *digest != state.engine_sha256)
    });
    value["can_enable"] = json!(can_enable);
    value["can_disable"] = json!(can_disable);
    value["can_configure"] = json!(if enabled { can_disable } else { can_enable });
    value["configuration_error"] = json!(configuration_error);
    value["upgrade_available"] = json!(upgrade_available);
    value["engine_sha256"] = json!(state.map(|state| &state.engine_sha256));
    value["bundled_engine_sha256"] = json!(bundled_engine);
    if value["reason"].is_null() {
        value["reason"] = if configuration_error.is_some() {
            json!(configuration_error)
        } else if upgrade_available {
            json!("A verified KPM engine image upgrade is available")
        } else {
            Value::Null
        };
    }
}

#[cfg(any(target_os = "android", test))]
fn apply_runtime_configuration_status(
    value: &mut Value,
    state: Option<&IntegrationState>,
    active: bool,
    arm64: bool,
    bundled_engine: Result<String>,
) {
    let enabled = state.is_some_and(|state| state.enabled);
    let can_enable = arm64 && bundled_engine.is_ok() && (state.is_some() || !active);
    let can_disable = arm64 && state.is_some();
    let configuration_error = bundled_engine
        .as_ref()
        .err()
        .map(|error| format!("{error:#}"));
    let bundled_engine = bundled_engine.ok();
    let upgrade_available = state.is_some_and(|state| {
        state.enabled
            && bundled_engine
                .as_ref()
                .is_some_and(|digest| *digest != state.engine_sha256)
    });
    value["integration_verified"] = json!(false);
    value["can_enable"] = json!(can_enable);
    value["can_disable"] = json!(can_disable);
    value["can_configure"] = json!(if enabled { can_disable } else { can_enable });
    value["configuration_error"] = json!(configuration_error);
    value["upgrade_available"] = json!(upgrade_available);
    value["engine_sha256"] = json!(state.map(|state| &state.engine_sha256));
    value["bundled_engine_sha256"] = json!(bundled_engine);
}

#[cfg(any(target_os = "android", test))]
fn slot_state_name(slot: &str) -> Result<String> {
    ensure!(matches!(slot, "" | "_a" | "_b"), "invalid slot suffix");
    Ok(format!(
        "state{}.json",
        if slot.is_empty() { "_single" } else { slot }
    ))
}

#[cfg(target_os = "android")]
fn state_path() -> Result<PathBuf> {
    Ok(Path::new(INTEGRATION_DIR)
        .join(slot_state_name(&crate::boot_patch::get_slot_suffix(false))?))
}

#[cfg(target_os = "android")]
fn read_state() -> Result<Option<IntegrationState>> {
    crate::storage::prepare_kpm()?;
    let path = state_path()?;
    match fs::read(path) {
        Ok(bytes) => {
            let mut state: IntegrationState = serde_json::from_slice(&bytes)?;
            ensure!(state.version == 1, "unsupported integration state");
            ensure!(
                state.slot == crate::boot_patch::get_slot_suffix(false),
                "integration state slot does not match"
            );
            if state.original_kernel.parent() != Some(Path::new(INTEGRATION_DIR)) {
                let filename = format!("kernel-{}.original", state.original_sha256);
                ensure!(
                    state.original_sha256.len() == 64
                        && state
                            .original_sha256
                            .bytes()
                            .all(|byte| byte.is_ascii_hexdigit()),
                    "invalid original kernel checksum"
                );
                state.original_kernel = Path::new(INTEGRATION_DIR).join(filename);
            }
            Ok(Some(state))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error.into()),
    }
}

#[cfg(target_os = "android")]
pub fn configured_enabled() -> Result<bool> {
    Ok(read_state()?.is_some_and(|state| state.enabled))
}

#[cfg(target_os = "android")]
pub fn runtime_status(active: bool) -> Result<Value> {
    ensure!(
        rustix::process::getuid().is_root(),
        "KPM status requires root"
    );
    let state = read_state()?;
    let same_boot = state.as_ref().is_some_and(|state| {
        fs::read_to_string("/proc/sys/kernel/random/boot_id")
            .is_ok_and(|id| id.trim() == state.boot_id)
    });
    let mut value = transition_value(
        state.as_ref().map(|state| state.enabled),
        active,
        crate::kpm::engine_managed(),
        same_boot,
        ImageUpdate::from(state.as_ref().is_some_and(|state| state.reboot_pending)),
    );
    apply_runtime_configuration_status(
        &mut value,
        state.as_ref(),
        active,
        cfg!(target_arch = "aarch64"),
        crate::assets::get_asset_data("kpimg").map(|data| sha256::digest(data.as_ref())),
    );
    Ok(value)
}

#[cfg(target_os = "android")]
pub fn transition_status(active: bool) -> Result<Value> {
    ensure!(
        rustix::process::getuid().is_root(),
        "KPM integration status requires root"
    );
    let runtime_ready = crate::kpm::engine_managed();
    let mut state = match read_state() {
        Ok(state) => state,
        Err(error) => {
            let reason = format!("Cannot verify KPM integration state: {error:#}");
            let mut value =
                transition_value(None, active, runtime_ready, false, ImageUpdate::Unchanged);
            value["can_configure"] = json!(false);
            value["can_enable"] = json!(false);
            value["can_disable"] = json!(false);
            value["state_known"] = json!(false);
            value["configuration_error"] = json!(reason);
            value["reason"] = json!(reason);
            return Ok(value);
        }
    };
    let mut recovery_error = None;
    if state.is_none() && active {
        let recovery = (|| -> Result<IntegrationState> {
            let directory = Path::new(INTEGRATION_DIR);
            image_transaction::secure_dir(directory)?;
            let lock = std::fs::OpenOptions::new()
                .read(true)
                .write(true)
                .create(true)
                .truncate(false)
                .open(directory.join("operation.lock"))?;
            image_transaction::try_lock_file(&lock)?;
            read_state()?.map_or_else(recover_running_integration, Ok)
        })();
        match recovery {
            Ok(recovered) => state = Some(recovered),
            Err(error) => recovery_error = Some(error),
        }
    }
    let configuration =
        recovery_error.map_or_else(|| configuration_engine(state.as_ref(), active), Err);
    let disable = (|| -> Result<()> {
        let state = state
            .as_ref()
            .context("KPM disable requires a verified XKCNSU restoration record")?;
        let slot = crate::boot_patch::get_slot_suffix(false);
        let kmi = crate::boot_patch::get_current_kmi()?;
        let source = image_transaction::read_image(&kernel_partition_path()?)?;
        verified_original(state, &source, Path::new(INTEGRATION_DIR), &slot, &kmi)?;
        Ok(())
    })();
    let same_boot = state.as_ref().is_some_and(|state| {
        fs::read_to_string("/proc/sys/kernel/random/boot_id")
            .is_ok_and(|id| id.trim() == state.boot_id)
    });
    let mut value = transition_value(
        state.as_ref().map(|state| state.enabled),
        active,
        runtime_ready,
        same_boot,
        ImageUpdate::from(state.as_ref().is_some_and(|state| state.reboot_pending)),
    );
    value["state_known"] = json!(state.is_some() || !active);
    apply_configuration_status(&mut value, state.as_ref(), configuration, &disable);
    Ok(value)
}

#[cfg(any(target_os = "android", test))]
#[derive(Clone, Copy, PartialEq, Eq)]
enum ImageUpdate {
    Unchanged,
    Staged,
}

#[cfg(any(target_os = "android", test))]
impl From<bool> for ImageUpdate {
    fn from(staged: bool) -> Self {
        if staged {
            Self::Staged
        } else {
            Self::Unchanged
        }
    }
}

#[cfg(any(target_os = "android", test))]
fn transition_value(
    configured: Option<bool>,
    active: bool,
    runtime_ready: bool,
    same_boot: bool,
    image_update: ImageUpdate,
) -> Value {
    let enabled = configured.unwrap_or(false);
    let mismatch =
        configured.is_some_and(|enabled| enabled != active || (enabled && !runtime_ready));
    let failed = mismatch && !same_boot;
    json!({
        "configured_enabled": enabled,
        "state_known": configured.is_some() || !active,
        "reboot_required": same_boot && (mismatch || (enabled && image_update == ImageUpdate::Staged)),
        "transition_failed": failed,
        "reason": if failed && enabled && active {
            Some("KPM engine still lacks required safety capabilities after reboot")
        } else if failed && enabled {
            Some("KPM engine did not start after reboot")
        } else if failed {
            Some("KPM engine is still running after reboot")
        } else if enabled && image_update == ImageUpdate::Staged && same_boot {
            Some("KPM engine image change is staged; reboot required")
        } else {
            None
        },
    })
}

#[cfg(target_os = "android")]
fn configuration_engine(state: Option<&IntegrationState>, active: bool) -> Result<String> {
    let digest = sha256::digest(crate::assets::get_asset_data("kpimg")?.as_ref());
    let slot = crate::boot_patch::get_slot_suffix(false);
    let kmi = crate::boot_patch::get_current_kmi()?;
    let source = image_transaction::read_image(&kernel_partition_path()?)?;
    let baseline = if let Some(state) = state {
        let original = verified_original(state, &source, Path::new(INTEGRATION_DIR), &slot, &kmi)?;
        decoded_kernel(&replace_kernel(&source, original, true)?)?
    } else {
        ensure!(
            !active,
            "active KPM engine has no verified XKCNSU restoration record"
        );
        decoded_kernel(&source)?
    };
    validate_baseline(&baseline)?;
    let tool = embedded_tool("kptools", true)?;
    let input = tempfile::NamedTempFile::new()?;
    fs::write(input.path(), &baseline)?;
    let flags = checked_tool(
        &tool,
        &[
            std::ffi::OsStr::new("-f"),
            std::ffi::OsStr::new("-i"),
            input.path().as_os_str(),
        ],
    )?;
    validate_kernel_flags(&flags)?;
    let info = checked_tool(
        &tool,
        &[
            std::ffi::OsStr::new("-l"),
            std::ffi::OsStr::new("-i"),
            input.path().as_os_str(),
        ],
    )?;
    ensure!(
        info.lines().any(|line| line.trim() == "patched=false"),
        "boot kernel has no verified XKCNSU restoration record"
    );
    Ok(digest)
}

#[cfg(target_os = "android")]
fn embedded_tool(name: &str, executable: bool) -> Result<PathBuf> {
    use std::os::unix::fs::PermissionsExt;
    let data =
        crate::assets::get_asset_data(name).with_context(|| format!("missing bundled {name}"))?;
    let directory = Path::new(INTEGRATION_DIR).join("bin");
    let path = directory.join(name);
    image_transaction::atomic_write(&path, &data)?;
    fs::set_permissions(
        &path,
        fs::Permissions::from_mode(if executable { 0o700 } else { 0o600 }),
    )?;
    Ok(path)
}

#[cfg(target_os = "android")]
fn recover_running_integration() -> Result<IntegrationState> {
    let _image_lock = image_transaction::operation_lock(Path::new(INTEGRATION_DIR))?;
    let slot = crate::boot_patch::get_slot_suffix(false);
    let source = image_transaction::read_image(&kernel_partition_path()?)?;
    let kernel = raw_kernel(&source)?;
    let decoded = decoded_kernel(&source)?;
    let tool = embedded_tool("kptools", true)?;
    let input = tempfile::NamedTempFile::new()?;
    fs::write(input.path(), &decoded)?;
    let info = checked_tool(
        &tool,
        &[
            std::ffi::OsStr::new("-l"),
            std::ffi::OsStr::new("-i"),
            input.path().as_os_str(),
        ],
    )?;
    ensure!(
        info.lines().any(|line| line.trim() == "patched=true"),
        "active KPM engine has no verifiable bundled patch in the boot image"
    );
    let kpimg = embedded_tool("kpimg", false)?;
    let recovered = recover_kernel_bytes(&decoded, &tool, &kpimg)?;
    let original = raw_kernel(&replace_kernel(&source, recovered, false)?)?;
    let original_sha256 = sha256::digest(&original);
    let original_kernel =
        Path::new(INTEGRATION_DIR).join(format!("kernel-{original_sha256}.original"));
    publish_original(&original_kernel, &original)?;
    let state = IntegrationState {
        version: 1,
        enabled: true,
        slot,
        kmi: crate::boot_patch::get_current_kmi()?,
        original_kernel,
        original_sha256,
        patched_sha256: sha256::digest(&kernel),
        boot_id: fs::read_to_string("/proc/sys/kernel/random/boot_id")?
            .trim()
            .to_owned(),
        engine_sha256: sha256::digest(crate::assets::get_asset_data("kpimg")?.as_ref()),
        reboot_pending: false,
    };
    image_transaction::atomic_write(&state_path()?, &serde_json::to_vec_pretty(&state)?)?;
    Ok(state)
}

#[cfg(target_os = "android")]
pub fn set_enabled(enabled: bool) -> Result<Value> {
    use std::fs::OpenOptions;
    crate::ksucalls::ensure_kernel_integrations_allowed()?;
    ensure!(
        rustix::process::getuid().is_root(),
        "KPM integration requires root"
    );
    ensure!(
        cfg!(target_arch = "aarch64"),
        "KPM engine installation requires an ARM64 kernel image"
    );
    image_transaction::recover_pending()?;
    crate::storage::prepare_kpm()?;
    let directory = Path::new(INTEGRATION_DIR);
    image_transaction::secure_dir(directory)?;
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(directory.join("operation.lock"))?;
    image_transaction::try_lock_file(&lock)?;
    let slot = crate::boot_patch::get_slot_suffix(false);
    ensure!(
        matches!(slot.as_str(), "" | "_a" | "_b"),
        "invalid slot suffix"
    );
    let kmi = crate::boot_patch::get_current_kmi()?;
    let target = kernel_partition_path()?;
    let source = image_transaction::read_image(&target)?;
    let kernel = raw_kernel(&source)?;
    let current_sha256 = sha256::digest(&kernel);
    let active = crate::kpm::engine_active();
    let runtime_ready = crate::kpm::engine_managed();
    let old_state = match read_state()? {
        None if active => Some(recover_running_integration()?),
        state => state,
    };
    let boot_id = fs::read_to_string("/proc/sys/kernel/random/boot_id")?
        .trim()
        .to_owned();
    let (result, state) = if enabled {
        let engine_sha256 = sha256::digest(crate::assets::get_asset_data("kpimg")?.as_ref());
        if let Some(state) = old_state.as_ref() {
            verified_original(state, &source, directory, &slot, &kmi)?;
            if state.enabled
                && state.engine_sha256 == engine_sha256
                && (runtime_ready || (state.reboot_pending && state.boot_id == boot_id))
            {
                return transition_status(active);
            }
        }
        let tool = embedded_tool("kptools", true)?;
        let kpimg = embedded_tool("kpimg", false)?;
        let candidate = enabled_candidate(
            old_state.as_ref(),
            &source,
            directory,
            &IntegrationContext {
                slot: &slot,
                kmi: &kmi,
                boot_id: &boot_id,
                engine_sha256: &engine_sha256,
                active,
                runtime_ready,
            },
            |baseline| patch_kernel_bytes(baseline, &tool, &kpimg),
        )?;
        let original = if let Some(state) = old_state.as_ref() {
            verified_original(state, &source, directory, &slot, &kmi)?
        } else {
            kernel
        };
        publish_original(&candidate.1.original_kernel, &original)?;
        candidate
    } else if let Some(state) = old_state {
        let candidate =
            disabled_candidate(&state, &source, directory, &slot, &kmi, &boot_id, active)?;
        if !state.enabled && current_sha256 == state.original_sha256 {
            return transition_status(active);
        }
        candidate
    } else {
        ensure!(
            !crate::kpm::engine_active(),
            "active KPM engine has no XKCNSU restoration record"
        );
        return transition_status(false);
    };
    let state_bytes = serde_json::to_vec_pretty(&state)?;
    let receipt = image_transaction::install_with_state(
        if enabled { "enable-kpm" } else { "disable-kpm" },
        &[image_transaction::ImageWrite {
            target,
            image: result,
            expected_sha256: Some(sha256::digest(&source)),
        }],
        &[image_transaction::StateWrite {
            path: state_path()?,
            contents: state_bytes,
        }],
        &directory.join(format!(
            "transactions{}",
            if state.slot.is_empty() {
                "_single"
            } else {
                &state.slot
            }
        )),
    )?;
    let mut status = transition_status(crate::kpm::engine_active())?;
    status["journal"] = json!(receipt.journal);
    Ok(status)
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn integration_transition_waits_for_reboot_in_both_directions() {
        for enabled in [false, true] {
            for active in [false, true] {
                let value =
                    transition_value(Some(enabled), active, active, true, ImageUpdate::Unchanged);
                assert_eq!(value["configured_enabled"], enabled);
                assert_eq!(value["reboot_required"], enabled != active);
                assert_eq!(value["transition_failed"], false);
                assert!(value["reason"].is_null());
            }
        }
    }

    #[test]
    fn integration_transition_fails_after_reboot_in_both_directions() {
        for enabled in [false, true] {
            for active in [false, true] {
                let value =
                    transition_value(Some(enabled), active, active, false, ImageUpdate::Unchanged);
                assert_eq!(value["configured_enabled"], enabled);
                assert_eq!(value["reboot_required"], false);
                assert_eq!(value["transition_failed"], enabled != active);
                if enabled == active {
                    assert!(value["reason"].is_null());
                } else {
                    assert_eq!(
                        value["reason"],
                        if enabled {
                            "KPM engine did not start after reboot"
                        } else {
                            "KPM engine is still running after reboot"
                        }
                    );
                }
            }
        }
    }

    #[test]
    fn unconfigured_engine_has_no_integration_transition() {
        for active in [false, true] {
            let value = transition_value(None, active, active, false, ImageUpdate::Unchanged);
            assert_eq!(value["configured_enabled"], false);
            assert_eq!(value["reboot_required"], false);
            assert_eq!(value["transition_failed"], false);
            assert_eq!(value["state_known"], !active);
            assert!(value["reason"].is_null());
        }
    }

    #[test]
    fn init_boot_is_not_accepted_as_kernel_target() {
        let mut image = vec![0; 8192];
        image[..8].copy_from_slice(b"ANDROID!");
        image[20..24].copy_from_slice(&1584_u32.to_le_bytes());
        image[40..44].copy_from_slice(&4_u32.to_le_bytes());
        assert!(parse_boot(&image).is_err());
    }

    #[test]
    fn legacy_boot_headers_preserve_kernel_and_ramdisk_on_round_trip() {
        for version in 0_u32..=2 {
            let mut image = vec![0; 12_288];
            image[..8].copy_from_slice(b"ANDROID!");
            image[8..12].copy_from_slice(&64_u32.to_le_bytes());
            image[16..20].copy_from_slice(&16_u32.to_le_bytes());
            image[36..40].copy_from_slice(&4096_u32.to_le_bytes());
            image[40..44].copy_from_slice(&version.to_le_bytes());
            let header_size = if version == 1 { 1648_u32 } else { 1660_u32 };
            if version > 0 {
                image[1644..1648].copy_from_slice(&header_size.to_le_bytes());
            }
            image[4096 + 0x38..4096 + 0x3c].copy_from_slice(b"ARM\x64");
            image[8192..8208].copy_from_slice(b"ramdisk-preserve");
            let original = raw_kernel(&image).unwrap();
            let mut replacement = original.clone();
            replacement[0] = 0x55;
            let patched = replace_kernel(&image, replacement.clone(), true).unwrap();
            assert_eq!(raw_kernel(&patched).unwrap(), replacement);
            let restored = replace_kernel(&patched, original.clone(), true).unwrap();
            assert_eq!(raw_kernel(&restored).unwrap(), original);
            assert_eq!(
                parse_boot(&restored)
                    .unwrap()
                    .get_blocks()
                    .get_ramdisk()
                    .unwrap()
                    .get_data(),
                b"ramdisk-preserve"
            );
        }
    }

    #[test]
    fn missing_symbols_or_builtin_kpm_are_rejected() {
        assert!(validate_kernel_flags("CONFIG_KALLSYMS=y\n").is_err());
        assert!(validate_kernel_flags("CONFIG_KALLSYMS_ALL=y\nCONFIG_KPM=y\n").is_err());
        assert!(validate_kernel_flags("CONFIG_KALLSYMS_ALL=y\n# CONFIG_KPM is not set\n").is_ok());
    }

    #[test]
    fn malformed_boot_does_not_panic() {
        for length in [0, 7, 40, 4095] {
            assert!(parse_boot(&vec![0; length]).is_err());
        }
    }

    #[test]
    fn recovery_rejects_invalid_kernel_before_executing_tools() {
        let root = tempfile::tempdir().unwrap();
        let tool = root.path().join("missing-kptools");
        let engine = root.path().join("missing-kpimg");
        for bytes in [Vec::new(), vec![0; 64]] {
            let error = recover_kernel_bytes(&bytes, &tool, &engine).unwrap_err();
            assert!(error.to_string().contains("ARM64"));
        }
    }

    fn test_boot() -> Vec<u8> {
        let mut image = vec![0; 8192];
        image[..8].copy_from_slice(b"ANDROID!");
        image[8..12].copy_from_slice(&64_u32.to_le_bytes());
        image[20..24].copy_from_slice(&1584_u32.to_le_bytes());
        image[40..44].copy_from_slice(&4_u32.to_le_bytes());
        image[4096 + 0x38..4096 + 0x3c].copy_from_slice(b"ARM\x64");
        image
    }

    fn legacy_integration(directory: &Path) -> (Vec<u8>, IntegrationState, Vec<u8>) {
        let source = test_boot();
        let original = raw_kernel(&source).unwrap();
        let original_sha256 = sha256::digest(&original);
        let original_kernel = directory.join(format!("kernel-{original_sha256}.original"));
        fs::write(&original_kernel, &original).unwrap();
        let mut legacy_kernel = original.clone();
        legacy_kernel[0] = 0x11;
        let source = replace_kernel(&source, legacy_kernel, false).unwrap();
        let state = IntegrationState {
            version: 1,
            enabled: true,
            slot: "_a".into(),
            kmi: "android14-6.1".into(),
            original_kernel,
            original_sha256,
            patched_sha256: sha256::digest(raw_kernel(&source).unwrap()),
            boot_id: "old-boot".into(),
            engine_sha256: String::new(),
            reboot_pending: false,
        };
        (source, state, original)
    }

    #[test]
    fn runtime_update_notice_requires_enabled_integration_and_changed_engine() {
        let directory = tempfile::tempdir().unwrap();
        let (_, mut state, _) = legacy_integration(directory.path());
        let engine = sha256::digest(b"new engine");
        fs::remove_file(&state.original_kernel).unwrap();
        for (enabled, installed, expected) in [
            (true, String::new(), true),
            (true, sha256::digest(b"old engine"), true),
            (true, engine.clone(), false),
            (false, sha256::digest(b"old engine"), false),
        ] {
            state.enabled = enabled;
            state.engine_sha256 = installed;
            let mut value =
                transition_value(Some(enabled), true, true, false, ImageUpdate::Unchanged);
            apply_runtime_configuration_status(
                &mut value,
                Some(&state),
                true,
                true,
                Ok(engine.clone()),
            );
            assert_eq!(value["upgrade_available"], expected);
            assert_eq!(value["integration_verified"], false);
            assert_eq!(value["can_enable"], true);
            assert_eq!(value["can_disable"], true);
        }
        assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 0);
    }

    #[test]
    fn runtime_status_cannot_authorize_unknown_active_engine() {
        for active in [false, true] {
            let mut value = transition_value(None, active, active, false, ImageUpdate::Unchanged);
            apply_runtime_configuration_status(
                &mut value,
                None,
                active,
                true,
                Ok(sha256::digest(b"engine")),
            );
            assert_eq!(value["state_known"], !active);
            assert_eq!(value["can_enable"], !active);
            assert_eq!(value["can_disable"], false);
            assert_eq!(value["upgrade_available"], false);
            assert_eq!(value["integration_verified"], false);
        }
    }

    #[test]
    fn runtime_status_keeps_disable_request_when_bundle_is_unavailable() {
        let directory = tempfile::tempdir().unwrap();
        let (_, state, _) = legacy_integration(directory.path());
        let mut value = transition_value(Some(true), true, true, false, ImageUpdate::Unchanged);
        apply_runtime_configuration_status(
            &mut value,
            Some(&state),
            true,
            true,
            Err(anyhow::anyhow!("bundled engine missing")),
        );
        assert_eq!(value["can_enable"], false);
        assert_eq!(value["can_disable"], true);
        assert_eq!(value["upgrade_available"], false);
        assert_eq!(value["integration_verified"], false);
    }

    #[test]
    fn verified_disable_survives_failed_enable_tools_and_commits_restoration() {
        let directory = tempfile::tempdir().unwrap();
        let (source, state, original) = legacy_integration(directory.path());
        let enable = checked_tool(&directory.path().join("missing-kptools"), &[]);
        assert!(enable.is_err());
        let disable =
            verified_original(&state, &source, directory.path(), "_a", "android14-6.1").map(|_| ());
        let mut status = transition_value(Some(true), true, false, true, ImageUpdate::Unchanged);
        apply_configuration_status(&mut status, Some(&state), enable, &disable);
        assert_eq!(status["can_enable"], false);
        assert_eq!(status["can_disable"], true);
        assert_eq!(status["can_configure"], true);
        assert!(status["configuration_error"].is_null());
        assert!(!directory.path().join("bin").exists());

        let (result, disabled) = disabled_candidate(
            &state,
            &source,
            directory.path(),
            "_a",
            "android14-6.1",
            "current-boot",
            true,
        )
        .unwrap();
        let target = directory.path().join("boot_a.img");
        let state_path = directory.path().join("state_a.json");
        fs::write(&target, &source).unwrap();
        fs::write(&state_path, serde_json::to_vec(&state).unwrap()).unwrap();
        let receipt = image_transaction::install_with_state(
            "disable-kpm",
            &[image_transaction::ImageWrite {
                target: target.clone(),
                image: result,
                expected_sha256: Some(sha256::digest(&source)),
            }],
            &[image_transaction::StateWrite {
                path: state_path.clone(),
                contents: serde_json::to_vec(&disabled).unwrap(),
            }],
            &directory.path().join("transactions_a"),
        )
        .unwrap();
        let restored = fs::read(&target).unwrap();
        let persisted: IntegrationState =
            serde_json::from_slice(&fs::read(state_path).unwrap()).unwrap();
        assert_eq!(raw_kernel(&restored).unwrap(), original);
        assert!(!persisted.enabled);
        assert_eq!(persisted.boot_id, "current-boot");
        assert!(persisted.reboot_pending);
        verified_original(
            &persisted,
            &restored,
            directory.path(),
            "_a",
            "android14-6.1",
        )
        .unwrap();
        assert_eq!(fs::read(&state.original_kernel).unwrap(), original);
        assert_eq!(
            inspection(&source).unwrap()["ramdisk_sha256"],
            inspection(&restored).unwrap()["ramdisk_sha256"]
        );
        assert_eq!(
            inspection(&source).unwrap()["cmdline"],
            inspection(&restored).unwrap()["cmdline"]
        );
        let journal: Value = serde_json::from_slice(&fs::read(receipt.journal).unwrap()).unwrap();
        assert_eq!(journal["state"], "committed");
        assert_eq!(journal["label"], "disable-kpm");
    }

    #[test]
    fn disable_rejects_stale_state_or_damaged_original_without_writes() {
        let directory = tempfile::tempdir().unwrap();
        let (source, state, original) = legacy_integration(directory.path());
        for change in 0..7 {
            let mut changed = state.clone();
            let mut boot = source.clone();
            match change {
                0 => changed.slot = "_b".into(),
                1 => changed.kmi = "android15-6.6".into(),
                2 => changed.version = 2,
                3 => changed.original_sha256 = sha256::digest(b"wrong original"),
                4 => changed.original_kernel = directory.path().join("unexpected.original"),
                5 => changed.patched_sha256 = sha256::digest(b"wrong patch"),
                6 => boot[4096] ^= 1,
                _ => unreachable!(),
            }
            let disable = disabled_candidate(
                &changed,
                &boot,
                directory.path(),
                "_a",
                "android14-6.1",
                "current-boot",
                true,
            );
            assert!(disable.is_err());
            let mut status =
                transition_value(Some(true), true, false, true, ImageUpdate::Unchanged);
            apply_configuration_status(
                &mut status,
                Some(&changed),
                Err(anyhow::anyhow!("enable preflight failed")),
                &disable.map(|_| ()),
            );
            assert_eq!(status["can_disable"], false);
            assert_eq!(status["can_configure"], false);
        }
        assert_eq!(fs::read(&state.original_kernel).unwrap(), original);
        assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 1);
    }

    #[test]
    fn unverified_active_engine_has_no_configuration_direction() {
        let mut status = transition_value(None, true, false, false, ImageUpdate::Unchanged);
        apply_configuration_status(
            &mut status,
            None,
            Err(anyhow::anyhow!("engine has no verified recovery record")),
            &Err(anyhow::anyhow!("engine has no verified restoration record")),
        );
        assert_eq!(status["state_known"], false);
        assert_eq!(status["can_enable"], false);
        assert_eq!(status["can_disable"], false);
        assert_eq!(status["can_configure"], false);
    }

    #[test]
    fn legacy_enabled_integration_repatches_verified_original_and_keeps_ramdisk() {
        let directory = tempfile::tempdir().unwrap();
        let (source, state, original) = legacy_integration(directory.path());
        let engine = sha256::digest(b"current bundled engine");
        let calls = std::cell::Cell::new(0);
        let (result, updated) = enabled_candidate(
            Some(&state),
            &source,
            directory.path(),
            &IntegrationContext {
                slot: "_a",
                kmi: "android14-6.1",
                boot_id: "current-boot",
                engine_sha256: &engine,
                active: true,
                runtime_ready: false,
            },
            |baseline| {
                calls.set(calls.get() + 1);
                assert_eq!(baseline, original);
                let mut patched = baseline.to_vec();
                patched[0] = 0x22;
                Ok(patched)
            },
        )
        .unwrap();
        assert_eq!(calls.get(), 1);
        assert_ne!(raw_kernel(&result).unwrap(), raw_kernel(&source).unwrap());
        assert_eq!(updated.engine_sha256, engine);
        assert_eq!(updated.original_sha256, state.original_sha256);
        assert_eq!(updated.original_kernel, state.original_kernel);
        assert_eq!(
            updated.patched_sha256,
            sha256::digest(raw_kernel(&result).unwrap())
        );
        assert!(updated.reboot_pending);
        assert_eq!(fs::read(&state.original_kernel).unwrap(), original);
        assert_eq!(
            inspection(&source).unwrap()["ramdisk_sha256"],
            inspection(&result).unwrap()["ramdisk_sha256"]
        );
        assert_eq!(
            inspection(&source).unwrap()["cmdline"],
            inspection(&result).unwrap()["cmdline"]
        );
        let pending = transition_value(
            Some(true),
            true,
            false,
            true,
            ImageUpdate::from(updated.reboot_pending),
        );
        assert_eq!(pending["reboot_required"], true);
        assert_eq!(pending["transition_failed"], false);
        let failed = transition_value(
            Some(true),
            true,
            false,
            false,
            ImageUpdate::from(updated.reboot_pending),
        );
        assert_eq!(failed["reboot_required"], false);
        assert_eq!(failed["transition_failed"], true);
        let ready = transition_value(
            Some(true),
            true,
            true,
            false,
            ImageUpdate::from(updated.reboot_pending),
        );
        assert_eq!(ready["reboot_required"], false);
        assert_eq!(ready["transition_failed"], false);
    }

    #[test]
    fn image_change_waits_for_reboot_even_when_old_engine_is_already_managed() {
        let directory = tempfile::tempdir().unwrap();
        let (source, state, _) = legacy_integration(directory.path());
        let engine = sha256::digest(b"new engine");
        let (_, updated) = enabled_candidate(
            Some(&state),
            &source,
            directory.path(),
            &IntegrationContext {
                slot: "_a",
                kmi: "android14-6.1",
                boot_id: "current-boot",
                engine_sha256: &engine,
                active: true,
                runtime_ready: true,
            },
            |baseline| {
                let mut patched = baseline.to_vec();
                patched[0] = 0x33;
                Ok(patched)
            },
        )
        .unwrap();
        assert!(updated.reboot_pending);
        assert_eq!(
            transition_value(Some(true), true, true, true, ImageUpdate::Staged)["reboot_required"],
            true
        );
    }

    #[test]
    fn stale_or_damaged_upgrade_state_never_reaches_the_patcher() {
        let directory = tempfile::tempdir().unwrap();
        let (source, state, original) = legacy_integration(directory.path());
        let engine = sha256::digest(b"new engine");
        for change in 0..7 {
            let mut changed = state.clone();
            let mut boot = source.clone();
            match change {
                0 => changed.slot = "_b".into(),
                1 => changed.kmi = "android15-6.6".into(),
                2 => changed.version = 2,
                3 => changed.original_sha256 = sha256::digest(b"wrong original"),
                4 => changed.original_kernel = directory.path().join("unexpected.original"),
                5 => changed.patched_sha256 = sha256::digest(b"wrong patch"),
                6 => boot[4096] ^= 1,
                _ => unreachable!(),
            }
            let called = std::cell::Cell::new(false);
            assert!(
                enabled_candidate(
                    Some(&changed),
                    &boot,
                    directory.path(),
                    &IntegrationContext {
                        slot: "_a",
                        kmi: "android14-6.1",
                        boot_id: "current-boot",
                        engine_sha256: &engine,
                        active: true,
                        runtime_ready: false,
                    },
                    |_| {
                        called.set(true);
                        Ok(original.clone())
                    },
                )
                .is_err()
            );
            assert!(!called.get());
        }
        fs::write(&state.original_kernel, b"damaged original").unwrap();
        assert!(
            verified_original(&state, &source, directory.path(), "_a", "android14-6.1").is_err()
        );
    }

    #[test]
    fn missing_or_symlinked_original_cannot_authorize_an_upgrade() {
        let directory = tempfile::tempdir().unwrap();
        let (source, state, original) = legacy_integration(directory.path());
        fs::remove_file(&state.original_kernel).unwrap();
        assert!(
            verified_original(&state, &source, directory.path(), "_a", "android14-6.1").is_err()
        );
        #[cfg(unix)]
        {
            let other = directory.path().join("other.original");
            fs::write(&other, original).unwrap();
            std::os::unix::fs::symlink(other, &state.original_kernel).unwrap();
            assert!(
                verified_original(&state, &source, directory.path(), "_a", "android14-6.1")
                    .is_err()
            );
        }
        #[cfg(not(unix))]
        let _ = original;
    }

    #[test]
    fn active_unknown_engine_without_exact_recovery_record_cannot_be_repatched() {
        let directory = tempfile::tempdir().unwrap();
        let engine = sha256::digest(b"new engine");
        let called = std::cell::Cell::new(false);
        assert!(
            enabled_candidate(
                None,
                &test_boot(),
                directory.path(),
                &IntegrationContext {
                    slot: "_a",
                    kmi: "android14-6.1",
                    boot_id: "current-boot",
                    engine_sha256: &engine,
                    active: true,
                    runtime_ready: false,
                },
                |_| {
                    called.set(true);
                    Ok(Vec::new())
                },
            )
            .is_err()
        );
        assert!(!called.get());
        assert_eq!(fs::read_dir(directory.path()).unwrap().count(), 0);
    }

    #[test]
    fn recovery_requires_exact_current_bundle_round_trip() {
        let original = raw_kernel(&test_boot()).unwrap();
        let mut old_patched = original.clone();
        old_patched[0] = 0x11;
        let exact = verify_recovered_kernel(&old_patched, original.clone(), |baseline| {
            assert_eq!(baseline, original);
            Ok(old_patched.clone())
        })
        .unwrap();
        assert_eq!(exact, original);
        assert!(
            verify_recovered_kernel(&old_patched, original.clone(), |baseline| {
                let mut current = baseline.to_vec();
                current[0] = 0x22;
                Ok(current)
            },)
            .is_err()
        );
        assert!(
            verify_recovered_kernel(&old_patched, original, |_| {
                anyhow::bail!("current bundled patcher failed")
            })
            .is_err()
        );
    }

    #[test]
    fn legacy_state_without_engine_metadata_remains_readable() {
        let directory = tempfile::tempdir().unwrap();
        let (source, state, _) = legacy_integration(directory.path());
        let mut value = serde_json::to_value(&state).unwrap();
        value.as_object_mut().unwrap().remove("engine_sha256");
        value.as_object_mut().unwrap().remove("reboot_pending");
        let restored: IntegrationState = serde_json::from_value(value).unwrap();
        assert!(restored.engine_sha256.is_empty());
        assert!(!restored.reboot_pending);
        assert!(
            verified_original(&restored, &source, directory.path(), "_a", "android14-6.1").is_ok()
        );
    }

    #[test]
    fn export_cannot_replace_its_boot_input() {
        let root = tempfile::tempdir().unwrap();
        let boot = root.path().join("boot.img");
        let bytes = test_boot();
        fs::write(&boot, &bytes).unwrap();
        assert!(
            run_image_command(ImageCommand::ExportKernel {
                boot: boot.clone(),
                out: boot.clone()
            })
            .is_err()
        );
        assert_eq!(fs::read(boot).unwrap(), bytes);
    }

    #[cfg(unix)]
    #[test]
    fn backup_and_output_aliases_are_rejected_before_publication() {
        let root = tempfile::tempdir().unwrap();
        let real = root.path().join("real");
        let link = root.path().join("link");
        fs::create_dir(&real).unwrap();
        std::os::unix::fs::symlink(&real, &link).unwrap();
        assert!(distinct_paths(&[&real.join("original"), &link.join("original")]).is_err());
        assert!(!real.join("original").exists());
    }

    #[test]
    fn missing_tools_do_not_publish_an_original_backup() {
        let root = tempfile::tempdir().unwrap();
        let boot = root.path().join("boot.img");
        let out = root.path().join("patched.img");
        let backup = root.path().join("original");
        fs::write(&boot, test_boot()).unwrap();
        for _ in 0..2 {
            assert!(
                run_image_command(ImageCommand::Patch {
                    boot: boot.clone(),
                    out: out.clone(),
                    kernel_backup: backup.clone(),
                    kptools: root.path().join("missing-kptools"),
                    kpimg: root.path().join("missing-kpimg"),
                })
                .is_err()
            );
            assert!(!backup.exists());
            assert!(!out.exists());
        }
    }

    #[test]
    fn matching_original_backup_can_be_reused_but_never_replaced() {
        let root = tempfile::tempdir().unwrap();
        let path = root.path().join("original");
        publish_original(&path, b"original").unwrap();
        publish_original(&path, b"original").unwrap();
        assert!(publish_original(&path, b"modified").is_err());
        assert_eq!(fs::read(path).unwrap(), b"original");
    }

    #[test]
    fn integration_state_is_separate_for_each_slot() {
        assert_ne!(
            slot_state_name("_a").unwrap(),
            slot_state_name("_b").unwrap()
        );
        assert_ne!(slot_state_name("").unwrap(), slot_state_name("_b").unwrap());
        assert!(slot_state_name("../state").is_err());
    }
}
