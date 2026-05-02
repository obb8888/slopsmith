(function () {
    'use strict';

    // keyed by pluginId
    const _tourPlugins = {};   // { id, name, has_screen } for tour-eligible plugins
    const _registry = {};      // registered imperative overrides
    let _activeTour = null;    // currently running Shepherd Tour instance
    let _activeTourPluginId = null;

    // ── localStorage helpers ──────────────────────────────────────────────

    function _seenKey(id)      { return 'slopsmith_tour_seen_' + id; }
    function _dismissedKey(id) { return 'slopsmith_tour_dismissed_' + id; }

    function hasSeen(pluginId) {
        try { return !!localStorage.getItem(_seenKey(pluginId)); } catch { return false; }
    }
    function hasDismissed(pluginId) {
        try { return !!localStorage.getItem(_dismissedKey(pluginId)); } catch { return false; }
    }

    function _markSeen(pluginId)      { try { localStorage.setItem(_seenKey(pluginId), '1'); } catch { /* private mode / quota */ } }
    function _markDismissed(pluginId) { try { localStorage.setItem(_dismissedKey(pluginId), '1'); } catch { /* private mode / quota */ } }

    // ── Trigger injection ─────────────────────────────────────────────────

    function _findTriggerByPluginId(container, pluginId) {
        const triggers = container.querySelectorAll('.slopsmith-tour-trigger');
        for (const trigger of triggers) {
            if (trigger.dataset.pluginId === pluginId) return trigger;
        }
        return null;
    }

    // opts.bottom = true  →  button anchored to bottom-right (for #player where
    //                         the top is overlapped by the transparent nav bar)
    // containerOrSelector may be a DOM Element or a CSS selector string.
    function injectTrigger(pluginId, containerOrSelector, opts) {
        opts = opts || {};
        let container;
        if (containerOrSelector instanceof Element) {
            container = containerOrSelector;
        } else {
            try {
                container = document.querySelector(containerOrSelector);
            } catch (e) {
                console.warn('[slopsmithTour] injectTrigger: invalid selector', containerOrSelector, e);
                return;
            }
        }
        if (!container) return;
        if (_findTriggerByPluginId(container, pluginId)) return;

        // Ensure the container establishes a positioning context so the
        // absolutely-positioned trigger button anchors to it, not the page.
        if (window.getComputedStyle(container).position === 'static') {
            container.style.position = 'relative';
        }

        const btn = document.createElement('button');
        btn.className = 'slopsmith-tour-trigger' +
            ((hasSeen(pluginId) || hasDismissed(pluginId)) ? '' : ' first-visit') +
            (opts.bottom ? ' position-bottom' : '');
        btn.dataset.pluginId = pluginId;
        btn.title = 'Take a tour of this plugin';
        btn.setAttribute('aria-label', 'Take a tour of this plugin');
        btn.textContent = '?';
        btn.addEventListener('click', () => {
            _removePrompt(pluginId, container);
            start(pluginId);
        });
        container.appendChild(btn);

        if (!hasSeen(pluginId) && !hasDismissed(pluginId)) {
            _showPrompt(pluginId, container, btn, opts);
        }
    }

    function _removePrompt(pluginId, container) {
        for (const el of container.querySelectorAll('.slopsmith-tour-prompt')) {
            if (el.dataset.pluginId === pluginId) { el.remove(); break; }
        }
    }

    function _showPrompt(pluginId, container, triggerBtn, opts) {
        opts = opts || {};
        const pluginName = _tourPlugins[pluginId] ? _tourPlugins[pluginId].name : pluginId;

        const prompt = document.createElement('div');
        prompt.className = 'slopsmith-tour-prompt' + (opts.bottom ? ' position-bottom' : '');
        prompt.dataset.pluginId = pluginId;
        const promptText = document.createElement('span');
        promptText.textContent = 'Take a quick tour of ';
        const bold = document.createElement('b');
        bold.textContent = pluginName;
        const promptSuffix = document.createTextNode('?');
        prompt.appendChild(promptText);
        prompt.appendChild(bold);
        prompt.appendChild(promptSuffix);

        const btns = document.createElement('div');
        btns.className = 'tour-prompt-buttons';

        const yesBtn = document.createElement('button');
        yesBtn.dataset.action = 'start';
        yesBtn.textContent = 'Yes';
        yesBtn.addEventListener('click', async () => {
            prompt.remove();
            const started = await start(pluginId);
            if (started === true) {
                _markDismissed(pluginId);
            }
        });

        const noBtn = document.createElement('button');
        noBtn.dataset.action = 'dismiss';
        noBtn.textContent = 'Not now';
        noBtn.addEventListener('click', () => {
            _markDismissed(pluginId);
            prompt.classList.add('fading');
            setTimeout(() => prompt.remove(), 500);
            if (triggerBtn) triggerBtn.classList.remove('first-visit');
        });

        btns.appendChild(yesBtn);
        btns.appendChild(noBtn);
        prompt.appendChild(btns);
        container.appendChild(prompt);

        // Auto-dismiss after 8 s
        const timer = setTimeout(() => {
            _markDismissed(pluginId);
            prompt.classList.add('fading');
            setTimeout(() => prompt.remove(), 500);
            if (triggerBtn) triggerBtn.classList.remove('first-visit');
        }, 8000);

        const obs = new MutationObserver(() => {
            if (!document.contains(prompt)) { clearTimeout(timer); obs.disconnect(); }
        });
        obs.observe(document.body, { childList: true, subtree: true });
    }

    // ── Step loading ──────────────────────────────────────────────────────

    async function _loadSteps(pluginId) {
        if (_registry[pluginId] && typeof _registry[pluginId].buildSteps === 'function') {
            try {
                const steps = await _registry[pluginId].buildSteps();
                if (steps && steps.length) return steps;
            } catch (e) {
                console.warn('[slopsmithTour] buildSteps() threw for', pluginId, e);
            }
        }
        try {
            const resp = await fetch('/api/plugins/' + encodeURIComponent(pluginId) + '/tour.json');
            if (!resp.ok) return [];
            const data = await resp.json();
            return Array.isArray(data.tour) ? data.tour : [];
        } catch (e) {
            console.warn('[slopsmithTour] Failed to load steps for', pluginId, e);
            return [];
        }
    }

    // ── Shepherd step mapping ─────────────────────────────────────────────

    function _mapSteps(rawSteps, tourInstance) {
        return rawSteps.map(raw => {
            // Pass title as a plain string (Shepherd renders it with innerHTML).
            // Keep text as a DOM node so tour.json content is never executed as HTML.
            const textEl = document.createElement('p');
            textEl.textContent = raw.content || '';

            const opts = {
                id: raw.id,
                title: esc(raw.title || ''),
                text: textEl,
                buttons: [
                    { text: 'Back', action: tourInstance.back.bind(tourInstance), secondary: true },
                    { text: 'Next', action: tourInstance.next.bind(tourInstance) },
                ],
                cancelIcon: { enabled: true },
            };

            if (raw.selector) {
                opts.attachTo = { element: raw.selector, on: raw.position || 'bottom' };
            }

            if (raw.shape === 'label') {
                opts.arrow = false;
            }

            if (raw.advance === 'click-target' && raw.selector) {
                opts.advanceOn = { selector: raw.selector, event: 'click' };
                opts.buttons = opts.buttons.filter(b => b.text !== 'Next');
                opts.buttons.push({ text: 'Skip', action: tourInstance.next.bind(tourInstance), secondary: true });
            }

            if (raw === rawSteps[0]) {
                opts.buttons = opts.buttons.filter(b => b.text !== 'Back');
            }
            if (raw === rawSteps[rawSteps.length - 1]) {
                opts.buttons = opts.buttons.map(b =>
                    (b.text === 'Next' || b.text === 'Skip')
                        ? { text: 'Done', action: tourInstance.complete.bind(tourInstance) }
                        : b
                );
            }

            return opts;
        });
    }

    // ── Start / cancel tour ───────────────────────────────────────────────

    async function start(pluginId) {
        if (typeof window.Shepherd === 'undefined' || !window.Shepherd.Tour) {
            console.error('[slopsmithTour] Shepherd.js not loaded — cannot start tour for', pluginId);
            return false;
        }
        if (_activeTour) {
            _activeTour.cancel();
            _activeTour = null;
        }

        const rawSteps = await _loadSteps(pluginId);
        if (!rawSteps.length) {
            console.warn('[slopsmithTour] No steps found for', pluginId);
            return false;
        }

        const hasSpotlight = rawSteps.some(s => s.shape === 'spotlight');

        const tour = new Shepherd.Tour({
            useModalOverlay: hasSpotlight,
            defaultStepOptions: {
                scrollTo: { behavior: 'smooth', block: 'center' },
                modalOverlayOpeningPadding: 8,
                modalOverlayOpeningRadius: 6,
            },
        });

        const mappedSteps = _mapSteps(rawSteps, tour);
        mappedSteps.forEach(s => tour.addStep(s));

        tour.on('complete', () => {
            _markSeen(pluginId);
            _registry[pluginId]?.onComplete?.();
            _activeTour = null;
            _activeTourPluginId = null;
            document.querySelectorAll('.slopsmith-tour-trigger')
                .forEach(b => { if (b.dataset.pluginId === pluginId) b.classList.remove('first-visit'); });
        });
        tour.on('cancel', () => {
            _markSeen(pluginId);
            _activeTour = null;
            _activeTourPluginId = null;
            document.querySelectorAll('.slopsmith-tour-trigger')
                .forEach(b => { if (b.dataset.pluginId === pluginId) b.classList.remove('first-visit'); });
        });

        _activeTour = tour;
        _activeTourPluginId = pluginId;
        _registry[pluginId]?.onStart?.();
        tour.start();
        return true;
    }

    // ── screen:changed handler ────────────────────────────────────────────

    function _onScreenChanged(ev) {
        const screenId = ev.detail && ev.detail.id;
        if (!screenId) return;

        // Cancel active tour if user navigated away
        if (_activeTour && _activeTourPluginId) {
            const p = _tourPlugins[_activeTourPluginId];
            const expectedScreen = p && !p.has_screen ? 'player' : ('plugin-' + _activeTourPluginId);
            if (screenId !== expectedScreen) {
                _activeTour.cancel();
                _activeTour = null;
                _activeTourPluginId = null;
            }
        }

        // Auto-inject for plugins with dedicated screen divs
        if (screenId.startsWith('plugin-')) {
            const pluginId = screenId.slice(7);
            if (_tourPlugins[pluginId]) {
                injectTrigger(pluginId, document.getElementById(screenId));
            }
        }

        // Auto-inject for viz plugins (no dedicated screen) when player activates.
        // Only inject for the currently selected viz plugin — injecting for all
        // tour-enabled viz plugins would stack multiple absolutely-positioned
        // buttons at the same coordinates when more than one is installed.
        if (screenId === 'player') {
            _injectPlayerVizTrigger();
        }
    }

    function _currentVizPluginId() {
        // Returns the viz plugin ID that is currently active.
        // For an explicit pick (not 'auto' or 'default'), returns it directly.
        // For 'auto' mode, evaluates each tour-enabled viz plugin's
        // matchesArrangement() predicate — mirroring _autoMatchViz() in app.js —
        // and returns the first match, or null when nothing matches.
        let sel = null;
        try { sel = localStorage.getItem('vizSelection'); } catch { /* private mode */ }
        if (!sel) {
            const picker = document.getElementById('viz-picker');
            if (picker) sel = picker.value;
        }
        if (sel && sel !== 'auto' && sel !== 'default') return sel;

        if (sel === 'auto') {
            const songInfo = (typeof highway !== 'undefined' && typeof highway.getSongInfo === 'function')
                ? (highway.getSongInfo() || {}) : {};
            // Mirror _autoMatchViz() in app.js: iterate #viz-picker options in DOM
            // order so the first match is the same plugin the picker would activate.
            const picker = document.getElementById('viz-picker');
            const candidateIds = picker
                ? Array.from(picker.options).map(o => o.value).filter(v => v !== 'auto' && v !== 'default')
                : Object.keys(_tourPlugins).filter(id => !_tourPlugins[id].has_screen);
            for (const pluginId of candidateIds) {
                if (!_tourPlugins[pluginId]) continue; // not a tour-enabled plugin
                const p = _tourPlugins[pluginId];
                if (p.has_screen) continue; // dedicated-screen plugins aren't viz plugins
                const factory = window['slopsmithViz_' + pluginId];
                if (typeof factory !== 'function') continue;
                const predicate = factory.matchesArrangement;
                if (typeof predicate !== 'function') continue;
                try { if (predicate(songInfo)) return pluginId; } catch { /* ignore */ }
            }
        }
        return null;
    }

    function _injectPlayerVizTrigger() {
        const player = document.getElementById('player');
        if (!player) return;
        const vizId = _currentVizPluginId();

        // Remove any player viz triggers that don't match the newly active plugin
        // (handles auto-mode switching between songs).
        player.querySelectorAll('.slopsmith-tour-trigger').forEach(btn => {
            if (btn.dataset.pluginId !== vizId) {
                for (const el of player.querySelectorAll('.slopsmith-tour-prompt')) {
                    if (el.dataset.pluginId === btn.dataset.pluginId) { el.remove(); break; }
                }
                btn.remove();
            }
        });

        if (!vizId) return;
        const p = _tourPlugins[vizId];
        if (p && !p.has_screen) {
            injectTrigger(p.id, '#player', { bottom: true });
        }
    }

    // ── Public API ────────────────────────────────────────────────────────

    function register(pluginId, opts) {
        opts = opts || {};
        _registry[pluginId] = {
            buildSteps: opts.buildSteps || null,
            onStart: opts.onStart || null,
            onComplete: opts.onComplete || null,
        };
        // injectTriggerInto is now optional — the engine handles viz plugins
        // autonomously via screen:changed. This hook remains for plugins that
        // want a custom container outside the normal flow.
        if (opts.injectTriggerInto && opts.injectTriggerInto !== '#player') {
            requestAnimationFrame(() => injectTrigger(pluginId, opts.injectTriggerInto));
        }
    }

    function reset(pluginId) {
        try {
            if (pluginId) {
                localStorage.removeItem(_seenKey(pluginId));
                localStorage.removeItem(_dismissedKey(pluginId));
            } else {
                // Collect keys first (localStorage.key(i) is the portable API),
                // then remove — avoids mutation during enumeration.
                const toRemove = [];
                for (let i = 0; i < localStorage.length; i++) {
                    const k = localStorage.key(i);
                    if (k && k.startsWith('slopsmith_tour_')) toRemove.push(k);
                }
                toRemove.forEach(k => localStorage.removeItem(k));
            }
        } catch { /* private mode — ignore */ }
    }

    window.slopsmithTour = { register, start, hasSeen, hasDismissed, reset };

    // ── Initialise after DOM ──────────────────────────────────────────────

    document.addEventListener('DOMContentLoaded', async () => {
        if (!window.Shepherd) {
            console.error('[slopsmithTour] Shepherd.js not loaded — tour engine disabled');
            return;
        }
        if (!window.slopsmith) {
            console.error('[slopsmithTour] window.slopsmith not found — tour engine disabled');
            return;
        }

        try {
            const resp = await fetch('/api/plugins');
            if (!resp.ok) return;
            const plugins = await resp.json();
            plugins.filter(p => p.has_tour).forEach(p => {
                _tourPlugins[p.id] = { id: p.id, name: p.name, has_screen: p.has_screen };
            });
        } catch (e) {
            console.warn('[slopsmithTour] Failed to load plugin list:', e);
            return;
        }

        window.slopsmith.on('screen:changed', _onScreenChanged);

        // In auto-viz mode, matchesArrangement() is only meaningful once a song
        // is loaded (getSongInfo() returns {}  before that). Re-run trigger
        // injection on song:ready so the ? button appears for the auto-matched viz.
        window.slopsmith.on('song:ready', () => {
            if (document.getElementById('player')?.classList.contains('active')) {
                _injectPlayerVizTrigger();
            }
        });

        // If the player screen is already active on load (e.g., deep-link), inject now
        if (document.getElementById('player')?.classList.contains('active')) {
            _injectPlayerVizTrigger();
        }
    });
})();
