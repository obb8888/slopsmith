// Verify static/app.js emits `loop:restart` exactly once when the A-B
// loop wraps, with the documented payload shape. Plugins (notedetect's
// drill-mode score capture) consume this contract.
//
// The test does not load the full app.js into a DOM — it extracts just
// the `startCountIn` function source via brace-matching and evaluates it
// in a vm sandbox with stubbed dependencies. This trades coverage of the
// surrounding script for isolation: a failure here points at the wrap
// path, not at unrelated DOM coupling.

const { test } = require('node:test');
const assert = require('node:assert/strict');
const fs = require('node:fs');
const path = require('node:path');
const vm = require('node:vm');

const APP_JS = path.join(__dirname, '..', '..', 'static', 'app.js');

// Pull the source of `async function startCountIn() { ... }` by finding
// the declaration and brace-matching to the closing brace. Brittle by
// design: if the function gets renamed or restructured, the test fails
// loudly with "function not found" rather than passing on stale code.
function extractFunction(src, signature) {
    const start = src.indexOf(signature);
    if (start === -1) throw new Error(`extractFunction: '${signature}' not found in app.js`);
    const openBrace = src.indexOf('{', start);
    let depth = 1;
    let i = openBrace + 1;
    while (i < src.length && depth > 0) {
        const ch = src[i];
        if (ch === '{') depth++;
        else if (ch === '}') depth--;
        i++;
    }
    if (depth !== 0) throw new Error(`extractFunction: unbalanced braces after '${signature}'`);
    return src.slice(start, i);
}

function buildSandbox() {
    const emitCalls = [];
    const sandbox = {
        // Globals the function reads/writes via closure. Declared as `var`
        // in the eval prelude so they attach to the sandbox.
        loopA: 10,
        loopB: 20,
        _countingIn: false,
        isPlaying: false,
        lastAudioTime: 0,

        // Browser-ish globals.
        performance: { now: () => Date.now() },
        // requestAnimationFrame: skip to t >= 1 in one tick so the rewind
        // animation completes synchronously and we reach the `_audioSeek`
        // continuation immediately.
        requestAnimationFrame(fn) {
            // Fire with `now` far enough in the future that
            // (now - rewindStart) / rewindDuration >= 1.
            queueMicrotask(() => fn(Date.now() + 10_000));
        },
        // setTimeout: swallow. beginCount schedules ticks via setTimeout;
        // we don't need them to fire — the emit happens before beginCount.
        setTimeout: () => 0,

        // Stubbed slopsmith DOM dependencies.
        audio: { pause() {} },
        jucePlayer: { pause: () => Promise.resolve(), play: () => Promise.resolve(true) },
        highway: { setTime() {}, getBPM: () => 120 },

        // Stubbed app.js helpers.
        _audioSeek: () => Promise.resolve(),
        playClick: () => {},
        showCountOverlay: () => {},
        hideCountOverlay: () => {},

        // Stubbed DOM access. Anything querying for a button just gets a
        // permissive object that ignores writes.
        document: {
            getElementById: () => ({
                textContent: '',
                className: '',
                classList: { add() {}, remove() {}, toggle() {} },
            }),
        },

        // Spy: records every emit call so the test can assert.
        window: {
            slopsmith: {
                emit(event, detail) { emitCalls.push({ event, detail }); },
                isPlaying: false,
            },
            _juceMode: false,
        },

        // Capture for assertions.
        __emitCalls: emitCalls,
        queueMicrotask,
    };
    vm.createContext(sandbox);
    return sandbox;
}

test('loop:restart fires once when wrap path runs', async () => {
    const src = fs.readFileSync(APP_JS, 'utf8');
    const startCountInSrc = extractFunction(src, 'async function startCountIn()');

    // Sanity check: the change under test is present at all. Catches
    // accidental revert before we even run the behavior assertion.
    assert.match(
        startCountInSrc,
        /window\.slopsmith\.emit\(\s*['"]loop:restart['"]/,
        'startCountIn is missing the loop:restart emit',
    );

    const sandbox = buildSandbox();
    // Re-declare the closure-scoped lets as vars so the function can read
    // them from the sandbox global, then define the function in-context.
    const prelude = `
        var loopA = ${sandbox.loopA};
        var loopB = ${sandbox.loopB};
        var _countingIn = false;
        var isPlaying = false;
        var lastAudioTime = 0;
        ${startCountInSrc}
        globalThis.__startCountIn = startCountIn;
    `;
    vm.runInContext(prelude, sandbox);

    await sandbox.__startCountIn();
    // Allow the queued requestAnimationFrame microtask + the _audioSeek
    // promise chain to settle. Two awaits is enough: rAF microtask -> rewind
    // completion -> _audioSeek().then() -> emit.
    await new Promise((r) => setImmediate(r));
    await new Promise((r) => setImmediate(r));

    const restarts = sandbox.__emitCalls.filter((c) => c.event === 'loop:restart');
    assert.equal(restarts.length, 1, `expected 1 loop:restart emit, got ${restarts.length}`);
    // Field-wise assertion: deepStrictEqual fails across vm-context object
    // realms because Object.prototype identities differ even when contents
    // match. Compare values, not prototype graphs.
    const detail = restarts[0].detail;
    assert.equal(detail.loopA, 10);
    assert.equal(detail.loopB, 20);
    assert.equal(detail.time, 10);
    assert.equal(Object.keys(detail).length, 3, `unexpected extra keys in detail: ${Object.keys(detail)}`);
});

test('loop:restart fires after highway.setTime(loopA), before beginCount', () => {
    // Source-order assertion: in the change under test, the emit must sit
    // between the chartTime reset and beginCount() so plugins capture the
    // wrap at the same moment chartTime jumps back, not after the count-in.
    const src = fs.readFileSync(APP_JS, 'utf8');
    const fn = extractFunction(src, 'async function startCountIn()');
    const setTimeIdx = fn.indexOf('highway.setTime(loopA)');
    const emitIdx = fn.search(/window\.slopsmith\.emit\(\s*['"]loop:restart['"]/);
    // Match the *call* `beginCount(...)`, not the inner `function beginCount()`
    // declaration that's hoisted alongside it inside startCountIn.
    const beginCallMatch = fn.match(/(?<!function\s)\bbeginCount\s*\(/);
    const beginCallIdx = beginCallMatch ? beginCallMatch.index : -1;
    assert.ok(setTimeIdx !== -1, 'highway.setTime(loopA) not found');
    assert.ok(emitIdx !== -1, 'loop:restart emit not found');
    assert.ok(beginCallIdx !== -1, 'beginCount() call not found');
    assert.ok(setTimeIdx < emitIdx, 'emit must come after highway.setTime(loopA)');
    assert.ok(emitIdx < beginCallIdx, 'emit must come before beginCount()');
});
