/* Shared sidebar / modal mobile navigation. No page/business state lives here. */
(() => {
    'use strict';
    const drawer = document.getElementById('app-drawer');
    const toggle = document.getElementById('nav-toggle');
    const close = document.getElementById('app-drawer-close');
    const backdrop = document.getElementById('app-drawer-backdrop');
    const desktop = window.matchMedia('(min-width: 1024px)');
    const background = [document.getElementById('app-header'), document.querySelector('.shell-content'), document.querySelector('.skip-link')];
    let open = false;
    let previousFocus = null;
    let focusInDrawer = false;
    const focusable = () => [...drawer.querySelectorAll('a[href], button, input')]
        .filter(el => !el.disabled && el.getClientRects().length && getComputedStyle(el).visibility !== 'hidden');

    function setOpen(value, restore = true) {
        open = Boolean(value) && !desktop.matches;
        drawer.classList.toggle('drawer-closed', !open);
        backdrop.classList.toggle('hidden', !open);
        toggle.setAttribute('aria-expanded', String(open));
        document.body.classList.toggle('navigation-open', open);
        background.forEach(el => { el.inert = open; });
        // Modal semantics only belong to the mobile overlay, not desktop nav.
        drawer.setAttribute('role', open ? 'dialog' : 'navigation');
        if (open) {
            drawer.setAttribute('aria-modal', 'true');
            previousFocus = document.activeElement;
            close.focus();
        } else {
            drawer.removeAttribute('aria-modal');
            document.dispatchEvent(new Event('navigation:closed'));
            if (restore && previousFocus && previousFocus.isConnected) previousFocus.focus();
            previousFocus = null;
        }
    }
    toggle.addEventListener('click', () => setOpen(!open));
    close.addEventListener('click', () => setOpen(false));
    backdrop.addEventListener('click', () => setOpen(false));
    drawer.querySelectorAll('a').forEach(link => link.addEventListener('click', () => setOpen(false)));
    document.addEventListener('keydown', event => {
        if (!open) return;
        if (event.key === 'Escape') {
            event.preventDefault();
            setOpen(false);
        } else if (event.key === 'Tab') {
            const items = focusable();
            const first = items[0], last = items[items.length - 1];
            if (event.shiftKey && (document.activeElement === first || !drawer.contains(document.activeElement))) {
                event.preventDefault(); last.focus();
            } else if (!event.shiftKey && (document.activeElement === last || !drawer.contains(document.activeElement))) {
                event.preventDefault(); first.focus();
            }
        }
    });
    document.addEventListener('focusin', event => {
        focusInDrawer = drawer.contains(event.target);
        if (open && !drawer.contains(event.target)) close.focus();
    });
    desktop.addEventListener('change', () => {
        const wasInside = open || focusInDrawer || drawer.contains(document.activeElement);
        setOpen(false, false);
        if (wasInside) {
            if (desktop.matches) drawer.querySelector('[aria-current="page"], a').focus();
            else toggle.focus();
        }
    });
    window.uniterNavigation = { setOpen };
})();
