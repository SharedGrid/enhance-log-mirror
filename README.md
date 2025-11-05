# Enhance Log Mirror

Enhance log mirroring service that continuously tails web server logs from Enhance and mirrors them to individual site directories with automatic rotation and retention management.

## Features

- Handles source log files that are frequently deleted, recreated, or truncated
- Daily log rotation at midnight
- Automatic cleanup of logs older than 30 days, can be changed by updating the "RETENTION_DAYS"
- File locking due protect from concurrent writes

## Requirements

- **OS**: Linux (tested on Ubuntu 24.04)
- **Python**: 3.8 or higher
- **Permissions**: Root access (required for chown operations)
- **Dependencies**:
  - `aiofiles` - Asynchronous file operations
  - `watchdog` - File system monitoring

## Architecture

```
Source: /var/local/enhance/webserver_logs/<uuid>.log
                     ↓
          [Enhance Log Mirror]
                     ↓
Destination: /var/www/<uuid>/access-logs/YYYY-MM-DD.log
```

The service:

1. Watches `/var/local/enhance/webserver_logs/` for log files
2. Tails each `<uuid>.log` file in real-time
3. Mirrors content to `/var/www/<uuid>/access-logs/`
4. Creates daily log files (e.g., `2025-11-05.log`)
5. Maintains ownership matching the site's directory
6. Continues operation through file deletion/recreation/truncation

## Installation

### Step 1: Create Installation Directory

```bash
sudo mkdir -p /opt/enhance-log-mirror
cd /opt/enhance-log-mirror
```

### Step 2: Set Up Python Virtual Environment

```bash
python3 -m venv venv
source venv/bin/activate
```

### Step 3: Install Dependencies

```bash
pip install aiofiles watchdog
```

### Step 4: Install the Application

Copy the `app.py` script to `/opt/enhance-log-mirror/app.py`:

```bash
# Download or copy the script
sudo nano /opt/enhance-log-mirror/app.py
# Paste the script content and save

# Make executable
sudo chmod +x /opt/enhance-log-mirror/app.py
```

### Step 5: Create Systemd Service

Create the systemd unit file:

```bash
sudo nano /etc/systemd/system/enhance-log-mirror.service
```

Paste the following content:

```ini
[Unit]
Description=Enhance Log Mirror
After=network.target

[Service]
ExecStart=/opt/enhance-log-mirror/venv/bin/python /opt/enhance-log-mirror/app.py
Restart=always
RestartSec=5
Type=simple
User=root
Group=root
ProtectSystem=full
ProtectHome=yes
PrivateTmp=yes
NoNewPrivileges=yes
ReadWritePaths=/var/local/enhance/webserver_logs /var/www
LimitNOFILE=65535

[Install]
WantedBy=multi-user.target
```

Save and exit (Ctrl+X, Y, Enter).

### Step 6: Enable and Start the Service

```bash
# Reload systemd to recognize the new service
sudo systemctl daemon-reload

# Enable the service to start on boot
sudo systemctl enable enhance-log-mirror

# Start the service
sudo systemctl start enhance-log-mirror

# Check service status
sudo systemctl status enhance-log-mirror
```

## Usage

### Managing the Service

**Start the service:**

```bash
sudo systemctl start enhance-log-mirror
```

**Stop the service:**

```bash
sudo systemctl stop enhance-log-mirror
```

**Restart the service:**

```bash
sudo systemctl restart enhance-log-mirror
```

**Check service status:**

```bash
sudo systemctl status enhance-log-mirror
```

**Enable on boot:**

```bash
sudo systemctl enable enhance-log-mirror
```

**Disable on boot:**

```bash
sudo systemctl disable enhance-log-mirror
```

### Viewing Logs

**View application logs:**

```bash
sudo tail -f /var/log/enhance-mirror.log
```

**View systemd service logs:**

```bash
sudo journalctl -u enhance-log-mirror -f
```

**View recent service logs:**

```bash
sudo journalctl -u enhance-log-mirror -n 100
```

**View logs from today:**

```bash
sudo journalctl -u enhance-log-mirror --since today
```

**View logs with full details:**

```bash
sudo journalctl -u enhance-log-mirror -xe
```

### Monitoring

**Check how many tailers are active:**

```bash
sudo grep "active tailers" /var/log/enhance-mirror.log | tail -1
```

**View statistics (reported every 5 minutes):**

```bash
sudo grep "Stats:" /var/log/enhance-mirror.log | tail -5
```

**Check for file recreations:**

```bash
sudo grep "File recreated" /var/log/enhance-mirror.log
```

**Check for truncations:**

```bash
sudo grep "File truncated" /var/log/enhance-mirror.log
```

## Configuration

Edit `/opt/enhance-log-mirror/app.py` to modify configuration:

```python
# ---------------- CONFIG ----------------
RETENTION_DAYS = 30
LOG_FILE = "/var/log/enhance-mirror.log"
# ---------------------------------------
```

After modifying the configuration:

```bash
sudo systemctl restart enhance-log-mirror
```

## Log Structure

### Source Logs

```
/var/local/enhance/webserver_logs/
├── uuid-1.log
├── uuid-2.log
└── uuid-3.log
```

### Destination Logs

```
/var/www/
├── uuid-1/
│   └── access-logs/
│       ├── 2025-11-01.log
│       ├── 2025-11-02.log
│       └── 2025-11-05.log
├── uuid-2/
│   └── access-logs/
│       └── 2025-11-05.log
└── uuid-3/
    └── access-logs/
        └── 2025-11-05.log
```

## How It Works

### Daily Rotation

At midnight (00:00):

- All tailers rotate to new date-based log files
- Previous day's logs are closed
- New files created with format `YYYY-MM-DD.log`

### Cleanup

At 00:30 daily:

- Scans all site directories for old logs
- Deletes logs older than `RETENTION_DAYS` (default: 30)
- Skips non-log directories for efficiency

## Troubleshooting

### Service Won't Start

**Check configuration:**

```bash
sudo /opt/enhance-log-mirror/venv/bin/python /opt/enhance-log-mirror/app.py
```

**Check for lock file:**

```bash
sudo rm /var/run/enhance-mirror-logs.lock
sudo systemctl start enhance-log-mirror
```

### No Logs Being Written

**Verify source files exist:**

```bash
ls -la /var/local/enhance/webserver_logs/
```

**Check service is running:**

```bash
sudo systemctl status enhance-log-mirror
```

**Check application logs:**

```bash
sudo tail -100 /var/log/enhance-mirror.log
```

**Verify tailers started:**

```bash
sudo grep "Starting tailer" /var/log/enhance-mirror.log
```

Adjust retention period if needed (edit `RETENTION_DAYS` in config).

## Uninstallation

To completely remove the service:

```bash
# Stop and disable service
sudo systemctl stop enhance-log-mirror
sudo systemctl disable enhance-log-mirror

# Remove systemd unit
sudo rm /etc/systemd/system/enhance-log-mirror.service
sudo systemctl daemon-reload

# Remove application
sudo rm -rf /opt/enhance-log-mirror

# Remove lock file
sudo rm -f /var/run/enhance-mirror-logs.lock

# Optional: Remove logs
sudo rm -f /var/log/enhance-mirror.log
```

## Development

### Testing

Run the script manually for testing:

```bash
cd /opt/enhance-log-mirror
source venv/bin/activate
python app.py
```

Press Ctrl+C to stop.

### Debugging

Enable debug logging by editing `app.py`:

```python
logging.basicConfig(
    level=logging.DEBUG,  # Change from INFO to DEBUG
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
```

## Changelog

### Version 1.0.0

- Initial release
