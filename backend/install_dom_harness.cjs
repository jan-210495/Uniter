'use strict';
const assert = require('node:assert/strict');
const vm = require('node:vm');
const input = JSON.parse(require('node:fs').readFileSync(0, 'utf8'));
async function run() {
    const listeners = {};
    const elements = input.elements;
    for (const el of Object.values(elements)) {
        el.addEventListener = (type, handler) => { el[type] = handler; };
    }
    const context = {
        document: {getElementById: id => elements[id] || null},
        window: {isSecureContext: true, matchMedia: () => ({matches: false}),
            addEventListener: (type, handler) => { listeners[type] = handler; }},
        navigator: {},
    };
    vm.runInNewContext(input.script, context);
    const button = elements['install-button'];
    const status = elements['install-status'];
    assert.equal(button.hidden, true, 'unsupported browser has no inert control');
    let prompted = 0;
    let prevented = 0;
    listeners.beforeinstallprompt({preventDefault() { prevented++; },
        prompt() { prompted++; }, userChoice: Promise.resolve({outcome: 'accepted'})});
    assert.equal(prevented, 1);
    assert.equal(button.hidden, false);
    assert.equal(prompted, 0, 'never prompt on load/event');
    await button.click();
    assert.equal(prompted, 1);
    assert.equal(button.hidden, true);
    assert.equal(status.textContent, status.dataset.accepted);
    await button.click();
    assert.equal(prompted, 1, 'a deferred event is single-use');
    listeners.appinstalled();
    assert.equal(button.hidden, true);
    assert.equal(status.textContent, status.dataset.installed);
    // Each scenario runs the actual served script in a fresh page context.
    function page(options = {}) {
        const handlers = {};
        const button = {hidden: true, addEventListener(type, fn) { this[type] = fn; }};
        const status = {dataset: elements['install-status'].dataset, textContent: ''};
        const display = {matches: !!options.standalone,
            addEventListener(type, fn) { this[type] = fn; }};
        const window = {isSecureContext: !options.insecure, matchMedia: () => display,
            addEventListener(type, fn) { handlers[type] = fn; }};
        vm.runInNewContext(input.script, {window,
            document: {getElementById: id => options.noUI ? null : id === 'install-button' ? button : status},
            navigator: {standalone: !!options.iosStandalone}});
        return {handlers, button, status, display};
    }
    for (const options of [{standalone: true}, {iosStandalone: true}]) {
        const p = page(options);
        assert.equal(p.status.textContent, p.status.dataset.installed);
        p.handlers.beforeinstallprompt?.({preventDefault() {}, prompt() { throw Error('installed'); }});
        assert.equal(p.button.hidden, true);
    }
    const insecure = page({insecure: true});
    insecure.handlers.beforeinstallprompt?.({preventDefault() {}, prompt() { throw Error('insecure'); }});
    assert.equal(insecure.button.hidden, true);
    const missing = page({noUI: true});
    assert.equal(missing.handlers.beforeinstallprompt, undefined);
    const dismissed = page();
    dismissed.handlers.beforeinstallprompt({preventDefault() {}, prompt() {}, userChoice: Promise.resolve({outcome: 'dismissed'})});
    await dismissed.button.click();
    assert.equal(dismissed.status.textContent, dismissed.status.dataset.dismissed);
    assert.equal(dismissed.button.hidden, true);
    for (const prompt of [() => { throw Error('blocked'); }, () => Promise.reject(Error('rejected'))]) {
        const p = page();
        p.handlers.beforeinstallprompt({preventDefault() {}, prompt});
        await p.button.click();
        assert.equal(p.status.textContent, p.status.dataset.error);
        assert.equal(p.button.hidden, true);
    }
    const race = page();
    let choose;
    let count = 0;
    race.handlers.beforeinstallprompt({preventDefault() {}, prompt() { count++; },
        userChoice: new Promise(resolve => { choose = resolve; })});
    const pending = race.button.click();
    await race.button.click();
    assert.equal(count, 1, 'rapid clicks cannot prompt twice');
    race.handlers.appinstalled();
    choose({outcome: 'dismissed'});
    await pending;
    assert.equal(race.status.textContent, race.status.dataset.installed);
    race.handlers.beforeinstallprompt({preventDefault() {}, prompt() { throw Error('already installed'); }});
    assert.equal(race.button.hidden, true);
    const changed = page();
    changed.handlers.beforeinstallprompt({preventDefault() {}, prompt() { throw Error('standalone'); }});
    changed.display.matches = true;
    changed.display.change();
    assert.equal(changed.button.hidden, true);
    assert.equal(changed.status.textContent, changed.status.dataset.installed);
    console.log('install harness passed');
}
run().catch(error => { console.error(error); process.exitCode = 1; });
