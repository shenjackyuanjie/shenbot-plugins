from __future__ import annotations

import re
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient
else:
    IcaNewMessage = TypeVar("NewMessage")
    IcaClient = TypeVar("IcaClient")


def gen_room() -> dict[int, list[int]]:
    return {i: [] for i in range(0, 24)}

VOTE = {}


def fmt_vote(room_id) -> str:
    global VOTE
    if room_id not in VOTE:
        VOTE[room_id] = gen_room()
    return "\n".join(
        f"{x}: {len(VOTE[room_id][x])}" for x in VOTE[room_id] if VOTE[room_id][x]
    )


def hypvote(msg: IcaNewMessage, client: IcaClient):
    global VOTE
    matchs = re.match("/hyp (.+)", msg.content + " ")
    if matchs:
        arg = matchs.group(1).split(" ")
    else:
        return
    if msg.room_id not in VOTE:
        VOTE[msg.room_id] = gen_room()
    if arg[0] == "vote":
        for x in arg[1:]:
            if x.isdigit() and 0 <= int(x) < 24:
                if msg.sender_id in VOTE[msg.room_id][int(x) % 24]:
                    continue
                VOTE[msg.room_id][int(x) % 24].append(msg.sender_id)
    elif arg[0] == "unvote":
        for x in arg[1:]:
            if x.isdigit() and 0 <= int(x) < 24:
                if msg.sender_id in VOTE[msg.room_id][int(x) % 24]:
                    VOTE[msg.room_id][int(x) % 24].remove(msg.sender_id)
    elif arg[0] == "clear":
        VOTE[msg.room_id] = gen_room()
    elif arg[0] == "view":
        ...
    elif arg == [] or arg[0] == "ls":
        res = fmt_vote(msg.room_id)
        reply = msg.reply_with(res)
        client.send_message(reply)
    elif arg[0] == "help":
        reply = msg.reply_with("""NAME
        /hyp - 计划时间，高效开黑
SYNOPSIS
        /hyp [command] [args]
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
            
        help
            show this help
AUTHOR
        dongdigua
        shenjack(bugfixs)
""")
        client.send_message(reply)


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    if (not (msg.is_from_self or msg.is_reply)) and msg.content.startswith("/hyp"):
        hypvote(msg, client)
