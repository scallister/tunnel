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

## Tests

```sh
python3 -m unittest discover -s tests -v
```

Tests use a fake local client; they do not open a tunnel or require credentials.
