# evse-controller

This is an unfinished system whose purpose is to control "smart" EVSEs such as the Wallbox Quasar. I have referred to
the source code of the [v2g-liberty](https://github.com/SeitaBV/v2g-liberty/) project to create it, but none of the code
is shared; this is intended to be simpler and more of a standalone project. In particular, this project is not intended
to use Home Assistant; if your needs include using Home Assistant then the above project may well be more suited to your
needs.

I have completed some library routines declaring the interfaces used for controlling EVSEs and reading from power
monitors and I have implemented these for the following devices:

* [Wallbox Quasar](https://wallbox.com/en_ca/quasar-dc-charger) for EV charging and discharging (V2G)
* [Shelly EM](https://shellystore.co.uk/product/shelly-em/) for energy monitoring enabling solar energy capture (S2V)
  and load following (V2H); here it is assumed that grid power is monitored through channel 0 and solar power through
  channel 1, but nothing is done with the solar power measurement other than reporting it. It may be more useful to
  monitor EVSE power through this channel.

I have also started work on a basic scheduler example and a basic load follower (for doing V2G and S2V) which can be
seen in files Scheduler.py and LoadFollower.py respectively.

This code may look more Javaesque than Pythonesque - I have more expertise in Java but I've chosen Python so that I can
learn a little bit more.

This uses code from a library that can use a Web API to control the Wallbox Quasar, but that code is only used to
restart the wallbox in the case of modbus failure, a condition that happens once every few days to myself. The library
used for this is to be found here:

* https://github.com/cliviu74/wallbox

This has been tested and seems to work well.

I have also added logging to InfluxDB OSS version 2. This is the open-source variant and is documented here:

* https://docs.influxdata.com/influxdb/v2/
* https://docs.influxdata.com/influxdb/v2/install/?t=Linux for installation instructions if using Linux (also go to
  that page for other operating systems as the instructions are also there)

I do not believe it will work with version 1. If you just try `apt install influxdb`, it is likely that you will get
version 1, so I would suggest installing as per the official instructions on the page above.

## How to use and develop

This isn't yet designed for end-users, only for software engineers and the like. You need to know at least a little
Python to use this.

Configuration files are provided to spin up a [dev container](https://code.visualstudio.com/docs/devcontainers/containers)
that contains the dependencies required to develop, test and run this in a Docker container. Alternatively, you can
install the following on your system:

* Python 3.11.7
* Use `pip install requests pyModbusTCP wallbox` to install using pip the required libraries (or install them however
  you like).
* If you are using InfluxDB version 2, also use `pip install influxdb-client`. 

IP addresses are in configuration.py, so if your setup happens to be very close to mine, you can feel free to make
changes.

## Provided samples

The following samples are provided:

* RemainDormant.py: Hold the wallbox in dormant state. (I used to use that to detach my vehicle.)
* ChargeFull.py: Set the wallbox to charge at maximum rate.
* flux.py: Control the wallbox in a way that works well with Octopus Flux. (I use this one! It was at one time called
  Scheduler2.py.)

To get this running after setting up the configuration file, you would run commands such as:

* `python3 flux.py` or `python flux.py` (whichever works for you but you must use python 3)
* `python[3] flux.py -?` will show a brief help page
* `python[3] flux.py -p` will put the Wallbox into dormant mode (i.e. pause charging and discharging) for ten minutes
  before anything else is done which is useful for disconnecting your vehicle before going on a journey.

## Roadmap

* Creation of abstract APIs to control EV charging and discharging and to use current-monitoring CT clamps other than
  the Shelly (complete)
* Add a user interface allowing for rapid termination of any current EV charging or discharging session (will probably
  use Python's own tkinter for this - not started)
* Add V2G and S2V capabilities that may be independently specified during a scheduled slot (this capability is now part
  of the library routines)
* Add scheduling functionality based on a user-selected desired schedule including percentage-of-charge targets
  (flux.py shows an example of this but this wants putting into a proper UI)

The above is an ideal and some of it is sure to be done out of order!

## Explanatory video

I have produced a video that explains my setup, how I use the system, and goes into some detail regarding the flux.py
scheduler. Note that it is long at 66 minutes! You can find that here:

* https://youtu.be/4bIpY-AyUUw
