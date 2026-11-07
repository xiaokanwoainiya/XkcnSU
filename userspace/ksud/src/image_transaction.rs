#[cfg(any(target_os = "android", test))]
use anyhow::bail;
use anyhow::{Context, Result, ensure};
#[cfg(any(target_os = "android", test))]
use serde::{Deserialize, Serialize};
#[cfg(any(target_os = "android", test))]
use std::collections::BTreeSet;
#[cfg(any(target_os = "android", test))]
use std::fs::OpenOptions;
use std::fs::{self, File};
#[cfg(any(target_os = "android", test))]
use std::io::Read;
use std::io::Write;
use std::path::Path;
#[cfg(any(target_os = "android", test))]
use std::path::PathBuf;

#[cfg(any(target_os = "android", test))]
const MAX_PARTITION_BYTES: u64 = 512 * 1024 * 1024;

#[cfg(any(target_os = "android", test))]
const LEGACY_IMAGE_ROOT: &str = "/data/adb/xkcnsu";
#[cfg(any(target_os = "android", test))]
const IMAGE_ROOT: &str = "/data/adb/ksu/XKCNSU";
#[cfg(any(target_os = "android", test))]
const LEGACY_KPM_ROOT: &str = "/data/adb/xkcnsu/kpm";
#[cfg(any(target_os = "android", test))]
const KPM_ROOT: &str = "/data/adb/ksu/XKCNSU/kpm";

#[cfg(any(target_os = "android", test))]
#[derive(Debug)]
pub struct ImageWrite {
    pub target: PathBuf,
    pub image: Vec<u8>,
    pub expected_sha256: Option<String>,
}

#[cfg(any(target_os = "android", test))]
#[derive(Debug)]
pub struct StateWrite {
    pub path: PathBuf,
    pub contents: Vec<u8>,
}

#[cfg(any(target_os = "android", test))]
#[derive(Debug, Clone, Serialize, Deserialize)]
struct StateChange {
    path: PathBuf,
    before: Option<Vec<u8>>,
    after: Vec<u8>,
}

#[cfg(any(target_os = "android", test))]
#[derive(Debug, Clone, Serialize, Deserialize)]
struct Entry {
    target: PathBuf,
    backup: PathBuf,
    staged: PathBuf,
    bytes: u64,
    before_sha256: String,
    after_sha256: String,
}

#[cfg(any(target_os = "android", test))]
#[derive(Debug, Serialize, Deserialize)]
struct Journal {
    version: u32,
    label: String,
    state: String,
    entries: Vec<Entry>,
    #[serde(default)]
    states: Vec<StateChange>,
    #[serde(default)]
    attempted: usize,
    #[serde(default)]
    device_serial: Option<String>,
    #[serde(default)]
    slot: Option<String>,
}

#[cfg(any(target_os = "android", test))]
#[derive(Debug)]
pub struct Receipt {
    pub journal: PathBuf,
}

pub fn secure_dir(path: &Path) -> Result<()> {
    fs::create_dir_all(path).with_context(|| format!("create {}", path.display()))?;
    ensure!(
        !fs::symlink_metadata(path)?.file_type().is_symlink(),
        "state directory is a symlink"
    );
    #[cfg(unix)]
    {
        use std::os::unix::fs::PermissionsExt;
        fs::set_permissions(path, fs::Permissions::from_mode(0o700))?;
    }
    Ok(())
}

pub fn atomic_write(path: &Path, bytes: &[u8]) -> Result<()> {
    let parent = path
        .parent()
        .filter(|parent| !parent.as_os_str().is_empty())
        .unwrap_or_else(|| Path::new("."));
    secure_dir(parent)?;
    if let Ok(metadata) = fs::symlink_metadata(path) {
        ensure!(
            !metadata.file_type().is_symlink(),
            "output is a symlink: {}",
            path.display()
        );
    }
    let mut file = tempfile::NamedTempFile::new_in(parent)?;
    file.write_all(bytes)?;
    file.as_file().sync_all()?;
    file.persist(path)
        .with_context(|| format!("persist {}", path.display()))?;
    #[cfg(unix)]
    File::open(parent)?.sync_all()?;
    Ok(())
}

#[cfg(any(target_os = "android", test))]
pub fn capacity(path: &Path) -> Result<u64> {
    let metadata = fs::metadata(path).with_context(|| format!("stat {}", path.display()))?;
    if metadata.is_file() {
        return Ok(metadata.len());
    }
    #[cfg(target_os = "android")]
    {
        use std::os::fd::AsRawFd;
        use std::os::unix::fs::FileTypeExt;
        ensure!(
            metadata.file_type().is_block_device(),
            "target is not a block device"
        );
        let file = File::open(path)?;
        let mut size: u64 = 0;
        // BLKGETSIZE64: query only; do not change device read-only flags.
        let request = libc::_IOR::<u64>(0x12, 114);
        let result = unsafe { libc::ioctl(file.as_raw_fd(), request, &raw mut size) };
        ensure!(
            result == 0,
            "cannot query partition size: {}",
            std::io::Error::last_os_error()
        );
        Ok(size)
    }
    #[cfg(not(target_os = "android"))]
    bail!("target is not a regular file")
}

#[cfg(any(target_os = "android", test))]
pub fn read_image(path: &Path) -> Result<Vec<u8>> {
    let size = capacity(path)?;
    ensure!(
        size > 0 && size <= MAX_PARTITION_BYTES,
        "unsupported partition size: {size}"
    );
    let mut bytes = Vec::with_capacity(usize::try_from(size)?);
    File::open(path)?.take(size).read_to_end(&mut bytes)?;
    ensure!(
        bytes.len() as u64 == size,
        "short read from {}",
        path.display()
    );
    Ok(bytes)
}

#[cfg(any(target_os = "android", test))]
fn write_image(path: &Path, bytes: &[u8]) -> Result<()> {
    let mut file = OpenOptions::new().write(true).truncate(false).open(path)?;
    file.write_all(bytes)?;
    file.sync_all()?;
    ensure!(
        sha256::digest(read_image(path)?) == sha256::digest(bytes),
        "partition read-back mismatch: {}",
        path.display()
    );
    Ok(())
}

#[cfg(any(target_os = "android", test))]
fn save_journal(path: &Path, journal: &Journal) -> Result<()> {
    atomic_write(path, &serde_json::to_vec_pretty(journal)?)
}

#[cfg(any(target_os = "android", test))]
fn read_state_file(path: &Path) -> Result<Option<Vec<u8>>> {
    match fs::read(path) {
        Ok(bytes) => {
            ensure!(bytes.len() <= 64 * 1024, "transaction state exceeds 64 KiB");
            Ok(Some(bytes))
        }
        Err(error) if error.kind() == std::io::ErrorKind::NotFound => Ok(None),
        Err(error) => Err(error).with_context(|| format!("read state {}", path.display())),
    }
}

#[cfg(any(target_os = "android", test))]
fn restore_state(
    state: &StateChange,
    publisher: &mut impl FnMut(&Path, &[u8]) -> Result<()>,
) -> Result<()> {
    let current = read_state_file(&state.path)?;
    if current == state.before {
        return Ok(());
    }
    ensure!(
        current.as_deref() == Some(state.after.as_slice()),
        "state was changed by another transaction: {}",
        state.path.display()
    );
    if let Some(before) = &state.before {
        publisher(&state.path, before)?;
    } else {
        fs::remove_file(&state.path)?;
        #[cfg(unix)]
        File::open(state.path.parent().context("state has no parent")?)?.sync_all()?;
    }
    ensure!(
        read_state_file(&state.path)? == state.before,
        "state restoration verification failed"
    );
    Ok(())
}

#[cfg(any(target_os = "android", test))]
fn restore(
    journal_path: &Path,
    journal: &mut Journal,
    count: usize,
    writer: &mut impl FnMut(&Path, &[u8]) -> Result<()>,
    publisher: &mut impl FnMut(&Path, &[u8]) -> Result<()>,
    persist_journal: &mut impl FnMut(&Path, &Journal) -> Result<()>,
) -> Result<()> {
    claim_pending(journal_path)?;
    journal.attempted = journal.attempted.max(count);
    let mut failures = Vec::new();
    journal.state = "restoring".into();
    if let Err(error) = persist_journal(journal_path, journal) {
        log::error!("Cannot persist recovery start; still restoring images: {error:#}");
    }
    for entry in journal.entries.iter().take(count).rev() {
        let result = (|| -> Result<()> {
            let backup = fs::read(&entry.backup)?;
            ensure!(backup.len() as u64 == entry.bytes, "backup size mismatch");
            ensure!(
                sha256::digest(&backup) == entry.before_sha256,
                "backup hash mismatch"
            );
            ensure!(
                capacity(&entry.target)? == entry.bytes,
                "partition size changed"
            );
            if read_image(&entry.target)
                .is_ok_and(|bytes| sha256::digest(bytes) == entry.before_sha256)
            {
                return Ok(());
            }
            writer(&entry.target, &backup)?;
            ensure!(
                sha256::digest(read_image(&entry.target)?) == entry.before_sha256,
                "rollback read-back mismatch"
            );
            Ok(())
        })();
        if let Err(error) = result {
            failures.push(format!("{}: {error:#}", entry.target.display()));
        }
    }
    for state in journal.states.iter().rev() {
        if let Err(error) = restore_state(state, publisher) {
            failures.push(format!("{}: {error:#}", state.path.display()));
        }
    }
    journal.state = if failures.is_empty() {
        "rolled_back"
    } else {
        "recovery_required"
    }
    .into();
    if let Err(error) = persist_journal(journal_path, journal) {
        failures.push(format!(
            "recovery journal could not be persisted: {error:#}"
        ));
    }
    if failures.is_empty() {
        clear_pending(journal_path)?;
    }
    ensure!(
        failures.is_empty(),
        "recovery requires attention: {}",
        failures.join("; ")
    );
    Ok(())
}

#[cfg(any(target_os = "android", test))]
fn install_with(
    label: &str,
    images: &[ImageWrite],
    directory: &Path,
    writer: &mut impl FnMut(&Path, &[u8]) -> Result<()>,
) -> Result<Receipt> {
    install_with_operations(
        label,
        images,
        &[],
        directory,
        writer,
        &mut atomic_write,
        &mut save_journal,
    )
}

#[cfg(any(target_os = "android", test))]
fn install_with_operations(
    label: &str,
    images: &[ImageWrite],
    states: &[StateWrite],
    directory: &Path,
    writer: &mut impl FnMut(&Path, &[u8]) -> Result<()>,
    publisher: &mut impl FnMut(&Path, &[u8]) -> Result<()>,
    persist_journal: &mut impl FnMut(&Path, &Journal) -> Result<()>,
) -> Result<Receipt> {
    ensure!(!images.is_empty(), "no images to install");
    secure_dir(directory)?;
    let _lock = operation_lock(directory)?;
    let pending = pending_path(directory);
    #[cfg(target_os = "android")]
    let legacy_pending = Path::new(LEGACY_IMAGE_ROOT).join("image-pending.json");
    #[cfg(not(target_os = "android"))]
    let legacy_pending = pending.clone();
    check_install_pending(&pending, &legacy_pending)?;
    for previous in fs::read_dir(directory)? {
        let journal_path = previous?.path().join("journal.json");
        if journal_path.is_file() {
            let previous: Journal = serde_json::from_slice(&fs::read(&journal_path)?)?;
            ensure!(
                matches!(previous.state.as_str(), "committed" | "rolled_back"),
                "an interrupted image operation needs recovery: {}",
                journal_path.display()
            );
        }
    }
    let operation = tempfile::Builder::new()
        .prefix("transaction-")
        .tempdir_in(directory)?;
    let operation = operation.keep();
    let journal_path = operation.join("journal.json");
    let mut journal = Journal {
        version: 1,
        label: label.into(),
        state: "preparing".into(),
        entries: Vec::new(),
        states: Vec::new(),
        attempted: 0,
        #[cfg(target_os = "android")]
        device_serial: crate::utils::getprop("ro.serialno"),
        #[cfg(not(target_os = "android"))]
        device_serial: None,
        #[cfg(target_os = "android")]
        slot: Some(crate::boot_patch::get_slot_suffix(false)),
        #[cfg(not(target_os = "android"))]
        slot: None,
    };
    let mut state_paths = BTreeSet::new();
    for state in states {
        ensure!(
            state.contents.len() <= 64 * 1024,
            "transaction state exceeds 64 KiB"
        );
        let parent = state.path.parent().context("state has no parent")?;
        secure_dir(parent)?;
        let path = parent
            .canonicalize()?
            .join(state.path.file_name().context("state has no name")?);
        ensure!(
            state_paths.insert(path.clone()),
            "duplicate transaction state file"
        );
        if let Ok(metadata) = fs::symlink_metadata(&path) {
            ensure!(
                metadata.is_file() && !metadata.file_type().is_symlink(),
                "state is not a regular file"
            );
        }
        journal.states.push(StateChange {
            before: read_state_file(&path)?,
            path,
            after: state.contents.clone(),
        });
    }
    let mut targets = BTreeSet::new();
    for (index, image) in images.iter().enumerate() {
        let target = image.target.canonicalize()?;
        ensure!(targets.insert(target.clone()), "duplicate target partition");
        ensure!(
            !state_paths.contains(&target),
            "state file aliases a target image"
        );
        let original = read_image(&target)?;
        if let Some(expected) = &image.expected_sha256 {
            ensure!(
                sha256::digest(&original) == *expected,
                "source partition changed while the candidate was prepared"
            );
        }
        ensure!(
            !image.image.is_empty() && image.image.len() <= original.len(),
            "candidate does not fit target"
        );
        let mut candidate = image.image.clone();
        candidate.resize(original.len(), 0);
        let backup = operation.join(format!("{index}.before.img"));
        let staged = operation.join(format!("{index}.after.img"));
        atomic_write(&backup, &original)?;
        atomic_write(&staged, &candidate)?;
        let entry = Entry {
            target,
            backup,
            staged,
            bytes: original.len() as u64,
            before_sha256: sha256::digest(&original),
            after_sha256: sha256::digest(&candidate),
        };
        ensure!(
            sha256::digest(fs::read(&entry.backup)?) == entry.before_sha256,
            "backup verification failed"
        );
        ensure!(
            sha256::digest(fs::read(&entry.staged)?) == entry.after_sha256,
            "staged verification failed"
        );
        journal.entries.push(entry);
    }
    journal.state = "prepared".into();
    persist_journal(&journal_path, &journal)?;
    atomic_write(
        &pending_path(directory),
        &serde_json::to_vec(&journal_path)?,
    )?;
    let mut attempted = 0;
    let result = (|| -> Result<()> {
        // Check every original before the first write, including unchanged targets.
        for entry in &journal.entries {
            ensure!(
                sha256::digest(read_image(&entry.target)?) == entry.before_sha256,
                "target changed during preflight"
            );
        }
        for index in 0..journal.entries.len() {
            journal.state = format!("writing:{index}");
            journal.attempted = index + 1;
            persist_journal(&journal_path, &journal)?;
            let entry = &journal.entries[index];
            let candidate = fs::read(&entry.staged)?;
            ensure!(
                sha256::digest(&candidate) == entry.after_sha256,
                "staged image changed"
            );
            attempted = index + 1;
            writer(&entry.target, &candidate)?;
            ensure!(
                sha256::digest(read_image(&entry.target)?) == entry.after_sha256,
                "partition read-back mismatch"
            );
        }
        for index in 0..journal.states.len() {
            journal.state = format!("publishing:{index}");
            persist_journal(&journal_path, &journal)?;
            let state = &journal.states[index];
            ensure!(
                read_state_file(&state.path)? == state.before,
                "state changed during image preparation"
            );
            publisher(&state.path, &state.after)?;
            ensure!(
                read_state_file(&state.path)?.as_deref() == Some(state.after.as_slice()),
                "published state read-back mismatch"
            );
        }
        journal.state = "committed".into();
        persist_journal(&journal_path, &journal)?;
        clear_pending(&journal_path)
    })();
    if let Err(error) = result {
        if let Err(rollback) = restore(
            &journal_path,
            &mut journal,
            attempted,
            writer,
            publisher,
            persist_journal,
        ) {
            journal.state = "recovery_required".into();
            let _ = persist_journal(&journal_path, &journal);
            bail!(
                "install failed: {error:#}; rollback failed: {rollback:#}; recovery journal: {}",
                journal_path.display()
            );
        }
        bail!("install failed and original images were restored: {error:#}");
    }
    Ok(Receipt {
        journal: journal_path,
    })
}

#[cfg(any(target_os = "android", test))]
pub fn install(label: &str, images: &[ImageWrite], directory: &Path) -> Result<Receipt> {
    install_with(label, images, directory, &mut write_image)
}

#[cfg(any(target_os = "android", test))]
pub fn install_with_state(
    label: &str,
    images: &[ImageWrite],
    states: &[StateWrite],
    directory: &Path,
) -> Result<Receipt> {
    install_with_operations(
        label,
        images,
        states,
        directory,
        &mut write_image,
        &mut atomic_write,
        &mut save_journal,
    )
}

#[cfg(any(target_os = "android", test))]
fn device_operation_root(directory: &Path) -> &Path {
    if directory.starts_with(LEGACY_IMAGE_ROOT) {
        Path::new(LEGACY_IMAGE_ROOT)
    } else {
        Path::new(IMAGE_ROOT)
    }
}

#[cfg(any(target_os = "android", test))]
fn check_install_pending(pending: &Path, legacy_pending: &Path) -> Result<()> {
    for path in [pending, legacy_pending] {
        match fs::symlink_metadata(path) {
            Ok(_) => bail!(
                "an interrupted image operation needs recovery: {}",
                path.display()
            ),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => {}
            Err(error) => {
                return Err(error).with_context(|| format!("inspect pending {}", path.display()));
            }
        }
    }
    Ok(())
}

#[cfg(any(target_os = "android", test))]
pub fn operation_lock(directory: &Path) -> Result<File> {
    #[cfg(target_os = "android")]
    let lock_path = device_operation_root(directory).join("image-operation.lock");
    #[cfg(not(target_os = "android"))]
    let lock_path = directory
        .parent()
        .unwrap_or(directory)
        .join("image-operation.lock");
    secure_dir(lock_path.parent().context("lock has no parent")?)?;
    let lock = OpenOptions::new()
        .read(true)
        .write(true)
        .create(true)
        .truncate(false)
        .open(lock_path)?;
    try_lock_file(&lock)?;
    Ok(lock)
}

#[cfg(target_os = "android")]
pub fn try_lock_file(lock: &File) -> Result<()> {
    use std::os::fd::AsRawFd;
    let result = unsafe { libc::flock(lock.as_raw_fd(), libc::LOCK_EX | libc::LOCK_NB) };
    ensure!(
        result == 0,
        "cannot acquire image operation lock: {}",
        std::io::Error::last_os_error()
    );
    Ok(())
}

#[cfg(all(not(target_os = "android"), test))]
fn try_lock_file(lock: &File) -> Result<()> {
    lock.try_lock()
        .map_err(|error| anyhow::anyhow!("another image operation is running: {error}"))?;
    Ok(())
}

#[cfg(any(target_os = "android", test))]
fn pending_path(directory: &Path) -> PathBuf {
    #[cfg(target_os = "android")]
    {
        device_operation_root(directory).join("image-pending.json")
    }
    #[cfg(not(target_os = "android"))]
    directory
        .parent()
        .unwrap_or(directory)
        .join("image-pending.json")
}

#[cfg(any(target_os = "android", test))]
fn clear_pending(journal: &Path) -> Result<()> {
    let directory = journal
        .parent()
        .context("journal has no operation directory")?
        .parent()
        .context("journal has no transaction directory")?;
    let pending = pending_path(directory);
    if pending.try_exists()? {
        let owner: PathBuf = serde_json::from_slice(&fs::read(&pending)?)?;
        ensure!(
            owner == journal,
            "another transaction owns the recovery record"
        );
        fs::remove_file(&pending)?;
        #[cfg(unix)]
        File::open(pending.parent().context("pending record has no parent")?)?.sync_all()?;
    }
    Ok(())
}

#[cfg(any(target_os = "android", test))]
fn claim_pending(journal: &Path) -> Result<()> {
    let directory = journal
        .parent()
        .context("journal has no operation directory")?
        .parent()
        .context("journal has no transaction directory")?;
    let pending = pending_path(directory);
    if pending.try_exists()? {
        let owner: PathBuf = serde_json::from_slice(&fs::read(&pending)?)?;
        ensure!(
            owner == journal,
            "another transaction owns unfinished recovery"
        );
    } else {
        atomic_write(&pending, &serde_json::to_vec(journal)?)?;
    }
    Ok(())
}

#[cfg(test)]
pub fn rollback(receipt: &Receipt) -> Result<()> {
    let directory = receipt
        .journal
        .parent()
        .context("journal has no operation directory")?
        .parent()
        .context("journal has no transaction directory")?;
    let _lock = operation_lock(directory)?;
    let mut journal: Journal = serde_json::from_slice(&fs::read(&receipt.journal)?)?;
    ensure!(
        journal.version == 1 && journal.state == "committed",
        "transaction cannot be rolled back"
    );
    validate_committed_rollback(&journal)?;
    let count = journal.entries.len();
    restore(
        &receipt.journal,
        &mut journal,
        count,
        &mut write_image,
        &mut atomic_write,
        &mut save_journal,
    )
}

#[cfg(any(target_os = "android", test))]
fn validate_committed_rollback(journal: &Journal) -> Result<()> {
    for entry in &journal.entries {
        let current = sha256::digest(read_image(&entry.target)?);
        ensure!(
            current == entry.before_sha256 || current == entry.after_sha256,
            "refusing to roll back over a later image: {}",
            entry.target.display()
        );
    }
    for state in &journal.states {
        let current = read_state_file(&state.path)?;
        ensure!(
            current == state.before || current.as_deref() == Some(state.after.as_slice()),
            "refusing to roll back over a later state: {}",
            state.path.display()
        );
    }
    Ok(())
}

#[cfg(any(target_os = "android", test))]
fn validate_recovery(journal_path: &Path, journal: &Journal) -> Result<bool> {
    if journal.state == "rolled_back" {
        for entry in &journal.entries {
            ensure!(
                sha256::digest(read_image(&entry.target)?) == entry.before_sha256,
                "rolled-back transaction is stale; no images were written"
            );
        }
        for state in &journal.states {
            ensure!(
                read_state_file(&state.path)? == state.before,
                "rolled-back state is stale; no images were written"
            );
        }
        return Ok(false);
    }
    if journal.state == "committed" {
        validate_committed_rollback(journal)?;
        return Ok(true);
    }
    let directory = journal_path
        .parent()
        .context("journal has no operation directory")?
        .parent()
        .context("journal has no transaction directory")?;
    let owner: PathBuf = serde_json::from_slice(
        &fs::read(pending_path(directory))
            .context("unfinished transaction has no global recovery ownership record")?,
    )?;
    ensure!(
        owner == journal_path,
        "another operation owns unfinished recovery"
    );
    Ok(true)
}

#[cfg(any(target_os = "android", test))]
fn read_recovery_file(path: &Path, limit: u64) -> Result<Vec<u8>> {
    let metadata = fs::symlink_metadata(path)?;
    ensure!(
        metadata.is_file() && !metadata.file_type().is_symlink() && metadata.len() <= limit,
        "invalid recovery file: {}",
        path.display()
    );
    let mut bytes = Vec::new();
    File::open(path)?.take(limit + 1).read_to_end(&mut bytes)?;
    ensure!(
        bytes.len() as u64 <= limit,
        "recovery file exceeds its limit"
    );
    Ok(bytes)
}

#[cfg(any(target_os = "android", test))]
fn reconcile_pending(
    pending: &Path,
    validate: impl FnOnce(&Path, &Journal) -> Result<()>,
) -> Result<()> {
    let owner: PathBuf = serde_json::from_slice(&read_recovery_file(pending, 4096)?)?;
    ensure!(owner.is_absolute(), "recovery journal path is not absolute");
    let journal_path = owner
        .canonicalize()
        .context("recovery journal is missing")?;
    ensure!(
        journal_path == owner,
        "recovery journal path is not canonical"
    );
    let mut journal: Journal =
        serde_json::from_slice(&read_recovery_file(&journal_path, 2 * 1024 * 1024)?)?;
    ensure!(
        journal.version == 1
            && !journal.entries.is_empty()
            && journal.entries.len() <= 3
            && journal.attempted <= journal.entries.len()
            && journal.states.len() <= 16,
        "invalid pending recovery journal"
    );
    let directory = journal_path.parent().context("journal has no parent")?;
    ensure!(
        pending_path(
            directory
                .parent()
                .context("journal has no transaction directory")?
        ) == pending,
        "pending journal belongs to another operation root"
    );
    validate(&journal_path, &journal)?;
    let mut before = true;
    let mut after = true;
    let mut targets = BTreeSet::new();
    for (index, entry) in journal.entries.iter().enumerate() {
        ensure!(
            targets.insert(&entry.target),
            "duplicate recovery partition"
        );
        ensure!(
            entry.bytes > 0 && entry.bytes <= MAX_PARTITION_BYTES,
            "invalid recovery partition size"
        );
        for (path, digest) in [
            (&entry.backup, &entry.before_sha256),
            (&entry.staged, &entry.after_sha256),
        ] {
            ensure!(
                path.parent() == Some(directory) && path.canonicalize()? == *path,
                "recovery image is outside its transaction"
            );
            let bytes = read_recovery_file(path, entry.bytes)?;
            ensure!(
                bytes.len() as u64 == entry.bytes && sha256::digest(&bytes) == *digest,
                "recovery image size or hash does not match"
            );
        }
        ensure!(
            capacity(&entry.target)? == entry.bytes,
            "recovery target size changed"
        );
        let current = sha256::digest(read_image(&entry.target)?);
        let original = current == entry.before_sha256;
        let candidate = current == entry.after_sha256;
        ensure!(
            original || candidate,
            "pending transaction target has an unknown image; use explicit image recovery: {}",
            entry.target.display()
        );
        if !matches!(journal.state.as_str(), "committed" | "rolled_back") {
            ensure!(
                index < journal.attempted || original,
                "an unattempted recovery target changed"
            );
        }
        before &= original;
        after &= candidate;
    }
    let mut state_paths = BTreeSet::new();
    for state in &journal.states {
        ensure!(
            state_paths.insert(&state.path)
                && state.after.len() <= 64 * 1024
                && state
                    .before
                    .as_ref()
                    .is_none_or(|bytes| bytes.len() <= 64 * 1024),
            "invalid recovery state"
        );
        if let Ok(metadata) = fs::symlink_metadata(&state.path) {
            ensure!(
                metadata.is_file() && !metadata.file_type().is_symlink(),
                "recovery state is not a regular file"
            );
        }
        let current = read_state_file(&state.path)?;
        let original = current == state.before;
        let candidate = current.as_deref() == Some(state.after.as_slice());
        ensure!(
            original || candidate,
            "pending transaction state changed; use explicit image recovery: {}",
            state.path.display()
        );
        before &= original;
        after &= candidate;
    }
    match journal.state.as_str() {
        "committed" => {
            ensure!(
                after,
                "committed transaction no longer matches its images and state"
            );
            clear_pending(&journal_path)?;
        }
        "rolled_back" => {
            ensure!(
                before,
                "rolled-back transaction no longer matches its originals"
            );
            clear_pending(&journal_path)?;
        }
        state => {
            let writing = state
                .strip_prefix("writing:")
                .and_then(|index| index.parse::<usize>().ok());
            let publishing = state
                .strip_prefix("publishing:")
                .and_then(|index| index.parse::<usize>().ok());
            ensure!(
                (state == "prepared" && journal.attempted == 0)
                    || writing.is_some_and(
                        |index| index < journal.entries.len() && journal.attempted == index + 1
                    )
                    || publishing.is_some_and(|index| index < journal.states.len()
                        && journal.attempted == journal.entries.len())
                    || matches!(state, "restoring" | "recovery_required"),
                "unknown pending transaction phase"
            );
            if after
                && journal.attempted == journal.entries.len()
                && !matches!(state, "restoring" | "recovery_required")
            {
                journal.state = "committed".into();
                save_journal(&journal_path, &journal)?;
                clear_pending(&journal_path)?;
            } else {
                let count = journal.attempted;
                restore(
                    &journal_path,
                    &mut journal,
                    count,
                    &mut write_image,
                    &mut atomic_write,
                    &mut save_journal,
                )?;
            }
        }
    }
    log::info!(
        "Reconciled pending image transaction: {}",
        journal_path.display()
    );
    Ok(())
}

#[cfg(target_os = "android")]
pub fn recover_pending() -> Result<()> {
    ensure!(
        rustix::process::getuid().is_root(),
        "image recovery requires root"
    );
    let pending = [IMAGE_ROOT, LEGACY_IMAGE_ROOT]
        .iter()
        .map(|root| Path::new(root).join("image-pending.json"))
        .filter_map(|path| match fs::symlink_metadata(&path) {
            Ok(_) => Some(Ok(path)),
            Err(error) if error.kind() == std::io::ErrorKind::NotFound => None,
            Err(error) => Some(Err(error)),
        })
        .collect::<std::io::Result<Vec<_>>>()?;
    ensure!(
        pending.len() <= 1,
        "multiple image operations need explicit recovery"
    );
    for pending in pending {
        let root = pending.parent().context("pending record has no parent")?;
        let _lock = operation_lock(root)?;
        reconcile_pending(&pending, |path, journal| {
            ensure!(
                path.starts_with(root),
                "recovery journal is outside its storage root"
            );
            ensure!(
                journal.device_serial == crate::utils::getprop("ro.serialno"),
                "recovery device does not match"
            );
            let slot = crate::boot_patch::get_slot_suffix(false);
            ensure!(
                matches!(slot.as_str(), "" | "_a" | "_b")
                    && journal.slot.as_deref() == Some(slot.as_str()),
                "recovery slot does not match"
            );
            let allowed = ["boot", "init_boot", "vendor_boot"]
                .iter()
                .filter_map(|name| {
                    PathBuf::from(format!("/dev/block/by-name/{name}{slot}"))
                        .canonicalize()
                        .ok()
                })
                .collect::<BTreeSet<_>>();
            for entry in &journal.entries {
                ensure!(
                    allowed.contains(&entry.target),
                    "recovery target is not an active-slot boot partition"
                );
            }
            let allowed_state = Path::new(root).join("kpm/integration").join(format!(
                "state{}.json",
                if slot.is_empty() { "_single" } else { &slot }
            ));
            ensure!(
                journal
                    .states
                    .iter()
                    .all(|state| state.path == allowed_state),
                "pending transaction contains an unknown state file"
            );
            Ok(())
        })
        .with_context(|| {
            format!(
                "Cannot recover pending image operation at {}",
                pending.display()
            )
        })?;
    }
    Ok(())
}

#[cfg(any(target_os = "android", test))]
fn relocated_kpm_path(path: &Path) -> Result<PathBuf> {
    use std::path::Component;
    ensure!(
        path.is_absolute()
            && path
                .components()
                .all(|part| matches!(part, Component::RootDir | Component::Normal(_))),
        "invalid migrated KPM path: {}",
        path.display()
    );
    let relative = path
        .strip_prefix(LEGACY_KPM_ROOT)
        .or_else(|_| path.strip_prefix(KPM_ROOT))
        .with_context(|| format!("unknown migrated KPM path: {}", path.display()))?;
    ensure!(
        !relative.as_os_str().is_empty(),
        "migrated KPM path refers to the storage root"
    );
    Ok(Path::new(KPM_ROOT).join(relative))
}

#[cfg(any(target_os = "android", test))]
fn relocate_migrated_journal(journal_path: &Path, journal: &mut Journal) -> Result<()> {
    if !journal_path.starts_with(KPM_ROOT) {
        return Ok(());
    }
    ensure!(
        relocated_kpm_path(journal_path)? == journal_path,
        "invalid migrated journal path"
    );
    let directory = journal_path.parent().context("journal has no parent")?;
    let migrated = journal
        .entries
        .iter()
        .flat_map(|entry| [&entry.backup, &entry.staged])
        .chain(journal.states.iter().map(|state| &state.path))
        .any(|path| path.starts_with(LEGACY_KPM_ROOT));
    ensure!(
        !migrated || matches!(journal.state.as_str(), "committed" | "rolled_back"),
        "unfinished or unknown legacy transaction cannot be relocated"
    );
    let paths = journal
        .entries
        .iter()
        .map(|entry| {
            let backup = relocated_kpm_path(&entry.backup)?;
            let staged = relocated_kpm_path(&entry.staged)?;
            ensure!(
                backup.starts_with(directory) && staged.starts_with(directory),
                "relocated image is outside its transaction"
            );
            Ok((backup, staged))
        })
        .collect::<Result<Vec<_>>>()?;
    let states = journal
        .states
        .iter()
        .map(|state| relocated_kpm_path(&state.path))
        .collect::<Result<Vec<_>>>()?;
    // Validate every path before changing the in-memory journal; payload bytes stay untouched.
    for (entry, (backup, staged)) in journal.entries.iter_mut().zip(paths) {
        entry.backup = backup;
        entry.staged = staged;
    }
    for (state, path) in journal.states.iter_mut().zip(states) {
        state.path = path;
    }
    Ok(())
}

#[cfg(target_os = "android")]
#[derive(clap::Subcommand, Debug)]
pub enum TransactionCommand {
    /// Verify every candidate and source, then write the selected active-slot images
    Install {
        manifest: PathBuf,
        #[arg(long)]
        apply: bool,
    },
    /// Restore exact partition backups from a verified transaction journal
    Recover {
        journal: PathBuf,
        #[arg(long)]
        apply: bool,
    },
}

#[cfg(target_os = "android")]
#[derive(Deserialize)]
struct DeviceManifest {
    version: u32,
    serial: String,
    slot: String,
    entries: Vec<DeviceEntry>,
}

#[cfg(target_os = "android")]
#[derive(Deserialize)]
struct DeviceEntry {
    partition: String,
    image: PathBuf,
    source_sha256: String,
    candidate_sha256: String,
}

#[cfg(target_os = "android")]
pub fn run(command: TransactionCommand) -> Result<()> {
    ensure!(
        rustix::process::getuid().is_root(),
        "image installation requires root"
    );
    let (manifest, apply) = match command {
        TransactionCommand::Install { manifest, apply } => (manifest, apply),
        TransactionCommand::Recover { journal, apply } => return recover_device(&journal, apply),
    };
    let manifest: DeviceManifest = serde_json::from_slice(&fs::read(manifest)?)?;
    ensure!(
        manifest.version == 1 && !manifest.entries.is_empty() && manifest.entries.len() <= 3,
        "invalid image manifest"
    );
    let actual_slot = crate::boot_patch::get_slot_suffix(false);
    ensure!(
        manifest.slot == actual_slot && matches!(actual_slot.as_str(), "" | "_a" | "_b"),
        "target slot changed"
    );
    ensure!(
        crate::utils::getprop("ro.serialno").as_deref() == Some(manifest.serial.as_str()),
        "target device serial changed"
    );
    let mut images = Vec::new();
    for entry in manifest.entries {
        ensure!(
            matches!(
                entry.partition.as_str(),
                "boot" | "init_boot" | "vendor_boot"
            ),
            "unsupported target partition"
        );
        ensure!(
            entry.source_sha256.len() == 64 && entry.candidate_sha256.len() == 64,
            "invalid source or candidate digest"
        );
        let target = PathBuf::from(format!(
            "/dev/block/by-name/{}{actual_slot}",
            entry.partition
        ));
        let image = fs::read(&entry.image)
            .with_context(|| format!("read candidate {}", entry.image.display()))?;
        ensure!(
            sha256::digest(&image) == entry.candidate_sha256,
            "candidate image changed"
        );
        ensure!(
            sha256::digest(read_image(&target)?) == entry.source_sha256,
            "target differs from its verified backup"
        );
        ensure!(
            image.len() as u64 == capacity(&target)?,
            "candidate and target partition sizes differ"
        );
        let magic: &[u8] = if entry.partition == "vendor_boot" {
            b"VNDRBOOT"
        } else {
            b"ANDROID!"
        };
        ensure!(
            image.starts_with(magic),
            "candidate image has the wrong container type"
        );
        let boot = android_bootimg::parser::BootImage::parse(&image)
            .context("candidate cannot be parsed")?;
        match entry.partition.as_str() {
            "boot" => ensure!(
                boot.get_blocks().get_kernel().is_some(),
                "boot candidate has no kernel"
            ),
            "init_boot" => ensure!(
                boot.get_blocks().get_kernel().is_none()
                    && boot.get_blocks().get_ramdisk().is_some(),
                "init_boot candidate layout is invalid"
            ),
            _ => {}
        }
        drop(boot);
        images.push(ImageWrite {
            target,
            image,
            expected_sha256: Some(entry.source_sha256),
        });
    }
    let receipt = if apply {
        Some(install(
            "xkcnsu-device-install",
            &images,
            &Path::new("/data/adb/ksu/XKCNSU").join("image_transactions"),
        )?)
    } else {
        None
    };
    println!(
        "{}",
        serde_json::json!({
            "ok": true,
            "data": {
                "validated": images.len(),
                "applied": apply,
                "journal": receipt.map(|receipt| receipt.journal),
                "reboot_required": apply,
            },
            "error": null,
        })
    );
    Ok(())
}

#[cfg(target_os = "android")]
fn recover_device(journal_path: &Path, apply: bool) -> Result<()> {
    let journal_path = journal_path.canonicalize()?;
    let directory = journal_path.parent().context("journal has no parent")?;
    let _lock = operation_lock(
        directory
            .parent()
            .context("journal has no transaction directory")?,
    )?;
    let mut journal: Journal = serde_json::from_slice(&fs::read(&journal_path)?)?;
    ensure!(
        journal.version == 1 && !journal.entries.is_empty() && journal.entries.len() <= 3,
        "invalid recovery journal"
    );
    ensure!(
        journal.device_serial == crate::utils::getprop("ro.serialno"),
        "recovery device does not match"
    );
    let slot = crate::boot_patch::get_slot_suffix(false);
    ensure!(
        journal.slot.as_deref() == Some(slot.as_str()),
        "recovery slot does not match"
    );
    relocate_migrated_journal(&journal_path, &mut journal)?;
    let allowed = ["boot", "init_boot", "vendor_boot"]
        .iter()
        .filter_map(|name| {
            PathBuf::from(format!("/dev/block/by-name/{name}{slot}"))
                .canonicalize()
                .ok()
        })
        .collect::<BTreeSet<_>>();
    for entry in &journal.entries {
        ensure!(
            allowed.contains(&entry.target),
            "recovery target is not an active-slot boot partition"
        );
        ensure!(
            entry.backup.canonicalize()?.starts_with(directory),
            "backup is outside its transaction"
        );
        let backup = fs::read(&entry.backup)?;
        ensure!(
            backup.len() as u64 == entry.bytes && capacity(&entry.target)? == entry.bytes,
            "recovery size does not match"
        );
        ensure!(
            sha256::digest(&backup) == entry.before_sha256,
            "recovery backup hash does not match"
        );
    }
    let needs_restore = validate_recovery(&journal_path, &journal)?;
    if apply && needs_restore {
        let count = if journal.state == "committed" {
            journal.entries.len()
        } else {
            journal.attempted
        };
        restore(
            &journal_path,
            &mut journal,
            count,
            &mut write_image,
            &mut atomic_write,
            &mut save_journal,
        )?;
    } else if apply {
        clear_pending(&journal_path)?;
    }
    println!(
        "{}",
        serde_json::json!({
            "ok": true,
            "data": {"validated": journal.entries.len(), "applied": apply, "journal": journal_path, "reboot_required": apply},
            "error": null,
        })
    );
    Ok(())
}

#[cfg(test)]
mod tests {
    use super::*;

    #[test]
    fn invalid_second_image_does_not_write_first_target() {
        let root = tempfile::tempdir().unwrap();
        let first = root.path().join("boot");
        let second = root.path().join("init_boot");
        fs::write(&first, b"original").unwrap();
        fs::write(&second, b"old").unwrap();
        let result = install(
            "test",
            &[
                ImageWrite {
                    target: first.clone(),
                    image: b"changed!".to_vec(),
                    expected_sha256: None,
                },
                ImageWrite {
                    target: second,
                    image: b"too long".to_vec(),
                    expected_sha256: None,
                },
            ],
            &root.path().join("transactions"),
        );
        assert!(result.is_err());
        assert_eq!(fs::read(first).unwrap(), b"original");
    }

    #[test]
    fn failed_second_write_restores_both_targets() {
        let root = tempfile::tempdir().unwrap();
        let first = root.path().join("boot");
        let second = root.path().join("init_boot");
        fs::write(&first, b"first-old").unwrap();
        fs::write(&second, b"second-old").unwrap();
        let mut calls = 0;
        let result = install_with(
            "test",
            &[
                ImageWrite {
                    target: first.clone(),
                    image: b"first-new".to_vec(),
                    expected_sha256: None,
                },
                ImageWrite {
                    target: second.clone(),
                    image: b"second-new".to_vec(),
                    expected_sha256: None,
                },
            ],
            &root.path().join("transactions"),
            &mut |path, data| {
                calls += 1;
                if calls == 2 {
                    OpenOptions::new()
                        .write(true)
                        .truncate(false)
                        .open(path)?
                        .write_all(b"partial")?;
                    bail!("injected write failure");
                }
                fs::write(path, data)?;
                Ok(())
            },
        );
        assert!(result.is_err());
        assert_eq!(fs::read(first).unwrap(), b"first-old");
        assert_eq!(fs::read(second).unwrap(), b"second-old");
    }

    #[test]
    fn committed_transaction_can_restore_exact_original() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("boot");
        fs::write(&target, b"old-image-padding").unwrap();
        let receipt = install(
            "test",
            &[ImageWrite {
                target: target.clone(),
                image: b"new".to_vec(),
                expected_sha256: None,
            }],
            &root.path().join("transactions"),
        )
        .unwrap();
        assert_eq!(&fs::read(&target).unwrap()[..3], b"new");
        rollback(&receipt).unwrap();
        assert_eq!(fs::read(target).unwrap(), b"old-image-padding");
    }

    #[test]
    fn stale_candidate_cannot_overwrite_another_installation() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("boot");
        fs::write(&target, b"another-install").unwrap();
        let result = install(
            "test",
            &[ImageWrite {
                target: target.clone(),
                image: b"candidate".to_vec(),
                expected_sha256: Some(sha256::digest(b"source-image")),
            }],
            &root.path().join("transactions"),
        );
        assert!(result.is_err());
        assert_eq!(fs::read(target).unwrap(), b"another-install");
    }

    #[test]
    fn journal_io_failure_does_not_prevent_restoration() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("boot");
        fs::write(&target, b"old-image").unwrap();
        let result = install_with_operations(
            "test",
            &[ImageWrite {
                target: target.clone(),
                image: b"new-image".to_vec(),
                expected_sha256: None,
            }],
            &[StateWrite {
                path: root.path().join("state.json"),
                contents: b"new-state".to_vec(),
            }],
            &root.path().join("transactions"),
            &mut write_image,
            &mut atomic_write,
            &mut |path, journal| {
                if matches!(journal.state.as_str(), "prepared" | "writing:0") {
                    save_journal(path, journal)
                } else {
                    bail!("injected journal I/O failure")
                }
            },
        );
        assert!(result.is_err());
        assert_eq!(fs::read(target).unwrap(), b"old-image");
    }

    #[test]
    fn one_failed_restoration_does_not_skip_other_partitions() {
        let root = tempfile::tempdir().unwrap();
        let first = root.path().join("boot");
        let second = root.path().join("init_boot");
        fs::write(&first, b"first-old").unwrap();
        fs::write(&second, b"second-old").unwrap();
        let mut calls = 0;
        let result = install_with(
            "test",
            &[
                ImageWrite {
                    target: first.clone(),
                    image: b"first-new".to_vec(),
                    expected_sha256: None,
                },
                ImageWrite {
                    target: second.clone(),
                    image: b"second-new".to_vec(),
                    expected_sha256: None,
                },
            ],
            &root.path().join("transactions"),
            &mut |path, data| {
                calls += 1;
                if path == second && calls >= 2 {
                    OpenOptions::new()
                        .write(true)
                        .truncate(false)
                        .open(path)?
                        .write_all(b"partial")?;
                    bail!("injected target I/O failure");
                }
                write_image(path, data)
            },
        );
        assert!(result.is_err());
        assert_eq!(fs::read(first).unwrap(), b"first-old");
        assert_ne!(fs::read(second).unwrap(), b"second-old");
    }

    #[test]
    fn an_old_receipt_cannot_undo_a_later_commit() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("boot");
        let directory = root.path().join("transactions");
        fs::write(&target, b"image-X").unwrap();
        let first = install(
            "A",
            &[ImageWrite {
                target: target.clone(),
                image: b"image-Y".to_vec(),
                expected_sha256: None,
            }],
            &directory,
        )
        .unwrap();
        install(
            "B",
            &[ImageWrite {
                target: target.clone(),
                image: b"image-Z".to_vec(),
                expected_sha256: None,
            }],
            &directory,
        )
        .unwrap();
        assert!(rollback(&first).is_err());
        assert_eq!(fs::read(target).unwrap(), b"image-Z");
    }

    fn failed_state_publication(publish_before_error: bool, initial_state: Option<&[u8]>) {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("boot");
        let state = root.path().join("state.json");
        fs::write(&target, b"old-image").unwrap();
        if let Some(bytes) = initial_state {
            fs::write(&state, bytes).unwrap();
        }
        let mut calls = 0;
        let result = install_with_operations(
            "test",
            &[ImageWrite {
                target: target.clone(),
                image: b"new-image".to_vec(),
                expected_sha256: None,
            }],
            &[StateWrite {
                path: state.clone(),
                contents: b"new-state".to_vec(),
            }],
            &root.path().join("transactions"),
            &mut write_image,
            &mut |path, bytes| {
                calls += 1;
                if calls == 1 {
                    if publish_before_error {
                        atomic_write(path, bytes)?;
                    }
                    bail!("injected publication failure");
                }
                atomic_write(path, bytes)
            },
            &mut save_journal,
        );
        assert!(result.is_err());
        assert_eq!(fs::read(target).unwrap(), b"old-image");
        assert_eq!(read_state_file(&state).unwrap().as_deref(), initial_state);
    }

    #[test]
    fn pre_rename_failure_restores_image_and_preserves_old_state() {
        failed_state_publication(false, Some(b"old-state"));
    }

    #[test]
    fn post_rename_failure_restores_image_and_old_state() {
        failed_state_publication(true, Some(b"old-state"));
    }

    #[test]
    fn post_rename_failure_restores_absent_state() {
        failed_state_publication(true, None);
    }

    #[test]
    fn image_and_state_publication_hold_the_same_lock() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("boot");
        let state = root.path().join("state.json");
        let directory = root.path().join("transactions");
        fs::write(&target, b"old-image").unwrap();
        install_with_operations(
            "test",
            &[ImageWrite {
                target,
                image: b"new-image".to_vec(),
                expected_sha256: None,
            }],
            &[StateWrite {
                path: state,
                contents: b"new-state".to_vec(),
            }],
            &directory,
            &mut write_image,
            &mut |path, bytes| {
                assert!(operation_lock(&directory).is_err());
                atomic_write(path, bytes)
            },
            &mut save_journal,
        )
        .unwrap();
    }

    #[test]
    fn committed_image_and_state_restore_together() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("boot");
        let state = root.path().join("state.json");
        fs::write(&target, b"old-image").unwrap();
        fs::write(&state, b"old-state").unwrap();
        let receipt = install_with_state(
            "test",
            &[ImageWrite {
                target: target.clone(),
                image: b"new-image".to_vec(),
                expected_sha256: None,
            }],
            &[StateWrite {
                path: state.clone(),
                contents: b"new-state".to_vec(),
            }],
            &root.path().join("transactions"),
        )
        .unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"new-image");
        assert_eq!(fs::read(&state).unwrap(), b"new-state");
        rollback(&receipt).unwrap();
        assert_eq!(fs::read(target).unwrap(), b"old-image");
        assert_eq!(fs::read(state).unwrap(), b"old-state");
    }

    #[test]
    fn rolled_back_record_cannot_overwrite_a_later_commit() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("boot");
        let state = root.path().join("state.json");
        let directory = root.path().join("transactions");
        fs::write(&target, b"image-X").unwrap();
        fs::write(&state, b"state-0").unwrap();
        let first = install_with_state(
            "A",
            &[ImageWrite {
                target: target.clone(),
                image: b"image-Y".to_vec(),
                expected_sha256: None,
            }],
            &[StateWrite {
                path: state.clone(),
                contents: b"state-1".to_vec(),
            }],
            &directory,
        )
        .unwrap();
        rollback(&first).unwrap();
        install_with_state(
            "B",
            &[ImageWrite {
                target: target.clone(),
                image: b"image-Z".to_vec(),
                expected_sha256: None,
            }],
            &[StateWrite {
                path: state.clone(),
                contents: b"state-2".to_vec(),
            }],
            &directory,
        )
        .unwrap();
        let old: Journal = serde_json::from_slice(&fs::read(&first.journal).unwrap()).unwrap();
        assert!(validate_recovery(&first.journal, &old).is_err());
        assert_eq!(fs::read(target).unwrap(), b"image-Z");
        assert_eq!(fs::read(state).unwrap(), b"state-2");
    }

    #[test]
    fn interrupted_recovery_blocks_installations_in_other_directories() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("boot");
        let first = root.path().join("A");
        let second = root.path().join("B");
        fs::write(&target, b"image-X").unwrap();
        let receipt = install(
            "A",
            &[ImageWrite {
                target: target.clone(),
                image: b"image-Y".to_vec(),
                expected_sha256: None,
            }],
            &first,
        )
        .unwrap();
        let mut journal: Journal =
            serde_json::from_slice(&fs::read(&receipt.journal).unwrap()).unwrap();
        journal.state = "recovery_required".into();
        save_journal(&receipt.journal, &journal).unwrap();
        atomic_write(
            &pending_path(&first),
            &serde_json::to_vec(&receipt.journal).unwrap(),
        )
        .unwrap();
        assert!(
            install(
                "B",
                &[ImageWrite {
                    target: target.clone(),
                    image: b"image-Z".to_vec(),
                    expected_sha256: None
                }],
                &second
            )
            .is_err()
        );
        assert!(validate_recovery(&receipt.journal, &journal).unwrap());
        assert_eq!(fs::read(target).unwrap(), b"image-Y");
    }

    #[test]
    fn interrupted_committed_recovery_can_resume_and_blocks_other_installers() {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("boot");
        let state = root.path().join("state.json");
        let directory = root.path().join("A");
        fs::write(&target, b"image-X").unwrap();
        fs::write(&state, b"state-0").unwrap();
        let receipt = install_with_state(
            "A",
            &[ImageWrite {
                target: target.clone(),
                image: b"image-Y".to_vec(),
                expected_sha256: None,
            }],
            &[StateWrite {
                path: state.clone(),
                contents: b"state-1".to_vec(),
            }],
            &directory,
        )
        .unwrap();
        let mut journal: Journal =
            serde_json::from_slice(&fs::read(&receipt.journal).unwrap()).unwrap();
        let interrupted = std::panic::catch_unwind(std::panic::AssertUnwindSafe(|| {
            restore(
                &receipt.journal,
                &mut journal,
                1,
                &mut write_image,
                &mut |_, _| -> Result<()> {
                    panic!("simulated interruption before state restore");
                },
                &mut save_journal,
            )
            .unwrap();
        }));
        assert!(interrupted.is_err());
        assert!(pending_path(&directory).exists());
        assert_eq!(fs::read(&target).unwrap(), b"image-X");
        assert_eq!(fs::read(&state).unwrap(), b"state-1");
        assert!(
            install(
                "B",
                &[ImageWrite {
                    target: target.clone(),
                    image: b"image-Z".to_vec(),
                    expected_sha256: None
                }],
                &root.path().join("B")
            )
            .is_err()
        );
        let mut saved: Journal =
            serde_json::from_slice(&fs::read(&receipt.journal).unwrap()).unwrap();
        assert!(validate_recovery(&receipt.journal, &saved).unwrap());
        restore(
            &receipt.journal,
            &mut saved,
            1,
            &mut write_image,
            &mut atomic_write,
            &mut save_journal,
        )
        .unwrap();
        assert!(!pending_path(&directory).exists());
        assert_eq!(fs::read(target).unwrap(), b"image-X");
        assert_eq!(fs::read(state).unwrap(), b"state-0");
    }

    fn legacy_kpm_history(state: &str) -> (PathBuf, Journal) {
        let relative = "integration/transactions_b/transaction-history";
        let old = Path::new(LEGACY_KPM_ROOT).join(relative);
        let path = Path::new(KPM_ROOT).join(relative).join("journal.json");
        let journal = Journal {
            version: 1,
            label: "enable-kpm".into(),
            state: state.into(),
            entries: vec![Entry {
                target: PathBuf::from("/dev/block/by-name/boot_b"),
                backup: old.join("0.before.img"),
                staged: old.join("0.after.img"),
                bytes: 9,
                before_sha256: sha256::digest(b"image-old"),
                after_sha256: sha256::digest(b"image-new"),
            }],
            states: vec![StateChange {
                path: Path::new(LEGACY_KPM_ROOT).join("integration/state_b.json"),
                before: Some(
                    br#"{"original_kernel":"/data/adb/xkcnsu/kpm/integration/old.original"}"#
                        .to_vec(),
                ),
                after: br#"{"original_kernel":"/data/adb/xkcnsu/kpm/integration/new.original"}"#
                    .to_vec(),
            }],
            attempted: 1,
            device_serial: Some("fixture-device".into()),
            slot: Some("_b".into()),
        };
        (path, journal)
    }

    #[test]
    fn completed_kpm_history_paths_relocate_without_changing_payloads() {
        for status in ["committed", "rolled_back"] {
            let (path, mut journal) = legacy_kpm_history(status);
            let original: Journal =
                serde_json::from_slice(&serde_json::to_vec(&journal).unwrap()).unwrap();
            relocate_migrated_journal(&path, &mut journal).unwrap();
            let directory = path.parent().unwrap();
            assert_eq!(journal.entries[0].backup, directory.join("0.before.img"));
            assert_eq!(journal.entries[0].staged, directory.join("0.after.img"));
            assert_eq!(
                journal.states[0].path,
                Path::new(KPM_ROOT).join("integration/state_b.json")
            );
            assert_eq!(journal.states[0].before, original.states[0].before);
            assert_eq!(journal.states[0].after, original.states[0].after);
            assert_eq!(journal.entries[0].target, original.entries[0].target);
            assert_eq!(journal.entries[0].bytes, original.entries[0].bytes);
            assert_eq!(
                journal.entries[0].before_sha256,
                original.entries[0].before_sha256
            );
            assert_eq!(
                journal.entries[0].after_sha256,
                original.entries[0].after_sha256
            );
            assert_eq!(journal.device_serial, original.device_serial);
            assert_eq!(journal.slot, original.slot);
            assert_eq!(journal.state, status);
            let relocated = serde_json::to_vec(&journal).unwrap();
            relocate_migrated_journal(&path, &mut journal).unwrap();
            assert_eq!(serde_json::to_vec(&journal).unwrap(), relocated);
        }
    }

    #[test]
    fn unfinished_or_unknown_legacy_history_cannot_relocate() {
        for status in [
            "prepared",
            "writing:0",
            "publishing:0",
            "restoring",
            "recovery_required",
            "unknown",
        ] {
            let (path, mut journal) = legacy_kpm_history(status);
            let original = serde_json::to_vec(&journal).unwrap();
            assert!(relocate_migrated_journal(&path, &mut journal).is_err());
            assert_eq!(serde_json::to_vec(&journal).unwrap(), original);
        }
    }

    #[test]
    fn unknown_or_out_of_transaction_history_paths_are_rejected() {
        for mutation in 0..6 {
            let (path, mut journal) = legacy_kpm_history("committed");
            match mutation {
                0 => {
                    journal.entries[0].backup =
                        PathBuf::from("/data/adb/xkcnsu/kpm-other/0.before.img");
                }
                1 => journal.entries[0].staged = PathBuf::from("/tmp/0.after.img"),
                2 => journal.states[0].path = PathBuf::from("/data/adb/ksu/.allowlist"),
                3 => {
                    journal.entries[0].backup = Path::new(LEGACY_KPM_ROOT)
                        .join("integration/transactions_b/another/0.before.img");
                }
                4 => {
                    journal.entries[0].staged = Path::new(LEGACY_KPM_ROOT)
                        .join("integration/transactions_b/transaction-history/../0.after.img");
                }
                _ => journal.states[0].path = PathBuf::from("integration/state_b.json"),
            }
            let original = serde_json::to_vec(&journal).unwrap();
            assert!(relocate_migrated_journal(&path, &mut journal).is_err());
            assert_eq!(serde_json::to_vec(&journal).unwrap(), original);
        }
    }

    #[test]
    fn relocated_interrupted_recovery_keeps_current_paths() {
        let (path, mut journal) = legacy_kpm_history("committed");
        relocate_migrated_journal(&path, &mut journal).unwrap();
        // A later interrupted recovery has already persisted the relocated paths.
        journal.state = "restoring".into();
        let original = serde_json::to_vec(&journal).unwrap();
        relocate_migrated_journal(&path, &mut journal).unwrap();
        assert_eq!(serde_json::to_vec(&journal).unwrap(), original);
    }

    #[test]
    fn legacy_and_unrelated_journals_are_not_relocated() {
        for path in [
            "/data/adb/xkcnsu/kpm/integration/transactions_b/old/journal.json",
            "/data/adb/ksu/XKCNSU/image_transactions/current/journal.json",
            "/data/adb/ksu/XKCNSU/kpm-other/current/journal.json",
        ] {
            let (_, mut journal) = legacy_kpm_history("recovery_required");
            let original = serde_json::to_vec(&journal).unwrap();
            relocate_migrated_journal(Path::new(path), &mut journal).unwrap();
            assert_eq!(serde_json::to_vec(&journal).unwrap(), original);
        }
    }

    #[test]
    fn legacy_and_current_directories_select_matching_global_roots() {
        for (directory, root) in [
            (
                "/data/adb/xkcnsu/kpm/integration/transactions_b",
                LEGACY_IMAGE_ROOT,
            ),
            ("/data/adb/xkcnsu/image_transactions", LEGACY_IMAGE_ROOT),
            (
                "/data/adb/ksu/XKCNSU/kpm/integration/transactions_b",
                IMAGE_ROOT,
            ),
            ("/data/adb/ksu/XKCNSU/image_transactions", IMAGE_ROOT),
            ("/data/adb/xkcnsu-other/image_transactions", IMAGE_ROOT),
        ] {
            assert_eq!(device_operation_root(Path::new(directory)), Path::new(root));
        }
    }

    #[test]
    fn legacy_pending_blocks_new_install_preflight() {
        let root = tempfile::tempdir().unwrap();
        let current = root.path().join("ksu/XKCNSU/image-pending.json");
        let legacy = root.path().join("xkcnsu/image-pending.json");
        fs::create_dir_all(legacy.parent().unwrap()).unwrap();
        let owner = serde_json::to_vec(&Path::new(LEGACY_KPM_ROOT).join("journal.json")).unwrap();
        fs::write(&legacy, &owner).unwrap();
        let error = check_install_pending(&current, &legacy).unwrap_err();
        assert!(error.to_string().contains("needs recovery"));
        assert_eq!(fs::read(&legacy).unwrap(), owner);
        assert!(!current.parent().unwrap().exists());
    }

    #[test]
    fn new_pending_blocks_install_without_creating_legacy_storage() {
        let root = tempfile::tempdir().unwrap();
        let current = root.path().join("ksu/XKCNSU/image-pending.json");
        let legacy = root.path().join("xkcnsu/image-pending.json");
        fs::create_dir_all(current.parent().unwrap()).unwrap();
        fs::write(&current, b"current-owner").unwrap();
        assert!(check_install_pending(&current, &legacy).is_err());
        assert_eq!(fs::read(&current).unwrap(), b"current-owner");
        assert!(!legacy.parent().unwrap().exists());
    }

    #[test]
    fn absent_pending_checks_do_not_create_storage() {
        let root = tempfile::tempdir().unwrap();
        let current = root.path().join("ksu/XKCNSU/image-pending.json");
        let legacy = root.path().join("xkcnsu/image-pending.json");
        check_install_pending(&current, &legacy).unwrap();
        assert!(!current.parent().unwrap().exists());
        assert!(!legacy.parent().unwrap().exists());
    }

    #[test]
    fn dangling_legacy_pending_is_not_bypassed() {
        let root = tempfile::tempdir().unwrap();
        let current = root.path().join("ksu/XKCNSU/image-pending.json");
        let legacy = root.path().join("xkcnsu/image-pending.json");
        fs::create_dir_all(legacy.parent().unwrap()).unwrap();
        std::os::unix::fs::symlink("missing-owner", &legacy).unwrap();
        assert!(check_install_pending(&current, &legacy).is_err());
        assert_eq!(fs::read_link(&legacy).unwrap(), Path::new("missing-owner"));
        assert!(!current.parent().unwrap().exists());
    }

    fn pending_fixture() -> (tempfile::TempDir, PathBuf, PathBuf, Receipt) {
        let root = tempfile::tempdir().unwrap();
        let target = root.path().join("boot");
        let state = root.path().join("state.json");
        fs::write(&target, b"image-old").unwrap();
        fs::write(&state, b"disabled").unwrap();
        let receipt = install_with_state(
            "enable-kpm",
            &[ImageWrite {
                target: target.clone(),
                image: b"image-new".to_vec(),
                expected_sha256: None,
            }],
            &[StateWrite {
                path: state.clone(),
                contents: b"enabled".to_vec(),
            }],
            &root.path().join("transactions"),
        )
        .unwrap();
        atomic_write(
            &root.path().join("image-pending.json"),
            &serde_json::to_vec(&receipt.journal).unwrap(),
        )
        .unwrap();
        (root, target, state, receipt)
    }

    #[test]
    fn verified_committed_marker_is_cleared_and_disable_can_proceed() {
        let (root, target, state, _) = pending_fixture();
        let directory = root.path().join("transactions");
        let images = [ImageWrite {
            target: target.clone(),
            image: b"image-old".to_vec(),
            expected_sha256: Some(sha256::digest(b"image-new")),
        }];
        assert!(install("disable-kpm", &images, &directory).is_err());
        reconcile_pending(&root.path().join("image-pending.json"), |_, _| Ok(())).unwrap();
        assert_eq!(fs::read(&target).unwrap(), b"image-new");
        assert_eq!(fs::read(&state).unwrap(), b"enabled");
        install_with_state(
            "disable-kpm",
            &images,
            &[StateWrite {
                path: state.clone(),
                contents: b"disabled".to_vec(),
            }],
            &directory,
        )
        .unwrap();
        assert_eq!(fs::read(target).unwrap(), b"image-old");
        assert_eq!(fs::read(state).unwrap(), b"disabled");
    }

    #[test]
    fn interrupted_image_and_state_publication_is_reconciled_together() {
        for phase in [
            "writing:0",
            "publishing:0",
            "restoring",
            "recovery_required",
        ] {
            let (root, target, state, receipt) = pending_fixture();
            let mut journal: Journal =
                serde_json::from_slice(&fs::read(&receipt.journal).unwrap()).unwrap();
            journal.state = phase.into();
            fs::write(&state, b"disabled").unwrap();
            save_journal(&receipt.journal, &journal).unwrap();
            reconcile_pending(&root.path().join("image-pending.json"), |_, _| Ok(())).unwrap();
            assert_eq!(fs::read(target).unwrap(), b"image-old");
            assert_eq!(fs::read(state).unwrap(), b"disabled");
            assert!(!root.path().join("image-pending.json").exists());
            let journal: Journal =
                serde_json::from_slice(&fs::read(receipt.journal).unwrap()).unwrap();
            assert_eq!(journal.state, "rolled_back");
        }
    }

    #[test]
    fn durable_publication_finishes_commit_without_restoring_the_old_image() {
        let (root, target, state, receipt) = pending_fixture();
        let mut journal: Journal =
            serde_json::from_slice(&fs::read(&receipt.journal).unwrap()).unwrap();
        journal.state = "publishing:0".into();
        save_journal(&receipt.journal, &journal).unwrap();
        reconcile_pending(&root.path().join("image-pending.json"), |_, _| Ok(())).unwrap();
        assert_eq!(fs::read(target).unwrap(), b"image-new");
        assert_eq!(fs::read(state).unwrap(), b"enabled");
        let journal: Journal = serde_json::from_slice(&fs::read(receipt.journal).unwrap()).unwrap();
        assert_eq!(journal.state, "committed");
        assert!(!root.path().join("image-pending.json").exists());
    }

    #[test]
    fn restored_transaction_only_clears_its_own_marker() {
        let (root, target, state, receipt) = pending_fixture();
        let mut journal: Journal =
            serde_json::from_slice(&fs::read(&receipt.journal).unwrap()).unwrap();
        journal.state = "rolled_back".into();
        fs::write(&target, b"image-old").unwrap();
        fs::write(&state, b"disabled").unwrap();
        save_journal(&receipt.journal, &journal).unwrap();
        reconcile_pending(&root.path().join("image-pending.json"), |_, _| Ok(())).unwrap();
        assert_eq!(fs::read(target).unwrap(), b"image-old");
        assert_eq!(fs::read(state).unwrap(), b"disabled");
        assert!(!root.path().join("image-pending.json").exists());
    }

    #[test]
    fn uncertain_pending_recovery_preserves_images_states_and_ownership() {
        for mutation in 0..8 {
            let (root, target, state, receipt) = pending_fixture();
            let mut journal: Journal =
                serde_json::from_slice(&fs::read(&receipt.journal).unwrap()).unwrap();
            journal.state = "writing:0".into();
            match mutation {
                0 => fs::write(&target, b"image-OTA").unwrap(),
                1 => fs::write(&state, b"newer-state").unwrap(),
                2 => fs::write(&journal.entries[0].backup, b"bad-image").unwrap(),
                3 => fs::write(&journal.entries[0].staged, b"bad-image").unwrap(),
                4 => journal.attempted = 0,
                5 => journal.attempted = 2,
                6 => journal.state = "unknown".into(),
                _ => journal.state = "committed".into(),
            }
            if mutation == 7 {
                fs::write(&state, b"disabled").unwrap();
            }
            save_journal(&receipt.journal, &journal).unwrap();
            let image_before = fs::read(&target).unwrap();
            let state_before = fs::read(&state).unwrap();
            let pending = root.path().join("image-pending.json");
            let pending_before = fs::read(&pending).unwrap();
            assert!(reconcile_pending(&pending, |_, _| Ok(())).is_err());
            assert_eq!(fs::read(target).unwrap(), image_before);
            assert_eq!(fs::read(state).unwrap(), state_before);
            assert_eq!(fs::read(pending).unwrap(), pending_before);
        }
    }

    #[test]
    fn recovery_checks_device_constraints_before_any_mutation() {
        let (root, target, state, _) = pending_fixture();
        let pending = root.path().join("image-pending.json");
        assert!(
            reconcile_pending(&pending, |_, _| bail!("recovery device does not match")).is_err()
        );
        assert_eq!(fs::read(target).unwrap(), b"image-new");
        assert_eq!(fs::read(state).unwrap(), b"enabled");
        assert!(pending.exists());
    }

    #[test]
    fn symlink_recovery_marker_is_rejected() {
        let (root, target, state, _) = pending_fixture();
        let pending = root.path().join("image-pending.json");
        let saved = root.path().join("saved-marker");
        fs::rename(&pending, &saved).unwrap();
        std::os::unix::fs::symlink(&saved, &pending).unwrap();
        assert!(reconcile_pending(&pending, |_, _| Ok(())).is_err());
        assert_eq!(fs::read(target).unwrap(), b"image-new");
        assert_eq!(fs::read(state).unwrap(), b"enabled");
        assert!(
            fs::symlink_metadata(pending)
                .unwrap()
                .file_type()
                .is_symlink()
        );
    }
}
