# 🧹 WhatsApp Sweeper

A local, fast tool for finding and deleting the media WhatsApp Desktop has quietly piled up on your Mac.

## The problem

WhatsApp Desktop downloads every photo, video, and voice note you ever receive and keeps it forever in a local cache. It's not unusual for that folder to quietly grow to 20-30GB — years of stickers, forwarded videos, and group-chat noise you'll never look at again.

WhatsApp's engineers clearly track exactly which file belongs to which chat and sender — it's all sitting right there in their own local database. They just never shipped a way for you to see or clean it up. So here we are.

## What it does

WhatsApp Sweeper scans WhatsApp Desktop's media cache, cross-references it against WhatsApp's own local chat database to figure out who sent each file and from which chat, and gives you a simple visual grid — sorted by size, biggest offenders first — so you can select what to delete and reclaim the space in a couple of minutes.

- **Sorted by size** — see what's actually eating your disk first
- **Real thumbnails** — generated from the source files, not WhatsApp's blurry tiny cache
- **Sender & group filters** — include/exclude by person or by group, combinable
- **Finder-style selection** — click, shift-click for ranges, drag to multi-select
- **Runs entirely on your machine** — no network calls, no accounts, nothing uploaded

> [!NOTE]
> This only reads WhatsApp's local cache and database to *display* information. It never sends anything anywhere — everything happens on `localhost`.

## Requirements

- macOS (this reads WhatsApp Desktop's macOS-specific cache and database — it won't work anywhere else)
- WhatsApp Desktop, installed and opened at least once
- Python 3.9+
- [ffmpeg](https://ffmpeg.org) (optional, for sharper video thumbnails — falls back to WhatsApp's own cached preview frame if it's missing)

## Quick start

No install step needed — just run it:

```bash
uvx whatsapp-sweeper
```

or, with [pipx](https://pipx.pypa.io):

```bash
pipx run whatsapp-sweeper
```

Either one launches a local server and opens your browser to it automatically. Leave the terminal running while you use it; `Ctrl+C` to stop.

## Using it

- Click a thumbnail to open the file in its default app; double-click also works.
- Click, `Shift`-click, `Cmd`-click, or drag across thumbnails to select multiple — like Finder's icon view.
- Use the sidebar to filter by sender or by group/chat. Clicking a name cycles it through include-only → exclude → off, and both filters combine.
- **Select all** selects everything matching the current filter, not just what's scrolled into view.

> [!IMPORTANT]
> Deleting is permanent — there is no trash or undo. Review your selection before confirming. WhatsApp will simply treat removed media as not-yet-downloaded if you ever need it again.

## How sender resolution works

Media files on disk are just UUIDs sitting in per-chat folders. To label each one with a sender and date, WhatsApp Sweeper reads `ChatStorage.sqlite` — the same database WhatsApp Desktop itself uses — joining the media entry to its message, then to the group member and chat, to resolve a display name. Older files that have aged out of WhatsApp's own message history simply show up as "Unknown" — that's WhatsApp's retention limit, not a bug here.
