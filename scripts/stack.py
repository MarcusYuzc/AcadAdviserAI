#!/usr/bin/env python3
"""Run the AcadAdviserAI stack on this Mac, using only loopback ports.

  stack.py up        Start databases and services; restart any service that crashes.
  stack.py status    Show service health.
  stack.py doctor    Check readiness for public traffic.
  stack.py caddy     Run the public HTTPS entry point.

The 90xx ports and a separate PSA data directory keep public traffic away from the
development services on 8000-8003 and from private evaluation data.
"""
from __future__ import annotations

import argparse
import os
import shutil
import signal
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
HOME = ROOT.parent
LOGS = ROOT / "logs"
DATA = ROOT / "data"
INTERNAL_DB_PORTS = (5432, 5433)


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.is_file():
        for line in env_file.read_text().splitlines():
            line = line.strip()
            if line and not line.startswith("#") and "=" in line:
                key, value = line.split("=", 1)
                values[key.strip()] = value.strip()
    return values


def services(env: dict[str, str]) -> list[dict]:
    psa_data = DATA / "psa"
    return [
        {"name": "student-normalizer", "repo": HOME / "StudentNormalizer", "port": 9000,
         "app": "student_normalizer.api:app", "env": {}},
        {"name": "school-info-processor", "repo": HOME / "SchoolInfoProcessor", "port": 9001,
         "app": "school_info_processor.api:app", "env": {}},
        {"name": "program-search-agent", "repo": HOME / "ProgramSearchAgent", "port": 9003,
         "app": "program_search_agent.api:app", "env": {
             "OPENAI_AUTH_MODE": env.get("PSA_AUTH_MODE", "api_key"),
             "STUDENT_NORMALIZER_URL": "http://127.0.0.1:9000",
             "PROGRAM_SEARCH_AGENT_DATA_DIR": str(psa_data),
             "TELEMETRY_DIR": str(psa_data / "telemetry"),
         }},
        {"name": "gateway", "repo": ROOT, "port": 9080, "app": "acadadviser.app:app", "env": {}},
    ]


def healthy(port: int) -> bool:
    try:
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=3) as response:
            return response.status == 200
    except (urllib.error.URLError, OSError):
        return False


def spawn(service: dict) -> subprocess.Popen:
    LOGS.mkdir(exist_ok=True)
    log = open(LOGS / f"{service['name']}.log", "ab")
    return subprocess.Popen(
        [str(service["repo"] / ".venv/bin/uvicorn"), service["app"],
         "--host", "127.0.0.1", "--port", str(service["port"]), "--no-server-header"],
        cwd=service["repo"], env={**os.environ, **service["env"]}, stdout=log, stderr=log,
    )


def compose_up() -> None:
    for repo in ("StudentNormalizer", "SchoolInfoProcessor"):
        subprocess.run(["docker", "compose", "up", "-d", "--wait"], cwd=HOME / repo, check=True)


def up(_args: argparse.Namespace) -> int:
    env = read_env()
    compose_up()
    (DATA / "psa").mkdir(parents=True, exist_ok=True)
    # -s holds off system sleep on AC power. It cannot stop lid-close sleep.
    caffeinate = subprocess.Popen(["caffeinate", "-s", "-w", str(os.getpid())])
    running = {service["name"]: (service, spawn(service)) for service in services(env)}

    def stop(*_: object) -> None:
        for _service, process in running.values():
            process.terminate()
        caffeinate.terminate()
        sys.exit(0)

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    print("Stack starting. Logs are in", LOGS, flush=True)
    while True:
        time.sleep(5)
        for name, (service, process) in list(running.items()):
            if process.poll() is not None:
                print(f"{name} exited with {process.returncode}; restarting", flush=True)
                time.sleep(5)
                running[name] = (service, spawn(service))


def status(_args: argparse.Namespace) -> int:
    code = 0
    for service in services(read_env()):
        ok = healthy(service["port"])
        code = code or (0 if ok else 1)
        print(f"{'up  ' if ok else 'DOWN'}  {service['name']}  127.0.0.1:{service['port']}")
    return code


def wildcard_listeners(port: int) -> bool:
    result = subprocess.run(
        ["lsof", "-nP", f"-iTCP:{port}", "-sTCP:LISTEN"], capture_output=True, text=True
    )
    return any(f"*:{port}" in line or f"0.0.0.0:{port}" in line for line in result.stdout.splitlines())


def env_has_value(path: Path, key: str) -> bool:
    if not path.is_file():
        return False
    return any(
        line.startswith(f"{key}=") and line.split("=", 1)[1].strip()
        for line in path.read_text().splitlines()
    )


def resolves_to_this_network(domain: str) -> tuple[bool, str]:
    try:
        addresses = {info[4][0] for info in socket.getaddrinfo(domain, 443, socket.AF_INET)}
    except OSError:
        return False, "no DNS address"
    try:
        with urllib.request.urlopen("https://api.ipify.org", timeout=5) as response:
            public_ip = response.read().decode().strip()
    except (urllib.error.URLError, OSError):
        return False, f"resolves to {sorted(addresses)}; could not read this network's public IP"
    return public_ip in addresses, f"DNS {sorted(addresses)}, this network {public_ip}"


def doctor(_args: argparse.Namespace) -> int:
    env = read_env()
    domain = env.get("SITE_DOMAIN", "")
    checks: list[tuple[bool, str, str]] = []

    def check(ok: bool, label: str, fix: str = "") -> None:
        checks.append((ok, label, fix))

    for service in services(env):
        check(healthy(service["port"]), f"{service['name']} is healthy", "run: scripts/stack.py up")
    check(bool(domain), "SITE_DOMAIN is set", "set it in .env")
    check(bool(env.get("CONTACT_EMAIL")), "CONTACT_EMAIL is set", "set it in .env")
    check(env.get("COOKIE_SECURE", "true").lower() == "true", "COOKIE_SECURE is true", "set it in .env")
    check(env.get("PSA_AUTH_MODE", "api_key") == "api_key", "PSA uses a metered API key",
          "a personal ChatGPT plan must not serve the public; set PSA_AUTH_MODE=api_key")
    check(env_has_value(HOME / "ProgramSearchAgent/.env", "OPENAI_API_KEY"), "PSA has an OpenAI API key",
          "add OPENAI_API_KEY to ProgramSearchAgent/.env")
    for port in INTERNAL_DB_PORTS:
        check(not wildcard_listeners(port), f"Postgres {port} is not open to the network",
              f'in docker-compose.yml bind it to "127.0.0.1:{port}:5432", then docker compose up -d')
    check(shutil.which("caddy") is not None, "Caddy is installed", "run: brew install caddy")
    if domain:
        ok, detail = resolves_to_this_network(domain)
        check(ok, f"{domain} points at this network ({detail})", "set the DNS A records at the registrar")

    for ok, label, fix in checks:
        print(f"{'ok  ' if ok else 'FAIL'}  {label}" + (f"\n      -> {fix}" if fix and not ok else ""))
    return 0 if all(ok for ok, _, _ in checks) else 1


def caddy(_args: argparse.Namespace) -> int:
    env = read_env()
    LOGS.mkdir(exist_ok=True)
    os.execvpe("caddy", ["caddy", "run", "--config", str(ROOT / "deploy/Caddyfile"), "--adapter", "caddyfile"], {
        **os.environ,
        "SITE_DOMAIN": env["SITE_DOMAIN"],
        "ACME_EMAIL": env["CONTACT_EMAIL"],
        "AA_LOG_DIR": str(LOGS),
    })
    return 1


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    commands = parser.add_subparsers(required=True)
    for name, handler in (("up", up), ("status", status), ("doctor", doctor), ("caddy", caddy)):
        commands.add_parser(name).set_defaults(handler=handler)
    args = parser.parse_args()
    return args.handler(args)


if __name__ == "__main__":
    sys.exit(main())
