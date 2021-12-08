import copy
import datetime
import sys
import os
import time
import argparse
import traceback

import pytz
import syncthing
from influxdb import InfluxDBClient
import yaml
from yaml2dataclass import Schema, SchemaPath

from typing import Optional, Dict, Type, List
from dataclasses import dataclass, asdict, field


@dataclass
class SyncthingConfiguration(Schema):
    name: str
    api_key: str
    host: str = 'localhost'
    port: int = field(default=8384)
    timeout: float = field(default=10.0)
    is_https: bool = field(default=False)
    ssl_cert_file: Optional[str] = field(default=None)
    tags: Optional[List[str]] = field(default_factory=lambda: [])

    def get_client_params(self):
        result = asdict(self)
        if "name" in result:
            del result["name"]
        if "tags" in result:
            del result["tags"]
        return result


@dataclass
class InfluxDbConfiguration(Schema):
    host: str
    port: int  # Common ports: 443
    ssl: bool
    verify_ssl: bool
    database: str
    username: str
    password: str

    def get_client_params(self):
        result = asdict(self)
        if "tags" in result:
            del result["tags"]
        return result


@dataclass
class MeasurementConfiguration(Schema):
    devices: str
    folders: str


@dataclass
class AppConfiguration(Schema):
    syncthings: Dict[str, SyncthingConfiguration]
    influxes: Dict[str, InfluxDbConfiguration]
    measurements: MeasurementConfiguration

    @classmethod
    def _load_dict(cls, props_dict, dest_cls: Type[Schema], add_name: bool = False):
        result = {}
        for name, value in props_dict.items():
            arguments = {}
            arguments.update(value)
            if add_name:
                arguments["name"] = name
            result[name] = dest_cls.scm_load_from_dict(arguments)
        return result

    @classmethod
    def scm_convert(cls, values: dict, path: SchemaPath):
        values["syncthings"] = cls._load_dict(values["syncthings"], SyncthingConfiguration, True)
        values["influxes"] = cls._load_dict(values["influxes"], InfluxDbConfiguration)
        return values


def load_app_config(stream) -> AppConfiguration:
    """Load application configuration from a stream."""
    obj = yaml.safe_load(stream)
    return AppConfiguration.scm_load_from_dict(obj)


def error(message: str):
    sys.stderr.write("\nerror: " + message + "\n")
    sys.stderr.flush()
    raise SystemExit(-1)


def info(*values):
    if not args.silent:
        print(*values)


def main():
    # Collect data
    points = []
    for sync in config.syncthings.values():
        info("    Connect syncthing %s" % sync.name)
        proto_tags = {"cfg_name": sync.name}
        if sync.tags:
            proto_tags.update(sync.tags)

        conn_args = sync.get_client_params()
        q_started = time.time()

        conn = syncthing.Syncthing(**conn_args)
        now = datetime.datetime.now(tz=pytz.UTC)
        sync_cfg = conn.system.config()
        # My own device id
        my_device = sync_cfg["defaults"]["folder"]["devices"][0]
        my_id = my_device["deviceID"]
        proto_tags["my_id"] = my_id
        # Collect device stats
        device_stats = conn.stats.device()
        # List all remote devices
        remote_devices = []
        for device in sync_cfg["devices"]:
            device_id = device["deviceID"]
            if device_id == my_id:
                proto_tags["my_name"] = device["name"]
            else:
                stats = device_stats[device_id]
                last_seen = syncthing.parse_datetime(stats["lastSeen"])
                last_seen_since = now - last_seen
                remote_devices.append({
                    "tags": {
                        "id": device["deviceID"],  # Device ID
                        "name": device["name"],  # Device Name
                    },
                    "fields": {
                        "last_seen_since_sec": last_seen_since.total_seconds(),  # Number of seconds last seen
                    }
                })
        # Folders
        folders = []
        for folder in sync_cfg["folders"]:
            # Get completion for my own device
            completion = conn.database.completion(my_id, folder["id"])
            folders.append({
                "tags": {"id": folder["id"], "label": folder["label"], "path": folder["path"]},
                "fields": {"completion": completion},
            })
        q_elapsed = time.time() - q_started
        proto_fields = {"q_elapsed": q_elapsed}

        # Create data points for devices
        for device in remote_devices:
            tags = copy.copy(proto_tags)
            tags.update(device["tags"])
            fields = copy.copy(proto_fields)
            fields.update(device["fields"])
            point = dict(measurement=config.measurements.devices, tags=tags, fields=fields)
            points.append(point)

        # Create points for folders
        for folder in folders:
            tags = copy.copy(proto_tags)
            tags.update(folder["tags"])
            fields = copy.copy(proto_fields)
            fields.update(folder["fields"])
            point = dict(measurement=config.measurements.folders, tags=tags, fields=fields)
            points.append(point)

    if not points:
        return

    for influx_name, influx in config.influxes.items():
        info("    Sending %d point(s) to influxdb %s" % (len(points), influx_name))
        try:
            influx = config.influxes[influx_name]
            client = InfluxDBClient(**asdict(influx))
            client.write_points(points)
        except:
            if args.halt_on_send_error:
                raise
            else:
                traceback.print_exc(file=sys.stderr)


parser = argparse.ArgumentParser(description='Monitor your Syncthing instances with influxdb.')

parser.add_argument('-c', "--config", dest="config", default=None,
                    help="Configuration file for application. Default is syncflux.yml. "
                         "See syncflux_example.yml for an example.")
parser.add_argument("--config-dir", dest="config_dir", default=None,
                    help="Configuration directory. All config files with .yml extension will be processed one by one.")
parser.add_argument('-n', "--count", dest="count", default=1, type=int,
                    help="Number of test runs. Default is one. Use -1 to run indefinitely.")
parser.add_argument('-w', "--wait", dest="wait", default=60, type=float,
                    help="Number of seconds between test runs.")
parser.add_argument("-s", "--silent", dest='silent', action="store_true", default=False,
                    help="Supress all messages except errors.")
parser.add_argument("-v", "--verbose", dest='verbose', action="store_true", default=False,
                    help="Be verbose."
                    )
parser.add_argument("--halt-on-send-error", dest="halt_on_send_error", default=False, action="store_true",
                    help="Halt when cannot send data to influxdb. The default is to ignore the error.")

args = parser.parse_args()
if args.silent and args.verbose:
    parser.error("Cannot use --silent and --verbose at the same time.")
if args.config is None:
    args.config = "syncflux.yml"
if (args.config is not None) and (args.config_dir is not None):
    parser.error("You must give either --config or --config-dir (exactly one of them)")

if args.count == 0:
    parser.error("Test run count cannot be zero.")

if args.wait <= 0:
    parser.error("Wait time must be positive.")

if args.config:
    config_files = [args.config]
else:
    config_files = []
    for file_name in sorted(os.listdir(args.config_dir)):
        ext = os.path.splitext(file_name)[1]
        if ext.lower() == ".yml":
            fpath = os.path.join(args.config_dir, file_name)
            config_files.append(fpath)

index = 0
while args.count < 0 or index < args.count:

    if args.count != 1:
        info("Pass #%d started" % (index + 1))

    started = time.time()
    for config_file in config_files:
        if not os.path.isfile(config_file):
            parser.error("Cannot open %s" % config_file)
        config = load_app_config(open(config_file, "r"))
        main()
    elapsed = time.time() - started

    index += 1

    last_one = (args.count > 0) and (index == args.count)
    if not last_one:
        remaining = args.wait - elapsed
        if remaining > 0:
            if not args.silent:
                info("Pass #%d elapsed %.2f sec, waiting %.2f sec for next." % (index, elapsed, remaining))
            time.sleep(args.wait)
    else:
        info("Pass #%d elapsed %.2f sec" % (index, elapsed))

    info("")
