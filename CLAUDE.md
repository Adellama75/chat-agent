# Notes for Claude sessions

- This is the shared agent of whatsapp-reader and discord-reader (sibling checkouts `../whatsapp-reader`,
  `../discord-reader`). After a change here, run this repo's tests and both readers'.
- Keep it transport-agnostic: anything that knows about WhatsApp or Discord belongs in a reader's adapter.
