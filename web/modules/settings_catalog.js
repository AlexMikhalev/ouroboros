import { apiFetch } from './api_client.js';
import { setInlineStatus } from './ui_helpers.js';
export const MODEL_CATALOG_TIMEOUT_MS = 25000;
let catalogRefreshSeq = 0;

/**
 * Read provenance belongs to discovery, never to the owner's saved assignment.
 * One unreachable source among several is `partial`: the catalogs that did
 * answer are real, and claiming the whole read failed hid working API models.
 */
export function catalogReadState(data = {}) {
    if (data.read_state) return data.read_state;
    if (data.stale || data.freshness === 'stale') return 'stale';
    if (data.error || data.errors?.length) {
        return Array.isArray(data.items) && data.items.length ? 'partial' : 'failed';
    }
    if (data.partial) return 'partial';
    return Array.isArray(data.items) ? 'ok' : 'not_read';
}

/** Enrich the existing editor state; a read gap cannot erase last-known choices. */
export function mergeModelCatalog(previous = {}, incoming = {}) {
    const read_state = catalogReadState(incoming);
    const retain = read_state !== 'ok';
    const merge = (key, identity) => {
        const before = Array.isArray(previous[key]) ? previous[key] : [];
        if (!Array.isArray(incoming[key])) return before;
        if (!retain) return incoming[key];
        const retained = key === 'items' ? before.flatMap((item) => {
            const account = (incoming.account_catalogs || []).filter((envelope) => !item.source_id || item.source_id === envelope.source)
                .flatMap((envelope) => envelope.accounts || []).find((entry) => entry.credentialProfileId === item.credential_profile_id);
            return account?.catalog ? [] : [{ ...item, ...(account ? { availability: account.availability, problem: account.problem } : {}) }];
        }) : before;
        const entries = new Map(retained.map((item) => [identity(item), item]));
        for (const item of incoming[key]) entries.set(identity(item), item);
        return [...entries.values()];
    };
    return { ...(retain ? previous : {}), ...incoming, read_state,
        sources_read_state: Array.isArray(incoming.model_sources) ? read_state : (read_state === 'ok' ? 'not_read' : read_state),
        errors: incoming.errors || (incoming.error ? [{ error: incoming.error }] : []),
        items: merge('items', (item) => JSON.stringify([item.value || item.id, item.credential_profile_id || ''])),
        model_sources: merge('model_sources', (source) => source.id),
    };
}

/** Keep last-known native choices only for account reads that did not succeed. */
export function mergeHarnessModelCatalog(previous, current) {
    return { ...current, models: mergeModelCatalog({ items: previous?.models || [] }, {
        items: current.models || [], partial: current.model_catalog?.partial,
        account_catalogs: current.model_catalog ? [current.model_catalog] : [],
        errors: current.models_error ? [{ error: current.models_error }] : [],
    }).items };
}

/** httpx appends a documentation pointer to every status error; the owner needs the status, not the link. */
function readErrorText(error) {
    const text = [error.credential_profile_id, error.error || error.code || error.provider_id]
        .filter(Boolean).join(': ');
    return text.split(/\s*For more information check:/)[0].trim();
}

/**
 * Name what actually happened: which read failed, and what is usable anyway.
 * `compact` is the per-row form under a section banner that already lists the
 * failed reads: one short sentence, never the error list repeated per row.
 */
export function catalogReadNote(data = {}, { compact = false } = {}) {
    const state = catalogReadState(data);
    if (state === 'ok') return '';
    if (compact) {
        const short = state === 'partial' ? 'Some model sources could not be read.'
            : state === 'stale' ? 'Model catalog is last known.'
                : ['failed', 'transport'].includes(state) ? 'Model catalog could not be read.'
                    : 'Model catalog has not been read yet.';
        return `${short} Existing suggestions and your selection are kept.`;
    }
    const errors = (data.errors || []).map(readErrorText);
    const loaded = (Array.isArray(data.items) ? data.items : []).filter(
        (item) => !String(item?.value || item?.id || '').startsWith('claudexor::')).length;
    const reason = state === 'partial' && errors.length
        ? `Some model sources could not be read: ${errors.join('; ')}.${loaded
            ? ` ${loaded} API model${loaded === 1 ? '' : 's'} loaded.` : ''}`
        : errors.length ? `Model catalog could not be read: ${errors.join('; ')}.`
            : state === 'stale' ? 'Model catalog is last known.' : state === 'partial' ? 'Some account model lists could not be read.'
                : ['failed', 'transport'].includes(state) ? 'Model catalog could not be read.' : 'Model catalog has not been read yet.';
    return `${reason} Existing suggestions and your selection are kept. Refresh Model Catalog in Models to retry.`;
}

function setCatalogStatus(statusEl, text, tone = 'muted') {
    setInlineStatus(statusEl, text, tone);
}

function broadcastCatalog(data) {
    document.dispatchEvent(new CustomEvent('settings-model-catalog:updated', {
        detail: data,
    }));
}

function fillCatalogDatalist(data) {
    const list = document.getElementById('settings-model-catalog');
    if (list) {
        const { items } = mergeModelCatalog({ items: [...list.options].map((option) => ({ value: option.value, label: option.label })) }, data);
        list.innerHTML = '';
        for (const item of items) {
            const option = document.createElement('option');
            option.value = item.value || item.id || '';
            option.label = item.label || item.provider || '';
            list.appendChild(option);
        }
    }
    broadcastCatalog(data);
}

export async function refreshModelCatalog({ button } = {}) {
    const refreshSeq = ++catalogRefreshSeq;
    const statusEl = document.getElementById('settings-model-catalog-status');
    setCatalogStatus(statusEl, 'Refreshing model catalog...', 'muted');
    if (button) {
        button.disabled = true;
        button.setAttribute('aria-busy', 'true');
    }
    const controller = new AbortController();
    const timeoutId = setTimeout(() => controller.abort(), MODEL_CATALOG_TIMEOUT_MS);

    try {
        const resp = await apiFetch('/api/model-catalog', {
            cache: 'no-store',
            signal: controller.signal,
        });
        const data = await resp.json();
        if (!resp.ok) throw new Error(data.error || `HTTP ${resp.status}`);
        if (!Array.isArray(data?.items)) throw new Error(data?.error || 'Model catalog response has no model list');

        const items = Array.isArray(data.items) ? data.items : [];
        const errors = Array.isArray(data.errors) ? data.errors : [];
        if (refreshSeq !== catalogRefreshSeq) {
            return { items, errors, stale: true };
        }
        const read_state = catalogReadState(data);
        fillCatalogDatalist({ ...data, read_state });

        if (read_state !== 'ok') {
            setCatalogStatus(statusEl, catalogReadNote({ ...data, read_state }), 'warn');
        } else if (items.length) {
            setCatalogStatus(statusEl, `Loaded ${items.length} models.`, 'ok');
        } else {
            setCatalogStatus(statusEl, 'No provider catalogs available yet. This is optional.', 'muted');
        }
        return { ...data, items, errors, read_state };
    } catch (err) {
        if (refreshSeq !== catalogRefreshSeq) {
            return { items: [], errors: [{ provider_id: 'catalog', error: 'stale refresh' }], stale: true };
        }
        const message = err?.name === 'AbortError'
            ? `Timed out after ${Math.round(MODEL_CATALOG_TIMEOUT_MS / 1000)}s`
            : (err.message || err);
        fillCatalogDatalist({ read_state: 'transport', errors: [{ provider_id: 'catalog', error: String(message) }] });
        setCatalogStatus(
            statusEl,
            `Model catalog failed: ${message}. This is optional.`,
            'warn',
        );
        return { items: [], errors: [{ provider_id: 'catalog', error: String(message) }] };
    } finally {
        clearTimeout(timeoutId);
        if (button && refreshSeq === catalogRefreshSeq) {
            button.disabled = false;
            button.removeAttribute('aria-busy');
        }
    }
}
