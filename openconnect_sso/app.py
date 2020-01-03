import asyncio
import getpass
import json
import logging
import os
import signal
from pathlib import Path

import structlog
from prompt_toolkit import HTML
from prompt_toolkit.eventloop import use_asyncio_event_loop
from prompt_toolkit.shortcuts import radiolist_dialog

from openconnect_sso import config
from openconnect_sso.authenticator import Authenticator, AuthResponseError
from openconnect_sso.browser import Terminated
from openconnect_sso.config import Credentials
from openconnect_sso.profile import get_profiles

from requests.exceptions import HTTPError

logger = structlog.get_logger()


def run(args):
    configure_logger(logging.getLogger(), args.log_level)
    loop = asyncio.get_event_loop()
    use_asyncio_event_loop(loop)

    try:
        return asyncio.get_event_loop().run_until_complete(_run(args))
    except KeyboardInterrupt:
        logger.warn("CTRL-C pressed, exiting")
    except Terminated:
        logger.warn("Browser window terminated, exiting")
    except AuthResponseError as exc:
        logger.error(f"Required attributes not found in response (\"{exc}\", does this endpoint do SSO?), exiting")
    except HTTPError as exc:
        logger.error(f"Request error: {exc}")

def configure_logger(logger, level):
    structlog.configure(
        processors=[
            structlog.stdlib.add_log_level,
            structlog.stdlib.add_logger_name,
            structlog.processors.format_exc_info,
            structlog.stdlib.ProcessorFormatter.wrap_for_formatter,
        ],
        logger_factory=structlog.stdlib.LoggerFactory(),
    )

    formatter = structlog.stdlib.ProcessorFormatter(
        processor=structlog.dev.ConsoleRenderer()
    )

    handler = logging.StreamHandler()
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    logger.setLevel(level)


async def _run(args):
    cfg = config.load()

    credentials = None
    if cfg.credentials:
        credentials = cfg.credentials
    elif args.user:
        credentials = Credentials(args.user)
        credentials.password = getpass.getpass(prompt=f"Password ({args.user}): ")
        cfg.credentials = credentials

    if cfg.default_profile and not (args.use_profile_selector or args.server):
        selected_profile = cfg.default_profile
    elif args.use_profile_selector or args.profile_path:
        profiles = get_profiles(Path(args.profile_path))
        if not profiles:
            logger.error("No profile found")
            return 17

        selected_profile = await select_profile(profiles)
        if not selected_profile:
            logger.error("No profile selected")
            return 18
    elif args.server:
        selected_profile = config.HostProfile(args.server, args.usergroup)
    else:
        raise ValueError(
            "Cannot determine server address. Invalid arguments specified."
        )

    cfg.default_profile = selected_profile

    config.save(cfg)

    auth_response = await authenticate_to(selected_profile, credentials, args.version_string)
    if args.headless:
        logger.warn("Exiting after login, as requested")
        response_data = auth_response.asdict()
        response_data["headend"] = selected_profile.vpn_url
        print(json.dumps(response_data, indent=2))
        return 0

    return await run_openconnect(auth_response, selected_profile, args.openconnect_args)


async def select_profile(profile_list):
    selection = await radiolist_dialog(
        title="Select AnyConnect profile",
        text=HTML(
            "The following AnyConnect profiles are detected.\n"
            "The selection will be <b>saved</b> and not asked again unless the <pre>--profile-selector</pre> command line option is used"
        ),
        values=[(p, p.name) for i, p in enumerate(profile_list)],
        async_=True,
    ).to_asyncio_future()
    asyncio.get_event_loop().remove_signal_handler(signal.SIGWINCH)
    if not selection:
        return selection
    logger.info("Selected profile", profile=selection.name)
    return selection


def authenticate_to(host, credentials, version_string):
    logger.info("Authenticating to VPN endpoint", name=host.name, address=host.address)
    return Authenticator(host, credentials=credentials, version_string=version_string).authenticate()


async def run_openconnect(auth_info, host, args):
    command_line = [
        "sudo",
        "openconnect",
        "--cookie-on-stdin",
        "--servercert",
        auth_info.server_cert_hash,
        *args,
    ]

    logger.debug("Starting OpenConnect", command_line=command_line)
    proc = await asyncio.create_subprocess_exec(
        *command_line,
        host.vpn_url,
        stdin=asyncio.subprocess.PIPE,
        stdout=None,
        stderr=None,
    )
    proc.stdin.write(f"{auth_info.session_token}\n".encode())
    await proc.stdin.drain()
    await proc.wait()
