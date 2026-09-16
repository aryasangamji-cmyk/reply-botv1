PHASE 28 — COMBO PHOTO REGISTRATION HOTFIX

Fixes the callback parser for admin:combo:msgtype:photo:<combo_id> and text:<combo_id>.
The previous version read the callback segments at the wrong indexes, so the photo state was not stored as type=photo and incoming photos were ignored.

Photo workflow:
1. Choose Photo Message.
2. Send photo 1; it is downloaded into MEDIA_ROOT/combos and kept in the in-progress message.
3. Add Another Photo or Done — Photos Finished.
4. Caption is requested after Done.
5. Embedded link asks exact word/line, then URL, then Save Message.
6. After saving, Add Message can create another ordered message.

No database reset is required.
