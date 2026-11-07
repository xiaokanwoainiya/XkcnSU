package io.xkcnsu.acceptance.rootpolicy;

import android.app.Activity;
import android.content.Intent;
import android.os.Bundle;
import android.os.Handler;
import android.os.Looper;
import android.os.SystemClock;
import android.view.Gravity;
import android.view.WindowManager;
import android.widget.TextView;

public final class ProbeActivity extends Activity {
    private static final long HOLD_MS = 9000;
    private final Handler handler = new Handler(Looper.getMainLooper());
    private final Runnable finishProbe = this::finish;
    private boolean dispatched;
    private long finishAt;

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        TextView label = new TextView(this);
        label.setText("XKCNSU Root Test");
        label.setTextSize(20);
        label.setGravity(Gravity.CENTER);
        setContentView(label);
        if (state != null) {
            dispatched = state.getBoolean("probe_dispatched");
            finishAt = state.getLong("probe_finish_at");
        }
    }

    @Override
    protected void onPostResume() {
        super.onPostResume();
        if (!dispatched) {
            dispatched = true;
            finishAt = SystemClock.uptimeMillis() + HOLD_MS;
            String nonce = getIntent().getStringExtra("nonce");
            if (nonce != null && nonce.matches("[A-Za-z0-9._:-]{1,64}")) {
                Intent probe = new Intent(this, ProbeReceiver.class);
                probe.setAction("io.xkcnsu.acceptance.rootpolicy.PROBE");
                probe.putExtra("nonce", nonce);
                probe.addFlags(Intent.FLAG_RECEIVER_FOREGROUND);
                sendBroadcast(probe);
            }
        }
        handler.removeCallbacks(finishProbe);
        handler.postDelayed(finishProbe, Math.max(0, finishAt - SystemClock.uptimeMillis()));
    }

    @Override
    protected void onSaveInstanceState(Bundle state) {
        state.putBoolean("probe_dispatched", dispatched);
        state.putLong("probe_finish_at", finishAt);
        super.onSaveInstanceState(state);
    }

    @Override
    protected void onDestroy() {
        handler.removeCallbacks(finishProbe);
        super.onDestroy();
    }
}
