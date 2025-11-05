#!/usr/bin/env python3
import asyncio
import os
import aiofiles
import datetime
import sys
import fcntl
import logging
from watchdog.observers import Observer
from watchdog.events import FileSystemEventHandler

# Core Config
SOURCE_DIR = "/var/local/enhance/webserver_logs"
WWW_ROOT = "/var/www"
RETENTION_DAYS = 30 # Keep log files for 30 days
LOCK_FILE = "/var/run/enhance-mirror-logs.lock"
LOG_FILE = "/var/log/enhance-mirror.log"
MISSING_FILE_TIMEOUT = 300  # Stop tailer if file missing for 5 minutes (300 seconds)

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s - %(levelname)s - %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE),
        logging.StreamHandler()
    ]
)
logger = logging.getLogger(__name__)

lock_file = open(LOCK_FILE, "w")
try:
    fcntl.lockf(lock_file, fcntl.LOCK_EX | fcntl.LOCK_NB)
except OSError:
    logger.error("Another instance is already running. Exiting.")
    sys.exit(1)

class LogTailer:
    """
    Tails a log file that may be deleted, recreated, or truncated frequently.
    Keeps history of all changes and continues through file recreations.
    """
    def __init__(self, path, uuid):
        self.path = path
        self.uuid = uuid
        self.dest_dir = os.path.join(WWW_ROOT, uuid, "access-logs")
        os.makedirs(self.dest_dir, exist_ok=True)

        site_root = os.path.join(WWW_ROOT, uuid)
        try:
            stat_info = os.stat(site_root)
            self.uid = stat_info.st_uid
            self.gid = stat_info.st_gid
        except Exception as e:
            logger.warning(f"Could not get ownership for {site_root}, using current user: {e}")
            self.uid = os.getuid()
            self.gid = os.getgid()

        try:
            os.chown(self.dest_dir, self.uid, self.gid)
        except Exception as e:
            logger.warning(f"Could not chown directory {self.dest_dir}: {e}")

        self.current_date = datetime.date.today()
        self.position = 0
        self.inode = None
        self.running = True
        self.opened_files = set()
        self.last_line_fragment = b""
        
        self.bytes_written = 0
        self.file_recreations = 0
        self.truncations = 0

    async def run(self):
        """Main loop that continuously tails the log file."""
        logger.info(f"Starting tailer for {self.uuid} -> {self.dest_dir}")
        logger.debug(f"[{self.uuid}] Tailer starting, source: {self.path}")
        
        while self.running:
            try:
                await self._tail_file()
            except Exception as e:
                logger.error(f"Tailer for {self.uuid} crashed: {e}", exc_info=True)
                await asyncio.sleep(5)

    async def _tail_file(self):
        """
        Tail the log file, handling:
        - File deletion and recreation
        - File truncation
        - Inode changes
        - Permanent deletion (stops after 5 minutes)
        """
        while self.running:
            if not await self._wait_for_file():
                logger.error(f"[{self.uuid}] Stopping tailer - file permanently deleted")
                self.running = False
                break
            
            try:
                async with aiofiles.open(self.path, "rb") as f:
                    fd = f.fileno()
                    stat = os.fstat(fd)
                    inode = stat.st_ino
                    size = stat.st_size
                    
                    logger.debug(f"[{self.uuid}] Opened file, inode={inode}, size={size}, position={self.position}")
                    
                    if self.inode is not None and self.inode != inode:
                        logger.info(f"[{self.uuid}] File recreated (inode {self.inode} -> {inode})")
                        self.file_recreations += 1
                        self.position = 0
                        self.last_line_fragment = b""
                    elif size < self.position:
                        logger.info(f"[{self.uuid}] File truncated ({self.position} -> {size} bytes)")
                        self.truncations += 1
                        self.position = 0
                        self.last_line_fragment = b""
                    
                    self.inode = inode
                    
                    await f.seek(self.position)
                    logger.debug(f"[{self.uuid}] Seeking to position {self.position}")
                    
                    await self._read_loop(f, inode)
                    
            except FileNotFoundError:
                logger.debug(f"[{self.uuid}] File not found, waiting for recreation")
                await asyncio.sleep(1)
                continue
            except Exception as e:
                logger.error(f"[{self.uuid}] Error reading file: {e}", exc_info=True)
                await asyncio.sleep(2)
                continue

    async def _wait_for_file(self):
        """
        Wait for the log file to exist, with timeout.
        Returns True if file exists, False if timeout exceeded.
        Timeout is configured via MISSING_FILE_TIMEOUT (default: 5 minutes).
        """
        if os.path.exists(self.path):
            logger.debug(f"[{self.uuid}] File exists: {self.path}")
            return True
        
        logger.info(f"[{self.uuid}] Waiting for file {self.path} to be created")
        
        check_interval = 30
        max_wait_cycles = MISSING_FILE_TIMEOUT // check_interval
        
        for attempt in range(max_wait_cycles):
            for _ in range(check_interval):
                await asyncio.sleep(1)
                if os.path.exists(self.path):
                    logger.info(f"[{self.uuid}] File appeared: {self.path}")
                    return True
                
                if not self.running:
                    return False
            
            elapsed = (attempt + 1) * check_interval
            logger.warning(f"[{self.uuid}] File still missing after {elapsed}s: {self.path}")
        
        logger.error(f"[{self.uuid}] File permanently deleted after {MISSING_FILE_TIMEOUT}s, giving up")
        return False

    async def _read_loop(self, f, expected_inode):
        """
        Read data from file in a loop until:
        - File is deleted
        - File inode changes (recreated)
        - File is truncated
        - Tailer is stopped
        """
        chunks_read = 0
        while self.running:
            chunk = await f.read(8192)
            
            if chunk:
                chunks_read += 1
                logger.debug(f"[{self.uuid}] Read chunk #{chunks_read}, size={len(chunk)} bytes")
                await self._write_chunk(chunk)
                self.position += len(chunk)
                self.bytes_written += len(chunk)
            else:
                if chunks_read > 0:
                    logger.debug(f"[{self.uuid}] No more data after reading {chunks_read} chunks, waiting...")
                await asyncio.sleep(0.5)
                
                try:
                    stat = os.stat(self.path)
                    
                    if stat.st_ino != expected_inode:
                        logger.debug(f"[{self.uuid}] Inode changed, breaking read loop")
                        break
                    
                    if stat.st_size < self.position:
                        logger.debug(f"[{self.uuid}] File truncated, breaking read loop")
                        break
                    
                except FileNotFoundError:
                    logger.debug(f"[{self.uuid}] File deleted, breaking read loop")
                    self.inode = None
                    self.position = 0
                    break

    async def _write_chunk(self, chunk):
        """
        Write chunk to the destination log file.
        Handles partial lines by buffering them until complete.
        """
        data = self.last_line_fragment + chunk
        
        logger.debug(f"[{self.uuid}] _write_chunk called, chunk_size={len(chunk)}, "
                    f"fragment_size={len(self.last_line_fragment)}, total={len(data)}")
        
        if data and not data.endswith(b'\n'):
            last_newline = data.rfind(b'\n')
            if last_newline != -1:
                complete_data = data[:last_newline + 1]
                self.last_line_fragment = data[last_newline + 1:]
                logger.debug(f"[{self.uuid}] Partial line: writing {len(complete_data)} bytes, "
                           f"buffering {len(self.last_line_fragment)} bytes")
            else:
                self.last_line_fragment = data
                logger.debug(f"[{self.uuid}] No complete lines, buffering all {len(data)} bytes")
                return
        else:
            complete_data = data
            self.last_line_fragment = b""
            logger.debug(f"[{self.uuid}] Complete line(s), writing all {len(complete_data)} bytes")
        
        if not complete_data:
            logger.debug(f"[{self.uuid}] No complete data to write")
            return
        
        dest_file = os.path.join(self.dest_dir, f"{self.current_date}.log")
        logger.debug(f"[{self.uuid}] Writing to {dest_file}")
        
        try:
            async with aiofiles.open(dest_file, "ab") as out:
                fd = out.fileno()
                fcntl.flock(fd, fcntl.LOCK_EX)
                try:
                    await out.write(complete_data)
                    logger.debug(f"[{self.uuid}] Successfully wrote {len(complete_data)} bytes")
                finally:
                    fcntl.flock(fd, fcntl.LOCK_UN)
            
            if dest_file not in self.opened_files:
                try:
                    await asyncio.to_thread(os.chown, dest_file, self.uid, self.gid)
                    self.opened_files.add(dest_file)
                    logger.debug(f"[{self.uuid}] Chowned {dest_file} to {self.uid}:{self.gid}")
                except Exception as e:
                    logger.warning(f"Could not chown {dest_file}: {e}")
                    
        except Exception as e:
            logger.error(f"[{self.uuid}] Failed to write to {dest_file}: {e}", exc_info=True)

    def rotate(self, new_date):
        """Rotate to a new date's log file."""
        logger.info(f"[{self.uuid}] Rotating from {self.current_date} to {new_date}")
        self.current_date = new_date
        
    def stop(self):
        """Stop the tailer gracefully."""
        logger.info(f"[{self.uuid}] Stopping tailer (bytes written: {self.bytes_written}, "
                   f"recreations: {self.file_recreations}, truncations: {self.truncations})")
        self.running = False


class EnhanceWatcher(FileSystemEventHandler):
    """Watches for log file creation and manages tailers."""
    
    def __init__(self, loop=None):
        self.tailers = {}
        self.tasks = {}
        self.loop = loop

    def on_created(self, event):
        """Handle new log file creation."""
        if event.is_directory or not event.src_path.endswith(".log"):
            return
        
        uuid = os.path.basename(event.src_path).replace(".log", "")
        
        if uuid not in self.tailers:
            logger.info(f"Watchdog detected new log file: {uuid}")
            self._start_tailer(uuid, event.src_path)

    def on_modified(self, event):
        """Handle file modification - might catch files we missed on creation."""
        if event.is_directory or not event.src_path.endswith(".log"):
            return
        
        uuid = os.path.basename(event.src_path).replace(".log", "")
        
        if uuid not in self.tailers:
            logger.info(f"Watchdog detected modified log file (starting tailer): {uuid}")
            self._start_tailer(uuid, event.src_path)

    def on_deleted(self, event):
        """Handle log file deletion - but don't stop tailer, it will wait for recreation."""
        if event.is_directory or not event.src_path.endswith(".log"):
            return
        
        uuid = os.path.basename(event.src_path).replace(".log", "")
        logger.debug(f"Log file deleted: {uuid} (tailer will continue)")

    def _start_tailer(self, uuid, path):
        """Start a new tailer for a UUID. Thread-safe."""
        tailer = LogTailer(path, uuid)
        self.tailers[uuid] = tailer
        
        if self.loop:
            future = asyncio.run_coroutine_threadsafe(tailer.run(), self.loop)
            self.tasks[uuid] = future
            logger.debug(f"Scheduled task for {uuid} via run_coroutine_threadsafe")
        else:
            try:
                task = asyncio.create_task(tailer.run())
                self.tasks[uuid] = task
                logger.debug(f"Created task for {uuid} via create_task")
            except RuntimeError as e:
                logger.error(f"Cannot create task for {uuid}: {e}")

    def stop_all(self):
        """Stop all tailers gracefully."""
        logger.info("Stopping all tailers...")
        for tailer in self.tailers.values():
            tailer.stop()


async def rotate_logs(watcher):
    """Rotate all logs at midnight."""
    while True:
        now = datetime.datetime.now()
        next_midnight = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=0, second=0, microsecond=0
        )
        sleep_seconds = (next_midnight - now).total_seconds()
        
        await asyncio.sleep(sleep_seconds)
        
        new_date = next_midnight.date()
        logger.info(f"Rotating all logs to {new_date}")
        
        for tailer in watcher.tailers.values():
            tailer.rotate(new_date)


async def cleanup_logs(watcher):
    """Clean up old log files daily."""
    while True:
        now = datetime.datetime.now()
        next_clean = (now + datetime.timedelta(days=1)).replace(
            hour=0, minute=30, second=0, microsecond=0
        )
        
        if next_clean <= now:
            next_clean += datetime.timedelta(days=1)
        
        sleep_seconds = (next_clean - now).total_seconds()
        await asyncio.sleep(sleep_seconds)

        cutoff = datetime.datetime.now() - datetime.timedelta(days=RETENTION_DAYS)
        logger.info(f"Cleaning up logs older than {RETENTION_DAYS} days (before {cutoff.date()})")
        
        deleted_count = 0
        
        try:
            for uuid_dir in os.listdir(WWW_ROOT):
                access_logs_dir = os.path.join(WWW_ROOT, uuid_dir, "access-logs")
                
                if not os.path.isdir(access_logs_dir):
                    continue
                
                try:
                    for filename in os.listdir(access_logs_dir):
                        fpath = os.path.join(access_logs_dir, filename)
                        
                        if not os.path.isfile(fpath):
                            continue
                        
                        try:
                            if os.path.getmtime(fpath) < cutoff.timestamp():
                                await asyncio.to_thread(os.remove, fpath)
                                deleted_count += 1
                                logger.debug(f"Deleted old log: {fpath}")
                        except FileNotFoundError:
                            pass
                        except Exception as e:
                            logger.warning(f"Could not delete {fpath}: {e}")
                            
                except PermissionError as e:
                    logger.warning(f"Cannot access {access_logs_dir}: {e}")
                    
        except Exception as e:
            logger.error(f"Error during cleanup: {e}", exc_info=True)
        
        logger.info(f"Cleanup complete: deleted {deleted_count} old log files")


async def periodic_scanner(watcher):
    """
    Periodically scan for new log files that watchdog might have missed.
    This is a backup mechanism in case filesystem events are not triggered.
    """
    await asyncio.sleep(10)
    
    while True:
        try:
            existing_files = set()
            
            if os.path.isdir(SOURCE_DIR):
                for filename in os.listdir(SOURCE_DIR):
                    if filename.endswith(".log"):
                        uuid = filename.replace(".log", "")
                        existing_files.add(uuid)
                        
                        if uuid not in watcher.tailers:
                            path = os.path.join(SOURCE_DIR, filename)
                            logger.info(f"Periodic scan detected new log file: {uuid}")
                            watcher._start_tailer(uuid, path)
            
        except Exception as e:
            logger.error(f"Error in periodic scanner: {e}", exc_info=True)
        
        await asyncio.sleep(30)


async def stats_reporter(watcher):
    """Report statistics periodically."""
    while True:
        await asyncio.sleep(300)
        
        total_bytes = sum(t.bytes_written for t in watcher.tailers.values())
        total_recreations = sum(t.file_recreations for t in watcher.tailers.values())
        total_truncations = sum(t.truncations for t in watcher.tailers.values())
        
        logger.info(f"Stats: {len(watcher.tailers)} active tailers, "
                   f"{total_bytes / 1024 / 1024:.2f} MB written, "
                   f"{total_recreations} recreations, {total_truncations} truncations")


def validate_config():
    """Validate configuration before starting."""
    if not os.path.isdir(SOURCE_DIR):
        raise ValueError(f"SOURCE_DIR does not exist: {SOURCE_DIR}")
    if not os.path.isdir(WWW_ROOT):
        raise ValueError(f"WWW_ROOT does not exist: {WWW_ROOT}")
    if not os.access(SOURCE_DIR, os.R_OK):
        raise PermissionError(f"Cannot read SOURCE_DIR: {SOURCE_DIR}")
    if not os.access(WWW_ROOT, os.W_OK):
        raise PermissionError(f"Cannot write to WWW_ROOT: {WWW_ROOT}")
    
    logger.info("Configuration validated successfully")


async def main():
    """Main entry point."""
    logger.info("=" * 60)
    logger.info("Enhance Log Mirror starting")
    logger.info(f"Source: {SOURCE_DIR}")
    logger.info(f"Destination: {WWW_ROOT}")
    logger.info(f"Retention: {RETENTION_DAYS} days")
    logger.info("=" * 60)
    
    validate_config()
    
    loop = asyncio.get_running_loop()
    
    watcher = EnhanceWatcher(loop=loop)

    try:
        for filename in os.listdir(SOURCE_DIR):
            if filename.endswith(".log"):
                path = os.path.join(SOURCE_DIR, filename)
                uuid = filename.replace(".log", "")
                logger.info(f"Starting tailer for existing log: {uuid}")
                watcher._start_tailer(uuid, path)
    except Exception as e:
        logger.error(f"Error scanning source directory: {e}", exc_info=True)

    observer = Observer()
    observer.schedule(watcher, SOURCE_DIR, recursive=False)
    observer.start()
    logger.info("Filesystem observer started")

    tasks = [
        asyncio.create_task(rotate_logs(watcher), name="rotate"),
        asyncio.create_task(cleanup_logs(watcher), name="cleanup"),
        asyncio.create_task(stats_reporter(watcher), name="stats"),
        asyncio.create_task(periodic_scanner(watcher), name="scanner"),
    ]

    try:
        await asyncio.gather(*tasks)
    except asyncio.CancelledError:
        logger.info("Received cancellation signal")
    finally:
        observer.stop()
        observer.join()
        watcher.stop_all()
        
        await asyncio.sleep(2)
        
        for task in tasks:
            task.cancel()
        
        logger.info("Shutdown complete")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        logger.info("Received KeyboardInterrupt, shutting down...")
    except Exception as e:
        logger.critical(f"Fatal error: {e}", exc_info=True)
        sys.exit(1)
