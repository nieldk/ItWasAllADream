import concurrent.futures
import csv
import threading
import logging
import pathlib
import ipaddress
import argparse
from time import sleep
from decimal import Decimal
from getpass import getpass
from collections import defaultdict
from datetime import datetime
from itwasalladream import rprn_vector, par_vector

handler = logging.StreamHandler()
handler.setFormatter(
    logging.Formatter(
        style="{",
        fmt="[{name}] {levelname} - {message}"
    )
)

log = logging.getLogger("itwasalladream")
log.setLevel(logging.INFO)
log.addHandler(handler)

class AutomaticDriverEnumerationError(Exception):
    pass

def monitor_threadpool(pool, targets):
    log.debug('Started thread poller')

    while True:
        sleep(10)
        pool_size = pool._work_queue.qsize()
        log.info(pool_size)
        finished_threads = targets - pool_size
        percentage = Decimal(finished_threads) / Decimal(targets) * Decimal(100)
        log.info(f"completed: {percentage:.2f}% ({finished_threads}/{targets})")

def check(vector, username, password, domain, address, port, timeout):
    results = {
        "address": address,
        "protocol": vector.PROTOCOL,
        "vulnerable": False,
        "reason": ""
    }

    try:
        dce = vector.connect(
            username,
            password,
            domain,
            "",
            "",
            address,
            port,
            timeout
        )
    except Exception as e:
        log.debug(e)
        if str(e).find("ept_s_not_registered") != -1 or str(e).find("STATUS_OBJECT_NAME_NOT_FOUND") != -1:
            log.info(f"{address} is not vulnerable over {vector.PROTOCOL}. Reason: Print Spooler service is not running or inbound remote printing is disabled.")
            results["vulnerable"] = False
            results["Reason"] = "Response indicates the Print Spooler Service is not running or inbound remote printing is disabled"
        else:
            log.info(f"Unable to determine if {address} is vulnerable over {vector.PROTOCOL}. Reason: {e}")
            results["vulnerable"] = "Unknown"
            results["reason"] = str(e)

    else:
        local_ip = dce.get_rpc_transport().get_socket().getsockname()[0]
        share = f"\\\\{local_ip}\\itwasalladream\\bogus.dll"
        #share = f"\\\\192.168.3.1\\itwasalladream\\bogus.dll"

        try:
            blob = vector.getDrivers(dce)
            pDriverPath = str(pathlib.PureWindowsPath(blob['DriverPathArray']).parent) + '\\UNIDRV.DLL'
            if not ("FileRepository" in pDriverPath):
                log.error(f"pDriverPath {pDriverPath}, expected ':\\Windows\\System32\\DriverStore\\FileRepository\\...")
                raise AutomaticDriverEnumerationError
        except Exception as e:
            log.error(f"Failed to enumerate remote pDriverPath, unable to determine if host is vulnerable. Error: {e}")
            results["vulnerable"] = "unknown"
            results["reason"] = f"Unkown error while trying to automatically enumerate printer drivers: '{e}'"

        except AutomaticDriverEnumerationError as e:
            log.error("Failed to automatically enumerate printer drivers, unable to determine if host is vulnerable.")
            results["vulnerable"] = "unknown"
            results["reason"] = "Got unexpected value when trying to automatically enumerate printer drivers (this is necessary for the exploit to succeed)"

        else:
            log.debug(f"pDriverPath found: {pDriverPath}")
            log.debug(f"Attempting DLL execution {share}")
            for i in range(3, 0, -1):
                try:
                    vector.exploit(dce, pDriverPath, share)
                except Exception as e:
                    log.debug(e)
                    # Spooler Service attempted to grab the DLL, host is vulnerable
                    if str(e).find("ERROR_BAD_NETPATH") != -1:
                        log.info(f"{address} is vulnerable over {vector.PROTOCOL}. Reason: Host attempted to grab DLL from supplied share")
                        results["vulnerable"] = True
                        results["reason"] = "Host attempted to grab DLL from supplied share"
                        break

                    #elif str(e).find("ERROR_INVALID_HANDLE") != -1:
                    #    log.debug("Got invalid handle.. trying again")
                    #    continue

                    elif str(e).find("_access_denied") != -1:
                        log.info(f"{address} is not vulnerable over {vector.PROTOCOL}. Reason: RPC call returned access denied. This is usually an indication the host has been patched.")
                        results["vulnerable"] = False
                        results["reason"] = "RPC call returned access denied. This is usually an indication the host has been patched."
                        break

                    else:
                        log.info(f"Unable to determine if {address} is vulnerable over {vector.PROTOCOL}. Got unexpected response: {e}")
                        results["vulnerable"] = "Unknown"
                        results["reason"] = f"Unable to determine if host is vulnerable. Got unexpected response: {e}"
                        break
                else:
                    log.info(f"{address} is vulnerable over {vector.PROTOCOL}. Reason: Host copied the DLL you're hosting.")
                    results["vulnerable"] = True
                    results["reason"] = "Reason: Host copied the DLL you're hosting."
                    break

    return results

def main():
    parser = argparse.ArgumentParser(description="PrintNightmare (CVE-2021-34527) scanner", epilog="I used to read Word Up magazine!", formatter_class=argparse.ArgumentDefaultsHelpFormatter)
    parser.add_argument("-u", "--username", required=True, help="username to authenticate as")
    parser.add_argument("-p", "--password", help="password to authenticate as. If not specified will prompt.")
    parser.add_argument("-d", "--domain", required=True, help="domain to authenticate as")
    parser.add_argument("--timeout", default=30, type=int, help="Connection timeout in secods")
    parser.add_argument("--threads", default=100, type=int, help="Max concurrent threads")
    parser.add_argument("-v", "--verbose", action="store_true", help="Enable verbose output")
    parser.add_argument("target", help="Target subnet in CIDR notation")

    args = parser.parse_args()

    if args.verbose:
        log.setLevel(logging.DEBUG)

    if not args.password:
        args.password = getpass("Password:")

    port = "445"
    targets = ipaddress.ip_network(args.target)

    report_fields = ["address", "vulnerable", "exploitable_over_ms_rprn", "exploitable_over_ms_par", "reason_ms_rprn", "reason_ms_par"]
    scan_results = defaultdict(dict)

    time = datetime.now().strftime("%Y_%m_%d_%H%M%S")

    with open(f"report_{time}.csv", "w") as csvfile:
        writer = csv.DictWriter(csvfile, fieldnames=report_fields)
        writer.writeheader()

        with concurrent.futures.ThreadPoolExecutor(max_workers=args.threads) as ex:
            rprn_checks = {
                ex.submit(
                    check,
                    rprn_vector,
                    args.username,
                    args.password, 
                    args.domain,
                    str(address), 
                    port,
                    args.timeout
                ): str(address)
                for address in targets
            }

            par_checks = {
                ex.submit(
                    check,
                    par_vector,
                    args.username,
                    args.password,
                    args.domain,
                    str(address),
                    port,
                    args.timeout
                ): str(address)
                for address in targets
            }

            t = threading.Thread(target=monitor_threadpool, args=(ex, targets.num_addresses,))
            t.setDaemon(True)
            t.start()

            future_to_host = {**rprn_checks, **par_checks}
            for future in concurrent.futures.as_completed(future_to_host):
                host = future_to_host[future]

                try:
                    data = future.result()
                except Exception as e:
                    log.error(f"Check for {host} generated an exception: {e}")
                else:
                    scan_results[host]["address"] = host

                    if data["protocol"] == "MS-RPRN":
                        scan_results[host]["exploitable_over_ms_rprn"] = data["vulnerable"]
                        scan_results[host]["reason_ms_rprn"] = data["reason"]

                    elif data["protocol"] == "MS-PAR":
                        scan_results[host]["exploitable_over_ms_par"] = data["vulnerable"]
                        scan_results[host]["reason_ms_par"] = data["reason"]

        for _,v in scan_results.items():
            if (v["exploitable_over_ms_rprn"] == True) or (v["exploitable_over_ms_par"] == True):
                v["vulnerable"] = "Yes"
            elif v["exploitable_over_ms_rprn"] == "Unknown" or v["exploitable_over_ms_par"] == "Unknown":
                v["vulnerable"] = "Unknown"
            else:
                v["vulnerable"] = "No"

            writer.writerow(v)
