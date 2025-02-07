# You may want to copy the configuration variables listed here, make a new file called secret.py, and paste them there.
# The idea is that secret.py is excluded from version control and you will not be prompted to check them in if you
# cloned the repository.

# ***** START OF CONFIGURATION *****

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

# If using Octopus Energy API, provide the account number and API key here for future use
# and set OCTOPUS_IN_USE to True:
OCTOPUS_IN_USE = False
OCTOPUS_ACCOUNT = "A-12345678"
OCTOPUS_API_KEY = "sk_live_your_key_here"

# If you want logging to be enabled, set this to True:
LOGGING = True

# Set your default tariff here (at the time of writing, COSY or OCTGO):
DEFAULT_TARIFF = "OCTGO"

# ***** END OF CONFIGURATION *****

# If using the secret.py file, do not copy the statements below into it.
try:
    from secret import *
except ModuleNotFoundError:
    pass
