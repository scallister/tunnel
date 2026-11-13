# tunnel

A private tunnel script, not for public use.

This Linux client forwards a local HTTP service through an existing private
[frp](https://github.com/fatedier/frp) server. The server must already provide
subdomain routing and HTTPS termination. This repository contains no deployment
domain, server address, or authentication token.

## Configuration

Supply configuration through the process environment. Keep real values outside
this repository, for example in your shell configuration or secret manager.
The script does not load `.env` files.

| Variable | Meaning | Default |
| --- | --- | --- |
| `TUNNEL_DOMAIN` | Base domain for generated HTTPS URLs | Required |
| `TUNNEL_SERVER` | frp control-server hostname | Required |
| `TUNNEL_TOKEN` | Server authentication token | Required |
| `TUNNEL_SERVER_PORT` | Control-server port | `443` |
| `TUNNEL_PROTOCOL` | frp transport: `tcp`, `kcp`, `quic`, `websocket`, or `wss` | `wss` |
| `TUNNEL_NAME` | Optional tunnel name when no positional name is supplied | Random |

No real values are prefilled. Domain and server values must not include a URL
scheme, path, or port. The token authenticates tunnel registration; it does **not**
protect visitors to the forwarded HTTP service.

## Usage

Once the required environment variables are set:

```sh
./tunnel http 8080
./tunnel http 8080 myapp
./tunnel http localhost:8080 myapp
./tunnel http 8080 --timeout 30m
./tunnel --help
```

The script prints an HTTPS URL such as `https://t-myapp.example.com` and stays in
the foreground. Stop it with Ctrl+C. Multiple instances can run with different
names. Names are lowercased and prefixed with `t-` automatically.

Local targets must use plain HTTP; IPv6 targets are not supported.

Requires Linux (amd64 or arm64), Bash 4+, `curl`, `sha256sum`, `tar`, and standard
Unix utilities. The pinned frp 0.71.0 client is downloaded on first use, checked
against its pinned SHA-256 checksum, and cached under
`${XDG_DATA_HOME:-$HOME/.local/share}/private-tunnel`. A temporary configuration
file is owner-readable only and removed when the client exits.

## Temporary SSH access

Expose an existing local SSH port through the same HTTPS hostname routing:

```sh
./tunnel tcp 22 --timeout 30m
```

No server-side frp changes, public TCP port, sudo, or second SSH daemon are
needed. TCP mode binds its WebSocket relay to loopback and forwards only to
`127.0.0.1:<port>`. The existing SSH server controls which keys and users can
log in. **TCP mode adds no authentication of its own**; forward SSH or another
authenticated service, not an unprotected shell or admin socket.

TCP tunnels default to 30 minutes and allow at most 4 hours. The deadline starts
when the relay starts, including frp startup/download time. At expiry, Ctrl+C,
or client failure, the relay closes active connections and stops the tunnel.
An independent timer also bounds frp if the relay is killed abruptly. Cleanup
can take up to two additional seconds. The existing SSH server stays running;
commands deliberately detached on the remote machine can continue after an
SSH connection ends. This is an access timeout, not a remote command deadline.

HTTP mode remains unlimited by default; `--timeout 30m` adds a limit and returns
GNU timeout's exit status 124 when it expires. Positive durations use seconds
(`30` or `30s`), minutes (`30m`), or hours (`2h`).

### One-time setup on both machines

TCP mode and its SSH client transport require Python 3.10+, `venv`, and the
`websockets` package; the original HTTP mode has no Python dependency.

```sh
python3 -m venv .venv
.venv/bin/pip install -r requirements-tcp.txt
export TUNNEL_PYTHON="$PWD/.venv/bin/python"
```

On Ubuntu, install `python3-venv` first if Python reports it missing. Keep
`tunnel` and `tunnel_tcp.py` together when copying the client to another machine.

### Authorize a temporary key

On the connecting machine, generate a dedicated key:

```sh
ssh-keygen -t ed25519 -f ./temporary-access -N ''
```

On the host, add that **public** key to the desired user's `~/.ssh/authorized_keys`,
with an expiry matching the planned access window. The private key stays on the
connecting machine. For OpenSSH 7.7+ (including Ubuntu 22.04):

```sh
mkdir -p ~/.ssh
chmod 700 ~/.ssh
access_expires=$(date -u -d '+30 minutes' +%Y%m%d%H%M%SZ)
printf 'restrict,pty,expiry-time="%s" %s\n' "$access_expires" \
  'ssh-ed25519 REPLACE_WITH_PUBLIC_KEY temporary-access' >> ~/.ssh/authorized_keys
chmod 600 ~/.ssh/authorized_keys
```

The key expires even if the tunnel is restarted. `restrict,pty` permits a shell
but disables SSH forwarding and user startup hooks. It does not limit shell
commands or change the user's existing sudo privileges. Remove this dedicated
line afterward; do not replace the rest of `authorized_keys`.

Print the host's public SSH key, then start the tunnel:

```sh
cat /etc/ssh/ssh_host_ed25519_key.pub
./tunnel tcp 22 --timeout 30m
```

Share the generated HTTPS URL and that public host key with the connecting
operator over a trusted channel. No passwords or private keys need to be shared.

### Connect through HTTPS

Save the provided host key in a separate known-hosts file, prefixed with the
generated hostname, and use the HTTPS transport as OpenSSH's ProxyCommand:

```sh
printf '%s\n' 't-example.example.com ssh-ed25519 REPLACE_WITH_HOST_PUBLIC_KEY' > temporary-known-hosts
ssh -i ./temporary-access -o IdentitiesOnly=yes \
  -o StrictHostKeyChecking=yes -o UserKnownHostsFile=./temporary-known-hosts \
  -o "ProxyCommand=$PWD/tunnel connect %h" user@t-example.example.com
```

OpenSSH still verifies the host key and authenticates the client key. HTTPS
certificates are verified too. The transport honors the environment's HTTPS
proxy and CA trust (`SSL_CERT_FILE` / `SSL_CERT_DIR`). It carries SSH bytes and
does not execute commands itself. Keep the host-side tunnel in the foreground
or a terminal session until it expires; Ctrl+C ends access early.

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests use a fake frp client and loopback services; they do not open a public
tunnel or require credentials. TCP integration tests additionally require
`websockets`, OpenSSH client/server, and `openssl`. They exercise real SSH over
TLS/WebSocket, unauthorized keys, host-key pinning, live-session expiry, signal
cleanup, client failure, and termination of tunnel child processes.
