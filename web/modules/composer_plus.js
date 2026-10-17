// The composer's "+" menu filled from skills' contributions (docs/DESIGN.md "Composer owner controls", "Skills in the
// page"): after the host's Attach file, one section per skill under its label with its `composer.plus` items in the
// owner's arrangement, then the host's own items: "Add dictation…" while no speech engine can dictate here, once the
// voice controls have offered their chooser, and "Make a button with Ouroboros…", which starts a request in the draft for
// the owner to finish and send. Every item does its contribution's verb on this room's draft; none sends a message.
import { tr } from './i18n.js';
import { draftPort, getContributions, runContributionAction } from './contributions.js';

const dictation = { offer: null, listeners: new Set() };
const dictationChanged = () => { for (const listener of [...dictation.listeners]) listener(); };

/**
 * The voice controls' chooser behind "Add dictation…": `open({composer, draft})`, or `{open, shown()}` when the voice
 * controls decide themselves when the item shows (by default: while no speech-to-text engine is chosen). The item is
 * absent until one is set. Returns the unset.
 */
export function setDictationOffer(offer) {
    const next = typeof offer === 'function' ? { open: offer } : (typeof offer?.open === 'function' ? offer : null);
    dictation.offer = next;
    dictationChanged();
    return () => {
        if (dictation.offer !== next) return;
        dictation.offer = null;
        dictationChanged();
    };
}

/** What "Make a button with Ouroboros…" starts in the draft (the bundled templates skill teaches the rest). */
export const MAKE_A_BUTTON_TEXT = 'Make me a button in the + menu that ';

/**
 * Keep one room's `composer.plusMenu` in step with the contributions and the dictation offer. `notice(text, tone)`
 * says how a verb went (a toast when omitted). Returns the disposer.
 */
export function bindComposerPlus({ composer, store = getContributions(), notice = undefined, run = runContributionAction }) {
    const draft = draftPort(() => ({ composer }));
    const verb = (item) => () => { void run(item, { draft, notice }); };

    function sections() {
        const bySkill = new Map();
        for (const item of store.byPlacement('composer.plus')) {
            if (!bySkill.has(item.skill)) {
                bySkill.set(item.skill, { key: `skill:${item.skill}`, label: item.skill_label || item.skill, items: [] });
            }
            bySkill.get(item.skill).items.push({ key: item.key, label: item.label, title: item.title || '',
                icon: item.icon || '', onSelect: verb(item) });
        }
        const offer = dictation.offer;
        const host = [];
        if (offer && (offer.shown ? offer.shown() : !store.voice('stt')?.key)) {
            host.push({ key: 'dictation', label: tr('composer.plus.dictation', 'Add dictation…'), icon: 'mic',
                onSelect: () => offer.open({ composer, draft }) });
        }
        host.push({ key: 'make-button', label: tr('composer.plus.make_button', 'Make a button with Ouroboros…'),
            title: tr('composer.plus.make_button_title', 'Ask Ouroboros for a button of your own in this menu'),
            icon: 'sparkle',
            onSelect: () => { draft.insert(tr('composer.plus.make_button_text', MAKE_A_BUTTON_TEXT)); } });
        // The host's own items close the menu under Ouroboros's name, apart from the skills' sections above them.
        return [...bySkill.values(), { key: 'host', label: tr('composer.plus.host', 'Ouroboros'), items: host }];
    }

    const sync = () => composer.plusMenu.setSections(sections());
    const off = store.onChange(sync);
    dictation.listeners.add(sync);
    sync();
    return () => {
        off();
        dictation.listeners.delete(sync);
    };
}
