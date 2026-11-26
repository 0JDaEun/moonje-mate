# Moonje Mate backend proxy

Review implementation of the authenticated chat and OCR API used by the Flutter client. Python 3.10+ standard library. No new deployment or provider credentials included.

## Local offline tests

From the repository root:

```bash
cd backend
python -m unittest -v
```

## Server configuration

Set the five variables in .env.example using your deployment environment; the application does not load .env automatically. Configure the Flutter public proxy URL to the HTTPS endpoint. Use the Supabase publishable/anon key, never the service-role key in the app.

Run python proxy.py for local development only (loopback HTTP). For staging use a production WSGI server and TLS termination; the Flutter client requires HTTPS. The WSGI entry point is proxy:application. Do not expose the development server.

## Contract

GET /health is a liveness check, not a provider connectivity check. POST /chat and POST /ocr require a Supabase bearer access token and JSON with Content-Length. Supabase Auth verifies the token online. The server accepts only the existing model and system prompts, caps input and output token count, restricts OCR to one embedded image (3 MiB), rejects URL input, disables redirects, and returns a minimal compatible response without upstream error details.

The current limiter is 12 requests per authenticated user per minute per process. Before public release, add shared persistent quotas, pre-auth edge rate limits, concurrency limits, full image decoding and pixel limits, reverse-proxy request timeouts, operational monitoring and provider budget limits. The existing image check is only a signature check, not a complete image validator. Confirm provider configuration and response contracts with a staging account, and review data retention policies. No raw prompt or image is stored by this code; upstream provider retention is separate.

The proxy authenticates users, but does not enforce a paid entitlement. The chat prompt restrictions are not a guarantee of model behavior. No live API, DB, mobile, production or billing checks were performed.

Do not merge until the app can use the configured HTTPS proxy and signed release is validated.
