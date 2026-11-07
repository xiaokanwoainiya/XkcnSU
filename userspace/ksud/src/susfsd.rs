use anyhow::{Context, Result, bail, ensure};
use log::{info, warn};
use serde_json::{Value, json};
use std::ffi::CString;
use std::fs;
use std::fs::OpenOptions;
use std::io::Write;
use std::os::fd::AsRawFd;
use std::path::Path;
use std::thread;
use std::time::Duration;

#[path = "susfsd_protocol.rs"]
mod protocol;
use protocol::{Provider, Query, classify, decode_string, missing_imports};

const MAGIC1: libc::c_long = 0xDEAD_BEEF;
const MAGIC2: libc::c_long = 0xFAFA_FAFA;
const SHOW_VERSION: libc::c_long = 0x555e1;
const SHOW_FEATURES: libc::c_long = 0x555e2;
const SHOW_VARIANT: libc::c_long = 0x555e3;
const UNSUPPORTED: i32 = 126;
const MODULE_DIR: &str = "/sys/module/susfs_guard_lkm";
const CONFIG_ID: &str = "internal.ksud.susfsd";
const AUTOLOAD_KEY: &str = "autoload.enabled";
const FALLBACK_KMI_KEY: &str = "autoload.fallback_kmi";

struct OperationLock(fs::File);

impl OperationLock {
    fn acquire() -> Result<Self> {
        crate::utils::ensure_dir_exists(crate::defs::WORKING_DIR)?;
        let path = Path::new(crate::defs::WORKING_DIR).join(".susfsd.lock");
        let file = OpenOptions::new()
            .create(true)
            .truncate(false)
            .write(true)
            .open(path)?;
        // Load, prepare_unload and conflict-state writes must not interleave across CLI processes.
        ensure!(
            unsafe { libc::flock(file.as_raw_fd(), libc::LOCK_EX) } == 0,
            "Cannot lock SUSFS control: {}",
            std::io::Error::last_os_error()
        );
        Ok(Self(file))
    }
}

impl Drop for OperationLock {
    fn drop(&mut self) {
        unsafe { libc::flock(self.0.as_raw_fd(), libc::LOCK_UN) };
    }
}

fn write_parameter(name: &str, value: &str) -> Result<()> {
    let mut file = OpenOptions::new().write(true).open(parameter(name))?;
    file.write_all(value.as_bytes())?;
    Ok(())
}

fn query<const N: usize>(command: libc::c_long) -> Option<String> {
    let mut payload = Query {
        text: [0; N],
        err: UNSUPPORTED,
    };
    // The invalid reboot magics can only be consumed by a SUSFS handler.
    unsafe {
        libc::syscall(libc::SYS_reboot, MAGIC1, MAGIC2, command, &raw mut payload);
    }
    if payload.err == 0 {
        decode_string(&payload.text)
    } else {
        None
    }
}

fn parameter(name: &str) -> String {
    format!("{MODULE_DIR}/parameters/{name}")
}

fn read_bool(name: &str) -> Option<bool> {
    match fs::read_to_string(parameter(name)).ok()?.trim() {
        "1" | "Y" | "y" => Some(true),
        "0" | "N" | "n" => Some(false),
        _ => None,
    }
}

fn read_error(name: &str) -> Option<i32> {
    fs::read_to_string(parameter(name))
        .ok()?
        .trim()
        .parse()
        .ok()
}

struct Probe {
    module_present: bool,
    version: Option<String>,
    variant: Option<String>,
    features: Vec<String>,
    provider: Provider,
    load_error: Option<i32>,
    restore_error: Option<i32>,
    stopping: Option<bool>,
    unload_ready: Option<bool>,
}

impl Probe {
    fn read() -> Self {
        let module_present = Path::new(MODULE_DIR).is_dir();
        let version = query::<16>(SHOW_VERSION);
        let variant = query::<16>(SHOW_VARIANT);
        let features: Vec<String> = query::<8192>(SHOW_FEATURES)
            .map(|value| {
                value
                    .lines()
                    .filter(|line| !line.is_empty())
                    .map(str::to_owned)
                    .collect()
            })
            .unwrap_or_default();
        let builtin_symbols = !module_present
            && version.is_some()
            && resolve_symbols(&[
                "susfs_is_inode_sus_path",
                "susfs_sus_kstat_spoof_generic_fillattr",
            ])
            .is_ok_and(|addresses| {
                addresses.iter().any(|address| {
                    address.is_some_and(|address| address >= protocol::KERNEL_ADDRESS_MIN)
                })
            });
        let provider = classify(
            module_present,
            builtin_symbols,
            version.as_deref(),
            variant.as_deref(),
            &features,
        );
        Self {
            module_present,
            version,
            variant,
            features,
            provider,
            load_error: read_error("xkcnsu_load_error"),
            restore_error: read_error("xkcnsu_restore_error"),
            stopping: read_bool("xkcnsu_stopping"),
            unload_ready: read_bool("xkcnsu_unload_ready"),
        }
    }

    const fn owned_guard(&self) -> bool {
        self.module_present && self.load_error.is_some() && self.restore_error.is_some()
    }

    fn supported(&self) -> bool {
        match self.provider {
            Provider::Builtin => true,
            Provider::Lkm => {
                self.owned_guard()
                    && self.version.as_deref() == Some(protocol::MODULE_VERSION)
                    && self.variant.as_deref() == Some(protocol::MODULE_VARIANT)
                    && self
                        .features
                        .iter()
                        .any(|feature| feature == protocol::MODULE_MARKER)
                    && self.load_error == Some(0)
                    && self.restore_error == Some(0)
                    && self.stopping == Some(false)
                    && self.unload_ready == Some(false)
            }
            _ => false,
        }
    }

    fn runtime_supported(&self) -> bool {
        protocol::runtime_supported(
            self.version.as_deref(),
            &self.features,
            self.load_error,
            self.restore_error,
            self.stopping,
            self.unload_ready,
        )
    }

    fn reason(&self) -> Option<String> {
        if self.supported() {
            return None;
        }
        Some(match self.provider {
            Provider::Absent => "SUSFS is not loaded".into(),
            Provider::Builtin => unreachable!(),
            Provider::Inconsistent => {
                "SUSFS protocol and module identity disagree; refusing load/unload".into()
            }
            Provider::Lkm => format!(
                "Guard is resident but not operational: owned={}, load_error={:?}, restore_error={:?}, stopping={:?}, unload_ready={:?}; failed restoration requires reboot",
                self.owned_guard(),
                self.load_error,
                self.restore_error,
                self.stopping,
                self.unload_ready,
            ),
        })
    }
}

fn require_arm64() -> Result<()> {
    ensure!(
        cfg!(target_arch = "aarch64"),
        "SUSFS guard LKM is only available for arm64"
    );
    Ok(())
}

fn validate_kmi(kmi: &str) -> Result<()> {
    ensure!(protocol::valid_kmi(kmi), "Unsupported SUSFS KMI: {kmi}");
    Ok(())
}

fn select_kmi(requested: Option<&str>) -> Result<String> {
    let detected = crate::boot_patch::get_current_kmi().ok();
    let chosen = requested
        .or(detected.as_deref())
        .context("Cannot detect KMI; specify --kmi")?;
    validate_kmi(chosen)?;
    if let Some(detected) = detected.as_deref() {
        ensure!(
            chosen == detected,
            "Requested KMI {chosen} does not match detected KMI {detected}"
        );
    }
    let uname = rustix::system::uname();
    let release = uname.release().to_string_lossy();
    ensure!(
        protocol::kmi_matches_release(chosen, &release),
        "KMI {chosen} does not match kernel release {release}"
    );
    crate::assets::get_asset_data(&format!("{chosen}_susfs_guard_lkm.ko"))
        .with_context(|| format!("SUSFS asset missing for {chosen}"))?;
    Ok(chosen.to_owned())
}

fn resolve_symbols(names: &[&str]) -> Result<Vec<Option<u64>>> {
    let mut addresses = if crate::ksucalls::get_info().uapi_version >= 6 {
        match crate::ksucalls::lookup_symbols(names) {
            Ok(found) => found,
            Err(error) => {
                warn!("SUSFS kernel resolver failed: {error:#}; falling back to original resolver");
                vec![None; names.len()]
            }
        }
    } else {
        vec![None; names.len()]
    };
    ensure!(
        addresses.len() == names.len(),
        "Kernel resolver returned a mismatched address count"
    );
    if addresses.iter().any(Option::is_none) {
        match ksuinit::resolve_kallsyms(names) {
            Ok(fallback) => {
                ensure!(
                    fallback.len() == names.len(),
                    "Fallback resolver returned a mismatched address count"
                );
                for (address, fallback) in addresses.iter_mut().zip(fallback) {
                    if address.is_none() {
                        *address = fallback;
                    }
                }
            }
            Err(error) => warn!("SUSFS fallback resolver unavailable: {error:#}"),
        }
    }
    Ok(addresses)
}

fn resolve_imports(names: &[&str]) -> Result<Vec<Option<u64>>> {
    let addresses = resolve_symbols(names)?;
    let missing = missing_imports(names, &addresses);
    ensure!(
        missing.is_empty(),
        "Refusing SUSFS init_module: unresolved/invalid imports: {}",
        missing.join(", ")
    );
    Ok(addresses)
}

fn preflight(kmi: &str) -> Result<()> {
    let conflict_names = [
        "susfs_is_inode_sus_path",
        "susfs_sus_kstat_spoof_generic_fillattr",
    ];
    let conflicts = resolve_symbols(&conflict_names)?;
    ensure!(
        conflicts.iter().all(Option::is_none),
        "Source-integrated SUSFS symbols are present; guard load refused"
    );
    let mut required = vec![
        "sys_call_table",
        "kallsyms_lookup_name",
        "kallsyms_lookup",
        "register_kprobe",
        "unregister_kprobe",
        "register_kretprobe",
        "unregister_kretprobe",
        "task_work_add",
        "saved_boot_config",
        "init_mm",
        "__set_fixmap",
        "memstart_addr",
        "arm64_use_ng_mappings",
        "copy_to_kernel_nofault",
        "security_secctx_to_secid",
        "stop_machine",
        "param_ops_bool",
        "param_ops_int",
        "param_ops_string",
        "param_ops_ulong",
        "__symbol_get",
        "__symbol_put",
        "synchronize_rcu_tasks",
    ];
    required.push(if matches!(kmi, "android16-6.12" | "android17-6.18") {
        "static_calls_table"
    } else {
        "security_hook_heads"
    });
    resolve_imports(&required)?;
    let flush_names = ["dcache_clean_inval_poc", "__flush_dcache_area"];
    let flush = resolve_symbols(&flush_names)?;
    ensure!(
        flush
            .iter()
            .any(|address| address.is_some_and(|address| address >= protocol::KERNEL_ADDRESS_MIN)),
        "No usable ARM64 cache flush helper"
    );
    Ok(())
}

fn get_config(key: &str) -> Result<Option<String>> {
    crate::module_config::get_config_value(
        CONFIG_ID,
        key,
        crate::module_config::ConfigType::Persist,
    )
}

fn put_config(key: &str, value: &str) -> Result<()> {
    crate::module_config::set_config_value(
        CONFIG_ID,
        key,
        value,
        crate::module_config::ConfigType::Persist,
    )
}

pub fn autoload_enabled() -> Result<bool> {
    Ok(get_config(AUTOLOAD_KEY)?
        .as_deref()
        .is_some_and(crate::module_config::parse_bool_config))
}

pub fn set_autoload(enabled: bool, kmi: Option<&str>) -> Result<()> {
    let _operation = OperationLock::acquire()?;
    if !enabled {
        put_config(AUTOLOAD_KEY, "false")?;
        println!("SUSFS auto-load disabled");
        return Ok(());
    }
    require_arm64()?;
    ensure!(
        Probe::read().provider != Provider::Builtin,
        "SUSFS is built in; guard auto-load is unnecessary"
    );
    let chosen = select_kmi(kmi)?;
    put_config(FALLBACK_KMI_KEY, &chosen)?;
    put_config(AUTOLOAD_KEY, "true")?;
    println!("SUSFS auto-load enabled (KMI fallback: {chosen})");
    Ok(())
}

/// Unknown or incompatible engines conflict; managed XKCNSU engines omit this hook.
pub fn set_conflict_state(conflict: bool) -> Result<()> {
    let _operation = OperationLock::acquire()?;
    apply_conflict_state(conflict)
}

fn apply_conflict_state(conflict: bool) -> Result<()> {
    let before = Probe::read();
    if !before.module_present {
        return Ok(());
    }
    ensure!(
        before.owned_guard(),
        "Resident guard has no XKCNSU conflict-control contract"
    );
    let expected = if conflict { "1" } else { "0" };
    write_parameter("kpm_conflict", expected)
        .context("Cannot synchronize SUSFS AVC conflict state")?;
    ensure!(
        read_bool("kpm_conflict") == Some(conflict),
        "SUSFS AVC conflict state readback failed"
    );
    if conflict {
        ensure!(
            read_bool("avc_active") == Some(false),
            "SUSFS AVC probe did not disarm; KPM activation must remain blocked"
        );
    }
    Ok(())
}

pub fn load(kmi: Option<&str>) -> Result<()> {
    let _operation = OperationLock::acquire()?;
    require_arm64()?;
    let before = Probe::read();
    ensure!(
        before.provider == Provider::Absent,
        "SUSFS provider {:?} already exists; no guard was loaded",
        before.provider
    );
    let chosen = select_kmi(kmi)?;
    preflight(&chosen)?;
    let data = crate::assets::get_asset_data(&format!("{chosen}_susfs_guard_lkm.ko"))?;
    ensure!(
        protocol::arm64_module_elf(&data),
        "SUSFS asset is not an ARM64 ELF relocatable module"
    );
    let conflict = crate::kpm::avc_conflict();
    let params = CString::new(format!("kpm_conflict={}", u8::from(conflict)))?;
    ksuinit::load_module_with_resolver(&data, &params, resolve_imports)
        .context("Failed to load SUSFS guard with the existing XKCNSU loader")?;
    let after = Probe::read();
    if !after.supported() || after.provider != Provider::Lkm {
        let reason = after
            .reason()
            .unwrap_or_else(|| "Unexpected SUSFS provider after load".into());
        let rollback = if after.owned_guard() {
            unload_guard()
        } else {
            bail!("Guard load verification failed: {reason}")
        };
        bail!("Guard load verification failed: {reason}; rollback: {rollback:?}");
    }
    // Recheck after symbol binding; later transitions use the coordinator API.
    if let Err(error) = apply_conflict_state(crate::kpm::avc_conflict()) {
        let rollback = unload_guard();
        bail!("Guard conflict-state verification failed: {error:#}; rollback: {rollback:?}");
    }
    println!("Loaded SUSFS guard LKM for KMI {chosen}");
    Ok(())
}

pub fn unload() -> Result<()> {
    let _operation = OperationLock::acquire()?;
    unload_guard()
}

fn unload_guard() -> Result<()> {
    let before = Probe::read();
    ensure!(
        before.module_present && before.provider == Provider::Lkm,
        "No independent SUSFS guard is loaded (provider {:?})",
        before.provider
    );
    ensure!(
        before.owned_guard(),
        "Refusing to unload a foreign guard without verified restoration"
    );
    ensure!(
        before.restore_error == Some(0),
        "SUSFS restoration previously failed; reboot required"
    );
    write_parameter("prepare_unload", "1")
        .context("SUSFS teardown failed; guard and root references retained")?;
    ensure!(
        read_bool("xkcnsu_unload_ready") == Some(true)
            && read_error("xkcnsu_restore_error") == Some(0),
        "SUSFS teardown verification failed; refusing delete_module"
    );
    for attempt in 0..5 {
        match rustix::system::delete_module(c"susfs_guard_lkm", 0) {
            Ok(()) => break,
            Err(error)
                if (error == rustix::io::Errno::BUSY || error == rustix::io::Errno::AGAIN)
                    && attempt < 4 =>
            {
                thread::sleep(Duration::from_millis(20));
            }
            Err(error) => {
                return Err(error).context(
                    "Guard is prepared but delete_module failed; no force-unload attempted",
                );
            }
        }
    }
    let after = Probe::read();
    ensure!(
        !after.module_present && after.provider == Provider::Absent,
        "SUSFS unload returned but a module or protocol remains"
    );
    println!("Unloaded SUSFS guard LKM");
    Ok(())
}

pub fn load_if_autoload_enabled() -> Result<()> {
    if !autoload_enabled()? {
        return Ok(());
    }
    let before = Probe::read();
    if before.supported() {
        info!(
            "SUSFS {:?} already available; auto-load skipped",
            before.provider
        );
        if before.provider == Provider::Lkm {
            set_conflict_state(crate::kpm::avc_conflict())?;
        }
        return Ok(());
    }
    let fallback = get_config(FALLBACK_KMI_KEY)?;
    // A detected KMI is authoritative; the saved value is used only when detection fails.
    let detected = crate::boot_patch::get_current_kmi().ok();
    load(detected.as_deref().or(fallback.as_deref()))
}

pub fn version() -> Result<String> {
    query::<16>(SHOW_VERSION).context("SUSFS version probe is unsupported or malformed")
}

pub fn variant() -> Result<String> {
    query::<16>(SHOW_VARIANT).context("SUSFS variant probe is unsupported or malformed")
}

pub fn show_version() -> Result<()> {
    println!("{}", version()?);
    Ok(())
}

pub fn show_variant() -> Result<()> {
    println!("{}", variant()?);
    Ok(())
}

pub fn show_features(check_only: bool) -> Result<()> {
    let probe = Probe::read();
    ensure!(
        probe.supported(),
        "{}",
        probe.reason().unwrap_or_else(|| "Unsupported".into())
    );
    if check_only {
        println!("Supported");
    } else {
        for feature in probe.features {
            println!("{feature}");
        }
    }
    Ok(())
}

/// Stable UI contract. A resident but failed guard is loaded=true, supported=false.
/// Configuration errors propagate so callers can retain the previous UI state.
pub fn status_json() -> Result<Value> {
    let probe = Probe::read();
    let autoload = autoload_enabled().context("Cannot read SUSFS auto-load config")?;
    let detected = crate::boot_patch::get_current_kmi().ok();
    let kmi = match detected {
        Some(kmi) => Some(kmi),
        None => get_config(FALLBACK_KMI_KEY).context("Cannot read SUSFS fallback KMI config")?,
    };
    let supported = probe.runtime_supported();
    let reason = if supported { None } else { probe.reason() };
    Ok(json!({
        "supported": supported,
        "loaded": probe.module_present || (supported && probe.provider != Provider::Builtin),
        "builtin": probe.provider == Provider::Builtin,
        "autoload": autoload,
        "version": probe.version,
        "variant": probe.variant,
        "kmi": kmi,
        "reason": reason,
    }))
}

pub fn status() -> Result<()> {
    println!("{}", status_json()?);
    Ok(())
}
