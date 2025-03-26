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
  - Use `podman-compose` instead of `docker-compose`
  - Example conversion:
    ```bash
    # Replace 'docker' with 'podman'
    podman-compose up -d
    ```

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

### Prerequisites
- Chosen container runtime installed and configured to start at boot
- Container compose tool if using Docker Compose format

### Setup Steps

1. **Configure chosen container runtime to start at boot**

   **Docker**:
   - Linux: `sudo systemctl enable docker`
   - Windows: Docker Desktop settings → "Start Docker Desktop when you log in"
   - macOS: Docker Desktop settings → "Start Docker Desktop when you log in"

   **Podman**:
   - Linux: `systemctl --user enable podman.socket`
   - Windows: Run `podman machine start` and add to Task Scheduler
   - macOS: `brew services start podman`

   **containerd**:
   - Linux: `sudo systemctl enable containerd`
   - Windows: `sc.exe config containerd start=auto`
   - Note: Additional setup needed for compose-style workflows

   **LXC/LXD**:
   - Linux: `sudo systemctl enable lxd.service`
   - Windows/macOS: Not natively supported

2. **Create deployment directory**
   ```bash
   mkdir evse-service
   cd evse-service
   ```

3. **Create docker-compose.yml**
   ```yaml
   version: '3.8'
   services:
     evse-controller:
       image: evse-controller:latest
       build: .
       volumes:
         - evse-data:/workspace/data
       environment:
         - EVSE_DATA_DIR=/workspace/data
       ports:
         - "5000:5000"
       restart: unless-stopped
       healthcheck:
         test: ["CMD", "curl", "-f", "http://localhost:5000/api/status"]
         interval: 30s
         timeout: 10s
         retries: 3
         start_period: 40s

   volumes:
     evse-data:
       name: evse-controller-data
   ```

4. **Start the service**
   ```bash
   docker-compose up -d
   ```

### Managing the Service

- **View logs**:
  ```bash
  docker-compose logs -f
  ```

- **Stop service**:
  ```bash
  docker-compose down
  ```

- **Update to new version**:
  ```bash
  docker-compose pull
  docker-compose up -d
  ```

- **Check service status**:
  ```bash
  docker-compose ps
  ```

### Data Persistence

All data is stored in the `evse-data` Docker volume:
- Configuration files
- Log files
- State information

To backup this data:
```bash
docker run --rm -v evse-data:/data -v $(pwd):/backup alpine tar czf /backup/evse-data-backup.tar.gz /data
```

## Alternative Deployment Methods

### Linux (systemd)
For users who prefer not to use containers, a systemd service file is provided in `deployment/systemd/evse-controller.service`.

### Windows Service
For Windows users who prefer native services, installation instructions using NSSM (Non-Sucking Service Manager) are provided in `deployment/windows/README.md`.

## Monitoring and Maintenance

### Health Checks
The container includes a health check that verifies the API is responding. You can monitor this with:
```bash
docker inspect --format "{{.State.Health.Status}}" evse-controller
```

### Log Rotation
Docker handles log rotation automatically. To configure:
```json
{
  "log-driver": "json-file",
  "log-opts": {
    "max-size": "10m",
    "max-file": "3"
  }
}
```

### Backup Recommendations
- Regular backups of the data volume
- Export of InfluxDB data if used
- Backup of configuration files

## Troubleshooting

### Common Issues

1. **Service won't start after system reboot**
   - Check Docker daemon status
   - Verify network availability
   - Check logs for startup errors

2. **Container keeps restarting**
   - Check logs for error messages
   - Verify configuration
   - Check device connectivity

3. **Web interface unavailable**
   - Verify port mapping
   - Check container logs
   - Confirm network configuration

### Getting Help
- Check the project's GitHub issues
- Review logs using `docker-compose logs`
- Include relevant logs and container status when reporting issues

## Deployment on Resource-Constrained Systems

### Raspberry Pi and Similar Devices

The EVSE Controller can run on devices like Raspberry Pi, but some container runtimes may be more suitable than others. The following recommendations are based on general principles and community feedback - please note that not all options have been extensively tested.

#### Recommended Options

1. **Podman (Recommended for Pi)**
   - Lighter resource usage than Docker
   - No daemon process required
   - Root-less operation
   - Installation:
     ```bash
     sudo apt-get install podman podman-compose
     ```

2. **containerd + nerdctl**
   - Minimal resource footprint
   - Used in lightweight Kubernetes distributions
   - Good for systems with limited memory
   - Note: Setup is more complex than Podman

3. **Docker (Tested but heavier)**
   - Works but consumes more resources
   - Requires running daemon
   - Most documented option
   - Consider for consistency with other systems

#### Resource Considerations

- **Memory Usage**:
  - Docker: ~300MB+ baseline
  - Podman: ~100MB baseline
  - containerd: ~50MB baseline
  - Application itself: ~100MB during operation

- **Storage**:
  - Allow at least 1GB for container images and volumes
  - Consider external storage for logs if running long-term

- **CPU**:
  - Any modern Pi (3B+ or newer) should handle the workload
  - Container runtime overhead is minimal during normal operation

#### ⚠️ Important Notes

- These recommendations are preliminary and based on general principles
- Actual performance may vary based on your specific setup
- We welcome feedback from users running on resource-constrained systems
- Please share your experiences via GitHub issues or discussions

#### Getting Started on Pi

1. **Choose your runtime**:
   ```bash
   # For Podman (recommended):
   sudo apt-get update
   sudo apt-get install podman podman-compose

   # For Docker (alternative):
   curl -fsSL https://get.docker.com | sh
   ```

2. **Adjust resource limits**:
   ```yaml
   # docker-compose.yml or podman-compose.yml
   services:
     evse-controller:
       # ... other config ...
       deploy:
         resources:
           limits:
             memory: 256M
           reservations:
             memory: 128M
   ```

3. **Monitor resource usage**:
   ```bash
   # Basic system monitoring
   top
   free -h
   df -h

   # Container-specific monitoring
   podman stats  # or 'docker stats'
   ```

#### Known Issues

- Docker daemon may be resource-intensive on boot
- Log rotation is crucial on systems with limited storage
- Some Pi models may need additional cooling under load

We encourage users to share their experiences and configurations when running on resource-constrained systems. This section will be updated as we receive more real-world feedback.
