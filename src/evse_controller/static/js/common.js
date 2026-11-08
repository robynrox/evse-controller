const VALID_STATES = {
    'charge': 'Charge at maximum rate',
    'discharge': 'Discharge at maximum rate',
    'pause': 'Stop charging/discharging',
    'smart': 'Use configured smart tariff (set in Configuration)',
    'unplug': 'Prepare for cable removal',
    'freerun': 'Disable OCPP connectivity and enter FREERUN mode',
    'ocpp': 'Enter OCPP mode - EVSE controlled via OCPP protocol',
    'solar': 'Solar-only charging mode',
    'power-home': 'Power home from vehicle battery',
    'balance': 'Balance between solar charging and home power'
};

function populateStateSelect(selectElement) {
    for (const [state, description] of Object.entries(VALID_STATES)) {
        const option = document.createElement('option');
        option.value = state;
        option.textContent = `${state} - ${description}`;
        selectElement.appendChild(option);
    }
}

/* Chart zoom ---------------------------------------------------------
 *
 * Any button with data-chart-zoom opens the SVG in its surrounding
 * .panel in a larger, scrollable overlay.  The overlay starts at 1.6x the
 * viewport width (minimum 1800 px) and can be zoomed in and out, so the
 * small axis labels of a dense report stay readable.
 */
(function () {
    var MIN_WIDTH = 900;
    var MAX_WIDTH = 6000;
    var STEP = 1.35;

    function chartTitle(panel) {
        var heading = panel.previousElementSibling;
        if (heading && /^H[1-6]$/.test(heading.tagName)) {
            return heading.textContent.trim();
        }
        return 'Chart';
    }

    function zoomButton(label, className) {
        var button = document.createElement('button');
        button.type = 'button';
        button.className = 'chart-zoom-control ' + className;
        button.textContent = label;
        return button;
    }

    function openZoom(svg, title, trigger) {
        var overlay = document.createElement('div');
        overlay.className = 'chart-zoom-overlay';
        overlay.setAttribute('role', 'dialog');
        overlay.setAttribute('aria-modal', 'true');
        overlay.setAttribute('aria-label', title + ' (zoomed)');

        var toolbar = document.createElement('div');
        toolbar.className = 'chart-zoom-toolbar';

        var titleElement = document.createElement('span');
        titleElement.className = 'chart-zoom-title';
        titleElement.textContent = title;

        var controls = document.createElement('div');
        controls.className = 'chart-zoom-controls';

        var zoomOut = zoomButton('−', 'chart-zoom-out');
        zoomOut.setAttribute('aria-label', 'Zoom out');
        var zoomIn = zoomButton('+', 'chart-zoom-in');
        zoomIn.setAttribute('aria-label', 'Zoom in');
        var closeButton = zoomButton('Close', 'chart-zoom-close');

        controls.appendChild(zoomOut);
        controls.appendChild(zoomIn);
        controls.appendChild(closeButton);
        toolbar.appendChild(titleElement);
        toolbar.appendChild(controls);

        var body = document.createElement('div');
        body.className = 'chart-zoom-body';

        var clone = svg.cloneNode(true);
        clone.classList.add('chart-zoom-svg');
        clone.style.maxWidth = 'none';
        clone.style.height = 'auto';
        body.appendChild(clone);

        overlay.appendChild(toolbar);
        overlay.appendChild(body);
        document.body.appendChild(overlay);
        document.documentElement.classList.add('chart-zoom-open');

        var width = Math.max(
            1800,
            Math.round(window.innerWidth * 1.6)
        );

        function applyWidth() {
            clone.style.width = width + 'px';
            zoomOut.disabled = width <= MIN_WIDTH;
            zoomIn.disabled = width >= MAX_WIDTH;
        }

        function closeOverlay() {
            document.removeEventListener('keydown', onKey);
            document.documentElement.classList.remove('chart-zoom-open');
            overlay.remove();
            if (trigger) {
                trigger.focus();
            }
        }

        function onKey(event) {
            if (event.key === 'Escape') {
                closeOverlay();
            } else if (event.key === '+' || event.key === '=') {
                event.preventDefault();
                zoomIn.click();
            } else if (event.key === '-' || event.key === '_') {
                event.preventDefault();
                zoomOut.click();
            }
        }

        zoomOut.addEventListener('click', function () {
            width = Math.max(MIN_WIDTH, Math.round(width / STEP));
            applyWidth();
        });
        zoomIn.addEventListener('click', function () {
            width = Math.min(MAX_WIDTH, Math.round(width * STEP));
            applyWidth();
        });
        closeButton.addEventListener('click', closeOverlay);
        overlay.addEventListener('click', function (event) {
            if (event.target === overlay) {
                closeOverlay();
            }
        });
        document.addEventListener('keydown', onKey);

        applyWidth();
        closeButton.focus();
    }

    function setupChartZoom() {
        var buttons = document.querySelectorAll('[data-chart-zoom]');
        Array.prototype.forEach.call(buttons, function (button) {
            var panel = button.closest('.panel');
            var svg = panel ? panel.querySelector('svg') : null;
            if (!svg) {
                button.hidden = true;
                return;
            }
            var title = chartTitle(panel);
            button.setAttribute('aria-label', 'Zoom ' + title);
            button.addEventListener('click', function () {
                openZoom(svg, title, button);
            });
        });
    }

    if (document.readyState === 'loading') {
        document.addEventListener('DOMContentLoaded', setupChartZoom);
    } else {
        setupChartZoom();
    }
})();