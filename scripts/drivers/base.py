"""drivers/base.py — the shared driver contract.

Every driver satisfies exactly this shape (SPEC-PROXY-DRIVERS §2). Nothing here
knows about a specific host system, and nothing here prices a lane.
"""


class Driver:
    """Base contract. Subclasses declare id/surfaces/wire_format/config_path.

    A driver must NOT:
      * branch on provider or model names (the router owns lane selection);
      * hold credentials (the host sends its own key; the proxy walks the chain
        with its own);
      * make the host unable to run when it fails (fail-open is the contract).
    """

    #: slug used as the outcome row's `source_system`
    id = None
    #: which surfaces exist for this host: any of W(ire) L(aunch) A(CP) T(elemetry)
    surfaces = ()
    #: the dialect the HOST speaks (and therefore what the proxy must accept)
    wire_format = 'openai-chat'
    #: where the user points the host at the proxy — DOCUMENTED, never patched
    config_path = None
    #: the base URL shape the host needs (documented verbatim, see §3 wire facts)
    base_url_shape = '/v1'

    @classmethod
    def caller_id(cls):
        """Identity the host sends so proxied attempts are attributed to it.

        The proxy reads `x-router-caller` and stamps the outcome row's
        source_system with it, so a proxied session is not anonymous.
        """
        return cls.id

    @classmethod
    def config(cls, proxy_base):
        """The snippet a user applies to route this host through the proxy."""
        raise NotImplementedError

    @classmethod
    def row_for(cls, **kw):
        """Telemetry reader: host's own store -> one TR-049 outcome row."""
        raise NotImplementedError


_REGISTRY = {}


def register(driver_cls):
    _REGISTRY[driver_cls.id] = driver_cls
    return driver_cls


def get_driver(driver_id):
    """Resolve a driver by id. Never raises for an unknown id — returns None so
    a caller can degrade visibly instead of failing."""
    if not _REGISTRY:
        _autoload()
    return _REGISTRY.get(driver_id)


def list_drivers():
    if not _REGISTRY:
        _autoload()
    return sorted(_REGISTRY)


def _autoload():
    import importlib
    import os
    import pkgutil
    here = os.path.dirname(os.path.abspath(__file__))
    for mod in pkgutil.iter_modules([here]):
        if mod.name in ('base', '__init__') or mod.name.startswith('_'):
            continue
        importlib.import_module(f'{__name__.rsplit(".", 1)[0]}.{mod.name}'
                                if '.' in __name__ else mod.name)
