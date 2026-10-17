// The Main and Project composer (docs/DESIGN.md "Controls and editable choices", "Composer owner controls",
// "Chat attachments"): its markup inside the room's input dock, the message field (its growth, Enter, the input
// recall and the room's draft), Swarm, the room's Context and Effort capsules with their readback, the staged
// attachments with their paste and drop routes, and the send path that turns the draft into the owner's frame.
// The room (chat.js) mounts the markup, namespaces its ids and shows in its feed what was sent.
import { bindComposerFileTargets, cleanupUploadedAttachments } from './chat_media.js';
import { composerText, createComposerAttachments } from './chat_attachments.js';
import { loadChatInputHistory, saveChatInputHistory } from './chat_activity.js';
import { clientSurfaceField } from './client_surface.js';
import { apiClient, apiFetch } from './api_client.js';
import { bindEnterSubmit } from './ui_interactions.js';
import { createComposerOwnerControls } from './composer_owner_controls.js';
import { attachChatInputSizing } from './chat_input_sizing.js';
import { showToast } from './toast.js';

const CHAT_DRAFT_KEY = 'ouro_chat_draft';
const CHAT_INPUT_HISTORY_KEY = 'ouro_chat_input_history';

/** The composer inside the room's `.chat-input-wrap`; chat.js mounts it and namespaces its `chat-*` ids in a Project. */
export function composerMarkup() {
    return `
                <div id="chat-attachment-preview" class="chat-attachment-preview"></div>
                <div class="chat-toolbar-row">
                    <div class="chat-composer-pills" id="chat-composer-pills">
                        <button class="chat-swarm" id="chat-swarm" type="button" data-armed="false" title="Swarm: route your next message into a new managed task, run a deep plan review with plan_task, then delegate when parallel work helps. Auto-disarms after sending.">Swarm</button>
                    </div>
                    <div class="chat-context-mode" id="chat-context-mode" data-context-mode="max"></div>
                </div>
                <div class="chat-text-row">
                    <button class="chat-attach-btn" id="chat-attach" type="button" title="Attach file">
                        <svg width="18" height="18" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" stroke-linejoin="round"><path d="M21.44 11.05l-9.19 9.19a6 6 0 0 1-8.49-8.49l9.19-9.19a4 4 0 0 1 5.66 5.66l-9.2 9.19a2 2 0 0 1-2.83-2.83l8.49-8.48"/></svg>
                    </button>
                    <input type="file" id="chat-file-input" class="chat-file-input-hidden" accept="*/*" multiple>
                    <textarea id="chat-input" placeholder="Message Ouroboros..." rows="1" autocorrect="off" autocapitalize="off" spellcheck="false"></textarea>
                    <div class="chat-send-group">
                        <button class="chat-send-inline" id="chat-send" title="Send message">Send</button>
                    </div>
                </div>`;
}

/**
 * Wires one room's composer once chat.js has mounted `composerMarkup()` and namespaced its ids. `byId(suffix)`
 * resolves a namespaced id; `page` is the room (the drop target and the capsules' docking box); `ws` the shared
 * socket; `replies` the room's reply controller, whose reference rides the frame; `storeKey(base)` the room's
 * sessionStorage key. `refreshState()` re-reads the room's settings after a capsule saves, `onLayout()` re-measures
 * the room's dock, and `onSent({ text, frame, result, views })` shows an accepted send in the feed.
 */
export function createComposer({ page, byId, ws, replies, chatSessionId, isMain, chatId, projectId, storeKey,
    refreshState, onLayout, onSent }) {
    const input = byId('input');
    const sendBtn = byId('send');
    const attachBtn = byId('attach');
    const fileInput = byId('file-input');
    let destroyed = false, cognitionRead = false;

    // Staged files, their thumbnails and object URLs, and the upload on Send.
    const attachments = createComposerAttachments({ preview: byId('attachment-preview'), attachBtn, fileInput, input,
        showToast, onLayout: () => requestAnimationFrame(() => onLayout()) });

    attachBtn.addEventListener('click', () => fileInput.click());

    // Local-only staging avoids orphan uploads and fast-send races.
    fileInput.addEventListener('change', () => {
        const files = Array.from(fileInput.files || []);
        fileInput.value = '';
        attachments.stage(files);
    });

    bindComposerFileTargets({ page, inputArea: byId('input-area'), input, stagePendingFiles: attachments.stage });

    const inputHistory = loadChatInputHistory(sessionStorage, CHAT_INPUT_HISTORY_KEY);
    let inputHistoryIndex = inputHistory.length;
    let inputDraft = '';
    let inputHistorySeededFromServer = false; // set true only after a successful server-side recall seed

    function rememberInput(text) {
        if (!text) return;
        if (inputHistory[inputHistory.length - 1] !== text) inputHistory.push(text);
        saveChatInputHistory(sessionStorage, CHAT_INPUT_HISTORY_KEY, inputHistory);
        inputHistoryIndex = inputHistory.length;
        inputDraft = '';
    }

    // The first successful history read seeds the recall with the room's own sent words.
    function seedInputHistory(messages) {
        if (inputHistorySeededFromServer) return;
        const serverTexts = [];
        for (const msg of messages) {
            if (msg.role !== 'user') continue;
            let text = (msg.text || '').trim();
            if (text) serverTexts.push(text);
        }
        const combined = [...serverTexts, ...inputHistory];
        const deduped = [];
        const seen = new Set();
        for (let i = combined.length - 1; i >= 0; i--) {
            if (!seen.has(combined[i])) {
                deduped.unshift(combined[i]);
                seen.add(combined[i]);
            }
        }
        inputHistory.length = 0;
        inputHistory.push(...deduped.slice(-50));
        saveChatInputHistory(sessionStorage, CHAT_INPUT_HISTORY_KEY, inputHistory);
        inputHistoryIndex = inputHistory.length;
        inputHistorySeededFromServer = true;
    }

    // Text height is capped by chat_input_sizing.js.
    const inputSizing = attachChatInputSizing(input);
    function resizeChatInput() {
        inputSizing.sync();
        onLayout();
    }

    function restoreInputHistory(step) {
        if (!inputHistory.length) return;
        if (step < 0) {
            if (input.selectionStart !== 0 || input.selectionEnd !== 0) return;
            if (inputHistoryIndex === inputHistory.length) inputDraft = input.value;
            inputHistoryIndex = Math.max(0, inputHistoryIndex - 1);
            input.value = inputHistory[inputHistoryIndex] || '';
        } else {
            if (input.selectionStart !== input.value.length || input.selectionEnd !== input.value.length) return;
            inputHistoryIndex = Math.min(inputHistory.length, inputHistoryIndex + 1);
            input.value = inputHistoryIndex === inputHistory.length ? inputDraft : (inputHistory[inputHistoryIndex] || '');
        }
        resizeChatInput();
        const cursor = input.value.length;
        input.setSelectionRange(cursor, cursor);
    }

    async function sendMessage(planMode = false) {
        if (sendBtn.disabled) return;  // guard against Enter re-entry during async upload
        if (ownerControls.hasPendingSave()) {  // both room settings land before the next message
            setSendBusy(true, 'Saving');
            try { if (!(await ownerControls.pendingSave())) return; } finally { setSendBusy(false); }
        }
        let text = input.value.trim();
        const hasAttachments = attachments.count > 0;
        let uploadedAttachments = [];
        if (!text && !hasAttachments) return;
        if (hasAttachments) {
            // Upload at Send; offline queuing orphans files.
            if (ws.ws?.readyState !== WebSocket.OPEN) {
                showToast('Cannot attach file while offline. Reconnect and try again.', 'error');
                return;
            }
            setSendBusy(true, attachments.count > 1 ? 'Uploading files' : 'Uploading');
            try {
                uploadedAttachments = await attachments.upload(() => ws.ws?.readyState === WebSocket.OPEN);
            } catch (e) {
                await cleanupUploadedAttachments(e.uploaded || []);
                showToast('Upload error: ' + e.message, 'error');
                return;  // staged files and their previews remain so the user can retry
            } finally {
                setSendBusy(false);
            }
            // Model sees file names; /restart stays exact, bubbles use server views.
            text = composerText(text, uploadedAttachments.map((item) => item.view?.name || item.display_name));
        }
        if (!text) return;
        const forcePlan = !!planMode && !text.startsWith('/');
        const frame = {
            type: 'chat',
            content: text,
            sender_session_id: chatSessionId,
            force_plan: forcePlan,
            ...(isMain ? {} : { chat_id: chatId }),
            ...(projectId ? { project_id: projectId } : {}),
            // Structured metadata rides the frame: model staging and native image blocks.
            ...(hasAttachments ? { attachments: uploadedAttachments.map(({ filename, display_name, mime }) => ({ filename, display_name, mime })) } : {}),
            ...replies.frameField(),
            ...clientSurfaceField(),
        };
        const result = ws.send(frame, hasAttachments ? { queue: false } : undefined);
        if (hasAttachments && result?.status !== 'sent') {
            await cleanupUploadedAttachments(uploadedAttachments);
            showToast('Connection lost before send. Reconnect and try again.', 'error');
            return;
        }
        // Send consumes Swarm.
        if (planMode) setSwarm(false);
        if (hasAttachments) attachments.clear();
        rememberInput(text);
        input.value = '';
        clearInputDraft();
        onSent({ text, frame, result, views: uploadedAttachments.map((item) => item.view) });
    }

    // Send mode lives on DOM so CSS and click/Enter share one source.
    const sendGroup = page.querySelector('.chat-send-group');

    // Swarm requests planning once, then disarms.
    const swarmBtn = byId('swarm');
    function swarmArmed() {
        return swarmBtn?.dataset.armed === 'true';
    }
    function setSwarm(armed) {
        if (swarmBtn) swarmBtn.dataset.armed = armed ? 'true' : 'false';
    }

    function setSendBusy(busy, label = '') {
        sendGroup.dataset.busy = busy ? '1' : '0';
        sendBtn.disabled = busy;
        if (busy) {
            sendBtn.textContent = label || 'Sending';
            sendBtn.title = label || 'Sending';
        } else {
            sendBtn.textContent = 'Send';
            sendBtn.title = 'Send message';
        }
    }

    swarmBtn?.addEventListener('click', () => setSwarm(!swarmArmed()));

    // Each composer owns its room's Context and Effort settings.
    const ownerControls = createComposerOwnerControls({ row: page.querySelector('.chat-toolbar-row'), box: page, byId, apiFetch, showToast,
        projectId, saveEffortRange: apiClient.ownerEffortRange, refreshState, onLayout });

    // Rooms outside the bounded sidebar still read their settings.
    async function refreshProjectCognition() {
        if (destroyed || cognitionRead) return;
        cognitionRead = true;
        try {
            const room = await apiFetch(`/api/projects?chat_id=${encodeURIComponent(chatId)}`, { cache: 'no-store' });
            if (room.ok && !destroyed) ownerControls.syncState(await room.json());
        } catch { /* Preserve the last room value, never substitute Main. */ }
        finally { cognitionRead = false; }
    }

    // Do not pass MouseEvent as planMode.
    sendBtn.addEventListener('click', () => sendMessage(swarmArmed()));
    bindEnterSubmit(input, sendBtn);
    input.addEventListener('keydown', (e) => {
        if (e.key === 'ArrowUp' && !e.shiftKey) {
            restoreInputHistory(-1);
        } else if (e.key === 'ArrowDown' && !e.shiftKey) {
            restoreInputHistory(1);
        }
    });

    // Per-thread drafts survive room closure; Send clears them.
    function saveInputDraft() {
        try {
            if (input.value) sessionStorage.setItem(storeKey(CHAT_DRAFT_KEY), input.value);
            else sessionStorage.removeItem(storeKey(CHAT_DRAFT_KEY));
        } catch {}
    }

    function clearInputDraft() {
        try { sessionStorage.removeItem(storeKey(CHAT_DRAFT_KEY)); } catch {}
    }

    try {
        const savedDraft = sessionStorage.getItem(storeKey(CHAT_DRAFT_KEY)) || '';
        if (savedDraft && !input.value) {
            input.value = savedDraft;
            inputDraft = savedDraft;
            resizeChatInput();
        }
    } catch {}

    // Typing, and every change this module makes on another's behalf, keeps the recall's draft, the height and the
    // room's draft current.
    function onInput() {
        if (inputHistoryIndex === inputHistory.length) inputDraft = input.value;
        resizeChatInput();
        saveInputDraft();
    }
    input.addEventListener('input', onInput);

    return {
        input,
        focus: () => input.focus({ preventScroll: true }),
        getDraft: () => input.value,
        /** Replace the draft (it is kept for the room like typed words). */
        setDraft(text) {
            input.value = String(text ?? '');
            onInput();
        },
        /** Insert words at the caret, replacing a selection, and leave the caret after them in the focused field. */
        insertAtCursor(text) {
            const words = String(text ?? '');
            const start = input.selectionStart ?? input.value.length;
            const end = input.selectionEnd ?? start;
            input.value = input.value.slice(0, start) + words + input.value.slice(end);
            input.focus({ preventScroll: true });
            input.setSelectionRange(start + words.length, start + words.length);
            onInput();
        },
        // After a send, once the feed shows it: the emptied field's height and the room's dock.
        resize: resizeChatInput,
        seedInputHistory,
        // The room's Context and Effort readback, from Main's state or the named Project.
        syncState(data) {
            ownerControls.syncState(data);
            if (projectId && !data?.projects?.some(project => project.id === projectId && project.cognition)) void refreshProjectCognition();
        },
        // A brief composer brighten when a task leaves the main chat for a project — a calm "you're free to start
        // something else" signal (P3). The CSS animation owns the duration; the class leaves with it, no timer.
        signalFreed() {
            const row = page.querySelector('.chat-text-row');
            row?.classList.add('chat-freed');
            row?.addEventListener('animationend', function done(event) {
                if (event.target !== row) return;  // a child's animation is not this cue
                row.classList.remove('chat-freed');
                row.removeEventListener('animationend', done);
            });
        },
        // Unsendable client-side state: staged File objects or an upload in flight.
        hasPendingWork: () => attachments.count > 0 || attachments.busy,
        destroy() {
            destroyed = true;
            attachments.destroy();
            ownerControls.destroy();
            inputSizing.dispose();
        },
    };
}
