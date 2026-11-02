/**
 * Drives the platform's component classes that it styles but no longer scripts.
 *
 * DefectDojo 3.x dropped the Bootstrap bundle while keeping the CSS contract for tabs
 * (.nav-tabs / .tab-pane.active) and collapsible panels (.collapse / .collapse.in). Modals
 * moved to the native <dialog>, which needs nothing; these two still need the class moved.
 * The select widget moved too: bootstrap-select is gone and Chosen is what the platform now
 * ships and styles, so a dressed select is asked for here rather than at each call site.
 *
 * Alpine ships with the platform and drives its own markup, but these pages are jQuery and
 * innerHTML driven: panels are replaced wholesale on every status refresh and tabs are
 * switched from unrelated handlers, so component state is read from the DOM here instead of
 * held in a framework. Both notify listeners with a custom event, which is what replaced the
 * Bootstrap "shown" events those handlers used to wait for.
 */
(function (global) {
    "use strict";

    // A select narrow enough to read at a glance needs no search box; every one of ours
    // sits in a fluid row, so the widget is told to follow the row rather than measure
    // a select that may still be hidden when it is dressed.
    var DEFAULT_SELECT_OPTIONS = {disable_search_threshold: 10, width: "100%"};

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

    /**
     * Dress a <select> with the platform's own widget, and keep it in step with its options.
     *
     * Chosen reads the options once, at init, and shows a stale list afterwards unless it is
     * told they changed -- which is why a call site that fills a select asks again here.
     */
    global.aistEnhanceSelect = function (select, options) {
        var jq = global.jQuery;
        if (!select || !jq || !jq.fn || !jq.fn.chosen) {
            return;
        }
        var $select = jq(select);
        if ($select.data("chosen")) {
            $select.trigger("chosen:updated");
            return;
        }
        $select.addClass("chosen-select").chosen(options || DEFAULT_SELECT_OPTIONS);
    };

    /**
     * Subscribe to a select's change, whether a person used the widget or the element itself.
     *
     * Chosen announces a choice with jQuery's trigger, which runs jQuery handlers only and is
     * never seen by a listener added with addEventListener. One jQuery subscription catches
     * both that and a real change event, so the handler runs exactly once either way.
     */
    global.aistOnSelectChange = function (select, handler) {
        if (global.jQuery) {
            global.jQuery(select).on("change", handler);
            return;
        }
        select.addEventListener("change", handler);
    };

    /**
     * Dress the selects on one of our pages, as the 2.x base template did for every select.
     *
     * Scoped to a page of ours on purpose. The platform's own pages pick their widget per
     * form -- Select2 on the filter forms, a plain select elsewhere -- and dressing those a
     * second time leaves two widgets stacked on one field. DataTables builds and owns its
     * page-length select, so that one is left alone wherever it appears.
     */
    global.aistDressSelects = function (root) {
        var scope = root || document;
        // A required select is left alone: the widget hides the element it replaces, and a
        // browser refuses to report a constraint violation on a control it cannot focus, so
        // the form simply never submits and nothing on screen says why.
        var selects = scope.querySelectorAll(
            "select:not(.dt-input):not(.chosen-select):not([required])",
        );
        Array.prototype.forEach.call(selects, function (select) {
            global.aistEnhanceSelect(select);
        });
    };

    /**
     * The same, once the page has finished setting itself up.
     *
     * A page fills some of its selects from its own scripts, and those run on ready; sweeping
     * after them means the widget reads a list that is already complete.
     */
    global.aistDressSelectsOnReady = function (root) {
        function sweep() {
            global.setTimeout(function () {
                global.aistDressSelects(root);
            }, 0);
        }
        if (document.readyState === "loading") {
            document.addEventListener("DOMContentLoaded", sweep);
            return;
        }
        sweep();
    };

    /**
     * Close the panels that share an accordion with this one.
     *
     * A panel names its accordion with data-parent, which is how the Bootstrap plugin was
     * told that only one of a group is open at a time.
     */
    function closeAccordionSiblings(pane) {
        var parent = pane.dataset.parent || pane.dataset.bsParent;
        var accordion = parent && document.querySelector(parent);
        if (!accordion) {
            return;
        }
        var open = accordion.querySelectorAll(".collapse.in");
        Array.prototype.forEach.call(open, function (sibling) {
            if (sibling === pane) {
                return;
            }
            sibling.classList.remove("in");
            // An id is free to contain a quote or a dot, either of which would otherwise end
            // the attribute value early and make this select the wrong element or throw.
            var escaped = global.CSS && global.CSS.escape ? global.CSS.escape(sibling.id) : sibling.id;
            var trigger = accordion.querySelector('[aria-controls="' + escaped + '"]');
            if (trigger) {
                trigger.setAttribute("aria-expanded", "false");
            }
        });
    }

    global.aistToggleCollapse = function (trigger, paneId) {
        var pane = document.getElementById(paneId);
        if (!pane) {
            return;
        }
        var shown = pane.classList.toggle("in");
        trigger.setAttribute("aria-expanded", shown);
        if (shown) {
            closeAccordionSiblings(pane);
            dispatch(pane, "aist:collapse-shown");
        }
    };

})(window);
