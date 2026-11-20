"""OpenVPN's up-script in "only NEOX" mode (installed as /usr/lib/neox/vpn/neox-vpn-up).
OpenVPN passes the tunnel's settings in the environment; see netns.openvpn_up_env."""
from __future__ import annotations

import os
import sys

from .netns import NetnsError, openvpn_up_env


def main() -> int:
    try:
        openvpn_up_env(dict(os.environ))
        return 0
    except (NetnsError, KeyError, ValueError) as exc:
        print(f"neox-vpn-up: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
