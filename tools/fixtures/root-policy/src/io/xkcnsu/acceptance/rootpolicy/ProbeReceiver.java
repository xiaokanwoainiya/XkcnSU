package io.xkcnsu.acceptance.rootpolicy;

import android.content.BroadcastReceiver;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.util.Log;

import org.json.JSONException;
import org.json.JSONObject;

import java.io.ByteArrayOutputStream;
import java.io.FileInputStream;
import java.io.IOException;
import java.io.InputStream;
import java.nio.charset.StandardCharsets;
import java.util.concurrent.CountDownLatch;
import java.util.concurrent.TimeUnit;
import java.util.concurrent.atomic.AtomicBoolean;

public final class ProbeReceiver extends BroadcastReceiver {
    private static final String ACTION = "io.xkcnsu.acceptance.rootpolicy.PROBE";
    private static final String TAG = "XKCNSUAcceptance";
    private static final int CAPTURE_BYTES = 1024;
    private static final int LOG_BYTES = 3500;
    private static final long PROCESS_MS = 6000;
    private static final long TOTAL_MS = 7500;
    private static final AtomicBoolean BUSY = new AtomicBoolean();

    @Override
    public void onReceive(Context context, Intent intent) {
        if (intent == null || !ACTION.equals(intent.getAction())
                || !new ComponentName(context, ProbeReceiver.class).equals(intent.getComponent())) {
            return;
        }
        String nonce = intent.getStringExtra("nonce");
        if (nonce == null || !nonce.matches("[A-Za-z0-9._:-]{1,64}")) {
            return;
        }
        if (!BUSY.compareAndSet(false, true)) {
            ProbeResult busy = new ProbeResult();
            busy.error = "probe_already_running";
            emit(nonce, busy);
            return;
        }
        PendingResult pending = goAsync();
        ProbeCall call = new ProbeCall(nonce, pending);
        Thread worker = new Thread(() -> {
            try {
                call.finish(execute(new ProcessBuilder("/system/bin/su", "-c", "id"), call));
            } finally {
                // Do not accumulate collectors if a descendant retains an open pipe.
                if (call.quiescent()) {
                    BUSY.set(false);
                }
            }
        }, "XKCNSU-probe");
        worker.setDaemon(true);
        Thread watchdog = new Thread(call::watch, "XKCNSU-deadline");
        watchdog.setDaemon(true);
        try {
            watchdog.start();
            worker.start();
        } catch (RuntimeException error) {
            BUSY.set(false);
            ProbeResult failed = new ProbeResult();
            failed.error = error.toString();
            call.finish(failed);
        }
    }

    private static final class ProbeCall {
        final long started = System.nanoTime();
        final long deadline = started + TimeUnit.MILLISECONDS.toNanos(TOTAL_MS);
        final AtomicBoolean finished = new AtomicBoolean();
        final CountDownLatch done = new CountDownLatch(1);
        final String nonce;
        final PendingResult pending;
        volatile java.lang.Process process;
        volatile Collector stdout;
        volatile Collector stderr;

        ProbeCall(String nonce, PendingResult pending) {
            this.nonce = nonce;
            this.pending = pending;
        }

        void finish(ProbeResult result) {
            if (finished.compareAndSet(false, true)) {
                try {
                    emit(nonce, result);
                } finally {
                    try {
                        pending.finish();
                    } finally {
                        done.countDown();
                    }
                }
            }
        }

        void watch() {
            try {
                if (done.await(remaining(deadline), TimeUnit.NANOSECONDS)) {
                    return;
                }
            } catch (InterruptedException failure) {
                Thread.currentThread().interrupt();
            }
            ProbeResult timeout = snapshot(this);
            timeout.timedOut = true;
            timeout.error = "probe_deadline_exceeded";
            finish(timeout);
            terminate(process);
        }

        boolean quiescent() {
            return (process == null || !process.isAlive())
                    && (stdout == null || !stdout.isAlive())
                    && (stderr == null || !stderr.isAlive());
        }
    }

    private static final class ProbeResult {
        Integer exit;
        String stdout = "";
        String stderr = "";
        String error;
        boolean timedOut;
        boolean stdoutTruncated;
        boolean stderrTruncated;
        boolean stdoutComplete;
        boolean stderrComplete;
        long elapsedMs;
    }

    private static final class Collector extends Thread {
        private final InputStream stream;
        private final ByteArrayOutputStream captured = new ByteArrayOutputStream(CAPTURE_BYTES);
        private volatile boolean truncated;
        private volatile boolean complete;
        private volatile String error;

        Collector(InputStream stream, String name) {
            super(name);
            this.stream = stream;
            setDaemon(true);
        }

        @Override
        public void run() {
            byte[] buffer = new byte[1024];
            try (InputStream input = stream) {
                int count;
                while ((count = input.read(buffer)) != -1) {
                    synchronized (captured) {
                        int keep = Math.min(count, CAPTURE_BYTES - captured.size());
                        captured.write(buffer, 0, keep);
                        truncated |= keep < count;
                    }
                }
                complete = true;
            } catch (IOException failure) {
                error = failure.toString();
            }
        }

        String text() {
            synchronized (captured) {
                return new String(captured.toByteArray(), StandardCharsets.UTF_8);
            }
        }
    }

    private static long remaining(long deadline) {
        return Math.max(0, deadline - System.nanoTime());
    }

    private static void joinUntil(Thread thread, long deadline) throws InterruptedException {
        long nanos = remaining(deadline);
        if (nanos > 0) {
            TimeUnit.NANOSECONDS.timedJoin(thread, nanos);
        }
    }

    private static void terminate(java.lang.Process process) {
        if (process != null && process.isAlive()) {
            try {
                process.destroyForcibly();
            } catch (RuntimeException ignored) {
                // A failed kill must not suppress the timeout record or pending.finish().
            }
        }
    }

    private static ProbeResult snapshot(ProbeCall call) {
        ProbeResult result = new ProbeResult();
        java.lang.Process process = call.process;
        if (process != null && !process.isAlive()) {
            result.exit = process.exitValue();
        }
        Collector stdout = call.stdout;
        if (stdout != null) {
            result.stdout = stdout.text();
            result.stdoutTruncated = stdout.truncated;
            result.stdoutComplete = stdout.complete;
            if (stdout.error != null) {
                result.error = "stdout: " + stdout.error;
            }
        }
        Collector stderr = call.stderr;
        if (stderr != null) {
            result.stderr = stderr.text();
            result.stderrTruncated = stderr.truncated;
            result.stderrComplete = stderr.complete;
            if (stderr.error != null) {
                result.error = "stderr: " + stderr.error;
            }
        }
        result.elapsedMs = TimeUnit.NANOSECONDS.toMillis(System.nanoTime() - call.started);
        return result;
    }

    private static ProbeResult execute(ProcessBuilder builder, ProbeCall call) {
        String error = null;
        boolean timedOut = false;
        try {
            java.lang.Process process = builder.start();
            call.process = process;
            if (call.finished.get()) {
                terminate(process);
                return snapshot(call);
            }
            call.stdout = new Collector(process.getInputStream(), "XKCNSU-stdout");
            call.stderr = new Collector(process.getErrorStream(), "XKCNSU-stderr");
            call.stdout.start();
            call.stderr.start();
            process.getOutputStream().close();
            long processDeadline = call.started + TimeUnit.MILLISECONDS.toNanos(PROCESS_MS);
            if (!process.waitFor(remaining(processDeadline), TimeUnit.NANOSECONDS)) {
                timedOut = true;
                terminate(process);
                process.waitFor(remaining(call.deadline), TimeUnit.NANOSECONDS);
            }
            joinUntil(call.stdout, call.deadline);
            joinUntil(call.stderr, call.deadline);
            timedOut |= call.stdout.isAlive() || call.stderr.isAlive();
        } catch (IOException | RuntimeException failure) {
            error = failure.toString();
        } catch (InterruptedException failure) {
            Thread.currentThread().interrupt();
            error = "probe_interrupted";
        } finally {
            terminate(call.process);
        }
        ProbeResult result = snapshot(call);
        result.timedOut = timedOut;
        if (error != null) {
            result.error = error;
        }
        return result;
    }

    private static String readSelf(String path, int limit) throws IOException {
        try (FileInputStream input = new FileInputStream(path)) {
            byte[] buffer = new byte[limit];
            int used = 0;
            int count;
            while (used < limit && (count = input.read(buffer, used, limit - used)) > 0) {
                used += count;
            }
            return new String(buffer, 0, used, StandardCharsets.UTF_8).trim();
        }
    }

    private static void put(JSONObject object, String key, Object value) {
        try {
            object.put(key, value == null ? JSONObject.NULL : value);
        } catch (JSONException failure) {
            throw new IllegalStateException(failure);
        }
    }

    private static String clipped(String value, int limit) {
        return value != null && value.length() > limit ? value.substring(0, limit) : value;
    }

    private static void emit(String nonce, ProbeResult result) {
        JSONObject event = new JSONObject();
        put(event, "schema", 1);
        put(event, "nonce", nonce);
        put(event, "uid", android.os.Process.myUid());
        put(event, "su_exit", result.exit);
        put(event, "timed_out", result.timedOut);
        put(event, "elapsed_ms", result.elapsedMs);
        put(event, "stdout_complete", result.stdoutComplete);
        put(event, "stderr_complete", result.stderrComplete);
        put(event, "error", clipped(result.error, 96));
        try {
            put(event, "selinux", readSelf("/proc/self/attr/current", 160));
            String status = readSelf("/proc/self/status", 4096);
            String procUid = null;
            for (String line : status.split("\n")) {
                if (line.startsWith("Uid:")) {
                    procUid = line.substring(4).trim();
                    break;
                }
            }
            put(event, "proc_uid", clipped(procUid, 80));
        } catch (IOException failure) {
            put(event, "identity_error", clipped(failure.toString(), 96));
        }
        String stdout = result.stdout;
        String stderr = result.stderr;
        String json;
        do {
            put(event, "stdout", stdout);
            put(event, "stderr", stderr);
            put(event, "stdout_truncated", result.stdoutTruncated);
            put(event, "stderr_truncated", result.stderrTruncated);
            json = event.toString();
            if (json.getBytes(StandardCharsets.UTF_8).length <= LOG_BYTES) {
                break;
            }
            // JSON escaping can expand control bytes sixfold; bound the final record too.
            stdout = stdout.substring(0, stdout.length() / 2);
            stderr = stderr.substring(0, stderr.length() / 2);
            result.stdoutTruncated = true;
            result.stderrTruncated = true;
        } while (true);
        Log.i(TAG, json);
    }
}
