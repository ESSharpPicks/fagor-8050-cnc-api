# Fagor 8050 M CNC Advisory API

A read-only advisory service for GPT Actions. It performs static checks,
transparent feed/RPM calculations, and simple 2D cutter-radius offsets. It does
not connect to, upload to, or control a CNC machine.

## Local run

```powershell
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt
$env:CNC_API_KEY = 'replace-with-a-long-random-secret'
.venv\Scripts\uvicorn app.main:app --reload
```

Open `http://127.0.0.1:8000/docs` for interactive API documentation.

## Tests

```powershell
.venv\Scripts\pytest -q
```

## Render deployment

1. Push this folder to a GitHub repository.
2. In Render, choose **New → Blueprint** and select the repository.
3. Render reads `render.yaml`, deploys the container, generates an API key, and
   assigns an address such as `https://fagor-8050-cnc-api.onrender.com`.
4. In Render, copy `CNC_API_KEY` from the service's Environment settings.
5. Put the real Render URL into the GPT Action schema.
6. Configure GPT Action authentication as an API key using custom header
   `X-API-Key`, then enter the Render secret.

## Safety boundary

This API cannot prove collision freedom, controller compatibility, workholding
adequacy, tool suitability, or machine safety. Always compare against the exact
Fagor 8050 M software manual and machine-builder documentation, backplot,
simulate, dry-run above stock, and perform a controlled first prove-out.

