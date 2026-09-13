package ai.ouroboros.android;

import android.accessibilityservice.AccessibilityService;
import android.view.accessibility.AccessibilityNodeInfo;
import android.view.accessibility.AccessibilityWindowInfo;
import org.json.JSONArray;
import org.json.JSONObject;

/** Optional user-enabled UI control surface; disabled until Android consent. */
public final class OuroborosAccessibilityService extends AccessibilityService {
    private static volatile OuroborosAccessibilityService instance;

    @Override public void onServiceConnected() {
        instance = this;
        super.onServiceConnected();
    }

    @Override public void onAccessibilityEvent(android.view.accessibility.AccessibilityEvent event) { }
    @Override public void onInterrupt() { }

    @Override public void onDestroy() {
        if (instance == this) instance = null;
        super.onDestroy();
    }

    static JSONObject state() throws Exception {
        return new JSONObject().put("enabled", instance != null)
                .put("coverage", "user_enabled_accessibility_service");
    }

    static JSONObject windows() throws Exception {
        OuroborosAccessibilityService service = instance;
        JSONArray rows = new JSONArray();
        if (service != null) {
            for (AccessibilityWindowInfo window : service.getWindows()) {
                if (window == null || window.getRoot() == null) continue;
                AccessibilityNodeInfo root = window.getRoot();
                rows.put(new JSONObject().put("package", String.valueOf(root.getPackageName()))
                        .put("class", String.valueOf(root.getClassName()))
                        .put("active", window.isActive()).put("focused", window.isFocused()));
                root.recycle();
            }
        }
        return new JSONObject().put("enabled", service != null).put("windows", rows)
                .put("coverage", "window_metadata_only");
    }

    static JSONObject perform(JSONObject params) throws Exception {
        OuroborosAccessibilityService service = instance;
        if (service == null) return new JSONObject().put("enabled", false).put("performed", false)
                .put("reason", "accessibility_not_enabled");
        String action = params.optString("action", "");
        boolean performed;
        switch (action) {
            case "back": performed = service.performGlobalAction(GLOBAL_ACTION_BACK); break;
            case "home": performed = service.performGlobalAction(GLOBAL_ACTION_HOME); break;
            case "notifications": performed = service.performGlobalAction(GLOBAL_ACTION_NOTIFICATIONS); break;
            case "quick_settings": performed = service.performGlobalAction(GLOBAL_ACTION_QUICK_SETTINGS); break;
            case "click_text":
                String text = params.getString("text");
                performed = clickText(service.getRootInActiveWindow(), text);
                break;
            default: throw new IllegalArgumentException("Unsupported accessibility action: " + action);
        }
        return new JSONObject().put("enabled", true).put("performed", performed)
                .put("action", action).put("coverage", "owner_requested_ui_action");
    }

    private static boolean clickText(AccessibilityNodeInfo node, String text) {
        if (node == null) return false;
        try {
            for (AccessibilityNodeInfo match : node.findAccessibilityNodeInfosByText(text)) {
                if (match != null && match.isClickable()) return match.performAction(AccessibilityNodeInfo.ACTION_CLICK);
            }
            for (int i = 0; i < node.getChildCount(); i++) if (clickText(node.getChild(i), text)) return true;
            return false;
        } finally { node.recycle(); }
    }
}
