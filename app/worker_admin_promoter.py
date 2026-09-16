import inspect
import os
import shutil
import tempfile

from telethon import TelegramClient, types
from telethon.errors import UserAlreadyParticipantError
from telethon.tl.functions.channels import (
    EditAdminRequest,
    InviteToChannelRequest,
)


def _api_credentials():
    api_id = (
        os.getenv("TELEGRAM_API_ID")
        or os.getenv("MAIN_ACCOUNT_API_ID")
        or os.getenv("API_ID")
    )

    api_hash = (
        os.getenv("TELEGRAM_API_HASH")
        or os.getenv("MAIN_ACCOUNT_API_HASH")
        or os.getenv("API_HASH")
    )

    if not api_id or not api_hash:
        raise RuntimeError(
            "Telegram API ID/HASH are not configured."
        )

    return int(api_id), api_hash


def _worker_sessions():
    base = "/opt/new-ai-bot/data/admin_workers"

    result = []

    for i in range(1, 21):
        path = f"{base}/worker_{i}.session"

        if os.path.isfile(path):
            result.append(path)

    return result


async def _find_target(client, internal_id):
    # Normal dialogs.
    async for dialog in client.iter_dialogs():
        entity = dialog.entity

        if int(getattr(entity, "id", 0) or 0) == int(internal_id):
            return entity

    # Archived dialogs.
    async for dialog in client.iter_dialogs(
        archived=True
    ):
        entity = dialog.entity

        if int(getattr(entity, "id", 0) or 0) == int(internal_id):
            return entity

    return None


def _admin_rights(target):
    """
    Telegram uses different valid admin-right combinations for
    broadcast channels and megagroups.
    """
    supported = set(
        inspect.signature(
            types.ChatAdminRights
        ).parameters
    )

    is_broadcast = bool(
        getattr(target, "broadcast", False)
    )

    is_megagroup = bool(
        getattr(target, "megagroup", False)
    )

    if is_broadcast:
        # Channel-safe permissions required by the access system.
        wanted = {
            "change_info": False,
            "post_messages": False,
            "edit_messages": False,
            "delete_messages": False,
            "ban_users": True,
            "invite_users": True,
            "pin_messages": False,
            "manage_call": False,
            "anonymous": False,
            "manage_topics": False,
            "post_stories": False,
            "edit_stories": False,
            "delete_stories": False,
        }
    else:
        # Supergroup/group access-system permissions.
        wanted = {
            "change_info": True,
            "post_messages": False,
            "edit_messages": False,
            "delete_messages": True,
            "ban_users": True,
            "invite_users": True,
            "pin_messages": True,
            "manage_call": True,
            "anonymous": True,
            "manage_topics": True,
            "post_stories": False,
            "edit_stories": False,
            "delete_stories": False,
        }

    kwargs = {
        key: value
        for key, value in wanted.items()
        if key in supported
    }

    return types.ChatAdminRights(
        **kwargs
    )


async def auto_prepare_bot_admin(
    bot_username,
    private_chat_id,
):
    """
    Try worker_1, worker_2... until a worker that owns/administers
    the target can add/promote the AI bot.

    Worker session files themselves are never modified: each attempt
    uses a temporary copy.
    """

    if not bot_username:
        raise RuntimeError(
            "AI bot username is unavailable."
        )

    bot_username = str(
        bot_username
    ).lstrip("@")

    # Bot API -100123456 -> Telethon channel id 123456.
    raw = str(int(private_chat_id))

    if not raw.startswith("-100"):
        raise RuntimeError(
            "Target is not a Telegram channel/supergroup ID."
        )

    internal_id = int(
        raw[4:]
    )

    api_id, api_hash = _api_credentials()

    workers = _worker_sessions()

    if not workers:
        raise RuntimeError(
            "No admin worker sessions found."
        )

    failures = []

    for worker_path in workers:
        temp_dir = tempfile.mkdtemp(
            prefix="nba_worker_"
        )

        temp_session = os.path.join(
            temp_dir,
            "worker.session"
        )

        try:
            shutil.copy2(
                worker_path,
                temp_session
            )

            client = TelegramClient(
                temp_session,
                api_id,
                api_hash,
            )

            await client.connect()

            if not await client.is_user_authorized():
                failures.append(
                    os.path.basename(worker_path)
                    + ": not authorized"
                )
                await client.disconnect()
                continue

            target = await _find_target(
                client,
                internal_id
            )

            if target is None:
                failures.append(
                    os.path.basename(worker_path)
                    + ": target not accessible"
                )
                await client.disconnect()
                continue

            bot_entity = await client.get_entity(
                bot_username
            )

            is_broadcast = bool(
                getattr(
                    target,
                    "broadcast",
                    False,
                )
            )

            is_megagroup = bool(
                getattr(
                    target,
                    "megagroup",
                    False,
                )
            )

            # Broadcast channels do not allow bots to be added as
            # ordinary members. They must be added directly as admins.
            if not is_broadcast:
                try:
                    await client(
                        InviteToChannelRequest(
                            target,
                            [bot_entity],
                        )
                    )

                except UserAlreadyParticipantError:
                    pass

            await client(
                EditAdminRequest(
                    channel=target,
                    user_id=bot_entity,
                    admin_rights=_admin_rights(
                        target
                    ),
                    rank="Access Bot",
                )
            )

            target_type = (
                "broadcast_channel"
                if is_broadcast
                else (
                    "supergroup"
                    if is_megagroup
                    else "group"
                )
            )

            await client.disconnect()

            return {
                "ok": True,
                "worker": os.path.basename(
                    worker_path
                ),
                "target_id": int(
                    private_chat_id
                ),
                "target_type": target_type,
            }

        except Exception as exc:
            failures.append(
                os.path.basename(worker_path)
                + ": "
                + type(exc).__name__
                + ": "
                + str(exc)[:160]
            )

            try:
                await client.disconnect()
            except Exception:
                pass

        finally:
            shutil.rmtree(
                temp_dir,
                ignore_errors=True
            )

    raise RuntimeError(
        "No saved worker could add/promote the AI bot. "
        + " | ".join(failures)
    )
