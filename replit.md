# TEKZ-AUTO-NEWS

A Python Telegram bot that curates technology and AI news from trusted RSS feeds, rewrites stories in Arabic with OpenAI, and publishes them with images while tracking duplicates in SQLite.

## Run & Operate

- `python main.py` — run the Telegram news bot
- `python -m py_compile main.py tekz_auto_news/*.py` — check Python syntax
- `pnpm --filter @workspace/api-server run dev` — run the existing API server (port 5000)
- `pnpm run typecheck` — full typecheck across all packages
- `pnpm run build` — typecheck + build all packages
- `pnpm --filter @workspace/api-spec run codegen` — regenerate API hooks and Zod schemas from the OpenAPI spec
- `pnpm --filter @workspace/db run push` — push DB schema changes (dev only)
- Required secrets: `OPENAI_API_KEY`, `TELEGRAM_BOT_TOKEN`
- Required environment variable: `TELEGRAM_CHAT_ID`

## Stack

- Python 3.12, OpenAI Python SDK, feedparser, requests, SQLite
- Existing pnpm workspace, Node.js 24, TypeScript 5.9
- Existing API: Express 5
- Existing DB package: PostgreSQL + Drizzle ORM
- Validation: Zod (`zod/v4`), `drizzle-zod`
- API codegen: Orval (from OpenAPI spec)
- Build: esbuild (CJS bundle)

## Where things live

- `main.py` and `tekz_auto_news/` — Telegram bot
- `rss_sources.json` — curated RSS source list
- `data/tekz_auto_news.db` — local SQLite article history (created at runtime)
- `requirements.txt` — Python dependencies

## Architecture decisions

- Use a Reserved VM deployment for continuous background operation; the bot does not serve HTTP traffic.
- Record source links and normalized title fingerprints in SQLite before processing so repeated feed items are not republished.
- Keep provider credentials in Replit Secrets; `TELEGRAM_CHAT_ID` is a non-secret environment variable.

## Product

- Collects recent technology and AI stories from RSS feeds.
- Rewrites only source-provided facts into professional Arabic, posts with an article image, and attributes the original source.
- Uses SQLite as a persistent duplicate guard across restarts.

## User preferences

- This project is for technology and AI news, not automotive news.

## Gotchas

- Telegram must be an administrator in the target channel with permission to post.
- A running editor workflow is not a guarantee of 24/7 operation after the workspace closes; use a Reserved VM deployment for that.

## Pointers

- See `.local/skills/pnpm-workspace/SKILL.md` for workspace structure, TypeScript setup, and package details.
