// Clean Media — in-player buttons.
//
// Injected into the web client's index.html by WebInjectionService. Adds two
// buttons to the video player control bar (administrators only):
//
//   * Flag this moment — captures a short window around the current playback
//     time as a hand-added (unapproved) `manual` finding, which then shows up
//     in the review page to confirm, retime or classify.
//   * Review this film — opens the worker's review page for what is playing.
//
// Everything uses only the public window.ApiClient, so it survives web client
// upgrades: current time comes from the <video> element (accurate, local), the
// item id from this device's active session, and calls reuse ApiClient's auth.
//
// The control bar is re-rendered several times a second while playing, which
// wipes anything added to it, so injection is idempotent and repeats on a short
// interval — the buttons cannot quietly vanish on a re-render.
(function () {
    'use strict';

    var script = document.currentScript;
    // Base URL of the CleanMedia controller, derived from this script's own
    // URL, so it is correct regardless of any Jellyfin base-path prefix.
    var base = (script && script.src ? script.src : '').replace(/PlayerScript\.js(\?.*)?$/, '');
    if (!base) { return; }

    var config = { enabled: true, padMs: 1500 };

    function api() { return window.ApiClient; }

    function boot() {
        var ac = api();
        if (!ac || typeof ac.getCurrentUser !== 'function') { setTimeout(boot, 500); return; }

        fetch(base + 'PlayerConfig')
            .then(function (r) { return r.json(); })
            .then(function (c) { if (c && typeof c === 'object') { config = c; } })
            .catch(function () { /* keep defaults if unreachable */ })
            .then(function () {
                if (!config.enabled) { return; }
                return ac.getCurrentUser().then(function (u) {
                    if (u && u.Policy && u.Policy.IsAdministrator) { watch(); }
                });
            })
            .catch(function () { /* non-admin or transient — no buttons */ });
    }

    // The OSD control bar is rebuilt constantly during playback, so re-add the
    // buttons on an interval (cheap, idempotent) and also on any DOM change for
    // a snappy first appearance.
    function watch() {
        new MutationObserver(tryInject).observe(document.body, { childList: true, subtree: true });
        setInterval(tryInject, 1000);
        tryInject();
    }

    function tryInject() {
        var bar = document.querySelector('.videoOsdBottom-maincontrols')
            || document.querySelector('.videoOsdBottom');
        if (!bar || bar.querySelector('.btnCleanMediaFlag')) { return; }

        // Land in the right-hand control cluster (subtitles/settings/fullscreen).
        var groups = bar.querySelectorAll('.buttons');
        var group = groups.length ? groups[groups.length - 1] : bar;
        group.appendChild(makeButton('flag', 'Flag this moment for Clean Media review', 'btnCleanMediaFlag', onFlag));
        group.appendChild(makeButton('rate_review', 'Open the Clean Media review page for this film', 'btnCleanMediaReview', onReview));
    }

    function makeButton(icon, title, cls, handler) {
        var btn = document.createElement('button');
        btn.setAttribute('is', 'paper-icon-button-light');
        btn.className = cls + ' autoSize paper-icon-button-light';
        btn.title = title;
        var span = document.createElement('span');
        span.className = 'material-icons';
        span.setAttribute('aria-hidden', 'true');
        span.textContent = icon;
        btn.appendChild(span);
        btn.addEventListener('click', handler);
        return btn;
    }

    var busy = false;
    function onFlag() {
        if (busy) { return; }
        var video = document.querySelector('video');
        if (!video) { toast('No video playing'); return; }

        var ms = Math.max(0, Math.round(video.currentTime * 1000));
        var pad = config.padMs || 1500;
        busy = true;

        currentItemId()
            .then(function (itemId) {
                if (!itemId) { throw new Error('could not identify the playing item'); }
                var ac = api();
                return ac.ajax({
                    type: 'POST',
                    url: ac.getUrl('CleanMedia/Segments', { itemId: itemId }),
                    contentType: 'application/json',
                    data: JSON.stringify({
                        startMs: Math.max(0, ms - pad),
                        endMs: ms + pad,
                        category: 'manual',
                        recommendedAction: 'skip',
                        reasoning: 'Flagged during playback at ' + fmt(ms)
                    })
                });
            })
            .then(function () { toast('Flagged ' + fmt(ms) + ' — review to confirm'); })
            .catch(function (e) { toast('Flag failed: ' + (e && e.message ? e.message : 'error')); })
            .then(function () { busy = false; });
    }

    function onReview() {
        // Open the tab synchronously so popup blockers allow it, then point it
        // at the review URL once the server resolves the item to a worker URL.
        var win = window.open('', '_blank');
        currentItemId()
            .then(function (itemId) {
                if (!itemId) { throw new Error('could not identify the playing item'); }
                var ac = api();
                return ac.ajax({ type: 'GET', url: ac.getUrl('CleanMedia/ReviewUrl', { itemId: itemId }), dataType: 'json' });
            })
            .then(function (res) {
                if (!res || !res.url) { throw new Error('worker URL not set in Clean Media settings'); }
                if (win) { win.location = res.url; } else { window.location = res.url; }
            })
            .catch(function (e) {
                if (win) { win.close(); }
                toast('Review failed: ' + (e && e.message ? e.message : 'error'));
            });
    }

    // The active session for this device carries the now-playing item id — a
    // purely public-API way to learn what is on screen.
    function currentItemId() {
        var ac = api();
        try {
            return ac.getSessions().then(function (sessions) {
                var dev = ac.deviceId();
                for (var i = 0; i < sessions.length; i++) {
                    var s = sessions[i];
                    if (s.DeviceId === dev && s.NowPlayingItem) { return s.NowPlayingItem.Id; }
                }
                return null;
            });
        } catch (e) {
            return Promise.resolve(null);
        }
    }

    function fmt(ms) {
        var t = Math.floor(ms / 1000);
        var h = Math.floor(t / 3600), m = Math.floor((t % 3600) / 60), s = t % 60;
        function p(n) { return (n < 10 ? '0' : '') + n; }
        return (h > 0 ? h + ':' : '') + p(m) + ':' + p(s);
    }

    function toast(msg) {
        var el = document.createElement('div');
        el.textContent = msg;
        el.style.cssText = 'position:fixed;bottom:12%;left:50%;transform:translateX(-50%);' +
            'background:rgba(0,0,0,0.85);color:#fff;padding:10px 18px;border-radius:6px;' +
            'font-size:15px;z-index:100000;pointer-events:none;transition:opacity .4s;opacity:1;';
        document.body.appendChild(el);
        setTimeout(function () { el.style.opacity = '0'; }, 2200);
        setTimeout(function () { if (el.parentNode) { el.parentNode.removeChild(el); } }, 2700);
    }

    boot();
})();
