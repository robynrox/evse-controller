"""
Redaction utility functions for sensitive data
"""


def redact_sensitive_data(data):
    """
    Redact sensitive information from data structures before logging.
    
    Args:
        data: The data to redact (dict, list, or other)
        
    Returns:
        The data with sensitive fields redacted
    """
    if isinstance(data, dict):
        redacted = {}
        for key, value in data.items():
            if key.lower() in ['password', 'chargepointidentity', 'charge_point_identity']:
                redacted[key] = 'REDACTED'
            else:
                redacted[key] = redact_sensitive_data(value)
        return redacted
    elif isinstance(data, list):
        return [redact_sensitive_data(item) for item in data]
    else:
        return data