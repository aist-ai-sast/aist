import Cookies from "js-cookie";

/* Shared by server-rendered pages and the client UI's HTTP adapter. */
(function (global) {
    "use strict";

    global.aistReadCookie = function (name) {
        return Cookies.get(name) || null;
    };

    global.aistCsrfToken = function (root, preferCookie) {
        var cookie = global.aistReadCookie("csrftoken");
        if (preferCookie && cookie) return cookie;
        var scope = root || (typeof document !== "undefined" ? document : null);
        var input = scope && scope.querySelector('input[name="csrfmiddlewaretoken"]');
        if (input && input.value) return input.value;
        var meta = scope && scope.querySelector('meta[name="csrf-token"]');
        return (meta && meta.content) || cookie || "";
    };
})(globalThis);
