# Add the address of your Wallbox here (an FQDN or an IP address):
WALLBOX_URL = "192.168.0.123"
# Add the username, password and serial number of your Wallbox here to allow automatic restart if it stops responding
# to modbus requests:
WALLBOX_USERNAME = "myemail@address.com"
WALLBOX_PASSWORD = "thisReallyIsntMyPassword"
WALLBOX_SERIAL = 12345
# Add the address of your Shelly EM here (an FQDN or an IP address):
SHELLY_URL = "192.168.0.124"
# If using InfluxDB to store logs, provide the URL, access token and organisation here:
INFLUXDB_URL = "http://localhost:8086"
INFLUXDB_TOKEN = "blahdeblah"
INFLUXDB_ORG = "aVeryOrganisedOrganisation"
USING_INFLUXDB = False

# The configuration requirements of this program may change over time. It is recommended that you copy the block of
# configuration variables from the source code and paste it below the line. This will facilitate merging of changes
# into your configuration as they occur.

