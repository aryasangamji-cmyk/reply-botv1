"""
Main Account Mode V1

Runs a Telethon user session inside the existing asyncio application.
It does NOT replace the existing Bot API bot. When the admin enables
main_account_mode, private customer messages received by the main account
are passed through the existing message_handler() using a transport proxy.

Security:
- Never log API hash, phone, session string, or verification codes.
- Session file must be chmod 600.
- Admin IDs are excluded from customer routing.
"""
import os
import html
import asyncio
import logging
# MAIN_ACCOUNT_LOGGER_FINAL_FIX_V1
from types import SimpleNamespace
from pathlib import Path

from telethon import TelegramClient, events
from telethon.tl import types as tl_types
from app.reply_context import save_reference
# NEW_BATCH_ACCESS_MAIN_BRIDGE_V1
from app.new_batch_access import (
    handle_main_account_outgoing,
)



def _caption_entities_for_telethon(raw_entities):
    """Convert Telegram/Python entity dictionaries or PTB entities to Telethon TL entities."""
    if not raw_entities:
        return None
    out=[]
    mapping={
        'bold': tl_types.MessageEntityBold,
        'italic': tl_types.MessageEntityItalic,
        'underline': tl_types.MessageEntityUnderline,
        'strikethrough': tl_types.MessageEntityStrike,
        'strike': tl_types.MessageEntityStrike,
        'code': tl_types.MessageEntityCode,
        'pre': tl_types.MessageEntityPre,
        'text_link': tl_types.MessageEntityTextUrl,
        'url': tl_types.MessageEntityUrl,
        'mention': tl_types.MessageEntityMention,
        'hashtag': tl_types.MessageEntityHashtag,
        'cashtag': tl_types.MessageEntityCashtag,
        'bot_command': tl_types.MessageEntityBotCommand,
        'email': tl_types.MessageEntityEmail,
        'phone_number': tl_types.MessageEntityPhone,
        'blockquote': getattr(tl_types, 'MessageEntityBlockquote', None),
        'spoiler': getattr(tl_types, 'MessageEntitySpoiler', None),
        'custom_emoji': getattr(tl_types, 'MessageEntityCustomEmoji', None),
    }
    for e in raw_entities:
        if isinstance(e, dict):
            typ=e.get('type'); off=e.get('offset',0); ln=e.get('length',0)
            extra=e
        else:
            typ=getattr(e,'type',None); off=getattr(e,'offset',0); ln=getattr(e,'length',0)
            extra={k:getattr(e,k,None) for k in ('url','language','custom_emoji_id')}
        cls=mapping.get(str(typ or '').lower())
        if not cls or ln is None:
            continue
        try:
            if cls is tl_types.MessageEntityTextUrl:
                url=extra.get('url') or ''
                out.append(cls(offset=int(off), length=int(ln), url=url))
            elif cls is tl_types.MessageEntityPre:
                out.append(cls(offset=int(off), length=int(ln), language=extra.get('language') or ''))
            elif cls is getattr(tl_types,'MessageEntityCustomEmoji',None):
                eid=extra.get('custom_emoji_id')
                if eid is not None: out.append(cls(offset=int(off), length=int(ln), document_id=int(eid)))
            else:
                out.append(cls(offset=int(off), length=int(ln)))
        except Exception:
            continue
    return out or None

SETTING_KEY = "main_account_mode_enabled"
SESSION_DEFAULT = "data/main_account.session"
API_ID_ENV = "MAIN_ACCOUNT_API_ID"
API_HASH_ENV = "MAIN_ACCOUNT_API_HASH"
PHONE_ENV = "MAIN_ACCOUNT_PHONE"
SESSION_ENV = "MAIN_ACCOUNT_SESSION"

class MainAccountTransport:
    def __init__(self, client, event, bot_api):
        self.client = client
        self.event = event
        self.chat_id = int(event.chat_id)
        self._bot_api = bot_api

    async def _customer_entity(self):
        sender = await self.event.get_sender()
        if sender is not None:
            return await self.client.get_input_entity(sender)

        entity = await self.event.get_input_sender()
        if entity is not None:
            return entity

        raise RuntimeError("Unable to resolve customer Telegram entity")

    async def _target(self, chat_id=None):
        if chat_id is None or int(chat_id) == self.chat_id:
            return await self._customer_entity()
        return chat_id

    def _formatting_entities(self, entities):
        return _caption_entities_for_telethon(entities)

    async def send_message(self, chat_id=None, text="", parse_mode=None,
                           disable_web_page_preview=False, **kwargs):
        if chat_id is not None and int(chat_id) != self.chat_id:
            return await self._bot_api.send_message(
                chat_id=chat_id,
                text=text or "",
                parse_mode=parse_mode,
                disable_web_page_preview=disable_web_page_preview,
                **kwargs
            )

        entity = await self._customer_entity()
        entities = kwargs.get("entities")
        # REPLY_THREAD_CONTEXT_V32: allow callers to use Telegram's native
        # reply relation without changing the existing transport contract.
        reply_to = kwargs.get("reply_to_message_id") or kwargs.get("reply_to")
        try:
            reply_to = int(reply_to or 0) or None
        except Exception:
            reply_to = None
        return await self.client.send_message(
            entity,
            text or "",
            parse_mode="html" if parse_mode == "HTML" and not entities else None,
            formatting_entities=self._formatting_entities(entities),
            link_preview=not disable_web_page_preview,
            reply_to=reply_to,
        )

    async def _send_file(self, chat_id, file, caption="", parse_mode=None, **kwargs):
        target = await self._target(chat_id)
        entities = kwargs.get("caption_entities")
        reply_to = kwargs.get("reply_to_message_id") or kwargs.get("reply_to")
        try:
            reply_to = int(reply_to or 0) or None
        except Exception:
            reply_to = None
        return await self.client.send_file(
            target,
            file,
            caption=caption or "",
            parse_mode="html" if parse_mode == "HTML" and not entities else None,
            formatting_entities=self._formatting_entities(entities),
            reply_to=reply_to,
        )

    async def send_photo(self, chat_id=None, photo=None, caption="",
                         parse_mode=None, **kwargs):
        return await self._send_file(chat_id, photo, caption, parse_mode, **kwargs)

    async def send_document(self, chat_id=None, document=None, caption="",
                            parse_mode=None, **kwargs):
        return await self._send_file(chat_id, document, caption, parse_mode, **kwargs)

    async def send_video(self, chat_id=None, video=None, caption="",
                         parse_mode=None, **kwargs):
        return await self._send_file(chat_id, video, caption, parse_mode, **kwargs)

    async def send_audio(self, chat_id=None, audio=None, caption="",
                         parse_mode=None, **kwargs):
        return await self._send_file(chat_id, audio, caption, parse_mode, **kwargs)

    async def send_sticker(self, chat_id=None, sticker=None, **kwargs):
        target = await self._target(chat_id)
        return await self.client.send_file(target, sticker)

    async def send_media_group(self, chat_id=None, media=None, **kwargs):
        target = self.chat_id if chat_id is None else chat_id

        # Non-main-account chats continue through normal Bot API transport.
        if int(target) != int(self.chat_id):
            return await self._bot_api.send_media_group(
                chat_id=target,
                media=media,
                **kwargs
            )

        items = list(media or [])
        if not items:
            return []

        from io import BytesIO

        entity = await self._customer_entity()
        files = []

        first_caption = ""
        first_entities = None

        media_root = (
            Path(__file__).resolve().parent.parent
            / "data"
            / "media"
        )

        for index, item in enumerate(items):
            media_obj = getattr(item, "media", item)

            if index == 0:
                first_caption = (
                    getattr(item, "caption", "")
                    or getattr(item, "text", "")
                    or ""
                )
                first_entities = getattr(item, "caption_entities", None)

            # Already a real local path.
            if isinstance(media_obj, (str, Path)):
                candidate = Path(media_obj)
                if candidate.is_file():
                    files.append(str(candidate))
                    continue

            # PTB InputFile.
            filename = (
                getattr(media_obj, "filename", None)
                or getattr(media_obj, "name", None)
                or ""
            )

            content = getattr(media_obj, "input_file_content", None)

            # If PTB supplied a filename, first recover the original
            # saved combo media file from our data/media directory.
            if filename:
                candidate = Path(filename)
                if candidate.is_file():
                    files.append(str(candidate))
                    continue

                basename = Path(filename).name
                matches = list(media_root.rglob(basename))
                if matches:
                    files.append(str(matches[0]))
                    continue

            # Final fallback: preserve the filename on the in-memory
            # stream so Telethon can correctly identify JPG/PNG as photos.
            if content is not None:
                stream = BytesIO(
                    content if isinstance(content, (bytes, bytearray))
                    else bytes(content)
                )

                safe_name = Path(filename).name if filename else ""
                if not safe_name:
                    mime = str(getattr(media_obj, "mimetype", "") or "").lower()
                    if mime == "image/png":
                        safe_name = f"album_{index}.png"
                    elif mime == "image/webp":
                        safe_name = f"album_{index}.webp"
                    else:
                        safe_name = f"album_{index}.jpg"

                stream.name = safe_name
                files.append(stream)
                continue

            raise TypeError(
                f"Cannot convert album media item {index} "
                f"to a local photo/file"
            )

        if not files:
            return []

        # The saved combo caption belongs to the whole album.
        # Repeat it for every album item so both photos receive it.
        # IMPORTANT:
        # Telegram albums have ONE caption attached to the album's
        # first media item. Passing a caption list here can cause the
        # caption to disappear when Telethon builds the album.
        #
        # Send one caption string + its formatting entities.
        # The two photos remain ONE Telegram album.
        converted_entities = self._formatting_entities(first_entities)

        return await self.client.send_file(
            entity,
            files,
            caption=first_caption or "",
            force_document=False,
            parse_mode=(
                "html"
                if kwargs.get("parse_mode") == "HTML"
                and not converted_entities
                else None
            ),
            formatting_entities=converted_entities,
        )

    async def reply_text(self, text="", parse_mode=None, disable_web_page_preview=False, **kwargs):
        entity = await self._customer_entity()
        entities = kwargs.get("entities")
        reply_to = kwargs.get("reply_to_message_id") or kwargs.get("reply_to")
        try:
            reply_to = int(reply_to or 0) or None
        except Exception:
            reply_to = None
        return await self.client.send_message(
            entity,
            text or "",
            parse_mode="html" if parse_mode == "HTML" and not entities else None,
            formatting_entities=self._formatting_entities(entities),
            link_preview=not disable_web_page_preview,
            reply_to=reply_to,
        )

    async def reply_photo(self, photo, caption="", parse_mode=None, **kwargs):
        return await self._send_file(None, photo, caption, parse_mode, **kwargs)

    async def reply_document(self, document, caption="", parse_mode=None, **kwargs):
        return await self._send_file(None, document, caption, parse_mode, **kwargs)

    async def reply_video(self, video, caption="", parse_mode=None, **kwargs):
        return await self._send_file(None, video, caption, parse_mode, **kwargs)

    async def delete(self):
        return await self._event.delete()

class MainAccountMessage:
    # PTB-compatible message adapter for Telethon customer DMs.
    def __init__(self, event):
        self._event = event
        self.chat_id = int(event.chat_id)
        self.message_id = int(getattr(event, 'id', 0) or 0)
        self.text = event.raw_text or ''
        self.caption = self.text
        self.from_user = SimpleNamespace(
            id=int(event.sender_id),
            first_name=getattr(event.sender, 'first_name', '') if event.sender else '',
            last_name=getattr(event.sender, 'last_name', '') if event.sender else '',
            username=getattr(event.sender, 'username', None) if event.sender else None,
        )
        self.date = getattr(event, 'date', None)
        self.entities = getattr(event.message, 'entities', None)
        self.caption_entities = getattr(event.message, 'entities', None)

    async def reply_text(self, text='', parse_mode=None, **kwargs):
        entities = kwargs.get('entities')
        reply_to = kwargs.get('reply_to_message_id') or kwargs.get('reply_to')
        try:
            reply_to = int(reply_to or 0) or None
        except Exception:
            reply_to = None
        return await self._event.client.send_message(
            await self._event.get_input_sender(),
            text or '',
            parse_mode='html' if parse_mode == 'HTML' and not entities else None,
            formatting_entities=_caption_entities_for_telethon(entities),
            reply_to=reply_to,
        )

    async def _reply_file(self, file, caption='', parse_mode=None, **kwargs):
        entities = kwargs.get('caption_entities')
        reply_to = kwargs.get('reply_to_message_id') or kwargs.get('reply_to')
        try:
            reply_to = int(reply_to or 0) or None
        except Exception:
            reply_to = None
        return await self._event.client.send_file(
            await self._event.get_input_sender(), file,
            caption=caption or '',
            parse_mode='html' if parse_mode == 'HTML' and not entities else None,
            formatting_entities=_caption_entities_for_telethon(entities),
            reply_to=reply_to,
        )

    async def reply_photo(self, photo, caption='', parse_mode=None, **kwargs):
        return await self._reply_file(photo, caption, parse_mode, **kwargs)

    async def reply_document(self, document, caption='', parse_mode=None, **kwargs):
        return await self._reply_file(document, caption, parse_mode, **kwargs)

    async def reply_video(self, video, caption='', parse_mode=None, **kwargs):
        return await self._reply_file(video, caption, parse_mode, **kwargs)

    async def delete(self):
        return await self._event.delete()

class MainAccountUpdate:
    def __init__(self, event):
        self._event = event
        self.message = MainAccountMessage(event)
        self.effective_user = SimpleNamespace(
            id=int(event.sender_id),
            first_name=getattr(event.sender, "first_name", "") if event.sender else "",
            last_name=getattr(event.sender, "last_name", "") if event.sender else "",
            username=getattr(event.sender, "username", None) if event.sender else None,
        )
        self.effective_chat = SimpleNamespace(id=int(event.chat_id), type="private")
        self.effective_message = self.message
        self.chat_member = None
        self.callback_query = None

async def ensure_main_account_schema(path):
    import aiosqlite
    async with aiosqlite.connect(path) as db:
        await db.execute("""
            CREATE TABLE IF NOT EXISTS main_account_customer_state (
                customer_id INTEGER PRIMARY KEY,
                state_json TEXT NOT NULL DEFAULT '{}',
                updated_at INTEGER NOT NULL DEFAULT 0
            )
        """)
        await db.execute("""
            CREATE TABLE IF NOT EXISTS main_account_settings (
                key TEXT PRIMARY KEY,
                value TEXT NOT NULL DEFAULT ''
            )
        """)
        await db.execute(
            "INSERT OR IGNORE INTO main_account_settings(key,value) VALUES(?,?)",
            (SETTING_KEY, "0")
        )
        await db.commit()

async def get_main_account_mode(path):
    import aiosqlite
    await ensure_main_account_schema(path)
    async with aiosqlite.connect(path) as db:
        row=await (await db.execute(
            "SELECT value FROM main_account_settings WHERE key=?",(SETTING_KEY,)
        )).fetchone()
    return bool(row and str(row[0])=='1')

async def set_main_account_mode(path, enabled):
    import aiosqlite
    await ensure_main_account_schema(path)
    async with aiosqlite.connect(path) as db:
        await db.execute(
            "INSERT INTO main_account_settings(key,value) VALUES(?,?) "
            "ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (SETTING_KEY,'1' if enabled else '0')
        )
        await db.commit()

def _admin_ids(settings):
    return set(int(x) for x in getattr(settings, "admin_user_ids", []) or [])

async def start_main_account_bridge(application):
    """
    Start the main-account listener. Returns the Telethon client or None.
    """
    settings = application.bot_data["settings"]
    await ensure_main_account_schema(settings.database_path)

    raw_api_id = os.getenv(API_ID_ENV, "").strip()
    api_hash = os.getenv(API_HASH_ENV, "").strip()
    phone = os.getenv(PHONE_ENV, "").strip()
    session = os.getenv(SESSION_ENV, SESSION_DEFAULT).strip() or SESSION_DEFAULT

    if not raw_api_id or not api_hash:
        application.bot_data["main_account_client"] = None
        return None

    try:
        api_id = int(raw_api_id)
    except ValueError:
        raise RuntimeError(f"{API_ID_ENV} must be numeric")

    Path(session).parent.mkdir(parents=True, exist_ok=True)

    client = TelegramClient(session, api_id, api_hash)

    # NEW_BATCH_ACCESS_MAIN_BRIDGE_V1_HANDLER
    # Only messages sent BY the logged-in Main Account reach this.
    @client.on(events.NewMessage(outgoing=True))
    async def _on_admin_access_command(event):
        try:
            # Make the main client available to trusted internal AI rules.
            application.bot_data["main_account_client"] = client
    
            await handle_main_account_outgoing(
                event,
                application,
                settings,
                client,
            )
    
        except Exception:
            logging.getLogger(__name__).exception(
                "Main-account access command failed"
            )
    
    @client.on(events.NewMessage(incoming=True))
    async def _on_customer_message(event):
        try:
            import logging
            logging.getLogger(__name__).info(
                "MAIN_ACCOUNT EVENT RECEIVED: chat_id=%s sender_id=%s private=%s text=%r",
                getattr(event, "chat_id", None),
                getattr(event, "sender_id", None),
                getattr(event, "is_private", None),
                (event.raw_text or "")[:150],
            )

            if not event.is_private or not event.sender_id:
                return

            # Ignore messages generated by this account itself and admins.
            if int(event.sender_id) in _admin_ids(settings):
                return

            if not await get_main_account_mode(settings.database_path):
                return

            text = event.raw_text or ""
            if not text.strip():
                return

            # Hard safety cap: same customer rule as the bot-side router.
            if len(text) > 150:
                return

            import logging
            # PRIVATE_MAIN_IGNORE_BOT_SENDERS_V1
            # Telegram bot accounts also appear as private chats.
            # Never auto-reply to another bot account.
            try:
                _sender_entity = await event.get_sender()

                if bool(getattr(_sender_entity, "bot", False)):
                    logging.getLogger(__name__).info(
                        "PRIVATE MAIN ONLY V4: ignored Telegram bot sender "
                        "chat_id=%r sender_id=%r username=%r",
                        getattr(event, "chat_id", None),
                        getattr(event, "sender_id", None),
                        getattr(_sender_entity, "username", None),
                    )
                    return

            except Exception:
                logging.getLogger(__name__).exception(
                    "PRIVATE MAIN ONLY V4: bot-sender check failed"
                )

            # PRIVATE_MAIN_ONLY_V3_BRIDGE
            # Never run customer auto-reply in groups/channels.
            if not bool(getattr(event, 'is_private', False)):
                logging.getLogger(__name__).info(
                    'PRIVATE MAIN ONLY V3: ignored non-private event '
                    'chat_id=%r sender_id=%r',
                    getattr(event, 'chat_id', None),
                    getattr(event, 'sender_id', None),
                )
                return

            logging.getLogger(__name__).info("MAIN_ACCOUNT CHECKPOINT 1: creating update")
            update = MainAccountUpdate(event)

            # MAIN_ACCOUNT_EVENT_ID_ANCHOR_V5
            # Preserve the real Telegram/Telethon message ID so the
            # downstream state system can use the customer's state-
            # setting message as an actual Telegram reply anchor.
            try:
                _real_event_message_id = int(
                    getattr(event, "id", 0)
                    or getattr(
                        getattr(event, "message", None),
                        "id",
                        0
                    )
                    or 0
                )

                setattr(
                    update,
                    "_main_account_event_id",
                    _real_event_message_id
                )

                # Also expose PTB-style IDs when the synthetic
                # message object permits attribute assignment.
                try:
                    setattr(
                        update.message,
                        "message_id",
                        _real_event_message_id
                    )
                except Exception:
                    pass

                try:
                    setattr(
                        update.message,
                        "id",
                        _real_event_message_id
                    )
                except Exception:
                    pass

                logging.getLogger(__name__).info(
                    "MAIN ACCOUNT EVENT ID V5: "
                    "chat_id=%r message_id=%r",
                    getattr(event, "chat_id", None),
                    _real_event_message_id,
                )

            except Exception:
                logging.getLogger(__name__).exception(
                    "MAIN ACCOUNT EVENT ID V5 failed"
                )
            state = application.bot_data.setdefault("main_account_user_data", {})
            user_data = state.setdefault(int(event.sender_id), {})

            transport = MainAccountTransport(client, event, application.bot)
            logging.getLogger(__name__).info("MAIN_ACCOUNT CHECKPOINT 2: context ready")
            context = SimpleNamespace(
                application=application,
                bot=transport,
                user_data=user_data,
                chat_data={},
                args=[],
                job=None,
            )

            # REPLY_CONTEXT_LOGGER_FIX_V2
            # MAIN_ACCOUNT_REPLY_CONTEXT_V1
            # If the customer replied to one of our previous
            # messages/photos/captions, preserve that reference.
            try:
                _reply_to_id = int(
                    getattr(
                        getattr(event, "message", None),
                        "reply_to_msg_id",
                        0
                    )
                    or 0
                )

                if _reply_to_id:
                    _reply_msg = await event.get_reply_message()

                    if _reply_msg is not None:
                        _reply_text = str(
                            getattr(
                                _reply_msg,
                                "message",
                                ""
                            )
                            or ""
                        ).strip()

                        if _reply_text:
                            _saved_ref = save_reference(
                                settings.database_path,
                                getattr(event, "chat_id", 0),
                                _reply_to_id,
                                _reply_text,
                            )

                            setattr(
                                update,
                                "_customer_reply_reference",
                                _saved_ref
                            )

                            logging.getLogger(__name__).info(
                                "REPLY CONTEXT CAPTURED: "
                                "chat_id=%r reply_to=%r label=%r",
                                getattr(event, "chat_id", None),
                                _reply_to_id,
                                (
                                    _saved_ref.get("reference_label")
                                    if _saved_ref
                                    else ""
                                ),
                            )

            except Exception:
                logging.getLogger(__name__).exception(
                    "Reply context capture failed"
                )

            # PRIVATE_MAIN_ONLY_V3_UPDATE_MARKER
            # This flag exists only on genuine Main Account private DMs.
            try:
                setattr(update, '_private_main_account_customer', True)
            except Exception:
                pass

            logging.getLogger(__name__).info("MAIN_ACCOUNT CHECKPOINT 3: calling message_handler")
            await application.bot_data["main_account_message_handler"](
                update, context
            )
            logging.getLogger(__name__).info("MAIN_ACCOUNT CHECKPOINT 4: message_handler returned")
        except Exception:
            import logging
            logging.getLogger(__name__).exception(
                "Main-account customer message processing failed"
            )

    session_path = Path(session)
    if not session_path.exists():
        import logging
        logging.getLogger(__name__).warning(
            "Main-account session not found at %s. Main Account Mode is installed but inactive until setup creates the session.",
            session
        )
        application.bot_data["main_account_client"] = None
        return None

    await client.start()

    me = await client.get_me()
    application.bot_data["main_account_client"] = client
    application.bot_data["main_account_user_id"] = int(me.id)
    application.bot_data["main_account_phone"] = getattr(me, "phone", None)
    return client
