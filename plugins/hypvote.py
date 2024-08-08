import io
import re
from typing import TYPE_CHECKING, TypeVar

if TYPE_CHECKING:
    from ica_typing import IcaNewMessage, IcaClient
else:
    IcaNewMessage = TypeVar("NewMessage")
    IcaClient = TypeVar("IcaClient")


def on_ica_message(msg: IcaNewMessage, client: IcaClient) -> None:
    if (not (msg.is_from_self or msg.is_reply)) and (msg.content.startswith("/hyp") or "any hyp" in msg.content):
        hypvote(msg)


vote = dict([(i, []) for i in range(0,24)]

def hypvote(msg):
    arg = re.match("/hyp(.+)", msg.content + " ").group(1)
    if arg[0] == "vote":
        map(lambda x: vote[int(x) % 24] += msg.sender, arg[1:])
    else if arg[0] == "unvote":
        map(lambda x: vote[int(x) % 24].remove(msg.sender), arg[1:])
    else if arg[0] == "clear":
        vote = dict([(i, []) for i in range(0,24)]
    else if arg == [] or arg[0] == "ls" or "any hyp"in msg.content:
        res = "\n".join([f"{x}\t{vote[x]}" for x in vote])
        msg.reply_with(res)
    else if arg[0] == "help":
        msg.reply_with("""NAME
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
        help
            show this help
AUTHOR
        dongdigua
""")
