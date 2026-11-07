# Root Policy Acceptance APK

This temporary APK is `io.xkcnsu.acceptance.rootpolicy`, labelled
`XKCNSU Root Test`. It has an exported launcher activity, one exported receiver,
and no requested permissions. It uses its real Android app UID. It does not edit
profiles, switch UIDs, access the network, or write report files.

Build from Linux/WSL:

```sh
bash tools/build-root-policy-fixture.sh
```

The script uses Java and the existing `/opt/xkcnsu/android-sdk` build tools
`37.0.0` with platform `37.0` (or `37`). The result is
`out/test-harness/root-policy/XKCNSU-Root-Test.apk`. Each build uses a fresh test
certificate and discards its private key. Reinstalling a different build requires
uninstalling the preceding build. No product signing material or release asset
staging is used.

The following device commands are for the operator's later acceptance run; the
build script never runs them:

```sh
adb -s SERIAL install -t out/test-harness/root-policy/XKCNSU-Root-Test.apk
adb -s SERIAL shell am start \
  -n io.xkcnsu.acceptance.rootpolicy/.ProbeActivity --es nonce before-001
adb -s SERIAL logcat -d -v raw -s XKCNSUAcceptance:I '*:S'
```

The launcher is `io.xkcnsu.acceptance.rootpolicy/.ProbeActivity`. Once resumed,
it sends an explicit same-package broadcast to `.ProbeReceiver` with the
unchanged nonce and `FLAG_RECEIVER_FOREGROUND`. It stays open for 9 seconds
from dispatch, keeping its window's screen on, then calls `finish()`. Wait for
that window to close before starting the next probe with a fresh nonce.
Configuration recreation preserves dispatch state and the finish deadline,
so it does not repeat the probe. Opening the launcher without a valid nonce
just displays `XKCNSU Root Test` for 9 seconds.

Use this foreground path when OEM cached-process restrictions interfere with
direct broadcasts. The activity does not invoke `onReceive()` or `goAsync()`
itself; Android delivers the broadcast through the existing receiver lifecycle.
The receiver's su command, timeout, output limits and JSON fields are unchanged.

Direct receiver invocation remains available:

```sh
adb -s SERIAL shell am broadcast --include-stopped-packages --receiver-foreground \
  -a io.xkcnsu.acceptance.rootpolicy.PROBE \
  -n io.xkcnsu.acceptance.rootpolicy/.ProbeReceiver --es nonce before-001
adb -s SERIAL logcat -d -v raw -s XKCNSUAcceptance:I '*:S'
```

Only explicit broadcasts with the action above and a nonce of 1-64 ASCII letters,
digits, dots, underscores, colons or hyphens start a probe. Each accepted probe
calls `goAsync()` and starts a background worker. Only one probe runs at a time;
an overlapping request logs `error: "probe_already_running"` without running su.
The worker executes the fixed argument vector `/system/bin/su -c id`; no intent
data is interpreted as a command.

Both streams are drained concurrently, retaining at most 1024 bytes per stream.
The process gets 6 seconds, with a shared 7.5-second deadline for termination and
pipe collection. It is forcibly destroyed on timeout. Collector threads are
daemon threads and are never joined indefinitely. The final JSON record is
limited to 3500 UTF-8 bytes, including JSON escaping, and logged with `Log.i`
under `XKCNSUAcceptance`. An independent watchdog reports a timeout and finishes
the pending result at the deadline even if process creation has not returned.
The receiver finishes its pending result once. If a timed-out process or its
collectors remain alive, later probes report busy; force-stop the test APK
before continuing in that case.

Match each record by its exact `nonce`. `uid` is the real app UID, `proc_uid`
contains the four UID values from `/proc/self/status`, and `selinux` is the app's
own `/proc/self/attr/current` label. `su_exit` is the child exit status or JSON
null if there was no observed exit. `stdout`/`stderr`, `timed_out`, stream
completion/truncation flags, `elapsed_ms`, and `error` describe the probe.
Identity read failures add `identity_error`. No report is an automatic pass:
`timed_out`, non-null `error`, or incomplete streams invalidate a successful-su
claim.

Use fresh nonces for this sequence:

1. Record the installed test package's actual UID and probe before granting it.
   Verify that it did not obtain root.
2. In the real Manager Superuser UI, grant only `XKCNSU Root Test`. Probe again
   and require `su_exit: 0`, complete streams, no errors or timeout, and
   `uid=0(root)` plus the expected root domain in `stdout`.
3. For persistence testing, reboot after granting, then probe again under a new
   nonce. Confirm the package still has the same app UID.
4. Revoke only this app in Manager, probe under a new nonce, and verify that root
   is denied again.
5. Uninstall `io.xkcnsu.acceptance.rootpolicy`; let the existing package observer
   and throne pruning handle its policy record.

The APK does not grant/revoke itself or infer authorization from a broadcast
delivery acknowledgement. An app-side exec failure is reported as an error,
not as proof that policy denied root.
