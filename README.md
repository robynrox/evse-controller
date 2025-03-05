# evse-controller

This is an unfinished system whose purpose is to control "smart" EVSEs such as the Wallbox Quasar. I have referred to
the source code of the [v2g-liberty](https://github.com/SeitaBV/v2g-liberty/) project to create it, but none of the code
is shared; this is intended to be simpler and more of a standalone project. In particular, this project is not intended
to use Home Assistant; if your needs include using Home Assistant then the above project may well be more suited to your
needs.

I have completed some library routines declaring the interfaces used for controlling EVSEs and reading from power
monitors and I have implemented these for the following devices:

* [Wallbox Quasar](https://wallbox.com/en_ca/quasar-dc-charger) for EV charging and discharging (V2G)
* [Shelly EM](https://shellystore.co.uk/product/shelly-em/) for energy monitoring enabling solar energy capture (like S2V)
  and load following (like V2H); here it is assumed that grid power is monitored through channel 0 and EVSE power through
  channel 1. The EVSE channel is only used for monitoring and reporting and could be substituted with the solar power, for
  example.

I have also started work on a basic scheduler example and a basic load follower (for doing V2G and S2V) which can be
seen in files Scheduler.py and LoadFollower.py respectively.

This code may look more Javaesque than Pythonesque - I have more expertise in Java but I've chosen Python so that I can
learn a little bit more.

This uses code from a library that can use a Web API to control the Wallbox Quasar, but that code is only used to
restart the wallbox in the case of modbus failure, a condition that happens once every few days to myself. The library
used for this can be found here:

* https://github.com/cliviu74/wallbox

This has been tested and seems to work well.

I have also added logging to InfluxDB OSS version 2. This is the open-source variant and is documented here:

* https://docs.influxdata.com/influxdb/v2/
* https://docs.influxdata.com/influxdb/v2/install/?t=Linux for installation instructions if using Linux (also go to
  that page for other operating systems as the instructions are also there).

I do not believe it will work with version 1. If you just try `apt install influxdb`, it is likely that you will get
version 1, so I would suggest installing as per the official instructions on the page above.

## How to use and develop

This isn't yet designed for end-users, only for software engineers and the like. You need to know at least a little
Python to use this.

### Option 1: Using Dev Container
Configuration files are provided to spin up a [dev container](https://code.visualstudio.com/docs/devcontainers/containers)
that contains the dependencies required to develop, test, and run this in a Docker container.

### Option 2: Manual Setup
* Python 3.11.7 / 3.12.3
* Use `python3 -m venv .` or if that fails try `python -m venv .` to create a virtual environment
* Run `source bin/activate` to use the virtual environment
* Install dependencies with: `pip install -r requirements.txt`

IP addresses are in `configuration.py`, so if your setup happens to be very close to mine, you can feel free to make
changes.

## Provided samples

The following samples are provided:

* octopus.py: Control the wallbox for Octopus Go or Cosy Octopus. This is what I expect to maintain going forward.
  It will have control logic to drive the Flux tariff added as well, and possibly Agile at some point - at the time
  of writing, it doesn't seem to be the case that the use of the Agile tariff is favourable.
* app.py: At the time of writing, this runs the controller along with a web interface that allows the same basic
  controls that you can type interactively into the terminal. You can access that web interface on port 5000. For example,
  on the host system, point your web browser to http://127.0.0.1:5000/. On another system on your local network,
  it might be accessed by using the name of the server, e.g. http://evserver:5000/, or alternatively I believe it
  tells you at the time of startup what IP address you can use.

To get this running after setting up the configuration file, you would run the following command:

* `python3 app.py` or `python app.py` (whichever works for you but you must use python 3)

I removed the command-line argument parsing since interactive control and web control are now available.

## Deprecated samples

These are older samples of code and are no longer guaranteed to work. These have been moved to the legacy-unsupported directory but would have to be moved back into the root directory as part of any process aimed at getting them to work again.

* RemainDormant.py: Hold the wallbox in dormant state. (I used to use that to detach my vehicle.)
* ChargeFull.py: Set the wallbox to charge at maximum rate.
* flux.py: Control the wallbox in a way that works well with Octopus Flux. (I use this one! It was at one time called
  Scheduler2.py.)
* cosy.py: Control the wallbox in a way that works well with Cosy Octopus.
* octgo.py: Control the wallbox in a way that works well with Octopus Go.
* scheduler.py: Another old sample that used to work with Octopus Flux.
* set-wallbox.py: A sample of how to use the modbus interface to control the wallbox.

## Roadmap

* Creation of abstract APIs to control EV charging and discharging and to use current-monitoring CT clamps other than
  the Shelly (complete)
* Add a user interface allowing for rapid termination of any current EV charging or discharging session (HTML seems to
  be the obvious way to go - this is now in progress and a working prototype is available)
* Add V2G and S2V capabilities that may be independently specified during a scheduled slot (this capability is now part
  of the library routines)
* Add scheduling functionality based on a user-selected desired schedule including percentage-of-charge targets
  (flux.py shows an example of this but this wants putting into a proper UI)
* I'm trying to attach greater importance to bug-fixing; it's more important for it to be solid than look pretty.

The above is an ideal and some of it is sure to be done out of order!

## Explanatory video

I have produced a video that explains my setup, how I use the system, and goes into some detail regarding the flux.py
scheduler. Note that it is long at 66 minutes! It is somewhat outdated now, but most of it still applies. You can find
that here:

* https://youtu.be/4bIpY-AyUUw
