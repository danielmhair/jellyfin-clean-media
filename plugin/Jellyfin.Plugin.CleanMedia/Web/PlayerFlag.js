// Clean Media — "flag this moment" button in the Jellyfin video player.
//
// Injected into the web client's index.html by WebInjectionService. Adds a
// flag button to the playback control bar. One press captures a short window
// around the current playback time as a hand-added (unapproved) finding, which
// then shows up in the Clean Media review page to confirm, retime or classify.
//
// Everything here uses only the public window.ApiClient, so it survives web
// client upgrades: current time comes from the <video> element (accurate,
// local), the item id from this device's active session, and the POST reuses
// ApiClient's own auth. The button is only added for administrators, because
// creating a finding requires elevation on the worker-facing controller.
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
            .catch(function () { /* non-admin or transient — no button */ });
    }

    // The OSD is rebuilt every time playback (re)starts, so re-inject on any
    // DOM change. tryInject is idempotent per control bar.
    function watch() {
        new MutationObserver(tryInject).observe(document.body, { childList: true, subtree: true });
        tryInject();
    }

    function tryInject() {
        var bars = document.querySelectorAll('.videoOsdBottom');
        for (var i = 0; i < bars.length; i++) {
            var bar = bars[i];
            if (bar.querySelector('.btnCleanMediaFlag')) { continue; }
            (bar.querySelector('.buttons') || bar).appendChild(makeButton());
        }
    }

    function makeButton() {
        var btn = document.createElement('button');
        btn.setAttribute('is', 'paper-icon-button-light');
        btn.className = 'btnCleanMediaFlag autoSize paper-icon-button-light';
        btn.title = 'Flag this moment for Clean Media review';
        var icon = document.createElement('span');
        icon.className = 'material-icons';
        icon.setAttribute('aria-hidden', 'true');
        icon.textContent = 'flag';
        btn.appendChild(icon);
        btn.addEventListener('click', onFlag);
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
