/**
 * Drives the platform's component classes that it styles but no longer scripts.
 *
 * DefectDojo 3.x dropped the Bootstrap bundle while keeping the CSS contract for tabs
 * (.nav-tabs / .tab-pane.active) and collapsible panels (.collapse / .collapse.in). Modals
 * moved to the native <dialog>, which needs nothing; these two still need the class moved.
 *
 * Alpine ships with the platform and drives its own markup, but these pages are jQuery and
 * innerHTML driven: panels are replaced wholesale on every status refresh and tabs are
 * switched from unrelated handlers, so component state is read from the DOM here instead of
 * held in a framework. Both notify listeners with a custom event, which is what replaced the
 * Bootstrap "shown" events those handlers used to wait for.
 */
(function (global) {
    "use strict";

    function dispatch(target, name) {
        target.dispatchEvent(new CustomEvent(name, {bubbles: true}));
    }

    global.aistShowTab = function (event, paneId) {
        if (event) {
            event.preventDefault();
        }
        var pane = document.getElementById(paneId);
        if (!pane) {
            return;
        }
        Array.prototype.forEach.call(pane.parentElement.children, function (sibling) {
            sibling.classList.toggle("active", sibling === pane);
            sibling.classList.toggle("in", sibling === pane);
        });
        var link = document.querySelector('a[href="#' + paneId + '"]');
        var list = link && link.parentElement && link.parentElement.parentElement;
        if (list) {
            Array.prototype.forEach.call(list.children, function (item) {
                item.classList.toggle("active", item === link.parentElement);
            });
        }
        dispatch(pane, "aist:tab-shown");
    };

    global.aistToggleCollapse = function (trigger, paneId) {
        var pane = document.getElementById(paneId);
        if (!pane) {
            return;
        }
        var shown = pane.classList.toggle("in");
        trigger.setAttribute("aria-expanded", shown);
        if (shown) {
            dispatch(pane, "aist:collapse-shown");
        }
    };
})(window);
