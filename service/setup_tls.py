"""Issue a private TLS certificate for the VPN and SSH-tunnel deployment."""
from pathlib import Path
import subprocess
import sys

root=Path(__file__).resolve().parent
public=root/'tls'
private=root/'secrets/tls'
public.mkdir(exist_ok=True)
private.mkdir(mode=0o700,exist_ok=True)
def openssl(*args):
    subprocess.run(['openssl',*map(str,args)],check=True,stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL)
renew='--renew' in sys.argv
if not (public/'ca.crt').exists() or renew:
    openssl('req','-x509','-newkey','rsa:3072','-nodes','-days','3650','-subj','/CN=NCO Private Recording CA',
            '-addext','basicConstraints=critical,CA:TRUE',
            '-addext','keyUsage=critical,keyCertSign,cRLSign',
            '-keyout',private/'ca.key','-out',public/'ca.crt')
if not (public/'server.crt').exists() or renew:
    openssl('req','-new','-newkey','rsa:3072','-nodes','-subj','/CN=localhost',
            '-keyout',private/'server.key','-out',private/'server.csr')
    extensions=private/'server.ext'
    extensions.write_text('subjectAltName=DNS:localhost,DNS:gateway,IP:127.0.0.1,IP:10.77.0.1,IP:202.112.51.208\n'
                          'basicConstraints=critical,CA:FALSE\nkeyUsage=critical,digitalSignature,keyEncipherment\nextendedKeyUsage=serverAuth\n')
    openssl('x509','-req','-in',private/'server.csr','-CA',public/'ca.crt','-CAkey',private/'ca.key',
            '-CAcreateserial','-days','180','-extfile',extensions,'-out',public/'server.crt')
for path in private.iterdir():path.chmod(0o600)
env=root/'.env'
values=dict(line.split('=',1) for line in env.read_text().splitlines() if '=' in line and not line.startswith('#'))
values['COOKIE_SECURE']='true'
values['SSL_CERT_FILE']='/run/tls/ca.crt'
env.write_text(''.join(k+'='+v+'\n' for k,v in values.items()))
env.chmod(0o600)
print('Private TLS certificate ready. CA public certificate:',public/'ca.crt')
