import unittest
from src.evse_controller.utils.redaction import redact_sensitive_data


class TestOCPPRedaction(unittest.TestCase):
    """Test cases for the redaction function in OCPP manager"""

    def test_redact_sensitive_data_dict(self):
        """Test redacting sensitive data from a dictionary"""
        input_data = {
            "type": "ocpp",
            "address": "https://example.com",
            "chargePointIdentity": "secret_identity",
            "password": "secret_password",
            "normal_field": "normal_value"
        }
        
        expected = {
            "type": "ocpp",
            "address": "https://example.com",
            "chargePointIdentity": "REDACTED",
            "password": "REDACTED",
            "normal_field": "normal_value"
        }
        
        result = redact_sensitive_data(input_data)
        self.assertEqual(result, expected)

    def test_redact_sensitive_data_nested_dict(self):
        """Test redacting sensitive data from nested dictionaries"""
        input_data = {
            "type": "ocpp",
            "config": {
                "chargePointIdentity": "nested_secret_identity",
                "password": "nested_secret_password",
                "other": "value"
            },
            "list_field": [
                {"chargePointIdentity": "list_secret", "password": "list_password"},
                {"normal": "data"}
            ]
        }
        
        expected = {
            "type": "ocpp",
            "config": {
                "chargePointIdentity": "REDACTED",
                "password": "REDACTED",
                "other": "value"
            },
            "list_field": [
                {"chargePointIdentity": "REDACTED", "password": "REDACTED"},
                {"normal": "data"}
            ]
        }
        
        result = redact_sensitive_data(input_data)
        self.assertEqual(result, expected)

    def test_redact_sensitive_data_list(self):
        """Test redacting sensitive data from a list"""
        input_data = [
            {"chargePointIdentity": "secret", "normal": "value"},
            {"password": "secret", "other": "value"}
        ]
        
        expected = [
            {"chargePointIdentity": "REDACTED", "normal": "value"},
            {"password": "REDACTED", "other": "value"}
        ]
        
        result = redact_sensitive_data(input_data)
        self.assertEqual(result, expected)

    def test_redact_sensitive_data_non_sensitive(self):
        """Test that non-sensitive data is not affected"""
        input_data = {
            "type": "ocpp",
            "address": "https://example.com",
            "normal_field": "normal_value",
            "another_field": 123
        }
        
        expected = {
            "type": "ocpp",
            "address": "https://example.com",
            "normal_field": "normal_value",
            "another_field": 123
        }
        
        result = redact_sensitive_data(input_data)
        self.assertEqual(result, expected)

    def test_redact_sensitive_data_case_insensitive(self):
        """Test that field name matching is case-insensitive"""
        input_data = {
            "chargepointidentity": "secret1",
            "ChargePointIdentity": "secret2",
            "PASSWORD": "secret3",
            "password": "secret4"
        }
        
        expected = {
            "chargepointidentity": "REDACTED",
            "ChargePointIdentity": "REDACTED",
            "PASSWORD": "REDACTED",
            "password": "REDACTED"
        }
        
        result = redact_sensitive_data(input_data)
        self.assertEqual(result, expected)

    def test_redact_sensitive_data_none_input(self):
        """Test redacting None input"""
        result = redact_sensitive_data(None)
        self.assertIsNone(result)

    def test_redact_sensitive_data_other_types(self):
        """Test redacting other data types"""
        self.assertEqual(redact_sensitive_data("string"), "string")
        self.assertEqual(redact_sensitive_data(123), 123)
        self.assertEqual(redact_sensitive_data(True), True)
        self.assertEqual(redact_sensitive_data(3.14), 3.14)


if __name__ == '__main__':
    unittest.main()