/** Telegram's haptic feedback (docs/DESIGN.md "Shell", Haptics; owner 19.10, 3A): one meaning per kind, rare, never a
 * substitute for what the screen shows. Only inside Telegram, through its `HapticFeedback` (Bot API 6.1); in a
 * browser, the desktop app and an older Telegram this does nothing, so no screen depends on it.
 *
 * - `snap`: the drawer, released, latched open or closed: a light impact.
 * - `tick`: a value stepped (an Effort or Context level): the picker's selection tick.
 * - `press`: a long press opened a row's menu: a medium impact, as the press is recognised.
 * - `threshold`: a swipe on a message reached its Reply point (again after pulling back under it): a medium impact,
 *   once per crossing, as Telegram's own swipe to reply. */
import { hostCall, isTelegramHost } from './host_telegram.js';

const KINDS = Object.freeze({
    snap: ['impactOccurred', 'light'],
    tick: ['selectionChanged'],
    press: ['impactOccurred', 'medium'],
    threshold: ['impactOccurred', 'medium'],
});

/** True when Telegram took the haptic. */
export function haptic(kind, { win = globalThis.window, doc = globalThis.document } = {}) {
    const spec = KINDS[kind];
    const app = win?.Telegram?.WebApp;
    if (!spec || !app?.HapticFeedback || !isTelegramHost({ win, doc })) return false;
    // A member of the app: gated by the app's own version.
    if (typeof app.isVersionAtLeast === 'function' && !app.isVersionAtLeast('6.1')) return false;
    const [method, ...args] = spec;
    return hostCall(app.HapticFeedback, method, '', ...args);
}
