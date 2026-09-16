# Phase 20 — Lightweight Training Reader

This patch replaces only `run_train_reader.py`.

## Changes
- Read All Chat is streamed in 100-message chunks instead of loading the entire chat into RAM.
- Only four messages are carried between chunks for the required ±4 context window.
- Read In Range remains bounded to the requested range.
- Training request table initialization remains in place.
- Telegram peer resolution from `tg://openmessage` remains in place.
- Latest-10 learning retention remains unchanged.
- No batches/courses/shortcuts/AI bot files are replaced.

## Why
The server has about 2 GiB RAM and no swap. The previous implementation accumulated the entire chat history in a Python list before processing. This version avoids that memory spike.
