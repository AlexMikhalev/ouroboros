// Neutral route-editor primitives shared by Available subagents and Review
// lanes. Semantic owners keep their own schemas: reviewer rows serialize
// `api_chat` + `profile_id`; task actors serialize `api_model` +
// `credential_profile_id`.

import { formatRelativeAge } from './ui_helpers.js';
import { escapeHtmlAttr as escapeHtml } from './utils.js';
import { modelChooserHtml, updateModelChooserOptions } from './model_chooser.js';

export const ROUTE_KIND_API_MODEL = 'api_model';
export const ROUTE_KIND_AGENT_SESSION = 'agent_session';
export const API_ROUTE_CHOICE = 'api';
export const EFFORT_CHOICES = ['none', 'minimal', 'low', 'medium', 'high', 'xhigh', 'max', 'ultra'];
export const PROCESSING_PREFERENCE_KEY = 'OUROBOROS_PROCESSING_PREFERENCE';
export const MODEL_PROCESSING_PREFERENCES_KEY = 'OUROBOROS_MODEL_PROCESSING_PREFERENCES';
export const PROCESSING_CHOICES = ['standard', 'fast', 'economy'];

export function processingLabel(value) {
    return { standard: 'Standard', fast: 'Fast', economy: 'Economy', mixed: 'Mixed', unknown: 'Unknown' }[value] || 'Route default';
}

export function processingIntentLabel(value, inherited = '') {
    return value ? `${processingLabel(value)} (override)`
        : inherited ? `${processingLabel(inherited)} (from Models)` : 'Route default (inherited)';
}

export function processingSelectHtml(attrs, selected, { global = false } = {}) {
    return selectHtml(attrs, [{ options: [
        { value: '', label: global ? 'Keep route defaults' : 'Use global setting' },
        ...PROCESSING_CHOICES.map((value) => ({ value, label: processingLabel(value) })),
    ] }], selected || '');
}

/** Intent controls never change model/effort or claim the route served this mode. */
export function processingDetailsHtml(attrs, selected, inherited = '') {
    return `<details class="model-role-details" data-processing-details><summary>Processing · <span data-processing-summary>${escapeHtml(processingIntentLabel(selected, inherited))}</span></summary>
        <label class="ui-field">Processing ${processingSelectHtml(attrs, selected)}</label>
        <div class="ui-field-help">Uses the same model and reasoning effort. An explicit native service choice takes precedence.</div></details>`;
}

export function processingCapabilityNote(preference, capability, transportPreferences) {
    if (preference && Array.isArray(transportPreferences) && !transportPreferences.includes(preference)) return `${processingLabel(preference)} processing is not advertised by this transport. Ordinary service remains available.`;
    return preference && Array.isArray(capability?.modes) && !capability.modes.includes(preference)
        ? `${processingLabel(preference)} is not advertised for this route. Ordinary service may be used; the model and effort stay the same.` : '';
}

/** Display only an existing execution receipt, keeping requested and observed separate. */
export function processingExecutionText(processing) {
    if (!processing || !('observed' in processing)) return '';
    const parts = [processing.observed === 'unknown' ? 'Applied processing not reported'
        : `Applied processing: ${processingLabel(processing.observed)}`];
    if (processing.requested) parts.push(`requested ${processingLabel(processing.requested)}`);
    if (processing.submittedNative) parts.push(`submitted service ${processing.submittedNative}`);
    if (processing.reason) parts.push(String(processing.reason));
    return parts.join(' · ');
}

export function parseModelSource(value) {
    const raw = String(value || '').trim();
    if (raw.startsWith('claudexor::')) {
        const target = raw.slice('claudexor::'.length);
        const split = target.indexOf('=');
        return { source: `subscription:${split < 0 ? target : target.slice(0, split)}`,
            model: split < 0 ? '' : target.slice(split + 1) };
    }
    const split = raw.indexOf('::');
    return split < 0 ? { source: 'openrouter', model: raw }
        : { source: raw.slice(0, split), model: raw.slice(split + 2) };
}

export function composeModelSource(source, model) {
    const value = String(model || '').trim();
    if (!value) return '';
    if (value.includes('::')) return value;
    if (source.startsWith('subscription:')) return `claudexor::${source.slice(13)}=${value}`;
    return source === 'openrouter' ? value : `${source}::${value}`;
}

/** Account binding is supported by agent sessions and subscription model routes. */
export function routeSupportsAccount(route) {
    return route?.kind === ROUTE_KIND_AGENT_SESSION
        || (['api_model', 'api_chat'].includes(route?.kind)
            && parseModelSource(route?.target_id).source.startsWith('subscription:'));
}

/** Source ids are opaque; only the model-sources envelope names their credential owner. */
export function routeModelFields(route, modelSources = []) {
    if (route?.kind === ROUTE_KIND_AGENT_SESSION) {
        return { ...splitSessionTarget(route.target_id), subscription: false };
    }
    const parsed = parseModelSource(route?.target_id);
    const subscription = parsed.source.startsWith('subscription:');
    const source = subscription ? parsed.source.slice(13) : '';
    const descriptor = modelSources.find((entry) => entry.id === source);
    return { source, sourceLabel: descriptor?.label || source, subscription,
        model: subscription ? parsed.model : String(route?.target_id || ''),
        harness: subscription ? String(descriptor?.credentialHarness || '') : '' };
}

export function routeTargetFromModel(route, model) {
    if (route?.kind === ROUTE_KIND_AGENT_SESSION) {
        return composeSessionTarget(splitSessionTarget(route.target_id).harness, model);
    }
    const { source, subscription } = routeModelFields(route);
    return subscription ? composeModelSource(`subscription:${source}`, model)
        || `claudexor::${source}=` : String(model || '');
}

/** A source change clears only source-bound fields, never the caller's delivery kind. */
export function changeRouteChoice(route, choice, { apiKind = ROUTE_KIND_API_MODEL } = {}) {
    if (encodeRouteChoice({ route }) === choice) return { ...route };
    const decoded = decodeRouteChoice(choice, { apiKind });
    return { kind: decoded.kind, target_id: decoded.harness || (decoded.source
        ? `claudexor::${decoded.source}=` : '') };
}

function routeCatalogItems(route, items = []) {
    const fields = routeModelFields(route);
    const pin = route?.credential_profile_id || route?.profile_id || '';
    return items.filter((item) => (!fields.subscription || parseModelSource(item?.value || item?.id || item).source === `subscription:${fields.source}`)
        && (!pin || !item?.credential_profile_id || item.credential_profile_id === pin));
}

/** Collapse duplicate values for the chooser, retaining account evidence in labels. */
export function catalogModelOptions(items = []) {
    const values = new Map();
    for (const item of items) {
        const value = String(item?.value || item?.id || item);
        const label = String(item?.name || item?.label || value);
        const account = item?.credential_profile_id;
        const facts = account ? [account, item.availability, item.observed_at
            ? formatRelativeAge(Date.parse(item.observed_at), 'just now') : 'date unknown'].filter(Boolean).join(' · ') : '';
        const current = values.get(value) || { value, label, accounts: [] };
        if (facts && !current.accounts.includes(facts)) current.accounts.push(facts);
        values.set(value, current);
    }
    return [...values.values()].map(({ value, label, accounts }) => ({ value,
        label: accounts.length ? `${label} · ${accounts.join('; ')}` : label }));
}

export function routeModelSuggestions(route, items = []) {
    const fields = routeModelFields(route);
    return routeCatalogItems(route, items).map((item) => String(item?.value || item?.id || item))
        .map((value) => fields.subscription ? parseModelSource(value).model : value);
}

/** Catalog suggestions, not an entitlement or context claim for the selected account. */
export function routeModelInputHtml(attrs, route, items, listId, { placeholder = 'Choose a model' } = {}) {
    const fields = routeModelFields(route);
    const values = catalogModelOptions(routeCatalogItems(route, items).map((item) => {
        const value = String(item?.value || item?.id || item);
        return { ...(typeof item === 'object' ? item : {}), value: fields.subscription ? parseModelSource(value).model : value };
    }));
    return modelChooserHtml(attrs, routeModelFields(route).model, listId, values, { placeholder });
}

/** Catalog repaint owns suggestions and native option labels, never a draft node. */
export function updateRouteControlOptions(current, desired) {
    for (const field of current.querySelectorAll('select')) {
        const marker = [...field.attributes].find((attr) => attr.name.startsWith('data-'));
        if (!marker) continue;
        const next = [...desired.querySelectorAll('select')]
            .find((node) => node.getAttribute(marker.name) === marker.value);
        if (next && field.innerHTML !== next.innerHTML) {
            const value = field.value;
            field.innerHTML = next.innerHTML;
            field.value = value;
        }
    }
    updateModelChooserOptions(current, desired);
}

export function mintStableId(prefix, takenIds) {
    const taken = new Set(takenIds || []);
    for (let attempt = 0; attempt < 1000; attempt += 1) {
        const candidate = `${prefix}_${Math.random().toString(36).slice(2, 8)}`;
        if (!taken.has(candidate)) return candidate;
    }
    return `${prefix}_${Date.now().toString(36)}`;
}

export function composeSessionTarget(harness, model) {
    const h = String(harness || '').trim();
    const m = String(model || '').trim();
    return m ? `${h}=${m}` : h;
}

export function splitSessionTarget(target) {
    const raw = String(target || '');
    const eq = raw.indexOf('=');
    if (eq < 0) return { harness: raw, model: '' };
    return { harness: raw.slice(0, eq), model: raw.slice(eq + 1) };
}

/** Effort already encoded in a Cursor/Agy compound model slug, if any. */
export function compoundSessionEffort(target) {
    const { harness, model } = splitSessionTarget(target);
    if (!['cursor', 'agy'].includes(String(harness || '')) || !model) return '';
    const compound = model.toLowerCase().endsWith('-fast') ? model.slice(0, -5) : model;
    const encoded = compound.slice(compound.lastIndexOf('-') + 1).toLowerCase();
    return EFFORT_CHOICES.includes(encoded) ? encoded : '';
}

/** Return the encoded effort only when a separate field contradicts it. */
export function compoundSessionEffortConflict(target, effort) {
    const encoded = compoundSessionEffort(target);
    const requested = String(effort || '').trim().toLowerCase();
    return encoded && requested && encoded !== requested ? encoded : '';
}

export function encodeRouteChoice(row) {
    if (row?.route?.kind === ROUTE_KIND_AGENT_SESSION) {
        return `session:${splitSessionTarget(row.route.target_id).harness}`;
    }
    const { source, subscription } = routeModelFields(row?.route);
    return subscription ? `subscription:${source}` : API_ROUTE_CHOICE;
}

export function decodeRouteChoice(value, { apiKind = ROUTE_KIND_API_MODEL } = {}) {
    const raw = String(value || '');
    if (raw.startsWith('session:')) {
        return { kind: ROUTE_KIND_AGENT_SESSION, harness: raw.slice('session:'.length) };
    }
    return raw.startsWith('subscription:')
        ? { kind: apiKind, source: raw.slice(13) } : { kind: apiKind };
}

export function normalizeRouteSpec(route, {
    apiKind = ROUTE_KIND_API_MODEL,
    apiAliases = ['api_model', 'api_chat', 'api'],
} = {}) {
    const input = route && typeof route === 'object' ? route : {};
    const kind = input.kind === ROUTE_KIND_AGENT_SESSION
        ? ROUTE_KIND_AGENT_SESSION
        : (apiAliases.includes(String(input.kind || '')) ? apiKind : String(input.kind || apiKind));
    return {
        kind,
        target_id: String(input.target_id || ''),
        credential_pin: String(input.credential_pin
            || input.credential_profile_id || input.profile_id || ''),
    };
}

export function serializeRouteSpec(route, {
    apiKind = ROUTE_KIND_API_MODEL,
    credentialField = 'credential_profile_id',
} = {}) {
    const normalized = normalizeRouteSpec(route, { apiKind });
    const out = {
        kind: normalized.kind === ROUTE_KIND_AGENT_SESSION
            ? ROUTE_KIND_AGENT_SESSION : apiKind,
        target_id: normalized.target_id,
    };
    if (routeSupportsAccount(out) && normalized.credential_pin) {
        out[credentialField] = normalized.credential_pin;
    }
    return out;
}

function undiscoveredLabel(value, known) {
    return `${value} (${known ? 'not in discovery' : 'not checked'})`;
}

export function routeChoiceGroups({
    harnesses = [], modelSources = [], currentChoice = '', catalogKnown = true, apiLabel = 'API model',
} = {}) {
    const sessionValues = (harnesses || [])
        .filter((harness) => harness && harness.id)
        .map((harness) => ({
            value: `session:${harness.id}`,
            label: `${harness.display_name || harness.id} (agent)`,
            disabled: harness.status && harness.status !== 'ok' && !harness.enabled,
        }));
    const savedChoice = String(currentChoice || '');
    if (savedChoice.startsWith('session:')
        && !sessionValues.some((option) => option.value === savedChoice)) {
        sessionValues.push({
            value: savedChoice,
            label: undiscoveredLabel(savedChoice.slice('session:'.length), catalogKnown),
        });
    }
    const modelValues = modelSources.map((source) => ({
        value: `subscription:${source.id}`, label: `${source.label || source.id} (model)`,
    }));
    if (savedChoice.startsWith('subscription:')
        && !modelValues.some((option) => option.value === savedChoice)) {
        modelValues.push({ value: savedChoice, label: `${savedChoice.slice(13)} (not checked)` });
    }
    return [
        ...(modelValues.length ? [{ label: 'Models — subscriptions', options: modelValues }] : []),
        { label: 'API', options: [{ value: API_ROUTE_CHOICE, label: apiLabel }] },
        sessionValues.length
            ? { label: 'Agents — subscriptions', options: sessionValues }
            : { label: 'Agents — subscriptions', options: [{
                value: '',
                disabled: true,
                label: catalogKnown
                    ? 'None available — no agent sources were listed'
                    : 'Could not be listed — see the service banner above',
            }] },
    ];
}

export function indexProfilesByHarness(payload) {
    const byHarness = {};
    const profiles = payload?.profiles?.profiles || [];
    for (const wrapper of Array.isArray(profiles) ? profiles : []) {
        const profile = wrapper?.profile || {};
        const harness = String(profile.harness_id || '');
        const id = String(profile.profile_id || '');
        if (!harness || !id) continue;
        (byHarness[harness] = byHarness[harness] || []).push({
            id,
            enabled: profile.enabled !== false,
        });
    }
    return byHarness;
}

export function profileEntry(entry) {
    if (typeof entry === 'string') return { id: entry, enabled: true };
    return { id: String(entry?.id || ''), enabled: entry?.enabled !== false };
}

/** Native model discovery is per account; an unread account is not an empty catalog. */
export function accountScopedModelCatalog(harness, pin = '') {
    const envelope = harness?.model_catalog;
    if (!Array.isArray(envelope?.accounts)) return harness;
    const accounts = envelope.accounts.filter((account) => !pin || account.credentialProfileId === pin);
    const gaps = accounts.filter((account) => !account.catalog);
    const error = gaps.map((account) => account.problem?.message || 'Account model list could not be read').join('; ')
        || ((!pin || !accounts.length) && envelope.partial ? 'Some account model lists could not be read' : '');
    return { ...harness, models: (harness.models || []).filter((item) => !pin || item.credential_profile_id === pin),
        models_error: error };
}

export function harnessModelsKnown(harness, catalogKnown = true) {
    return Boolean(catalogKnown) && !String(harness?.models_error || '');
}

export function modelsGapNote(harness, catalogKnown = true) {
    return catalogKnown && String(harness?.models_error || '')
        ? 'model list could not be read' : '';
}

export function sessionModelOptions(harness, currentModel, { catalogKnown = true } = {}) {
    const models = harness?.models || [];
    const options = [
        { value: '', label: 'Engine default model' },
        ...catalogModelOptions(models),
    ];
    if (currentModel && !options.some((option) => option.value === currentModel)) {
        options.push({
            value: currentModel,
            label: undiscoveredLabel(currentModel, harnessModelsKnown(harness, catalogKnown)),
        });
    }
    return options;
}

export function profileOptionsFor(profiles, savedPin, { accountsKnown = true } = {}) {
    const options = [
        { value: '', label: 'Account: automatic rotation' },
        ...(profiles || []).map(profileEntry).filter((profile) => profile.id).map((profile) => ({
            value: profile.id,
            label: `Account: ${profile.id} (pinned)${profile.enabled ? '' : ' (disabled)'}`,
        })),
    ];
    if (savedPin && !options.some((option) => option.value === savedPin)) {
        options.push({
            value: savedPin,
            label: `Account: ${undiscoveredLabel(savedPin, accountsKnown)}`,
        });
    }
    return options;
}

export function selectHtml(attrs, groups, selected) {
    const options = (groups || []).map((group) => {
        const body = (group.options || []).map((option) => {
            const isSelected = option.value === selected ? ' selected' : '';
            const disabled = option.disabled ? ' disabled' : '';
            return `<option value="${escapeHtml(option.value)}"${isSelected}${disabled}>${escapeHtml(option.label)}</option>`;
        }).join('');
        return group.label
            ? `<optgroup label="${escapeHtml(group.label)}">${body}</optgroup>` : body;
    }).join('');
    return `<select class="ui-control" ${attrs}>${options}</select>`;
}

export function effortSelectHtml(attrs, selected, surfaceDefault = 'route default') {
    const options = [
        { value: '', label: 'Default effort' },
        ...EFFORT_CHOICES.map((effort) => ({ value: effort, label: effort })),
    ];
    return selectHtml(
        `${attrs} title="Reasoning effort — default: ${escapeHtml(surfaceDefault)}"`,
        [{ label: '', options }],
        selected || '',
    );
}

export function describeExecutionEvidence(entry) {
    if (!entry || typeof entry !== 'object') return '';
    if ('requested_model' in entry || 'applied_model' in entry) {
        const parts = [];
        const route = String(entry.route || '');
        if (route) parts.push(`${route} session`);
        // Last-actual evidence is APPLIED telemetry only. Older receipts may
        // retain the requested route while omitting what the harness actually
        // served; never dress that requested value up as execution truth.
        const model = String(entry.applied_model || '');
        if (model) parts.push(model);
        else if (entry.requested_model) parts.push('model not disclosed');
        const account = String(entry.applied_profile || '');
        if (account) parts.push(`account ${account}`);
        const when = formatRelativeAge(Date.parse(entry.ts || ''), 'just now');
        const processing = processingExecutionText(entry.processing);
        if (processing) parts.push(processing);
        if (when) parts.push(when);
        return parts.join(' · ');
    }
    const effective = entry.effective || entry;
    const parts = [];
    const route = String(effective.route || effective.kind || '');
    if (route.startsWith(ROUTE_KIND_AGENT_SESSION)) {
        const harness = route.slice(ROUTE_KIND_AGENT_SESSION.length).replace(/^:/, '')
            || splitSessionTarget(effective.target_id || '').harness;
        parts.push(harness ? `${harness} session` : 'agent session');
    } else if (route) {
        parts.push('API model');
    }
    if (effective.model) parts.push(String(effective.model));
    const account = effective.credential_profile_id || effective.profile_id;
    if (account) parts.push(`account ${account}`);
    if (effective.access) parts.push(`access ${effective.access}`);
    const when = formatRelativeAge(Date.parse(entry.ts || ''), 'just now');
    const processing = processingExecutionText(effective.processing || entry.processing);
    if (processing) parts.push(processing);
    if (when) parts.push(when);
    return parts.join(' · ');
}
