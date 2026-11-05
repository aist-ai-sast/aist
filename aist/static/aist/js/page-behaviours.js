/**
 * Behaviour a page asks for with attributes on its markup, so a template carries no script of its own for it.
 *
 * - `x-data="aistElapsed(seconds)"` (Alpine) counts a run time up each second from the server-rendered value,
 *   in the `HH:MM:SS` form the `pipeline_duration` template tag renders. An element a refresh replaces starts
 *   again from its new value, so the count follows the server's clock; inside a `data-stale` region it stops.
 * - `[data-live-refresh]` on an htmx-refreshed element: a refresh the server refused (401, 403, 404) or
 *   redirected to another page reloads the page, so the user sees what the server answers instead; any other
 *   failed refresh marks the element `data-stale` and dims it, with a hint, until a refresh replaces it.
 * - `tr[data-href]` opens its link on a click anywhere in the row except its own controls.
 */
(function (global) {
    "use strict";

    var TICK_MS = 1000;
    var RELOAD_ON_STATUS = [401, 403, 404];
    var STALE_OPACITY = "0.6";
    var STALE_HINT = "Could not refresh; this may be out of date.";

    function formatElapsed(totalSeconds) {
        var h = Math.floor(totalSeconds / 3600);
        var m = Math.floor((totalSeconds % 3600) / 60);
        var s = totalSeconds % 60;
        return [h, m, s].map(function (n) { return String(n).padStart(2, "0"); }).join(":");
    }

    document.addEventListener("alpine:init", function () {
        global.Alpine.data("aistElapsed", function (seconds) {
            return {
                text: formatElapsed(seconds),
                timer: null,
                init: function () {
                    var component = this;
                    var shownAt = global.performance.now();
                    component.timer = global.setInterval(function () {
                        if (component.$el.closest("[data-stale]")) {
                            component.destroy();
                            return;
                        }
                        var counted = Math.floor((global.performance.now() - shownAt) / TICK_MS);
                        component.text = formatElapsed(seconds + counted);
                    }, TICK_MS);
                },
                destroy: function () {
                    global.clearInterval(this.timer);
                },
            };
        });
    });

    function isLiveRefresh(event) {
        return event.detail.elt && event.detail.elt.hasAttribute("data-live-refresh");
    }

    document.addEventListener("htmx:beforeSwap", function (event) {
        var xhr = event.detail.xhr;
        if (!isLiveRefresh(event) || !xhr.responseURL) {
            return;
        }
        if (new URL(xhr.responseURL).pathname !== global.location.pathname) {
            event.detail.shouldSwap = false;
            global.location.reload();
        }
    });

    function refreshFailed(event) {
        if (!isLiveRefresh(event)) {
            return;
        }
        var status = event.detail.xhr ? event.detail.xhr.status : 0;
        if (RELOAD_ON_STATUS.indexOf(status) !== -1) {
            global.location.reload();
            return;
        }
        var region = event.detail.elt;
        region.setAttribute("data-stale", "");
        region.style.opacity = STALE_OPACITY;
        region.title = STALE_HINT;
    }

    document.addEventListener("htmx:responseError", refreshFailed);
    document.addEventListener("htmx:sendError", refreshFailed);

    document.addEventListener("click", function (event) {
        var row = event.target.closest("tr[data-href]");
        if (!row || event.target.closest("a, button, input, select, textarea")) {
            return;
        }
        global.location.href = row.dataset.href;
    });
})(window);
