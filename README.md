# Traction alerts

## Run

```
uv sync
uv run traction
```

Fill in `.env` before the last step. Press Ctrl+C to stop.

## Videos

Put the Excel file of video links in the `input` folder.

## `.env`

| Variable | What |
|---|---|
| `DATABASE_URL` | Neon read-only connection string |
| `OPENPANEL_CLIENT_ID`, `OPENPANEL_CLIENT_SECRET`, `OPENPANEL_PROJECT_ID` | OpenPanel read client |
| `SMTP_HOST`, `SMTP_PORT`, `SMTP_USER`, `SMTP_PASSWORD` | Email sending (or `RESEND_API_KEY`) |
| `ALERT_FROM_EMAIL`, `ALERT_RECIPIENTS` | Sender and recipients |
| `YOUTUBE_API_KEY`, `SOCIAL_PROXY` | Optional |
