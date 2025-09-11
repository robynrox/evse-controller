# Running EVSE Controller as a Service

This guide explains how to run the EVSE Controller as a persistent service that starts automatically with your system.

## Container-Based Deployment (Recommended)

Running as a containerized service provides several advantages:
- Works consistently across different platforms (Linux, Windows, macOS)
- Automatic restart on failure or system reboot
- Isolated environment with bundled dependencies
- Simple upgrade process

### Container Runtime Options

#### Docker (Most Common)
The examples in this guide use Docker and Docker Compose due to their widespread adoption and extensive documentation. Installation instructions and examples using Docker are provided below.

#### Alternative Container Runtimes
The application can run in any OCI-compliant container runtime:

- **Podman**
  - Drop-in replacement for Docker
  - Daemonless architecture
  - Root-less containers by default

- **containerd**
  - Lightweight runtime
  - Used by Kubernetes
  - Requires additional tooling for compose-style workflows
  - Consider `nerdctl` as a Docker-compatible CLI

- **LXC/LXD**
  - System container focus
  - Closer to traditional virtualization
  - Different configuration approach required
  - Better for long-running system services

Choose based on your needs:
- Docker: Best for ease of use and documentation
- Podman: Better security, good for rootless deployment
- containerd: Minimal runtime, good for resource-constrained systems
- LXC/LXD: Better for system-level isolation

The documentation provided has been tested with Docker and Podman. If you wish to use any other alternative, these instructions may not be particularly useful.

### Prerequisites
- Chosen container runtime installed and configured to start at boot as needed
- Container compose tool if using Docker Compose format

### Setup Steps

1. **Start initial build and configuration of the system**
   ```bash
   [docker|podman] compose build
   [docker|podman] compose run --rm controller -m evse_controller.configure
   ```
   You will need to enter details of your system. It is possible to change these details when the service is running using a web interface if you wish, or you can return to this step to change the configruation using the text interface at any time.

2. **Start the service**
   ```bash
   [docker|podman] compose up
   ```

3. **Test the system**
   You should see log output, and you should be able to access the system through http://localhost:5000/ and see the web interface home page.
   
4. **Start the service as a daemon**
   Use Control-C in the terminal to exit the service and restart it with:
   ```bash
   [docker|podman] compose up -d
   ```

### Data Persistence

All data is stored in the `evse-controller_data` Docker volume:
- Configuration files
- Log files
- State information

### Time zone setting

The time zone is set within the Dockerfile to Europe/London as shipped. If you need to change that, it is this line that needs changing:

`RUN ln -fs /usr/share/zoneinfo/Europe/London /etc/localtime`

One way to change the time zone is to change what appears after `/usr/share/zoneinfo/`. A list of time zones is available here:

https://en.wikipedia.org/wiki/List_of_tz_database_time_zones

You would then need to rebuild the container using `[docker|podman] compose build` before bringing up a new one.
