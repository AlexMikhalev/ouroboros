package ai.ouroboros.android;

import android.app.PackageInstaller;
import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import org.json.JSONObject;

/** Records PackageInstaller completion without claiming that commit submission is success. */
public final class PackageInstallReceiver extends BroadcastReceiver {
    static final String ACTION = "ai.ouroboros.android.PACKAGE_INSTALL_RESULT";
    static final String KEY = "idempotency_key";
    static final String SESSION = "session_id";

    @Override public void onReceive(Context context, Intent intent) {
        if (!ACTION.equals(intent.getAction())) return;
        String key = intent.getStringExtra(KEY);
        if (key == null || key.isEmpty()) return;
        try {
            android.content.SharedPreferences prefs = context.getSharedPreferences(
                    "package_install_receipts", Context.MODE_PRIVATE);
            String previous = prefs.getString(key, null);
            if (previous == null) return;
            JSONObject receipt = new JSONObject(previous);
            int status = intent.getIntExtra(PackageInstaller.EXTRA_STATUS, Integer.MIN_VALUE);
            String statusName;
            if (status == PackageInstaller.STATUS_SUCCESS) statusName = "success";
            else if (status == PackageInstaller.STATUS_PENDING_USER_ACTION) statusName = "pending_user_action";
            else statusName = "failure";
            receipt.put("status", statusName).put("status_code", status)
                    .put("completion_observed", true).put("outcome", statusName)
                    .put("status_message", intent.getStringExtra(PackageInstaller.EXTRA_STATUS_MESSAGE));
            String packageName = intent.getStringExtra(PackageInstaller.EXTRA_PACKAGE_NAME);
            if (packageName != null) receipt.put("package", packageName);
            prefs.edit().putString(key, receipt.toString()).apply();
        } catch (Exception ignored) {
            // A malformed system callback cannot turn a submitted install into a false success.
        }
    }
}
