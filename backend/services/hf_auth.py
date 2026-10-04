"""Which hosts may receive the Hugging Face token.

The token authorizes the user's Hugging Face account, so it is only ever sent
over HTTPS to Hugging Face itself. Mirrors (configured or picked by automatic
endpoint selection) and CDN redirect targets get no ``Authorization`` header.
"""
from __future__ import annotations

from typing import Optional, Union
from urllib.parse import urlsplit

HF_AUTH_HOSTS = ("huggingface.co", "hf.co")


def host_gets_auth(url: Optional[str]) -> bool:
    """True when ``url`` is an HTTPS Hugging Face URL."""
    try:
        parsed = urlsplit(url or "")
        host = (parsed.hostname or "").lower()
    except ValueError:
        return False
    return parsed.scheme == "https" and (
        host in HF_AUTH_HOSTS or host.endswith(".huggingface.co")
    )


def hub_endpoint(endpoint: Optional[str]) -> str:
    """The endpoint huggingface_hub will contact for ``endpoint=``.

    ``None`` means the library default, which was fixed from ``HF_ENDPOINT``
    when huggingface_hub was imported and can differ from the current env.
    """
    if endpoint:
        return endpoint
    from huggingface_hub import constants

    return constants.ENDPOINT


def token_for_endpoint(
    endpoint: Optional[str], token: Optional[str]
) -> Union[str, None, bool]:
    """The ``token=`` value to hand huggingface_hub for ``endpoint``.

    Hugging Face hosts get ``token`` (``None`` keeps the library's own lookup).
    Any other host gets ``False``, which also stops huggingface_hub from
    sending a token it finds in ``HF_TOKEN`` or its token file.
    """
    if host_gets_auth(hub_endpoint(endpoint)):
        return token
    return False


def env_allows_token(environ) -> bool:
    """True unless ``environ['HF_ENDPOINT']`` names a non-Hugging Face host."""
    endpoint = (environ.get("HF_ENDPOINT") or "").strip()
    return not endpoint or host_gets_auth(endpoint)


_IMPLICIT_ENV = "HF_HUB_DISABLE_IMPLICIT_TOKEN"
_implicit_disabled_here = False


def apply_process_token_policy(environ: Optional[dict] = None) -> None:
    """Stop implicit token use when ``HF_ENDPOINT`` names a mirror.

    huggingface_hub, and every library built on it, sends the token from
    ``HF_TOKEN`` or its token file to whatever ``HF_ENDPOINT`` says. Setting
    ``HF_HUB_DISABLE_IMPLICIT_TOKEN`` stops that for engine processes that
    inherit the environment; the variable follows the current setting. The
    in-process endpoint is fixed when huggingface_hub is imported, so the
    in-process switch is only ever turned on, never back off.
    """
    global _implicit_disabled_here
    import os
    import sys

    env = os.environ if environ is None else environ
    if not env_allows_token(env):
        if not env.get(_IMPLICIT_ENV):
            env[_IMPLICIT_ENV] = "1"
            _implicit_disabled_here = True
    elif _implicit_disabled_here:
        env.pop(_IMPLICIT_ENV, None)
        _implicit_disabled_here = False
    # Not imported yet: it will read both variables from the environment.
    constants = sys.modules.get("huggingface_hub.constants") if environ is None else None
    if constants is not None and not host_gets_auth(constants.ENDPOINT):
        constants.HF_HUB_DISABLE_IMPLICIT_TOKEN = True


__all__ = [
    "HF_AUTH_HOSTS",
    "apply_process_token_policy",
    "env_allows_token",
    "host_gets_auth",
    "hub_endpoint",
    "token_for_endpoint",
]
