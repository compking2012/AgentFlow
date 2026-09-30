"""Bind node identity to the TLS transport, never a caller-supplied HTTP header."""

from uvicorn.protocols.http.h11_impl import H11Protocol


class CertificateScope:
    def __init__(self, app, certificate_der):
        self.app, self.certificate_der = app, certificate_der

    async def __call__(self, scope, receive, send):
        scope = {**scope, "extensions": {**scope.get("extensions", {}),
                                         "agentflow.peer_certificate_der": self.certificate_der}}
        await self.app(scope, receive, send)


class PeerCertificateH11Protocol(H11Protocol):
    def connection_made(self, transport):
        super().connection_made(transport)
        connection = transport.get_extra_info("ssl_object")
        certificate = connection.getpeercert(binary_form=True) if connection else None
        self.app = CertificateScope(self.app, certificate)

