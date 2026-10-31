# Arika observability and RAG sidecar

This stack is kept separate from the backend project. It builds the existing
NestJS app, places Nginx in front of it, runs a FastAPI metrics/RAG sidecar,
and configures Prometheus to scrape the sidecar's combined metrics endpoint.

## Project layout

```text
arika-observability/
├── docker-compose.yml
├── nginx/
│   ├── nginx.conf
│   └── native-api-locations.conf
├── prometheus/
│   ├── prometheus.yml
│   ├── prometheus.native.yml
│   └── alerts.native.yml
├── alertmanager/
│   └── alertmanager.native.yml
├── systemd/
│   ├── alertmanager.service
│   ├── arika-python-sidecar.service
│   └── node-exporter.service
└── sidecar/
    ├── Dockerfile
    ├── requirements.txt
    ├── app/
    │   ├── __init__.py
    │   └── main.py
    └── tests/
        └── test_main.py
```

The existing backend has a dedicated `src/metrics` NestJS module, registered
in `AppModule`, which exposes the default Node.js metrics and HTTP request
metrics at `/metrics`.

## Network and access model

- `app_edge` connects Nginx to NestJS. The backend port is not published on the
  host.
- `monitoring` is an isolated Docker network for Nginx, the sidecar, and
  Prometheus. Nginx permits `/metrics` and `/nginx_status` only from this
  network's `172.31.0.0/24` subnet.
- `sidecar_egress` gives the Python service outbound access for a future RAG
  provider integration without giving Prometheus that access.
- Nginx is published on `127.0.0.1:8080` by default. An existing host-level
  reverse proxy can forward general application traffic to that listener.
  Override `NGINX_HOST_PORT` to choose another host port; keep the loopback
  bind unless you have an intentional firewall and ingress policy.
- Prometheus scrapes only `python-sidecar:8000/metrics` over the private
  monitoring network. Its UI is bound to `127.0.0.1:9090` for local
  inspection, not exposed on a public interface.

## Existing Linux server (without Docker)

The Compose stack above is optional. On your server, you can keep the existing
Prometheus installation and Nginx virtual hosts; there is no requirement to
create an Nginx `conf.d` directory. Add the provided Nginx locations directly
inside your existing `api.arika.com.ng` server block.

### 1. Enable metrics in the deployed NestJS API

Deploy the backend changes, install its updated npm dependencies, build, and
restart the production process. Your API listens on port `3001`, so first
verify the local endpoint:

```bash
curl -fsS http://127.0.0.1:3001/metrics | head
```

Keep application ports `3001` and `3003` firewalled from public access; public
traffic should go through Nginx. These instructions scrape the production API
on port `3001`.

### 2. Add internal-only Nginx locations

Open the existing Nginx file containing your `api.arika.com.ng` `server`
block. Copy the contents of
[`nginx/native-api-locations.conf`](./nginx/native-api-locations.conf) into
that server block, alongside (not inside) its existing `location /` block.
The exact-match locations take precedence over `location /`.

Your existing API block should have this shape:

```nginx
server {
    listen 80;
    server_name api.arika.com.ng;

    location = /metrics {
        allow 127.0.0.1;
        allow ::1;
        deny all;
        proxy_pass http://127.0.0.1:3001/metrics;
        proxy_set_header Host $host;
    }

    location = /nginx_status {
        stub_status;
        access_log off;
        allow 127.0.0.1;
        allow ::1;
        deny all;
    }

    location / {
        proxy_pass http://127.0.0.1:3001;
        # Keep the proxy headers already in your server block.
    }
}
```

If you also have a separate TLS server block for `api.arika.com.ng`, add the
same two locations there too. Then validate and reload:

```bash
sudo nginx -t
sudo systemctl reload nginx
curl -fsS -H 'Host: api.arika.com.ng' \
  http://127.0.0.1/nginx_status
curl -fsS -H 'Host: api.arika.com.ng' \
  http://127.0.0.1/metrics | head
```

Requests from other hosts should receive `403` for both monitoring locations.
Do not make these locations public or point them at your `monitoring.arika.com.ng`
site.

### 3. Install and run the Python sidecar with systemd

The sidecar binds to loopback only, so it is reachable by local Prometheus but
not directly from the Internet. The supplied unit expects the project under
`/opt/arika/observability`; adjust its paths if you choose another location.
Use Python 3.10 or newer.

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin arika-monitoring
sudo install -d -o arika-monitoring -g arika-monitoring \
  /opt/arika/observability
sudo install -d -o root -g arika-monitoring -m 0750 /etc/arika
sudo install -d -o root -g root -m 0755 /etc/systemd/system
```

Copy this project's `sidecar/` directory to
`/opt/arika/observability/sidecar`, then create the virtual environment and
install dependencies:

```bash
sudo -u arika-monitoring python3 -m venv /opt/arika/observability/venv
sudo -u arika-monitoring \
  /opt/arika/observability/venv/bin/pip install \
  -r /opt/arika/observability/sidecar/requirements.txt
```

Create `/etc/arika/observability.env` with mode `0640` and group
`arika-monitoring`:

```ini
NGINX_STATUS_URL=http://127.0.0.1/nginx_status
NEST_METRICS_URL=http://127.0.0.1/metrics
NGINX_HOST_HEADER=api.arika.com.ng
HTTP_TIMEOUT_SECONDS=5
```

```bash
sudo chown root:arika-monitoring /etc/arika/observability.env
sudo chmod 0640 /etc/arika/observability.env
sudo install -o root -g root -m 0644 \
  systemd/arika-python-sidecar.service \
  /etc/systemd/system/arika-python-sidecar.service
sudo systemctl daemon-reload
sudo systemctl enable --now arika-python-sidecar
sudo systemctl status arika-python-sidecar
```

Verify locally:

```bash
curl -fsS http://127.0.0.1:8000/healthz
curl -fsS http://127.0.0.1:8000/metrics | head
```

### 4. Run Node Exporter locally

Install the official Node Exporter binary as `/usr/local/bin/node_exporter`,
create a locked-down `node_exporter` system user, and install the supplied
`systemd/node-exporter.service` as
`/etc/systemd/system/node-exporter.service`. Then enable it:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin node_exporter
sudo install -o root -g root -m 0755 \
  systemd/node-exporter.service \
  /etc/systemd/system/node-exporter.service
sudo systemctl daemon-reload
sudo systemctl enable --now node-exporter
curl -fsS http://127.0.0.1:9100/metrics | head
```

The unit binds Node Exporter to `127.0.0.1:9100`; do not change this to a
public listener. Use a release version appropriate for your server and install
the binary from the official Prometheus Node Exporter release source.

### 5. Connect your existing Prometheus and Alertmanager

[`prometheus/prometheus.native.yml`](./prometheus/prometheus.native.yml) is a
complete example for a fresh native Prometheus install. Since Prometheus is
already running on your server, **do not replace its current configuration
blindly**. Merge these parts into the config file your Prometheus service
actually uses:

- Add the sidecar (`127.0.0.1:8000`) and Node Exporter
  (`127.0.0.1:9100`) scrape jobs to its existing `scrape_configs`.
- Add `/etc/prometheus/rules/arika-alerts.yml` under `rule_files`.
- Add the loopback Alertmanager target (`127.0.0.1:9093`) under `alerting`.
- Preserve your existing Prometheus jobs, global settings, and external labels.

Copy [`prometheus/alerts.native.yml`](./prometheus/alerts.native.yml) to
`/etc/prometheus/rules/arika-alerts.yml` (create the rules directory if it
doesn't exist):

```bash
sudo install -d -o root -g root -m 0755 /etc/prometheus/rules
sudo install -o root -g root -m 0644 \
  prometheus/alerts.native.yml \
  /etc/prometheus/rules/arika-alerts.yml
```

Check the rules and the complete Prometheus config using the `promtool` binary
from your installed Prometheus release. Confirm the config is valid before
reloading/restarting Prometheus:

```bash
sudo promtool check rules /etc/prometheus/rules/arika-alerts.yml
sudo promtool check config /etc/prometheus/prometheus.yml
```

If your service uses another config path, substitute that path. Confirm it
with `systemctl cat prometheus` before editing. The native Alertmanager
configuration and systemd unit are provided in `alertmanager/` and `systemd/`.
Install the official Alertmanager binary at `/usr/local/bin/alertmanager`,
then set up its account, storage, config, and systemd service:

```bash
sudo useradd --system --no-create-home --shell /usr/sbin/nologin alertmanager
sudo install -d -o alertmanager -g alertmanager -m 0750 /var/lib/alertmanager
sudo install -d -o root -g alertmanager -m 0750 /etc/alertmanager
sudo install -o root -g alertmanager -m 0640 \
  alertmanager/alertmanager.native.yml \
  /etc/alertmanager/alertmanager.yml
sudo install -o root -g root -m 0644 \
  systemd/alertmanager.service \
  /etc/systemd/system/alertmanager.service
sudo systemctl daemon-reload
sudo systemctl enable --now alertmanager
```

This example's `default` receiver intentionally has no notification
integration: alerts are visible in Alertmanager, but you must configure an
email, chat, or other receiver before notifications are delivered. Once
Alertmanager is healthy and the Prometheus config validates, restart/reload
Prometheus:

```bash
sudo systemctl restart prometheus
```

Both exporter and Alertmanager listeners are loopback-only. Do not expose
ports `9100` or `9093` in the firewall.

### 6. Protect the existing Prometheus hostname

Your current `prometheus.arika.com.ng` Nginx virtual host proxies the
Prometheus UI publicly. TLS encrypts that connection but does not authenticate
users. Add an IP allow-list or Nginx Basic Auth to that server block (and
consider equivalent access control on `monitoring.arika.com.ng`) before using
it for operational metrics. Keep Alertmanager and Node Exporter off public
Nginx routes. For Basic Auth, install `apache2-utils`, create a password file
with `sudo htpasswd -c /etc/nginx/.htpasswd-prometheus <username>`, then put
these directives inside the Prometheus virtual host's existing `location /`:

```nginx
auth_basic "Prometheus";
auth_basic_user_file /etc/nginx/.htpasswd-prometheus;
```

### Staging API

The native sidecar above monitors production `api.arika.com.ng` on port `3001`.
To monitor staging on port `3003` too, enable the Nest metrics module in that
deployment and add a second, separately named scrape in the design; do not
simply concatenate both Nest exposition responses because they contain the
same metric names.

## Run the stack

1. Configure the backend environment file. Copy
   `../arika-be/.env.example` to `../arika-be/.env` and set valid values for
   all required application integrations, including MongoDB, Redis, and the
   messaging providers. The Nest app validates these settings at startup.
2. From this directory, build and start the services:

   ```bash
   docker compose up --build -d
   ```

3. Check service status and the application:

   ```bash
   docker compose ps
   curl http://127.0.0.1:8080/
   ```

4. Open Prometheus at `http://127.0.0.1:9090`. The aggregate metrics endpoint
   is internal to the Compose network. To
   inspect it from the sidecar container:

   ```bash
   docker compose exec python-sidecar \
     python -c "from urllib.request import urlopen; print(urlopen('http://127.0.0.1:8000/metrics').read().decode())"
   ```

Do not bind Prometheus or raw NestJS metrics to a public interface. Change the
Prometheus host binding only if deployment firewall and authentication rules
are explicitly configured.

## Metrics and endpoints

| Endpoint / metric | Purpose |
| --- | --- |
| NestJS `/metrics` (via Nginx) | Default Node.js process/runtime metrics and HTTP request count/latency |
| Nginx `/nginx_status` (internal only) | Nginx connection and request counters, translated into Prometheus metrics by Python |
| Python `/metrics` | Combines NestJS exposition with Nginx, Python process, sidecar HTTP, scrape-health, and RAG metrics |
| Python `/healthz` | Sidecar liveness check |
| Python `POST /ask-rag` | Placeholder endpoint; accepts `{"question":"..."}` and records query count and latency |

If either upstream scrape fails, the sidecar logs the failure, exposes a
zero-valued `nginx_scrape_success` or `nest_metrics_scrape_success` gauge, and
still returns its own metrics. Alert on those gauges as well as Prometheus's
`up` metric to distinguish an upstream failure from a sidecar failure.

The RAG route is intentionally a stub and does not call an LLM or retrieve
documents. Add authentication, request limits, retrieval, and model integration
before exposing it to untrusted clients.

## Local NestJS development

Install the backend dependency and start the Nest app normally; the metrics
endpoint is available at `http://localhost:<PORT>/metrics`. The normal
application environment requirements still apply.

The `TOKEN_ENCRYPTION_KEY` example in the backend env template is only a
format-valid placeholder. Replace it with a securely generated, private
32-byte key before running the application.

Run the sidecar checks locally after installing `sidecar/requirements.txt`:

```bash
PYTHONPATH=sidecar python -m unittest discover -s sidecar/tests -v
```
