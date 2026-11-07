#[test]
fn embedded_installer_is_shell_syntax_on_windows_checkouts() {
    #[cfg(unix)]
    use std::io::Write;
    #[cfg(unix)]
    use std::process::{Command, Stdio};
    const WINDOWS: &str = "#!/system/bin/sh\r\nprintf '%s\\n' XKCNSU\r\n";
    const NORMALIZED: &str = const_format::str_replace!(WINDOWS, "\r\n", "\n");
    const INSTALLER: &str = const_format::str_replace!(include_str!("installer.sh"), "\r\n", "\n");
    assert_eq!(NORMALIZED, "#!/system/bin/sh\nprintf '%s\\n' XKCNSU\n");
    assert!(!INSTALLER.contains('\r'));
    #[cfg(unix)]
    {
        let mut child = Command::new("sh")
            .arg("-n")
            .stdin(Stdio::piped())
            .spawn()
            .unwrap();
        child
            .stdin
            .take()
            .unwrap()
            .write_all(INSTALLER.as_bytes())
            .unwrap();
        assert!(child.wait().unwrap().success());
    }
}
