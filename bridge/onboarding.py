"""QR onboarding: the page a scanned pairing QR opens on the phone.

Unauthenticated on purpose, and safe because of what it does and doesn't
expose: it serves the APK binary (public code, signed) and a deep link built
from a code the scanner already had to be looking at. A bare /get with no code
never reveals the active one. It is reachable only over the tailnet or LAN -
the bridge is never port-forwarded.
"""
from bridge.settings import HOST_NAME, PORT
from bridge import updates

_PACKAGE = "com.cortana.mobile"

_PAGE = """<!doctype html><html><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width, initial-scale=1">
<title>Cortana Mobile - {host}</title><style>
body{{background:#221d33;color:#fdf3ec;font-family:sans-serif;margin:0;
     display:flex;flex-direction:column;align-items:center;gap:1.1rem;
     padding:3rem 1.5rem;text-align:center}}
h1{{font-size:1.15rem;letter-spacing:.18em;margin:0;color:#c9b8e8}}
p{{color:#9b93a8;font-size:.9rem;line-height:1.55;margin:0;max-width:26rem}}
.btn{{display:block;width:100%;max-width:22rem;box-sizing:border-box;
     padding:1rem;border:1px solid #ffab8f;border-radius:12px;color:#ffab8f;
     text-decoration:none;font-size:.95rem;letter-spacing:.1em}}
.warn{{color:#f08a9b;font-size:.85rem;max-width:22rem}}
.sphere{{width:72px;height:72px;border-radius:50%;
        background:radial-gradient(circle at 40% 36%,#b8ecff,#59b6f2 45%,#173a7a 80%,#0a1530)}}
</style></head><body>
<div class="sphere"></div>
<h1>CORTANA MOBILE</h1>
<p>Linking to <b>{host}</b>. Step 1 downloads the app (allow the install
when Android asks). Step 2 opens it and pairs this phone automatically.</p>
{download}
{pair}
<p>Already installed? Just tap step 2.</p>
</body></html>"""


def valid_code(raw):
    """Only a well-formed 6-digit code survives into the page."""
    raw = (raw or "").strip()
    return raw if len(raw) == 6 and raw.isdigit() else ""


def install_page(code, host):
    """HTML for /get. `host` is the address the phone actually reached us on,
    so the deep link always points back the way the phone came in."""
    apk = updates.apk_info()
    if apk.get("available"):
        version = f" (v{apk['version']})" if apk.get("version") else ""
        download = f"<a class='btn' href='/get/apk'>1 · DOWNLOAD THE APP{version}</a>"
    else:
        download = ("<div class='warn'>No APK on this machine yet - run git pull "
                    "in ~/cortana after CI finishes, then rescan.</div>")

    if code:
        link = (f"intent://pair?host={host}&port={PORT}&code={code}"
                f"#Intent;scheme=cortana;package={_PACKAGE};end")
        pair = f"<a class='btn' href='{link}'>2 · OPEN CORTANA &amp; PAIR</a>"
    else:
        pair = ("<div class='warn'>No pairing code in this link - tap PAIR A PHONE "
                "on the dashboard's MOBILE LINK module and scan the QR again.</div>")

    return _PAGE.format(host=HOST_NAME, download=download, pair=pair)


def qr_matrix(url):
    """(matrix, error): a 2D 0/1 grid the dashboard paints onto a canvas.
    Returns (None, reason) when the optional qrcode dependency is absent."""
    try:
        import qrcode
    except ImportError:
        return None, "qrcode lib missing - rerun bridge/install-bridge.sh"
    q = qrcode.QRCode(border=0)
    q.add_data(url)
    q.make(fit=True)
    return [[1 if cell else 0 for cell in row] for row in q.get_matrix()], None
