/*
 * Session CSRF + expiry helper for the app's same-origin fetch() calls.
 *
 * Adds the per-session CSRF token (from <meta name="csrf-token">) as the
 * X-CSRF-Token header on every state-changing same-origin request, and sends
 * the browser to the sign-in page when the session has ended. Cross-origin
 * requests are never given the token. Loaded synchronously before page scripts.
 */
(function () {
    'use strict';

    var nativeFetch = window.fetch;
    if (typeof nativeFetch !== 'function') {
        return;
    }
    var SAFE_METHODS = /^(GET|HEAD|OPTIONS|TRACE)$/i;
    var redirecting = false;

    function csrfToken() {
        var meta = document.querySelector('meta[name="csrf-token"]');
        return meta ? meta.getAttribute('content') || '' : '';
    }

    function isSameOrigin(url) {
        try {
            return new URL(url, window.location.href).origin === window.location.origin;
        } catch (error) {
            return false;
        }
    }

    function sendToSignIn() {
        if (redirecting) {
            return;
        }
        redirecting = true;
        var here = window.location.pathname + window.location.search;
        window.location.assign('/login?next=' + encodeURIComponent(here));
    }

    window.fetch = function (input, init) {
        var options = init ? Object.assign({}, init) : {};
        var isRequest = typeof Request !== 'undefined' && input instanceof Request;
        var url = isRequest ? input.url : String(input);
        var method = options.method || (isRequest ? input.method : 'GET');
        var sameOrigin = isSameOrigin(url);

        if (sameOrigin && !SAFE_METHODS.test(method)) {
            var headers = new Headers(options.headers || (isRequest ? input.headers : undefined));
            if (!headers.has('X-CSRF-Token')) {
                headers.set('X-CSRF-Token', csrfToken());
            }
            options.headers = headers;
        }

        return nativeFetch.call(window, input, options).then(function (response) {
            if (sameOrigin && response.status === 401) {
                sendToSignIn();
            }
            return response;
        });
    };
}());
