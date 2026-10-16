"""``emulators`` command line.

emulators serve                    # HTTP gateway :44080, HTTPS gateway :44443, per-provider ports
emulators serve --providers microsoft,zoom --no-provider-ports
emulators list                     # providers, ports and hostnames
emulators hosts --ip 127.0.0.1     # hosts-file lines for hostname routing
emulators certs --dir ./certs      # local CA + server certificate for the HTTPS gateway
emulators info stripe              # test credentials, actions, seeded collection sizes
emulators world people             # the synthetic organisation, for test authors
"""

from __future__ import annotations

import argparse
import asyncio
import contextlib
import json
import os
import sys
from dataclasses import asdict, is_dataclass
from pathlib import Path
from typing import Any

DEFAULT_PORT = 44080
DEFAULT_HTTPS_PORT = 44443


def _env_int(name: str, default: int) -> int:
    value = os.environ.get(name)
    return int(value) if value else default


def _hub(args: argparse.Namespace) -> Any:
    from emulators.core.hub import EmulatorHub

    providers = [p.strip() for p in args.providers.split(",")] if getattr(args, "providers", None) else None
    return EmulatorHub(
        providers=providers,
        seed=getattr(args, "seed", None),
        anchor=getattr(args, "anchor", None),
        webhook_mode=getattr(args, "webhook_mode", None),
    )


def _all_hosts(hub: Any) -> list[str]:
    return sorted({h for provider in hub.providers.values() for h in provider.hosts})


def cmd_serve(args: argparse.Namespace) -> int:
    import uvicorn

    from emulators.core.tls import concrete_hosts, ensure_certs

    hub = _hub(args)

    class Server(uvicorn.Server):
        @contextlib.contextmanager
        def capture_signals(self):  # type: ignore[override]
            yield

    servers: list[Server] = []
    lines: list[str] = []

    def add(app: Any, port: int, label: str, **ssl: Any) -> None:
        config = uvicorn.Config(
            app,
            host=args.host,
            port=port,
            log_level=args.log_level,
            lifespan="off",
            access_log=args.access_log,
            proxy_headers=False,
            **ssl,
        )
        servers.append(Server(config))
        scheme = "https" if ssl else "http"
        lines.append(
            f"  {label:<22} {scheme}://{'localhost' if args.host in ('0.0.0.0', '::') else args.host}:{port}"
        )

    add(hub.gateway, args.port, "gateway (http)")
    if args.https_port:
        cert_dir = Path(args.certs_dir)
        paths = ensure_certs(cert_dir, [*_all_hosts(hub), *concrete_hosts(_all_hosts(hub))])
        add(
            hub.gateway,
            args.https_port,
            "gateway (https)",
            ssl_certfile=str(paths.server_cert),
            ssl_keyfile=str(paths.server_key),
        )
        lines.append(f"  {'CA bundle':<22} {paths.ca_bundle}")
    if args.provider_ports:
        for name, provider in hub.providers.items():
            if provider.default_port:
                add(hub.apps[name], provider.default_port, name)

    print(
        f"emulators — seed={hub.seed!r} anchor={hub.world.anchor.date()} providers={','.join(hub.providers)}"
    )
    print("\n".join(lines))
    print("  control plane          GET /_emulator  ·  POST /_emulator/reset  ·  /<provider>/_emulator/docs")
    sys.stdout.flush()

    async def run() -> None:
        await asyncio.gather(*(s.serve() for s in servers))

    try:
        asyncio.run(run())
    except KeyboardInterrupt:
        for s in servers:
            s.should_exit = True
    return 0


def cmd_list(args: argparse.Namespace) -> int:
    from emulators.core.hub import discover_providers

    providers = discover_providers()
    width = max((len(n) for n in providers), default=8)
    for name, provider in providers.items():
        print(f"{name:<{width}}  :{provider.default_port:<6} {provider.title}")
        print(f"{'':<{width}}          hosts: {', '.join(provider.hosts)}")
    return 0


def cmd_hosts(args: argparse.Namespace) -> int:
    from emulators.core.hub import discover_providers
    from emulators.core.tls import concrete_hosts

    hosts = concrete_hosts(sorted({h for p in discover_providers().values() for h in p.hosts}))
    print("# emulators — add to /etc/hosts or C:\\Windows\\System32\\drivers\\etc\\hosts")
    for host in hosts:
        print(f"{args.ip}\t{host}")
    return 0


def cmd_certs(args: argparse.Namespace) -> int:
    from emulators.core.hub import discover_providers
    from emulators.core.tls import concrete_hosts, ensure_certs

    patterns = sorted({h for p in discover_providers().values() for h in p.hosts})
    paths = ensure_certs(Path(args.dir), [*patterns, *concrete_hosts(patterns)])
    print(f"CA certificate     {paths.ca_cert}")
    print(f"server certificate {paths.server_cert}")
    print(f"CA bundle          {paths.ca_bundle}   (system roots + emulator CA)")
    print("\nTrust it in clients, e.g.:")
    print(f"  export SSL_CERT_FILE={paths.ca_bundle}")
    print(f"  export REQUESTS_CA_BUNDLE={paths.ca_bundle}")
    print(f"  export NODE_EXTRA_CA_CERTS={paths.ca_cert}")
    return 0


def _jsonable(value: Any) -> Any:
    if is_dataclass(value):
        return {k: _jsonable(v) for k, v in asdict(value).items()}  # type: ignore[arg-type]
    if isinstance(value, (list, tuple)):
        return [_jsonable(v) for v in value]
    if isinstance(value, dict):
        return {k: _jsonable(v) for k, v in value.items()}
    if hasattr(value, "isoformat"):
        return value.isoformat()
    return value


def cmd_info(args: argparse.Namespace) -> int:
    hub = _hub(argparse.Namespace(providers=args.provider, seed=None, anchor=None, webhook_mode=None))
    info = {name: ctx.provider.info(ctx) for name, ctx in hub.contexts.items()}
    print(json.dumps(info, indent=2, default=str))
    return 0


def cmd_world(args: argparse.Namespace) -> int:
    from datetime import UTC, datetime

    from emulators.core.clock import parse_instant
    from emulators.world.generator import build_world

    anchor = parse_instant(args.anchor) if args.anchor else datetime.now(UTC)
    world = build_world(args.seed or os.environ.get("EMULATORS_SEED", "emulators"), anchor)
    section = args.section
    if section is None:
        print(f"{world.org.name} ({world.org.domain}) — seed {world.seed!r}, anchor {world.anchor.date()}")
        for name in (
            "people",
            "groups",
            "teams",
            "clients",
            "vendors",
            "engagements",
            "spaces",
            "documents",
            "meetings",
            "email_threads",
            "invoices",
            "bills",
            "deals",
            "leads",
            "conversations",
        ):
            print(f"  {name:<14} {len(getattr(world, name))}")
        print("\nids: " + json.dumps(_jsonable(world.org), indent=2))
        return 0
    items = getattr(world, section, None)
    if items is None:
        print(f"unknown section {section!r}", file=sys.stderr)
        return 2
    if args.json:
        print(json.dumps(_jsonable(items), indent=2, default=str))
    elif section == "people":
        for p in items:
            kind = "external" if p.is_external else ("departed" if not p.active else p.department_id)
            print(f"{p.id:<8} {p.display_name:<24} {p.email:<46} {p.job_title:<40} {kind}")
    else:
        for item in items:
            label = (
                getattr(item, "name", None) or getattr(item, "title", None) or getattr(item, "subject", "")
            )
            print(f"{item.id:<12} {label}")
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="emulators", description="High-fidelity SaaS API emulators.")
    sub = parser.add_subparsers(dest="command", required=True)

    serve = sub.add_parser("serve", help="run the emulators")
    serve.add_argument(
        "--providers",
        default=os.environ.get("EMULATORS_PROVIDERS"),
        help="comma-separated provider names (default: all)",
    )
    serve.add_argument("--host", default=os.environ.get("EMULATORS_HOST", "127.0.0.1"))
    serve.add_argument("--port", type=int, default=_env_int("EMULATORS_PORT", DEFAULT_PORT))
    serve.add_argument(
        "--https-port",
        type=int,
        default=_env_int("EMULATORS_HTTPS_PORT", DEFAULT_HTTPS_PORT),
        help="0 disables the HTTPS gateway",
    )
    serve.add_argument("--no-https", dest="https_port", action="store_const", const=0)
    serve.add_argument(
        "--provider-ports",
        dest="provider_ports",
        action="store_true",
        default=os.environ.get("EMULATORS_PROVIDER_PORTS", "1") != "0",
    )
    serve.add_argument("--no-provider-ports", dest="provider_ports", action="store_false")
    serve.add_argument("--certs-dir", default=os.environ.get("EMULATORS_CERTS_DIR", ".emulators/certs"))
    serve.add_argument("--seed", default=None)
    serve.add_argument("--anchor", default=None, help="world anchor date, e.g. 2026-10-01")
    serve.add_argument("--webhook-mode", choices=["async", "sync", "manual"], default=None)
    serve.add_argument("--log-level", default=os.environ.get("EMULATORS_LOG_LEVEL", "warning"))
    serve.add_argument("--access-log", action="store_true")
    serve.set_defaults(func=cmd_serve)

    sub.add_parser("list", help="list providers").set_defaults(func=cmd_list)

    hosts = sub.add_parser("hosts", help="print hosts-file lines")
    hosts.add_argument("--ip", default="127.0.0.1")
    hosts.set_defaults(func=cmd_hosts)

    certs = sub.add_parser("certs", help="create the local CA and gateway certificate")
    certs.add_argument("--dir", default=os.environ.get("EMULATORS_CERTS_DIR", ".emulators/certs"))
    certs.set_defaults(func=cmd_certs)

    info = sub.add_parser("info", help="credentials, actions and seeded data per provider")
    info.add_argument("provider", nargs="?", default=None)
    info.set_defaults(func=cmd_info)

    world = sub.add_parser("world", help="inspect the synthetic organisation")
    world.add_argument("section", nargs="?", default=None, help="people, clients, meetings, documents, ...")
    world.add_argument("--json", action="store_true")
    world.add_argument("--seed", default=None)
    world.add_argument("--anchor", default=os.environ.get("EMULATORS_ANCHOR"))
    world.set_defaults(func=cmd_world)
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    return int(args.func(args) or 0)


if __name__ == "__main__":
    raise SystemExit(main())
