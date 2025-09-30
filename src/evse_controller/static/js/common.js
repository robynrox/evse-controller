const VALID_STATES = {
    'charge': 'Charge at maximum rate',
    'discharge': 'Discharge at maximum rate',
    'pause': 'Stop charging/discharging',
    'octgo': 'Octopus Go tariff',
    'ioctgo': 'Intelligent Octopus Go tariff',
    'flux': 'Octopus Flux tariff',
    'cosy': 'Cosy Octopus tariff',
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