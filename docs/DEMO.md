# AIRUX 0.1 demo: five spoken commands on real hardware

Boot the USB stick (docs/USB.md); the live session is enough. Wait until AIRA's status says
"idle". Speak each command with **Ctrl+Space** (press, speak, press again). Fill in the table as
you go.

| # | Say | Shows | AIRA should | Result |
| --- | --- | --- | --- | --- |
| 1 | "How much free disk space do I have in my home folder?" | System agent, spoken answer | Say the free space in GiB. The number matches `df -h ~` in a terminal. | |
| 2 | "Create a file called demo.txt in my home folder that says hello from AIRA." | Desktop agent, read-back | Say it is done only after checking. `cat ~/demo.txt` shows the text. | |
| 3 | "Remind me in 2 minutes to stretch." | Assistant agent, reminder daemon | Confirm the time. Two minutes later: a desktop notification and she says it out loud. | |
| 4 | "Delete demo.txt in my home folder." | Permission broker | Put the delete under **Waiting for you** and do nothing until you click Confirm. After you confirm, the file is gone (`ls ~`). | |
| 5 | "What's the weather in Pune right now?" | Grounding guard | Say she's not sure, because she can't see the weather from here. She may offer a web search. She must not invent a forecast. | |

A command passes when AIRA does what the "AIRA should" column says. If she mishears you, say it
again. Record retries, and the time from the end of your speech to the start of her answer.

Recording the result:

- Machine:
- CPU / RAM / GPU:
- Live or installed:
- Commands passed: __ / 5
- Notes (retries, timings, anything wrong):
