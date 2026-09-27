"""Close the SDK's HTTP session even if its metadata-loading constructor fails."""

from hyperliquid.api import API
from hyperliquid.exchange import Exchange
from hyperliquid.info import Info


class RequestInfo(Info):
    def __init__(self, *args, **kwargs):
        try:
            super().__init__(*args, **kwargs)
        except Exception:
            session = getattr(self, "session", None)
            if session is not None:
                session.close()
            raise


# Keep the SDK's action/signing implementation, but use the closing Info wrapper
# during construction too: otherwise failed builder metadata requests leak sessions.
class RequestExchange(Exchange):
    def __init__(
        self,
        wallet,
        base_url=None,
        meta=None,
        vault_address=None,
        account_address=None,
        spot_meta=None,
        perp_dexs=None,
        timeout=None,
    ):
        API.__init__(self, base_url, timeout)
        self.wallet = wallet
        self.vault_address = vault_address
        self.account_address = account_address
        try:
            self.info = RequestInfo(base_url, True, meta, spot_meta, perp_dexs, timeout)
        except Exception:
            self.session.close()
            raise
        self.expires_after = None
