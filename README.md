# evse-controller

This is an unfinished system whose purpose is to control "smart" EVSEs such as the Wallbox Quasar. I have referred to
the source code of the [v2g-liberty](https://github.com/SeitaBV/v2g-liberty/) project to create it, but none of the code
is shared; this is intended to be simpler and more of a standalone project. In particular, this project is not intended
to use Home Assistant; if your needs include using Home Assistant then the above project may well be more suited to your
needs.

At this stage, I am working on library routines. These routines will work with systems such as the following:

* [Wallbox Quasar](https://wallbox.com/en_ca/quasar-dc-charger) for EV charging and discharging (V2G)
* [Shelly EM](https://shellystore.co.uk/product/shelly-em/) for energy monitoring enabling solar energy capture (S2V)
  and load following (V2H)

Eventually I intend to abstract the interfaces out so that the code can be used with other systems.

## How to use and develop

This isn't yet designed for end-users, only for software engineers and the like. You need to know at least a little
Python to use this.

Configuration files are provided to spin up a [dev container](https://code.visualstudio.com/docs/devcontainers/containers)
that contains the dependencies required to develop, test and run this in a Docker container. Alternatively, you can
install the following on your system:

* Python 3.11.7
* Use `pip install requests pyModbusTCP` to install using pip the required libraries (or install them however you like).

I've provided an example in scheduler.py, but it's very basic. IP addresses are in configuration.py, so if your setup
happens to be very close to mine, you can feel free to make changes.

## Roadmap

* Implement the APIs for the hardware I have (basic ones are now complete)
* Add scheduling functionality based on a user-selected desired schedule including percentage-of-charge targets
* Add a user interface allowing for rapid termination of any current EV charging or discharging session
* Add V2G and S2V capabilities that may be independently specified during a scheduled slot
* Add logging with a view to optimising V2G and S2V
* Creation of abstract APIs to control EV charging and discharging and to use current-monitoring CT clamps other than
  the Shelly

The above is an ideal and some of it is sure to be done out of order!
