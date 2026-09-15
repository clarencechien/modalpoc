"""CA bundle helper: NLSC servers (*.nlsc.gov.tw) do not send the TWCA intermediate
certificate, so verification fails with the stock bundle. Fetch the chain via the leaf's
AIA (caIssuers) URL and append it to certifi's bundle. Verification stays ON."""
from __future__ import annotations


def make_bundle(hosts=("3dtiles.nlsc.gov.tw", "i3s.nlsc.gov.tw", "3dmaps.nlsc.gov.tw"), out="/tmp/tw-bundle.pem") -> str:
    import ssl, socket, requests, certifi
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509.oid import ExtensionOID, AuthorityInformationAccessOID
    extra = b""
    for host in hosts:
        try:
            ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_CLIENT); ctx.check_hostname = False; ctx.verify_mode = ssl.CERT_NONE
            with socket.create_connection((host, 443), timeout=20) as s, ctx.wrap_socket(s, server_hostname=host) as ss:
                cert = x509.load_der_x509_certificate(ss.getpeercert(binary_form=True))
            for _ in range(3):
                aia = cert.extensions.get_extension_for_oid(ExtensionOID.AUTHORITY_INFORMATION_ACCESS).value
                urls = [d.access_location.value for d in aia if d.access_method == AuthorityInformationAccessOID.CA_ISSUERS]
                if not urls:
                    break
                data = requests.get(urls[0], timeout=20).content
                try:
                    cert = x509.load_der_x509_certificate(data)
                except Exception:
                    cert = x509.load_pem_x509_certificate(data)
                extra += cert.public_bytes(serialization.Encoding.PEM)
        except Exception as e:  # host unreachable: keep going
            print(f"[tls_tw] {host}: {e}")
    with open(out, "wb") as f:
        f.write(open(certifi.where(), "rb").read() + b"\n" + extra)
    return out
