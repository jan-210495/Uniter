/* Progressive install enhancement only: no worker, cache, requests or offline writes. */
(() => {
    'use strict';
    const button = document.getElementById('install-button');
    const status = document.getElementById('install-status');
    if (!button || !status) return;
    let deferredPrompt = null;
    const display = window.matchMedia('(display-mode: standalone)');
    let installed = display.matches || navigator.standalone === true;
    const say = key => { status.textContent = status.dataset[key]; };
    const markInstalled = () => {
        installed = true;
        deferredPrompt = null;
        button.hidden = true;
        say('installed');
    };
    if (installed) markInstalled();
    display.addEventListener?.('change', () => {
        if (display.matches) markInstalled();
    });

    window.addEventListener('beforeinstallprompt', event => {
        if (installed || !window.isSecureContext) return;
        event.preventDefault();
        deferredPrompt = event;
        button.hidden = false;
    });
    button.addEventListener('click', async () => {
        if (!deferredPrompt || installed) return;
        const prompt = deferredPrompt;
        deferredPrompt = null;
        button.hidden = true;
        try {
            await prompt.prompt();
            const choice = await prompt.userChoice;
            if (!installed) say(choice.outcome === 'accepted' ? 'accepted' : 'dismissed');
        } catch (_) {
            if (!installed) say('error');
        }
    });
    window.addEventListener('appinstalled', markInstalled);
})();
