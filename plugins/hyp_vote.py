from __future__ import annotations

from typing import TYPE_CHECKING

from shenbot_api import PluginManifest

if TYPE_CHECKING:
    from ica_typing import IcaClient, IcaNewMessage

VERSION = "1.2.1"
CMD_PREFIX = "/hyp"

PLUGIN_MANIFEST = PluginManifest(
    plugin_id="hyp_vote",
    name="vote for hypixel time",
    version=VERSION,
    description="投票选出群友玩hyp的时间吧",
    authors=[
        "dongdigua",
        "shenjack",
    ],
)


def gen_room() -> dict[int, dict[int, str]]:
    return {i: {} for i in range(0, 24)}


VOTE: dict[int, dict[int, dict[int, str]]] = {}


def room_votes(room_id: int) -> dict[int, dict[int, str]]:
    if room_id not in VOTE:
        VOTE[room_id] = gen_room()
    return VOTE[room_id]


def fmt_vote(room_id: int) -> str:
    votes = room_votes(room_id)
    return "\n".join(f"{x}: {len(votes[x])}" for x in votes if votes[x])


HELP_MSG = f"""{CMD_PREFIX} - 计划时间，高效开黑-v{VERSION}
SYNOPSIS
        {CMD_PREFIX} [command] [args]
OPTIONS
        vote <space seperated hour>
            vote for time you want to play
        unvote <space seperated hour>
            unvote for time you want to play
        clear
            clear the vote (OP only)
        ls
            list voted time, equivalent to empty
        view <space seperated hour>
            view who vote for the time
        help
            show this help
AUTHOR
        dongdigua
        shenjack(bugfixs)
"""


def hypvote(msg: IcaNewMessage, client: IcaClient, args: list[str]) -> None:
    votes = room_votes(msg.room_id)
    command = args[0]

    if command == "vote":
        for x in args[1:]:
            if x.isdigit() and 0 <= int(x) < 24:
                hour = int(x) % 24
                if msg.sender_id in votes[hour]:
                    continue
                votes[hour][msg.sender_id] = msg.sender_name
    elif command == "unvote":
        for x in args[1:]:
            if x.isdigit() and 0 <= int(x) < 24:
                hour = int(x) % 24
                if msg.sender_id in votes[hour]:
                    del votes[hour][msg.sender_id]
    elif command == "clear":
        VOTE[msg.room_id] = gen_room()
    elif command == "view":
        replys = "\n".join(
            f"{x}: {','.join(votes[int(x) % 24].values())}"
            for x in args[1:]
            if x.isdigit()
        )
        reply = msg.reply_with(replys)
        client.send_message(reply)
    elif command == "ls":
        res = fmt_vote(msg.room_id)
        reply = msg.reply_with(res)
        client.send_message(reply)
    elif command == "help":
        reply = msg.reply_with(HELP_MSG)
        client.send_message(reply)


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    if msg.is_from_self or msg.is_reply:
        return

    content = msg.content.strip()
    if content == CMD_PREFIX:
        reply = msg.reply_with(HELP_MSG)
        client.send_message(reply)
        return

    if not content.startswith(f"{CMD_PREFIX} "):
        return

    args = content[len(CMD_PREFIX) + 1 :].split()
    if not args:
        return
    hypvote(msg, client, args)
