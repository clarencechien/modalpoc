"""Probe the NLSC 多維度平台 from a Modal container (the sandbox proxy cannot reach it).
Findings are summarised in docs/google-api-handbook.md §6; the service list lives at
https://3dtiles.nlsc.gov.tw/tiles3d/service (3D Tiles) and https://i3s.nlsc.gov.tw/i3s/service (I3S).
The NLSC servers omit the TWCA intermediate certificate, so the chain is completed via AIA."""
import modal

app = modal.App("modalpoc-nlsc-probe")
img = modal.Image.debian_slim(python_version="3.11").pip_install("requests", "cryptography")


def make_bundle():
    import ssl, socket, requests, certifi
    from cryptography import x509
    from cryptography.hazmat.primitives import serialization
    from cryptography.x509.oid import ExtensionOID, AuthorityInformationAccessOID
    extra = b""
    for host in ("3dmaps.nlsc.gov.tw", "3dtiles.nlsc.gov.tw", "i3s.nlsc.gov.tw"):
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
        except Exception as e:
            print("chain", host, str(e)[:120])
    open("/tmp/bundle.pem", "wb").write(open(certifi.where(), "rb").read() + b"\n" + extra)
    return "/tmp/bundle.pem"


@app.function(image=img, timeout=900)
def discover() -> dict:
    import requests, re, json
    bundle = make_bundle()
    s = requests.Session(); s.headers["User-Agent"] = "Mozilla/5.0"
    out = {}
    for name, u in (("tiles3d", "https://3dtiles.nlsc.gov.tw/tiles3d/service"), ("i3s", "https://i3s.nlsc.gov.tw/i3s/service")):
        try:
            r = s.get(u, timeout=60, verify=bundle)
            out[name] = {"status": r.status_code, "ct": r.headers.get("content-type"), "body": r.text[:6000]}
        except Exception as e:
            out[name] = "ERR " + str(e)[:200]
    # try to locate a Taipei building tileset and read its root
    try:
        lst = json.loads(out["tiles3d"]["body"]) if isinstance(out["tiles3d"], dict) and out["tiles3d"]["status"] == 200 else None
    except Exception:
        lst = None
        try:
            lst = s.get("https://3dtiles.nlsc.gov.tw/tiles3d/service", timeout=60, verify=bundle).json()
        except Exception as e:
            out["list_err"] = str(e)[:200]
    if lst:
        entries = lst.get("LAYERS", {}).get("BUILDING", [])
        out["building_entries"] = entries[:40]
        cand = [e for e in entries if any(k in json.dumps(e, ensure_ascii=False) for k in ("臺北", "台北", "Taipei", "TPE", "63000"))]
        out["taipei_candidates"] = cand[:5]
        for e in (cand or entries)[:1]:
            url = e.get("URL") or e.get("url") or e.get("URL_TEMPLATE") or e.get("urltemplate") or ""
            out["root_url"] = url
            try:
                r = s.get(url, timeout=60, verify=bundle)
                root = r.json()
                rt = root.get("root", {})
                out["root"] = {"asset": root.get("asset"), "geometricError": root.get("geometricError"), "bv": rt.get("boundingVolume"),
                               "refine": rt.get("refine"), "transform": rt.get("transform"), "children": len(rt.get("children", [])),
                               "content": rt.get("content"), "child0": json.dumps(rt.get("children", [{}])[0])[:800]}
            except Exception as ex:
                out["root_err"] = str(ex)[:200]
    return out


@app.local_entrypoint()
def main():
    import json
    print(json.dumps(discover.remote(), ensure_ascii=False, indent=1)[:20000])
