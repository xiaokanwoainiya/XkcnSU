use std::cell::{Cell, RefCell};
use std::path::Path;

use super::*;

#[test]
fn empty_load_arguments_use_the_optional_null_argument() {
    let empty = std::ffi::CString::new("").unwrap();
    let literal = std::ffi::CString::new("XKCNSU literal").unwrap();
    assert_eq!(runtime::load_arguments_pointer(&empty), 0);
    assert_eq!(
        runtime::load_arguments_pointer(&literal),
        literal.as_ptr() as usize
    );
}

#[test]
fn version_probe_failure_preserves_presence_and_conflict() {
    let probe = runtime::probe_with(|command| match command {
        0x1000 => Ok(0x2026_2026),
        0x1008 => Err(failure("kernel_error", "VERSION unavailable")),
        _ => panic!("Unexpected capability probe after VERSION failed"),
    });
    assert!(probe.active);
    assert!(!probe.managed());
    assert!(probe.avc_conflict());
    assert!(probe.version.is_none());
    assert!(probe.error.unwrap().contains("VERSION unavailable"));
}

#[test]
fn avc_conflict_only_clears_for_absent_or_fully_managed_engines() {
    assert!(!Probe::default().avc_conflict());
    assert!(
        Probe {
            error: Some("unknown state".into()),
            ..Probe::default()
        }
        .avc_conflict()
    );
    for capabilities in [0_u64, 0x1f, 0x3e, 0x3f, 0x7f] {
        let probe = Probe {
            active: true,
            capabilities: Some(capabilities),
            ..Probe::default()
        };
        assert_eq!(probe.avc_conflict(), capabilities & 0x3f != 0x3f);
    }
}

fn put16(bytes: &mut [u8], offset: usize, value: u16) {
    bytes[offset..offset + 2].copy_from_slice(&value.to_le_bytes());
}

fn put32(bytes: &mut [u8], offset: usize, value: u32) {
    bytes[offset..offset + 4].copy_from_slice(&value.to_le_bytes());
}

fn put64(bytes: &mut [u8], offset: usize, value: u64) {
    bytes[offset..offset + 8].copy_from_slice(&value.to_le_bytes());
}

fn fixture(control: bool) -> Vec<u8> {
    let section_count = if control { 8 } else { 7 };
    let mut bytes = vec![0; 64 + section_count * 64];
    bytes[..4].copy_from_slice(b"\x7fELF");
    bytes[4..7].copy_from_slice(&[2, 1, 1]);
    put16(&mut bytes, 16, 1);
    put16(&mut bytes, 18, 183);
    put32(&mut bytes, 20, 1);
    put64(&mut bytes, 40, 64);
    put16(&mut bytes, 52, 64);
    put16(&mut bytes, 58, 64);
    put16(&mut bytes, 60, section_count as u16);
    put16(&mut bytes, 62, 1);
    let names = [
        "",
        ".shstrtab",
        ".strtab",
        ".symtab",
        ".kpm.info",
        ".kpm.init",
        ".kpm.exit",
        ".kpm.ctl0",
    ];
    let mut strings = Vec::new();
    let offsets = names
        .iter()
        .map(|name| {
            let offset = strings.len();
            strings.extend_from_slice(name.as_bytes());
            strings.push(0);
            offset
        })
        .collect::<Vec<_>>();
    let info =
        b"name=fixture\nmodule\0version=1.0\0author=XKCNSU\0description=Fixture\0license=GPL v2\0";
    let info = info
        .iter()
        .copied()
        .filter(|byte| *byte != b'\n')
        .collect::<Vec<_>>();
    let mut parts = vec![
        strings,
        vec![0],
        vec![0; 24],
        info,
        1_u64.to_le_bytes().to_vec(),
        1_u64.to_le_bytes().to_vec(),
    ];
    if control {
        parts.push(1_u64.to_le_bytes().to_vec());
    }
    for (part, data) in parts.iter().enumerate() {
        let index = part + 1;
        let offset = bytes.len();
        bytes.extend_from_slice(data);
        let header = 64 + index * 64;
        put32(&mut bytes, header, offsets[index] as u32);
        put32(
            &mut bytes,
            header + 4,
            match index {
                1 | 2 => 3,
                3 => 2,
                _ => 1,
            },
        );
        put64(&mut bytes, header + 8, if index >= 4 { 2 } else { 0 });
        put64(&mut bytes, header + 24, offset as u64);
        put64(&mut bytes, header + 32, data.len() as u64);
        put32(&mut bytes, header + 40, if index == 3 { 2 } else { 0 });
        put64(&mut bytes, header + 48, 1);
        put64(&mut bytes, header + 56, if index == 3 { 24 } else { 0 });
    }
    bytes
}

#[derive(Default)]
struct MockEngine {
    modules: RefCell<Vec<RuntimeModule>>,
    loads: Cell<usize>,
    controls: Cell<usize>,
    unloads: Cell<usize>,
    fail_load: Cell<bool>,
    fail_unload: Cell<bool>,
    arguments: RefCell<Vec<String>>,
    probe_override: RefCell<Option<Probe>>,
}

impl Engine for MockEngine {
    fn probe(&self) -> Probe {
        self.probe_override
            .borrow()
            .clone()
            .unwrap_or_else(|| Probe {
                active: true,
                version: Some("0.13.5".into()),
                capabilities: Some(runtime::REQUIRED_CAPABILITIES | runtime::UNLOAD_CAPABILITY),
                ..Probe::default()
            })
    }

    fn modules(&self) -> Result<Vec<RuntimeModule>> {
        Ok(self.modules.borrow().clone())
    }

    fn load(&self, image: &[u8], args: &str) -> Result<()> {
        self.loads.set(self.loads.get() + 1);
        self.arguments.borrow_mut().push(args.to_owned());
        if self.fail_load.get() {
            return Err(failure("kernel_error", "init failed"));
        }
        let metadata = elf::parse(image)?;
        self.modules.borrow_mut().push(RuntimeModule {
            name: metadata.name,
            version: metadata.version,
            author: metadata.author,
            description: metadata.description,
            args: args.into(),
            has_control: metadata.has_control,
            state: "loaded".into(),
            ..RuntimeModule::default()
        });
        Ok(())
    }

    fn control(&self, _name: &str, args: &str) -> Result<String> {
        self.controls.set(self.controls.get() + 1);
        Ok(args.to_owned())
    }

    fn unload(&self, name: &str) -> Result<()> {
        self.unloads.set(self.unloads.get() + 1);
        let mut modules = self.modules.borrow_mut();
        let module = modules
            .iter_mut()
            .find(|module| module.name == name)
            .unwrap();
        if self.fail_unload.get() {
            module.pending_unload = true;
            module.state = "pending-unload".into();
            Err(failure("kernel_error", "exit failed"))
        } else {
            modules.retain(|module| module.name != name);
            Ok(())
        }
    }
}

fn import_fixture(backend: &mut Backend<'_, MockEngine>, root: &Path, control: bool) -> String {
    let path = root.join("input.kpm");
    std::fs::write(&path, fixture(control)).unwrap();
    backend.import(&path).unwrap()["id"]
        .as_str()
        .unwrap()
        .to_owned()
}

#[test]
fn rejects_bad_elf_and_out_of_range_names() {
    assert!(elf::parse(b"not an ELF").is_err());
    let mut bytes = fixture(false);
    put16(&mut bytes, 62, 2000);
    assert!(elf::parse(&bytes).is_err());
    let mut bytes = fixture(false);
    put64(&mut bytes, 64 + 4 * 64 + 24, u64::MAX);
    assert!(elf::parse(&bytes).is_err());
}

#[test]
fn rejects_null_init_wrong_architecture_and_rel_tables() {
    let mut bytes = fixture(false);
    put16(&mut bytes, 18, 62);
    assert!(elf::parse(&bytes).is_err());
    let mut bytes = fixture(false);
    let section = 64 + 5 * 64;
    let offset = u64::from_le_bytes(bytes[section + 24..section + 32].try_into().unwrap()) as usize;
    bytes[offset..offset + 8].fill(0);
    assert!(elf::parse(&bytes).is_err());
    let mut bytes = fixture(false);
    put32(&mut bytes, 64 + 2 * 64 + 4, 9);
    assert!(elf::parse(&bytes).is_err());
}

#[test]
fn import_only_registers_and_autoload_defaults_off() {
    let root = tempfile::tempdir().unwrap();
    let engine = MockEngine::default();
    let mut backend = Backend::open(&root.path().join("state"), &engine, "boot-a".into()).unwrap();
    let id = import_fixture(&mut backend, root.path(), true);
    let info = backend.info(&id).unwrap();
    assert_eq!(info["loaded"], false);
    assert_eq!(info["autoload"], false);
    assert_eq!(info["sha256"], id);
    backend.load_autoload().unwrap();
    assert_eq!(engine.loads.get(), 0);
}

#[test]
fn loading_queries_kernel_and_preserves_literal_arguments() {
    let root = tempfile::tempdir().unwrap();
    let engine = MockEngine::default();
    let mut backend = Backend::open(&root.path().join("state"), &engine, "boot-a".into()).unwrap();
    let id = import_fixture(&mut backend, root.path(), true);
    let args = "$(touch SHOULD_NOT_EXIST); 'quoted' \"double\"";
    assert_eq!(backend.load(&id, args).unwrap()["loaded"], true);
    assert_eq!(engine.arguments.borrow()[0], args);
    engine.modules.borrow_mut().clear();
    assert_eq!(backend.info(&id).unwrap()["loaded"], false);
    assert!(!root.path().join("SHOULD_NOT_EXIST").exists());
}

#[test]
fn no_ctl0_never_enters_the_kernel_control_callback() {
    let root = tempfile::tempdir().unwrap();
    let engine = MockEngine::default();
    let mut backend = Backend::open(&root.path().join("state"), &engine, "boot-a".into()).unwrap();
    let id = import_fixture(&mut backend, root.path(), false);
    backend.load(&id, "").unwrap();
    let result = backend.control(&id, "enable");
    assert_eq!(envelope(&result)["error"]["code"], "no_control");
    assert_eq!(engine.controls.get(), 0);
}

#[test]
fn failed_load_is_quarantined_without_deleting_module() {
    let root = tempfile::tempdir().unwrap();
    let engine = MockEngine::default();
    let mut backend = Backend::open(&root.path().join("state"), &engine, "boot-a".into()).unwrap();
    let id = import_fixture(&mut backend, root.path(), true);
    backend.autoload(&id, true).unwrap();
    engine.fail_load.set(true);
    assert!(backend.load_autoload().is_err());
    let info = backend.info(&id).unwrap();
    assert_eq!(info["autoload"], false);
    assert!(info["last_error"].as_str().unwrap().contains("init failed"));
    assert!(backend.store.path(&id).unwrap().is_file());
}

#[test]
fn changed_content_never_reaches_kernel_loader() {
    let root = tempfile::tempdir().unwrap();
    let engine = MockEngine::default();
    let mut backend = Backend::open(&root.path().join("state"), &engine, "boot-a".into()).unwrap();
    let id = import_fixture(&mut backend, root.path(), true);
    std::fs::write(backend.store.path(&id).unwrap(), b"changed").unwrap();
    let result = backend.load(&id, "");
    assert_eq!(envelope(&result)["error"]["code"], "integrity_error");
    assert_eq!(engine.loads.get(), 0);
}

#[test]
fn failed_unload_keeps_the_file_and_reports_the_exit_error() {
    let root = tempfile::tempdir().unwrap();
    let engine = MockEngine::default();
    let mut backend = Backend::open(&root.path().join("state"), &engine, "boot-a".into()).unwrap();
    let id = import_fixture(&mut backend, root.path(), true);
    backend.load(&id, "").unwrap();
    backend.autoload(&id, true).unwrap();
    engine.fail_unload.set(true);
    let result = backend.remove(&id);
    assert_eq!(envelope(&result)["error"]["code"], "kernel_error");
    let info = backend.info(&id).unwrap();
    assert_eq!(info["loaded"], true);
    assert_eq!(info["pending_unload"], true);
    assert_eq!(info["autoload"], false);
    assert!(backend.store.path(&id).unwrap().is_file());
}

#[test]
fn successful_unload_allows_immediate_reload_and_remove() {
    let root = tempfile::tempdir().unwrap();
    let engine = MockEngine::default();
    let mut backend = Backend::open(&root.path().join("state"), &engine, "boot-a".into()).unwrap();
    let id = import_fixture(&mut backend, root.path(), true);
    backend.load(&id, "").unwrap();
    backend.autoload(&id, true).unwrap();
    let unloaded = backend.unload(&id).unwrap();
    assert_eq!(unloaded["loaded"], false);
    assert_eq!(unloaded["pending_unload"], false);
    assert_eq!(unloaded["autoload"], false);
    backend.load(&id, "reloaded").unwrap();
    assert_eq!(backend.remove(&id).unwrap()["removed"], true);
    assert!(engine.modules.borrow().is_empty());
    assert!(!backend.store.path(&id).unwrap().exists());
    assert!(backend.list().unwrap().as_array().unwrap().is_empty());
}

#[test]
fn old_engine_requires_update_instead_of_claiming_unload_success() {
    let root = tempfile::tempdir().unwrap();
    let engine = MockEngine::default();
    let mut backend = Backend::open(root.path(), &engine, "boot-a".into()).unwrap();
    let id = import_fixture(&mut backend, root.path(), true);
    backend.load(&id, "").unwrap();
    engine.probe_override.replace(Some(Probe {
        active: true,
        capabilities: Some(runtime::REQUIRED_CAPABILITIES),
        ..Probe::default()
    }));
    let result = backend.remove(&id);
    assert_eq!(envelope(&result)["error"]["code"], "kernel_update_required");
    assert_eq!(engine.unloads.get(), 0);
    assert!(backend.store.path(&id).unwrap().exists());
}

#[test]
fn interrupted_load_is_not_retried_on_next_boot() {
    let root = tempfile::tempdir().unwrap();
    let engine = MockEngine::default();
    let state = root.path().join("state");
    let id;
    {
        let mut backend = Backend::open(&state, &engine, "boot-a".into()).unwrap();
        id = import_fixture(&mut backend, root.path(), true);
        backend.store.records[0].autoload = true;
        backend.store.records[0].last_attempt_boot = "boot-a".into();
        backend.store.save().unwrap();
    }
    let mut backend = Backend::open(&state, &engine, "boot-b".into()).unwrap();
    assert!(backend.load_autoload().is_err());
    assert_eq!(engine.loads.get(), 0);
    assert_eq!(backend.info(&id).unwrap()["autoload"], false);
}

#[test]
fn registry_reopen_preserves_digest_arguments_and_autoload() {
    let root = tempfile::tempdir().unwrap();
    let engine = MockEngine::default();
    let state = root.path().join("state");
    let id;
    let args = "first\nsecond=$(literal); 'quoted'";
    {
        let mut backend = Backend::open(&state, &engine, "boot-a".into()).unwrap();
        id = import_fixture(&mut backend, root.path(), true);
        backend.load(&id, args).unwrap();
        backend.autoload(&id, true).unwrap();
    }
    engine.modules.borrow_mut().clear();
    let mut backend = Backend::open(&state, &engine, "boot-b".into()).unwrap();
    let info = backend.info(&id).unwrap();
    assert_eq!(info["args"], args);
    assert_eq!(info["sha256"], id);
    assert_eq!(info["autoload"], true);
    assert_eq!(info["loaded"], false);
    backend.load_autoload().unwrap();
    assert_eq!(engine.loads.get(), 2);
    assert_eq!(backend.info(&id).unwrap()["loaded"], true);
}

#[test]
fn incompatible_engine_cannot_mutate_kernel_state() {
    let result = require_engine(&Probe {
        active: true,
        version: Some("0.13.5".into()),
        capabilities: Some(0),
        ..Probe::default()
    })
    .map(|()| Value::Null);
    assert_eq!(envelope(&result)["error"]["code"], "incompatible_engine");
}

#[test]
fn incompatible_engine_without_verified_image_cannot_be_configured() {
    let probe = Probe {
        active: true,
        version: Some("0.13.6".into()),
        capabilities: Some(0),
        ..Probe::default()
    };
    let transition = json!({
        "configured_enabled": false, "reboot_required": true,
        "transition_failed": false, "reason": null, "can_configure": false,
    });
    let value = status_value(&probe, &transition, true, true).unwrap();
    assert_eq!(value["supported"], false);
    assert_eq!(value["active"], true);
    assert_eq!(value["version"], "0.13.6");
    assert_eq!(value["provider"], "incompatible");
    assert_eq!(value["can_configure"], false);
    assert_eq!(value["runtime_supported"], false);
    assert_eq!(value["capabilities"], 0);
    assert_eq!(value["required_capabilities"], 0x3f);
    assert!(
        value["reason"]
            .as_str()
            .unwrap()
            .contains("no verified upgrade image")
    );
}

#[test]
fn status_forwards_directional_configuration_with_root_and_arch_gates() {
    let transition = json!({
        "configured_enabled": true, "reboot_required": false,
        "can_configure": true, "can_enable": false, "can_disable": true,
    });
    let status = status_value(&Probe::default(), &transition, true, true).unwrap();
    assert_eq!(status["can_enable"], false);
    assert_eq!(status["can_disable"], true);
    for (arm64, root) in [(false, true), (true, false)] {
        let status = status_value(&Probe::default(), &transition, arm64, root).unwrap();
        assert_eq!(status["can_enable"], false);
        assert_eq!(status["can_disable"], false);
    }
    let legacy = json!({
        "configured_enabled": false, "reboot_required": false, "can_configure": true,
    });
    let status = status_value(&Probe::default(), &legacy, true, true).unwrap();
    assert_eq!(status["can_enable"], true);
    assert_eq!(status["can_disable"], true);
}

#[test]
fn missing_root_is_unavailable_and_not_a_failed_transition() {
    let transition = json!({
        "configured_enabled": false, "reboot_required": false,
        "transition_failed": false, "reason": null,
    });
    let value = status_value(&Probe::default(), &transition, true, false).unwrap();
    assert_eq!(value["supported"], false);
    assert_eq!(value["state_known"], false);
    assert_eq!(value["transition_failed"], false);
    assert_eq!(value["reboot_required"], false);
    assert!(value["reason"].as_str().unwrap().contains("Root"));
}

#[test]
fn managed_engine_and_post_reboot_failure_keep_image_transition_truth() {
    let managed = Probe {
        active: true,
        version: Some("0.13.5".into()),
        capabilities: Some(runtime::REQUIRED_CAPABILITIES),
        ..Probe::default()
    };
    let stable = json!({
        "configured_enabled": true, "reboot_required": false,
        "transition_failed": false, "reason": null,
    });
    let value = status_value(&managed, &stable, true, true).unwrap();
    assert_eq!(value["supported"], true);
    assert_eq!(value["provider"], "xkcnsu");
    assert_eq!(value["reboot_required"], false);

    let failed = json!({
        "configured_enabled": true, "reboot_required": false,
        "transition_failed": true, "reason": "Engine failed after reboot",
    });
    let value = status_value(&Probe::default(), &failed, true, true).unwrap();
    assert_eq!(value["supported"], true);
    assert_eq!(value["transition_failed"], true);
    assert_eq!(value["reboot_required"], false);
    assert_eq!(value["reason"], "Engine failed after reboot");
}

#[test]
fn path_only_engines_are_not_accepted_for_verified_buffer_loading() {
    let legacy = Probe {
        active: true,
        capabilities: Some(0x1f),
        ..Probe::default()
    };
    assert!(!legacy.managed());
    assert!(require_engine(&legacy).is_err());
}

#[test]
fn capability_probe_keeps_zero_distinct_from_query_failure() {
    let successful = runtime::probe_with(|command| match command {
        0x1000 => Ok(0x2026_2026),
        0x1008 => Ok(0x0d05),
        0x1033 => Ok(0),
        _ => panic!("unexpected command"),
    });
    let failed = runtime::probe_with(|command| match command {
        0x1000 => Ok(0x2026_2026),
        0x1008 => Ok(0x0d05),
        0x1033 => Err(failure("kernel_error", "ENOSYS capability command")),
        _ => panic!("unexpected command"),
    });
    assert!(successful.active && failed.active);
    assert_eq!(successful.capabilities, Some(0));
    assert!(successful.error.is_none());
    assert_eq!(failed.capabilities, None);
    assert!(failed.error.as_deref().unwrap().contains("ENOSYS"));
    let transition = json!({
        "configured_enabled": true, "reboot_required": true,
        "can_configure": true, "reason": null,
    });
    let status = status_value(&failed, &transition, true, true).unwrap();
    assert!(status["capabilities"].is_null());
    assert_eq!(status["required_capabilities"], 0x3f);
    assert_eq!(status["provider"], "unknown");
    assert_eq!(status["can_configure"], true);
    assert!(status["reason"].as_str().unwrap().contains("ENOSYS"));
    assert!(!failed.managed());
    assert!(require_engine(&failed).is_err());
}

#[test]
fn capability_superset_is_required_even_when_upgrade_is_available() {
    let transition = json!({
        "configured_enabled": true, "reboot_required": true,
        "can_configure": true, "reason": null,
    });
    for capabilities in [0_u64, 0x1f, 0x3e, 0x3f, 0x7f] {
        let probe = runtime::probe_with(|command| match command {
            0x1000 => Ok(0x2026_2026),
            0x1008 => Ok(0x0d05),
            0x1033 => Ok(i64::try_from(capabilities).unwrap()),
            _ => panic!("unexpected command"),
        });
        let ready = capabilities & 0x3f == 0x3f;
        let status = status_value(&probe, &transition, true, true).unwrap();
        assert_eq!(probe.managed(), ready);
        assert_eq!(require_engine(&probe).is_ok(), ready);
        assert_eq!(status["supported"], ready);
        assert_eq!(status["runtime_supported"], ready);
        assert_eq!(status["can_configure"], true);
        assert_eq!(status["capabilities"], capabilities);
    }
}

#[test]
fn configure_permission_never_bypasses_root_or_architecture() {
    let transition = json!({
        "configured_enabled": true, "reboot_required": false,
        "can_configure": true, "reason": null,
    });
    let probe = MockEngine::default().probe();
    for (arm64, root) in [(false, true), (true, false), (false, false)] {
        let status = status_value(&probe, &transition, arm64, root).unwrap();
        assert_eq!(status["can_configure"], false);
        assert_eq!(status["runtime_supported"], false);
    }
}

#[test]
fn invalid_configuration_keeps_runtime_evidence_and_refuses_configuration() {
    let probe = MockEngine::default().probe();
    let transition = json!({
        "configured_enabled": false, "reboot_required": false,
        "can_configure": false, "state_known": false,
        "configuration_error": "original kernel backup failed verification",
        "reason": "original kernel backup failed verification",
    });
    let status = status_value(&probe, &transition, true, true).unwrap();
    assert_eq!(status["runtime_supported"], true);
    assert_eq!(status["capabilities"], 0x7f);
    assert_eq!(status["can_configure"], false);
    assert_eq!(status["state_known"], false);
    assert_eq!(status["reason"], transition["reason"]);
}

#[test]
fn legacy_engine_upgrade_permission_does_not_enter_kernel_module_callbacks() {
    let root = tempfile::tempdir().unwrap();
    let engine = MockEngine::default();
    let mut backend = Backend::open(root.path(), &engine, "boot-a".into()).unwrap();
    let id = import_fixture(&mut backend, root.path(), true);
    let legacy = Probe {
        active: true,
        capabilities: Some(0x1f),
        ..Probe::default()
    };
    engine.probe_override.replace(Some(legacy.clone()));
    let transition = json!({
        "configured_enabled": true, "reboot_required": true,
        "can_configure": true, "reason": null,
    });
    assert_eq!(
        status_value(&legacy, &transition, true, true).unwrap()["can_configure"],
        true
    );
    assert!(backend.load(&id, "").is_err());
    assert!(backend.control(&id, "args").is_err());
    assert!(backend.unload(&id).is_err());
    assert!(backend.load_autoload().is_err());
    assert_eq!(engine.loads.get(), 0);
    assert_eq!(engine.controls.get(), 0);
    assert_eq!(engine.unloads.get(), 0);
}

#[test]
fn verified_module_returns_its_validated_bytes() {
    let root = tempfile::tempdir().unwrap();
    let engine = MockEngine::default();
    let mut backend = Backend::open(root.path(), &engine, "boot-a".into()).unwrap();
    let id = import_fixture(&mut backend, root.path(), true);
    let index = backend.index(&id).unwrap();
    let bytes = backend
        .store
        .verified_module(&backend.store.records[index])
        .unwrap();
    assert_eq!(sha256::digest(&bytes), id);
    std::fs::write(backend.store.path(&id).unwrap(), b"changed").unwrap();
    assert_eq!(sha256::digest(bytes), id);
    assert!(backend.load(&id, "").is_err());
    assert_eq!(engine.loads.get(), 0);
}

#[test]
fn registry_lock_and_invalid_ids_prevent_mutations() {
    let root = tempfile::tempdir().unwrap();
    let engine = MockEngine::default();
    let mut backend = Backend::open(root.path(), &engine, "boot-a".into()).unwrap();
    assert!(Store::open(root.path()).is_err());
    assert!(backend.remove("../../integration").is_err());
    assert_eq!(engine.loads.get(), 0);
}

#[test]
fn control_output_is_json_escaped_and_external_modules_are_visible() {
    let root = tempfile::tempdir().unwrap();
    let engine = MockEngine::default();
    engine.modules.borrow_mut().push(RuntimeModule {
        name: "external".into(),
        has_control: true,
        state: "loaded".into(),
        ..RuntimeModule::default()
    });
    let mut backend = Backend::open(root.path(), &engine, "boot-a".into()).unwrap();
    let id = runtime_id("external");
    assert_eq!(backend.info(&id).unwrap()["loaded"], true);
    let result = backend.control(&id, "\"value\"\nnew line").unwrap();
    let line = serde_json::to_string(&envelope(&Ok(result))).unwrap();
    assert_eq!(line.lines().count(), 1);
    assert!(
        serde_json::from_str::<Value>(&line).unwrap()["ok"]
            .as_bool()
            .unwrap()
    );
}

#[test]
fn clap_exposes_the_agreed_command_contract() {
    #[derive(clap::Parser)]
    struct Arguments {
        #[command(subcommand)]
        command: KpmCommand,
    }
    use clap::Parser;
    for args in [
        vec!["kpm", "status"],
        vec!["kpm", "list"],
        vec!["kpm", "import", "/tmp/example.kpm"],
        vec!["kpm", "info", "id"],
        vec!["kpm", "load", "id", "--args", "x"],
        vec!["kpm", "control", "id", "--args", "x"],
        vec!["kpm", "unload", "id"],
        vec!["kpm", "remove", "id"],
        vec!["kpm", "autoload", "id", "enable"],
        vec!["kpm", "integration", "disable"],
    ] {
        assert!(Arguments::try_parse_from(args).is_ok());
    }
}
